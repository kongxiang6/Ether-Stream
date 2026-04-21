from __future__ import annotations

import argparse
import ctypes
import dataclasses
import io
import signal
import threading
import time
import zlib
from typing import Optional, Tuple

from PIL import Image, ImageGrab

from ether_stream.common import (
    ETHER_TYPE,
    HEADER_SIZE,
    MAX_FRAME_PAYLOAD,
    Stats,
    list_interfaces,
    normalize_mac,
    pack_fragment,
    parse_bbox,
    parse_size,
    print_interfaces,
    resolve_source_mac,
    resolve_interface_name,
    split_chunks,
)

_DXCAM_MODULE = None
_DXCAM_LOADED = False
_MSS_MODULE = None
_MSS_LOADED = False
_CV2_MODULE = None
_NP_MODULE = None
_CV2_LOADED = False
_TURBOJPEG_ENCODER = None
_TURBOJPEG_BGR = None
_TURBOJPEG_RGB = None
_TURBOJPEG_SUBSAMPLE = None
_TURBOJPEG_NATIVE_API = False
_TURBOJPEG_LOADED = False
_SCAPY_ETHER = None
_SCAPY_SENDP = None
_SCAPY_CONF = None
_SCAPY_LOADED = False
SAFE_UDP_JPEG_BYTES = 60000
MIN_ADAPTIVE_JPEG_QUALITY = 10
_TIMER_RESOLUTION_LOCK = threading.Lock()
_TIMER_RESOLUTION_USERS = 0
_TIMER_RESOLUTION_MS = 1
THREAD_PRIORITY_ABOVE_NORMAL = 1
THREAD_PRIORITY_HIGHEST = 2


@dataclasses.dataclass
class CapturedFrame:
    frame: object
    captured_at: float
    capture_ms: float


@dataclasses.dataclass
class EncodedFrame:
    jpeg_bytes: bytes
    quality: int
    capture_sequence: int
    captured_at: float
    capture_ms: float
    encode_ms: float
    fragment_count: int


class LatestValueStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sequence = 0
        self._value: object | None = None

    def write(self, value: object) -> int:
        with self._lock:
            self._sequence += 1
            self._value = value
            return self._sequence

    def read_latest(self, last_sequence: int) -> Tuple[int, object | None]:
        with self._lock:
            if self._value is None or self._sequence == last_sequence:
                return last_sequence, None
            return self._sequence, self._value

    def peek_latest(self) -> Tuple[int, object | None]:
        with self._lock:
            return self._sequence, self._value


class JpegEncoder:
    def __init__(self) -> None:
        self._mode = "pillow"
        turbojpeg_encoder = _load_turbojpeg_backend()
        if turbojpeg_encoder is not None:
            self._mode = "turbojpeg"
            return
        cv2_module, np_module = _load_cv2_backend()
        if cv2_module is not None and np_module is not None:
            self._mode = "opencv"

    @property
    def mode(self) -> str:
        return self._mode

    def encode(self, frame: object, quality: int) -> bytes:
        quality = max(MIN_ADAPTIVE_JPEG_QUALITY, min(95, quality))
        if self._mode == "turbojpeg":
            jpeg_bytes = _encode_with_turbojpeg(frame, quality)
            if jpeg_bytes is not None:
                return jpeg_bytes
        if self._mode == "opencv":
            cv2_module, np_module = _load_cv2_backend()
            if cv2_module is not None and np_module is not None:
                bgr = _coerce_bgr_frame(frame, np_module)
                ok, encoded = cv2_module.imencode(".jpg", bgr, [int(cv2_module.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    return encoded.tobytes()
        buffer = io.BytesIO()
        _coerce_pillow_frame(frame).save(buffer, format="JPEG", quality=quality, optimize=False, subsampling=1)
        return buffer.getvalue()


def _sleep_until_precise(next_tick: float, interval: float) -> float:
    next_tick += interval
    while True:
        wait_time = next_tick - time.perf_counter()
        if wait_time <= 0:
            if interval <= 0:
                return time.perf_counter()
            missed = int((-wait_time) / interval) + 1
            return next_tick + (missed * interval)
        if wait_time > 0.003:
            time.sleep(wait_time - 0.0015)
            continue
        if wait_time > 0.001:
            time.sleep(0)
            continue
        while time.perf_counter() < next_tick:
            pass
        return next_tick


def _acquire_high_precision_timer() -> None:
    global _TIMER_RESOLUTION_USERS
    if not hasattr(ctypes, "windll"):
        return
    with _TIMER_RESOLUTION_LOCK:
        _TIMER_RESOLUTION_USERS += 1
        if _TIMER_RESOLUTION_USERS != 1:
            return
        try:
            ctypes.windll.winmm.timeBeginPeriod(_TIMER_RESOLUTION_MS)
        except Exception:
            _TIMER_RESOLUTION_USERS -= 1


def _release_high_precision_timer() -> None:
    global _TIMER_RESOLUTION_USERS
    if not hasattr(ctypes, "windll"):
        return
    with _TIMER_RESOLUTION_LOCK:
        if _TIMER_RESOLUTION_USERS <= 0:
            return
        _TIMER_RESOLUTION_USERS -= 1
        if _TIMER_RESOLUTION_USERS != 0:
            return
        try:
            ctypes.windll.winmm.timeEndPeriod(_TIMER_RESOLUTION_MS)
        except Exception:
            pass


def _set_current_thread_priority(priority: int) -> None:
    if not hasattr(ctypes, "windll"):
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), priority)
    except Exception:
        pass


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        *,
        stop_event: threading.Event,
        frame_store: LatestValueStore,
        bbox: Optional[Tuple[int, int, int, int]],
        target_size: Optional[Tuple[int, int]],
        fps: int,
        stats: Stats,
    ) -> None:
        super().__init__(daemon=True)
        self._stop_event = stop_event
        self._frame_store = frame_store
        self._config_lock = threading.Lock()
        self._config_version = 0
        self._bbox = bbox
        self._target_size = target_size
        self._fps = fps
        self._stats = stats

    def update_capture_region(
        self,
        *,
        bbox: Optional[Tuple[int, int, int, int]],
        target_size: Optional[Tuple[int, int]],
    ) -> None:
        with self._config_lock:
            self._bbox = bbox
            self._target_size = target_size
            self._config_version += 1

    def _capture_config_snapshot(self) -> tuple[Optional[Tuple[int, int, int, int]], Optional[Tuple[int, int]], int]:
        with self._config_lock:
            return self._bbox, self._target_size, self._config_version

    def run(self) -> None:
        _set_current_thread_priority(THREAD_PRIORITY_HIGHEST)
        _acquire_high_precision_timer()
        interval = 1.0 / max(1, self._fps)
        self._stats.set("capture_target_fps", float(self._fps))
        try:
            if self._run_dxcam(interval):
                return
            if self._run_mss(interval):
                return
            self._run_pillow(interval)
        finally:
            _release_high_precision_timer()

    def _run_dxcam(self, interval: float) -> bool:
        camera = _open_dxcam_camera()
        if camera is None:
            self._stats.set("capture_backend_dxcam", 0.0)
            return False
        self._stats.set("capture_backend_dxcam", 1.0)
        self._stats.set("capture_backend_mss", 0.0)
        self._stats.set("capture_backend_pillow", 0.0)
        next_tick = time.perf_counter()
        started = False
        active_bbox, _, active_version = self._capture_config_snapshot()
        try:
            self._start_dxcam(camera, active_bbox)
            started = True
            while not self._stop_event.is_set():
                try:
                    current_bbox, target_size, current_version = self._capture_config_snapshot()
                    if current_version != active_version:
                        self._restart_dxcam(camera, current_bbox)
                        active_bbox = current_bbox
                        active_version = current_version
                        time.sleep(min(0.002, interval))
                        next_tick = self._sleep_until(next_tick, interval)
                        continue
                    capture_start = time.perf_counter()
                    frame = camera.get_latest_frame()
                    if frame is None:
                        time.sleep(min(0.002, interval))
                        next_tick = self._sleep_until(next_tick, interval)
                        continue
                    frame = _maybe_resize_bgr(frame, target_size)
                    capture_ms = (time.perf_counter() - capture_start) * 1000.0
                    self._frame_store.write(CapturedFrame(frame=frame, captured_at=time.perf_counter(), capture_ms=capture_ms))
                    self._stats.add("captured_frames", 1)
                    self._stats.set("last_capture_ms", capture_ms)
                except Exception:
                    self._stats.add("capture_errors", 1)
                    time.sleep(0.010)
                next_tick = self._sleep_until(next_tick, interval)
            return True
        except Exception:
            self._stats.set("capture_backend_dxcam", 0.0)
            return False
        finally:
            if started:
                try:
                    camera.stop()
                except Exception:
                    pass
            try:
                camera.release()
            except Exception:
                pass

    def _start_dxcam(self, camera: object, bbox: Optional[Tuple[int, int, int, int]]) -> None:
        try:
            camera.start(region=bbox, target_fps=max(1, self._fps), video_mode=True)
        except TypeError:
            camera.start(region=bbox, target_fps=max(1, self._fps))

    def _restart_dxcam(self, camera: object, bbox: Optional[Tuple[int, int, int, int]]) -> None:
        try:
            camera.stop()
        except Exception:
            pass
        self._start_dxcam(camera, bbox)

    def _run_mss(self, interval: float) -> bool:
        mss_module = _load_mss_module()
        if mss_module is None:
            return False
        self._stats.set("capture_backend_dxcam", 0.0)
        self._stats.set("capture_backend_mss", 1.0)
        self._stats.set("capture_backend_pillow", 0.0)
        next_tick = time.perf_counter()
        try:
            with mss_module.mss() as capture:
                while not self._stop_event.is_set():
                    try:
                        bbox, target_size, _ = self._capture_config_snapshot()
                        capture_start = time.perf_counter()
                        frame = self._grab_with_mss(capture, bbox=bbox, target_size=target_size)
                        capture_ms = (time.perf_counter() - capture_start) * 1000.0
                        self._frame_store.write(CapturedFrame(frame=frame, captured_at=time.perf_counter(), capture_ms=capture_ms))
                        self._stats.add("captured_frames", 1)
                        self._stats.set("last_capture_ms", capture_ms)
                    except Exception:
                        self._stats.add("capture_errors", 1)
                        time.sleep(0.050)
                    next_tick = self._sleep_until(next_tick, interval)
            return True
        except Exception:
            return False

    def _run_pillow(self, interval: float) -> None:
        self._stats.set("capture_backend_dxcam", 0.0)
        self._stats.set("capture_backend_mss", 0.0)
        self._stats.set("capture_backend_pillow", 1.0)
        next_tick = time.perf_counter()
        while not self._stop_event.is_set():
            try:
                bbox, target_size, _ = self._capture_config_snapshot()
                capture_start = time.perf_counter()
                frame = self._grab_with_pillow(bbox=bbox, target_size=target_size)
                capture_ms = (time.perf_counter() - capture_start) * 1000.0
                self._frame_store.write(CapturedFrame(frame=frame, captured_at=time.perf_counter(), capture_ms=capture_ms))
                self._stats.add("captured_frames", 1)
                self._stats.set("last_capture_ms", capture_ms)
            except Exception:
                self._stats.add("capture_errors", 1)
                time.sleep(0.050)
            next_tick = self._sleep_until(next_tick, interval)

    def _grab_with_mss(
        self,
        capture: "mss.mss",
        *,
        bbox: Optional[Tuple[int, int, int, int]],
        target_size: Optional[Tuple[int, int]],
    ) -> object:
        if bbox:
            left, top, right, bottom = bbox
            monitor = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        else:
            monitor = capture.monitors[1]
        grabbed = capture.grab(monitor)
        bgr_frame = _maybe_capture_bgr(grabbed, target_size)
        if bgr_frame is not None:
            return bgr_frame
        frame = Image.frombytes("RGB", grabbed.size, grabbed.rgb)
        return _maybe_resize(frame, target_size)

    def _grab_with_pillow(
        self,
        *,
        bbox: Optional[Tuple[int, int, int, int]],
        target_size: Optional[Tuple[int, int]],
    ) -> Image.Image:
        frame = ImageGrab.grab(bbox=bbox, all_screens=bbox is None)
        return _maybe_resize(frame, target_size)

    @staticmethod
    def _sleep_until(next_tick: float, interval: float) -> float:
        return _sleep_until_precise(next_tick, interval)


class EncodeWorker(threading.Thread):
    def __init__(
        self,
        *,
        stop_event: threading.Event,
        capture_store: LatestValueStore,
        encoded_store: LatestValueStore,
        initial_quality: int,
        lock_quality: bool,
        frame_payload_budget: int,
        stats: Stats,
    ) -> None:
        super().__init__(daemon=True)
        self._stop_event = stop_event
        self._capture_store = capture_store
        self._encoded_store = encoded_store
        self._quality_ceiling = max(30, min(95, initial_quality))
        self._quality = self._quality_ceiling
        self._lock_quality = lock_quality
        self._frame_payload_budget = frame_payload_budget
        self._stats = stats
        self._encoder = JpegEncoder()
        self._recent_encode_ms: list[float] = []
        self._recent_fragments: list[int] = []
        self._recent_sizes: list[int] = []
        self._adjust_interval = 6
        self._frames_since_adjust = 0
        self._last_frame_signature: tuple[int, int, int] | None = None
        self._last_encoded_frame: EncodedFrame | None = None

    def run(self) -> None:
        _set_current_thread_priority(THREAD_PRIORITY_ABOVE_NORMAL)
        last_sequence = 0
        self._stats.set("jpeg_quality", self._quality)
        self._stats.set("jpeg_target_quality", float(self._quality_ceiling))
        self._stats.set("quality_locked", 1.0 if self._lock_quality else 0.0)
        self._stats.set("jpeg_encoder_turbojpeg", 1.0 if self._encoder.mode == "turbojpeg" else 0.0)
        self._stats.set("jpeg_encoder_opencv", 1.0 if self._encoder.mode == "opencv" else 0.0)
        self._stats.set("jpeg_encoder_pillow", 1.0 if self._encoder.mode == "pillow" else 0.0)
        while not self._stop_event.is_set():
            sequence, captured = self._capture_store.read_latest(last_sequence)
            if captured is None:
                time.sleep(0.001)
                continue
            if sequence > last_sequence + 1:
                skipped = sequence - last_sequence - 1
                self._stats.add("capture_to_encode_skips", skipped)
                self._stats.add("latest_frame_skips", skipped)
            last_sequence = sequence

            try:
                encode_start = time.perf_counter()
                assert isinstance(captured, CapturedFrame)
                frame_signature = _frame_signature(captured.frame)
                cached = self._last_encoded_frame
                reused_encoded = (
                    cached is not None
                    and self._last_frame_signature is not None
                    and frame_signature == self._last_frame_signature
                )
                if reused_encoded:
                    jpeg_bytes = cached.jpeg_bytes
                    actual_quality = cached.quality
                    sent_fragments = cached.fragment_count
                    self._stats.add("reused_encoded_frames", 1)
                else:
                    jpeg_bytes, actual_quality = self._encode_for_transport(captured.frame)
                    sent_fragments = _estimate_fragment_count(len(jpeg_bytes), self._frame_payload_budget)
                    self._last_frame_signature = frame_signature
                encode_ms = (time.perf_counter() - encode_start) * 1000.0
                if self._lock_quality:
                    self._quality = self._quality_ceiling
                else:
                    self._quality = actual_quality
                self._stats.set("last_encode_ms", encode_ms)
                self._stats.set("jpeg_quality", actual_quality)
                self._stats.set("last_jpeg_bytes", float(len(jpeg_bytes)))
                self._stats.add("encoded_frames", 1)
                self._stats.add("encoded_bytes", len(jpeg_bytes))
                self._stats.set("last_fragment_count", float(sent_fragments))
                encoded_frame = EncodedFrame(
                    jpeg_bytes=jpeg_bytes,
                    quality=actual_quality,
                    capture_sequence=sequence,
                    captured_at=captured.captured_at,
                    capture_ms=captured.capture_ms,
                    encode_ms=encode_ms,
                    fragment_count=sent_fragments,
                )
                self._encoded_store.write(encoded_frame)
                if reused_encoded:
                    self._last_encoded_frame = dataclasses.replace(
                        encoded_frame,
                        encode_ms=encode_ms,
                    )
                    continue
                self._last_encoded_frame = encoded_frame
                self._stats.add("fresh_encoded_frames", 1)
                if not self._lock_quality:
                    self._adjust_quality(encode_ms, len(jpeg_bytes), sent_fragments)
            except Exception:
                self._stats.add("encode_errors", 1)
                time.sleep(0.010)

    def _encode_for_transport(self, frame: object) -> Tuple[bytes, int]:
        base_quality = self._quality_ceiling if self._lock_quality else self._quality
        quality = max(MIN_ADAPTIVE_JPEG_QUALITY, min(self._quality_ceiling, base_quality))
        jpeg_bytes = self._encoder.encode(frame, quality)
        while len(jpeg_bytes) > SAFE_UDP_JPEG_BYTES and quality > MIN_ADAPTIVE_JPEG_QUALITY:
            if len(jpeg_bytes) > SAFE_UDP_JPEG_BYTES * 1.35:
                step = 8
            elif len(jpeg_bytes) > SAFE_UDP_JPEG_BYTES * 1.15:
                step = 5
            else:
                step = 3
            quality = max(MIN_ADAPTIVE_JPEG_QUALITY, quality - step)
            jpeg_bytes = self._encoder.encode(frame, quality)
            self._stats.add("oversize_reencodes", 1)
        if len(jpeg_bytes) > SAFE_UDP_JPEG_BYTES:
            self._stats.add("oversize_frames", 1)
        return jpeg_bytes, quality

    def _adjust_quality(self, encode_ms: float, jpeg_size: int, sent_fragments: int) -> None:
        self._recent_encode_ms.append(encode_ms)
        self._recent_fragments.append(sent_fragments)
        self._recent_sizes.append(jpeg_size)
        if len(self._recent_encode_ms) > 12:
            self._recent_encode_ms.pop(0)
            self._recent_fragments.pop(0)
            self._recent_sizes.pop(0)

        self._frames_since_adjust += 1
        if self._frames_since_adjust < self._adjust_interval or len(self._recent_encode_ms) < 4:
            self._stats.set("jpeg_quality", self._quality)
            return
        self._frames_since_adjust = 0

        payload_budget = max(1, self._frame_payload_budget - HEADER_SIZE)
        target_bytes = payload_budget * 24
        avg_encode = sum(self._recent_encode_ms) / len(self._recent_encode_ms)
        avg_fragments = sum(self._recent_fragments) / len(self._recent_fragments)
        avg_size = sum(self._recent_sizes) / len(self._recent_sizes)
        updated = self._quality

        if avg_encode > 13.0 or avg_fragments > 28 or avg_size > target_bytes * 1.08:
            updated -= 2
        elif avg_encode > 9.0 or avg_fragments > 22:
            updated -= 1
        elif avg_encode < 6.0 and avg_fragments < 16 and avg_size < target_bytes * 0.72:
            updated += 1
        updated = max(12, min(self._quality_ceiling, updated))
        self._quality = updated
        self._stats.set("jpeg_quality", updated)


class SendWorker(threading.Thread):
    def __init__(
        self,
        *,
        stop_event: threading.Event,
        encoded_store: LatestValueStore,
        interface_name: str,
        source_mac: str,
        target_mac: str,
        send_fps: int,
        frame_payload_budget: int,
        stats: Stats,
    ) -> None:
        super().__init__(daemon=True)
        self._stop_event = stop_event
        self._encoded_store = encoded_store
        self._interface_name = interface_name
        self._source_mac = source_mac
        self._target_mac = target_mac
        self._send_fps = send_fps
        self._frame_payload_budget = frame_payload_budget
        self._stats = stats
        self._ether_prefix = _build_ether_prefix(source_mac, target_mac)

    def run(self) -> None:
        _set_current_thread_priority(THREAD_PRIORITY_HIGHEST)
        _acquire_high_precision_timer()
        interval = 1.0 / max(1, self._send_fps)
        next_tick = time.perf_counter()
        frame_id = 0
        last_encoded_sequence = 0
        self._stats.set("send_target_fps", float(self._send_fps))
        raw_sender = None
        try:
            raw_sender = _open_layer2_sender(self._interface_name)
        except Exception:
            raw_sender = None
        self._stats.set("sender_backend_l2socket", 1.0 if raw_sender is not None else 0.0)
        try:
            while not self._stop_event.is_set():
                encoded_sequence, encoded = self._encoded_store.peek_latest()
                if encoded is None:
                    time.sleep(min(0.003, interval))
                    next_tick = self._sleep_until(next_tick, interval)
                    continue
                try:
                    assert isinstance(encoded, EncodedFrame)
                    if encoded_sequence == last_encoded_sequence:
                        self._stats.add("reused_frame_sends", 1)
                    else:
                        last_encoded_sequence = encoded_sequence
                        self._stats.set("last_source_capture_ms", encoded.capture_ms)
                        self._stats.set("last_source_encode_ms", encoded.encode_ms)
                    frame_age_ms = max(0.0, (time.perf_counter() - encoded.captured_at) * 1000.0)
                    send_started_ms = int(time.time() * 1000)
                    send_start = time.perf_counter()
                    sent_fragments = send_frame(
                        interface_name=self._interface_name,
                        source_mac=self._source_mac,
                        target_mac=self._target_mac,
                        frame_id=frame_id,
                        jpeg_bytes=encoded.jpeg_bytes,
                        frame_payload_budget=self._frame_payload_budget,
                        sent_timestamp_ms=send_started_ms,
                        sender_socket=raw_sender,
                        ether_prefix=self._ether_prefix,
                    )
                    send_ms = (time.perf_counter() - send_start) * 1000.0
                    self._stats.set("last_send_ms", send_ms)
                    self._stats.set("last_frame_age_ms", frame_age_ms)
                    self._stats.set("last_pipeline_ms", encoded.capture_ms + encoded.encode_ms + send_ms)
                    self._stats.set("last_fragment_count", float(sent_fragments))
                    self._stats.add("sent_frames", 1)
                    self._stats.add("sent_fragments", sent_fragments)
                    self._stats.add("sent_bytes", len(encoded.jpeg_bytes))
                    frame_id = (frame_id + 1) & 0xFFFFFFFF
                except Exception:
                    self._stats.add("send_errors", 1)
                    if raw_sender is not None:
                        try:
                            raw_sender.close()
                        except Exception:
                            pass
                        raw_sender = None
                        self._stats.set("sender_backend_l2socket", 0.0)
                    time.sleep(0.010)
                next_tick = self._sleep_until(next_tick, interval)
        finally:
            if raw_sender is not None:
                try:
                    raw_sender.close()
                except Exception:
                    pass
            _release_high_precision_timer()

    @staticmethod
    def _sleep_until(next_tick: float, interval: float) -> float:
        return _sleep_until_precise(next_tick, interval)


def _estimate_fragment_count(jpeg_size: int, frame_payload_budget: int) -> int:
    chunk_size = max(1, frame_payload_budget - HEADER_SIZE)
    return max(1, (jpeg_size + chunk_size - 1) // chunk_size)


def _frame_signature(frame: object) -> tuple[int, int, int]:
    if isinstance(frame, Image.Image):
        width, height = frame.size
        sample = frame.convert("RGB").resize((24, 24), Image.BILINEAR)
        return width, height, zlib.crc32(sample.tobytes())

    try:
        shape = getattr(frame, "shape")
        height = int(shape[0])
        width = int(shape[1])
        sample_y = max(1, height // 20)
        sample_x = max(1, width // 20)
        sample = frame[::sample_y, ::sample_x]
        if len(shape) >= 3:
            sample = sample[:, :, :3]
        return width, height, zlib.crc32(sample.tobytes())
    except Exception:
        fallback = _coerce_pillow_frame(frame)
        width, height = fallback.size
        sample = fallback.convert("RGB").resize((24, 24), Image.BILINEAR)
        return width, height, zlib.crc32(sample.tobytes())


def send_frame(
    *,
    interface_name: str,
    source_mac: str,
    target_mac: str,
    frame_id: int,
    jpeg_bytes: bytes,
    frame_payload_budget: int,
    sent_timestamp_ms: int,
    sender_socket: object | None = None,
    ether_prefix: bytes | None = None,
) -> int:
    chunk_size = max(1, frame_payload_budget - HEADER_SIZE)
    fragments = split_chunks(jpeg_bytes, chunk_size)
    if sender_socket is not None and ether_prefix is not None:
        for fragment_index, fragment in enumerate(fragments):
            payload = pack_fragment(
                frame_id,
                fragment_index,
                len(fragments),
                fragment,
                sent_timestamp_ms,
            )
            sender_socket.send(ether_prefix + payload)
        return len(fragments)

    ether_class, sendp_func, _ = _load_scapy_sender()
    if ether_class is None or sendp_func is None:
        raise RuntimeError("scapy is not available")
    packets = []
    for fragment_index, fragment in enumerate(fragments):
        payload = pack_fragment(
            frame_id,
            fragment_index,
            len(fragments),
            fragment,
            sent_timestamp_ms,
        )
        packets.append(ether_class(src=source_mac, dst=target_mac, type=ETHER_TYPE) / payload)
    sendp_func(packets, iface=interface_name, verbose=False)
    return len(packets)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Low-latency Ethernet frame sender")
    parser.add_argument("--iface", help="Scapy interface raw name")
    parser.add_argument("--dst-mac", help="Destination MAC address")
    parser.add_argument("--src-mac", help="Override source MAC address")
    parser.add_argument("--fps", type=int, default=30, help="Output/send FPS")
    parser.add_argument("--quality", type=int, default=80, help="Initial JPEG quality")
    parser.add_argument("--lock-quality", action="store_true", help="Keep JPEG quality fixed unless oversize fallback is required")
    parser.add_argument(
        "--frame-payload",
        type=int,
        default=MAX_FRAME_PAYLOAD,
        help="Ethernet payload budget per frame fragment",
    )
    parser.add_argument("--size", help="Resize captured image to WIDTHxHEIGHT")
    parser.add_argument("--bbox", help="Capture rectangle left,top,right,bottom")
    parser.add_argument("--list-ifaces", action="store_true", help="List interfaces and exit")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.list_ifaces:
        print_interfaces(list_interfaces())
        return 0

    if not args.iface or not args.dst_mac:
        parser.error("--iface and --dst-mac are required unless --list-ifaces is used")

    bbox = parse_bbox(args.bbox) if args.bbox else None
    target_size = parse_size(args.size) if args.size else None
    interface_name = resolve_interface_name(args.iface)
    target_mac = normalize_mac(args.dst_mac)
    source_mac = resolve_source_mac(interface_name, args.src_mac)
    display_refresh = get_primary_display_refresh_rate()
    capture_fps = max(1, min(args.fps, int(round(display_refresh)) if display_refresh > 0 else args.fps))

    stop_event = threading.Event()
    stats = Stats()
    capture_store = LatestValueStore()
    encoded_store = LatestValueStore()

    capture_worker = CaptureWorker(
        stop_event=stop_event,
        frame_store=capture_store,
        bbox=bbox,
        target_size=target_size,
        fps=capture_fps,
        stats=stats,
    )
    encode_worker = EncodeWorker(
        stop_event=stop_event,
        capture_store=capture_store,
        encoded_store=encoded_store,
        initial_quality=args.quality,
        lock_quality=args.lock_quality,
        frame_payload_budget=args.frame_payload,
        stats=stats,
    )
    send_worker = SendWorker(
        stop_event=stop_event,
        encoded_store=encoded_store,
        interface_name=interface_name,
        source_mac=source_mac,
        target_mac=target_mac,
        send_fps=args.fps,
        frame_payload_budget=args.frame_payload,
        stats=stats,
    )
    stats.set("display_refresh_fps", display_refresh)

    def _shutdown(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"sender start iface={interface_name}")
    print(f"src_mac={source_mac} dst_mac={target_mac}")
    if bbox:
        print(f"capture_bbox={bbox}")
    if target_size:
        print(f"resize={target_size[0]}x{target_size[1]}")
    print(f"display_refresh={display_refresh:.1f}Hz capture_fps={capture_fps} send_fps={args.fps}")

    capture_worker.start()
    encode_worker.start()
    send_worker.start()

    try:
        while not stop_event.is_set():
            time.sleep(1.0)
            snapshot = stats.snapshot()
            print(
                "captured={captured:.0f} encoded={encoded:.0f} sent={sent:.0f} "
                "reused={reused:.0f} q={quality:.0f} cap_ms={cap_ms:.1f} enc_ms={enc_ms:.1f} send_ms={send_ms:.1f}".format(
                    captured=snapshot.get("captured_frames", 0),
                    encoded=snapshot.get("encoded_frames", 0),
                    sent=snapshot.get("sent_frames", 0),
                    reused=snapshot.get("reused_frame_sends", 0),
                    quality=snapshot.get("jpeg_quality", 0),
                    cap_ms=snapshot.get("last_capture_ms", 0.0),
                    enc_ms=snapshot.get("last_encode_ms", 0.0),
                    send_ms=snapshot.get("last_send_ms", 0.0),
                )
            )
    finally:
        stop_event.set()
        capture_worker.join(timeout=2.0)
        encode_worker.join(timeout=2.0)
        send_worker.join(timeout=2.0)

    return 0


def get_primary_display_refresh_rate() -> float:
    if not hasattr(ctypes, "windll"):
        return 60.0
    try:
        user32 = ctypes.windll.user32

        class DEVMODEW(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", ctypes.c_wchar * 32),
                ("dmSpecVersion", ctypes.c_ushort),
                ("dmDriverVersion", ctypes.c_ushort),
                ("dmSize", ctypes.c_ushort),
                ("dmDriverExtra", ctypes.c_ushort),
                ("dmFields", ctypes.c_ulong),
                ("dmPositionX", ctypes.c_long),
                ("dmPositionY", ctypes.c_long),
                ("dmDisplayOrientation", ctypes.c_ulong),
                ("dmDisplayFixedOutput", ctypes.c_ulong),
                ("dmColor", ctypes.c_short),
                ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short),
                ("dmFormName", ctypes.c_wchar * 32),
                ("dmLogPixels", ctypes.c_ushort),
                ("dmBitsPerPel", ctypes.c_ulong),
                ("dmPelsWidth", ctypes.c_ulong),
                ("dmPelsHeight", ctypes.c_ulong),
                ("dmDisplayFlags", ctypes.c_ulong),
                ("dmDisplayFrequency", ctypes.c_ulong),
                ("dmICMMethod", ctypes.c_ulong),
                ("dmICMIntent", ctypes.c_ulong),
                ("dmMediaType", ctypes.c_ulong),
                ("dmDitherType", ctypes.c_ulong),
                ("dmReserved1", ctypes.c_ulong),
                ("dmReserved2", ctypes.c_ulong),
                ("dmPanningWidth", ctypes.c_ulong),
                ("dmPanningHeight", ctypes.c_ulong),
            ]

        devmode = DEVMODEW()
        devmode.dmSize = ctypes.sizeof(DEVMODEW)
        if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(devmode)):
            refresh = float(devmode.dmDisplayFrequency)
            if refresh > 1:
                return refresh
    except Exception:
        pass
    return 60.0


def _maybe_resize(frame: Image.Image, target_size: Optional[Tuple[int, int]]) -> Image.Image:
    if not target_size:
        return frame.convert("RGB")
    return frame.convert("RGB").resize(target_size, Image.Resampling.BILINEAR)


def _maybe_resize_bgr(frame: object, target_size: Optional[Tuple[int, int]]) -> object:
    if not target_size:
        return frame
    cv2_module, _ = _load_cv2_backend()
    if cv2_module is None:
        return frame
    try:
        height = int(getattr(frame, "shape")[0])
        width = int(getattr(frame, "shape")[1])
    except Exception:
        return frame
    if target_size == (width, height):
        return frame
    interpolation = cv2_module.INTER_AREA if target_size[0] < width or target_size[1] < height else cv2_module.INTER_LINEAR
    return cv2_module.resize(frame, target_size, interpolation=interpolation)


def _maybe_capture_bgr(grabbed: object, target_size: Optional[Tuple[int, int]]) -> object | None:
    cv2_module, np_module = _load_cv2_backend()
    if cv2_module is None or np_module is None:
        return None
    try:
        width = int(getattr(grabbed, "width"))
        height = int(getattr(grabbed, "height"))
        raw = getattr(grabbed, "raw")
        bgra = np_module.frombuffer(raw, dtype=np_module.uint8).reshape(height, width, 4)
        bgr = bgra[:, :, :3]
        return _maybe_resize_bgr(bgr.copy(), target_size)
    except Exception:
        return None


def _coerce_bgr_frame(frame: object, np_module: object) -> object:
    if isinstance(frame, Image.Image):
        rgb = frame.convert("RGB")
        array = np_module.asarray(rgb)
        return array[:, :, ::-1]
    return frame


def _coerce_pillow_frame(frame: object) -> Image.Image:
    if isinstance(frame, Image.Image):
        return frame.convert("RGB")
    cv2_module, np_module = _load_cv2_backend()
    if cv2_module is not None and np_module is not None:
        rgb = np_module.asarray(frame)[:, :, ::-1]
        return Image.fromarray(rgb, mode="RGB")
    raise TypeError("unsupported frame type for pillow encoding")


def _encode_with_turbojpeg(frame: object, quality: int) -> bytes | None:
    encoder = _load_turbojpeg_backend()
    _, np_module = _load_cv2_backend()
    if encoder is None or np_module is None:
        return None

    try:
        if _TURBOJPEG_NATIVE_API:
            if isinstance(frame, Image.Image):
                rgb = np_module.asarray(frame.convert("RGB"))
                return encoder(
                    rgb,
                    quality=quality,
                    subsamp=_TURBOJPEG_SUBSAMPLE,
                    pixelformat=_TURBOJPEG_RGB,
                )
            return encoder(
                frame,
                quality=quality,
                subsamp=_TURBOJPEG_SUBSAMPLE,
                pixelformat=_TURBOJPEG_BGR,
            )

        encode_kwargs = {"quality": quality}
        if _TURBOJPEG_SUBSAMPLE is not None:
            encode_kwargs["jpeg_subsample"] = _TURBOJPEG_SUBSAMPLE
        if isinstance(frame, Image.Image):
            rgb = np_module.asarray(frame.convert("RGB"))
            if _TURBOJPEG_RGB is not None:
                return encoder.encode(rgb, pixel_format=_TURBOJPEG_RGB, **encode_kwargs)
            if _TURBOJPEG_BGR is None:
                return None
            return encoder.encode(rgb[:, :, ::-1], pixel_format=_TURBOJPEG_BGR, **encode_kwargs)
        if _TURBOJPEG_BGR is None:
            return None
        return encoder.encode(frame, pixel_format=_TURBOJPEG_BGR, **encode_kwargs)
    except Exception:
        return None


def _load_dxcam_module():
    global _DXCAM_MODULE, _DXCAM_LOADED
    if _DXCAM_LOADED:
        return _DXCAM_MODULE
    try:
        import dxcam as imported_dxcam  # type: ignore
    except Exception:
        imported_dxcam = None
    _DXCAM_MODULE = imported_dxcam
    _DXCAM_LOADED = True
    return _DXCAM_MODULE


def _open_dxcam_camera():
    dxcam_module = _load_dxcam_module()
    if dxcam_module is None:
        return None
    try:
        return dxcam_module.create(output_color="BGR", max_buffer_len=2)
    except TypeError:
        try:
            return dxcam_module.create(output_color="BGR")
        except Exception:
            return None
    except Exception:
        return None


def _load_mss_module():
    global _MSS_MODULE, _MSS_LOADED
    if _MSS_LOADED:
        return _MSS_MODULE
    try:
        import mss as imported_mss  # type: ignore
    except Exception:
        imported_mss = None
    _MSS_MODULE = imported_mss
    _MSS_LOADED = True
    return _MSS_MODULE


def _load_cv2_backend():
    global _CV2_MODULE, _NP_MODULE, _CV2_LOADED
    if _CV2_LOADED:
        return _CV2_MODULE, _NP_MODULE
    try:
        import cv2 as imported_cv2  # type: ignore
        import numpy as imported_np  # type: ignore
    except Exception:
        imported_cv2 = None
        imported_np = None
    _CV2_MODULE = imported_cv2
    _NP_MODULE = imported_np
    _CV2_LOADED = True
    return _CV2_MODULE, _NP_MODULE


def _load_turbojpeg_backend():
    global _TURBOJPEG_ENCODER, _TURBOJPEG_BGR, _TURBOJPEG_RGB, _TURBOJPEG_SUBSAMPLE, _TURBOJPEG_NATIVE_API, _TURBOJPEG_LOADED
    if _TURBOJPEG_LOADED:
        return _TURBOJPEG_ENCODER
    try:
        from turbojpeg import TJPF_BGR as imported_tjpf_bgr  # type: ignore
        from turbojpeg import TJPF_RGB as imported_tjpf_rgb  # type: ignore
        from turbojpeg import TJSAMP_420 as imported_tjsamp_420  # type: ignore
        from turbojpeg import TurboJPEG  # type: ignore

        imported_encoder = TurboJPEG()
        native_api = False
    except Exception:
        try:
            import turbojpeg as imported_turbojpeg  # type: ignore

            imported_encoder = imported_turbojpeg.compress
            imported_tjpf_bgr = imported_turbojpeg.PF.BGR
            imported_tjpf_rgb = imported_turbojpeg.PF.RGB
            imported_tjsamp_420 = imported_turbojpeg.SAMP.Y420
            native_api = True
        except Exception:
            imported_encoder = None
            imported_tjpf_bgr = None
            imported_tjpf_rgb = None
            imported_tjsamp_420 = None
            native_api = False
    _TURBOJPEG_ENCODER = imported_encoder
    _TURBOJPEG_BGR = imported_tjpf_bgr
    _TURBOJPEG_RGB = imported_tjpf_rgb
    _TURBOJPEG_SUBSAMPLE = imported_tjsamp_420
    _TURBOJPEG_NATIVE_API = native_api
    _TURBOJPEG_LOADED = True
    return _TURBOJPEG_ENCODER


def _load_scapy_sender():
    global _SCAPY_ETHER, _SCAPY_SENDP, _SCAPY_CONF, _SCAPY_LOADED
    if _SCAPY_LOADED:
        return _SCAPY_ETHER, _SCAPY_SENDP, _SCAPY_CONF
    try:
        from scapy.all import Ether as imported_ether
        from scapy.all import conf as imported_conf
        from scapy.all import sendp as imported_sendp
    except Exception:
        imported_ether = None
        imported_conf = None
        imported_sendp = None
    _SCAPY_ETHER = imported_ether
    _SCAPY_SENDP = imported_sendp
    _SCAPY_CONF = imported_conf
    _SCAPY_LOADED = True
    return _SCAPY_ETHER, _SCAPY_SENDP, _SCAPY_CONF


def _open_layer2_sender(interface_name: str):
    _, _, scapy_conf = _load_scapy_sender()
    if scapy_conf is None:
        return None
    return scapy_conf.L2socket(iface=interface_name)


def _build_ether_prefix(source_mac: str, target_mac: str) -> bytes:
    return _mac_to_bytes(target_mac) + _mac_to_bytes(source_mac) + ETHER_TYPE.to_bytes(2, "big")


def _mac_to_bytes(mac_text: str) -> bytes:
    return bytes.fromhex(mac_text.replace(":", ""))


if __name__ == "__main__":
    raise SystemExit(main())
