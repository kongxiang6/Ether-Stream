from __future__ import annotations

import argparse
import io
import signal
import threading
import time
from pathlib import Path
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

_MSS_MODULE = None
_MSS_LOADED = False
_CV2_MODULE = None
_NP_MODULE = None
_CV2_LOADED = False
_SCAPY_ETHER = None
_SCAPY_SENDP = None
_SCAPY_CONF = None
_SCAPY_LOADED = False


class DoubleFrameBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sequence = 0
        self._frame: Optional[Image.Image] = None

    def write(self, frame: Image.Image) -> None:
        with self._lock:
            self._sequence += 1
            self._frame = frame

    def read_latest(self, last_sequence: int) -> Tuple[int, Optional[Image.Image]]:
        with self._lock:
            if self._frame is None or self._sequence == last_sequence:
                return last_sequence, None
            return self._sequence, self._frame


class JpegEncoder:
    def __init__(self) -> None:
        self._mode = "pillow"
        cv2_module, np_module = _load_cv2_backend()
        if cv2_module is not None and np_module is not None:
            self._mode = "opencv"

    @property
    def mode(self) -> str:
        return self._mode

    def encode(self, frame: Image.Image, quality: int) -> bytes:
        quality = max(30, min(95, quality))
        if self._mode == "opencv":
            cv2_module, np_module = _load_cv2_backend()
            if cv2_module is not None and np_module is not None:
                rgb = frame.convert("RGB")
                array = np_module.asarray(rgb)
                bgr = array[:, :, ::-1]
                ok, encoded = cv2_module.imencode(".jpg", bgr, [int(cv2_module.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    return encoded.tobytes()
        buffer = io.BytesIO()
        frame.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=False, subsampling=1)
        return buffer.getvalue()


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        *,
        stop_event: threading.Event,
        frame_buffer: DoubleFrameBuffer,
        bbox: Optional[Tuple[int, int, int, int]],
        target_size: Optional[Tuple[int, int]],
        fps: int,
        stats: Stats,
    ) -> None:
        super().__init__(daemon=True)
        self._stop_event = stop_event
        self._frame_buffer = frame_buffer
        self._bbox = bbox
        self._target_size = target_size
        self._fps = fps
        self._stats = stats

    def run(self) -> None:
        interval = 1.0 / max(1, self._fps)
        next_tick = time.perf_counter()
        mss_module = _load_mss_module()
        if mss_module is not None:
            with mss_module.mss() as capture:
                while not self._stop_event.is_set():
                    try:
                        frame = self._grab_with_mss(capture)
                        self._frame_buffer.write(frame)
                        self._stats.add("captured_frames", 1)
                    except Exception:
                        self._stats.add("capture_errors", 1)
                        time.sleep(0.050)
                    next_tick = self._sleep_until(next_tick, interval)
            return

        while not self._stop_event.is_set():
            try:
                frame = self._grab_with_pillow()
                self._frame_buffer.write(frame)
                self._stats.add("captured_frames", 1)
            except Exception:
                self._stats.add("capture_errors", 1)
                time.sleep(0.050)
            next_tick = self._sleep_until(next_tick, interval)

    def _grab_with_mss(self, capture: "mss.mss") -> Image.Image:
        if self._bbox:
            left, top, right, bottom = self._bbox
            monitor = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        else:
            monitor = capture.monitors[1]
        grabbed = capture.grab(monitor)
        frame = Image.frombytes("RGB", grabbed.size, grabbed.rgb)
        return _maybe_resize(frame, self._target_size)

    def _grab_with_pillow(self) -> Image.Image:
        frame = ImageGrab.grab(bbox=self._bbox, all_screens=self._bbox is None)
        return _maybe_resize(frame, self._target_size)

    def _sleep_until(self, next_tick: float, interval: float) -> float:
        next_tick += interval
        wait_time = next_tick - time.perf_counter()
        if wait_time > 0:
            time.sleep(wait_time)
        else:
            next_tick = time.perf_counter()
        return next_tick


class EncodeSendWorker(threading.Thread):
    def __init__(
        self,
        *,
        stop_event: threading.Event,
        frame_buffer: DoubleFrameBuffer,
        interface_name: str,
        source_mac: str,
        target_mac: str,
        initial_quality: int,
        frame_payload_budget: int,
        stats: Stats,
    ) -> None:
        super().__init__(daemon=True)
        self._stop_event = stop_event
        self._frame_buffer = frame_buffer
        self._interface_name = interface_name
        self._source_mac = source_mac
        self._target_mac = target_mac
        self._quality = max(30, min(95, initial_quality))
        self._frame_payload_budget = frame_payload_budget
        self._stats = stats
        self._encoder = JpegEncoder()
        self._ether_prefix = _build_ether_prefix(source_mac, target_mac)
        self._recent_encode_ms: list[float] = []
        self._recent_send_ms: list[float] = []
        self._recent_fragments: list[int] = []
        self._recent_sizes: list[int] = []
        self._adjust_interval = 6
        self._frames_since_adjust = 0

    def run(self) -> None:
        frame_id = 0
        last_sequence = 0
        self._stats.set("jpeg_quality", self._quality)
        raw_sender = None
        sender_backend = "scapy-sendp"
        try:
            raw_sender = _open_layer2_sender(self._interface_name)
            if raw_sender is not None:
                sender_backend = "scapy-l2socket"
        except Exception:
            raw_sender = None
        self._stats.set("sender_backend_l2socket", 1.0 if raw_sender is not None else 0.0)
        while not self._stop_event.is_set():
            sequence, frame = self._frame_buffer.read_latest(last_sequence)
            if frame is None:
                time.sleep(0.001)
                continue
            last_sequence = sequence

            try:
                encode_start = time.perf_counter()
                jpeg_bytes = self._encoder.encode(frame, self._quality)
                encode_ms = (time.perf_counter() - encode_start) * 1000.0
                self._stats.set("last_encode_ms", encode_ms)
                self._stats.add("encoded_frames", 1)
                self._stats.add("encoded_bytes", len(jpeg_bytes))

                send_started_ms = int(time.time() * 1000)
                send_start = time.perf_counter()
                sent_fragments = send_frame(
                    interface_name=self._interface_name,
                    source_mac=self._source_mac,
                    target_mac=self._target_mac,
                    frame_id=frame_id,
                    jpeg_bytes=jpeg_bytes,
                    frame_payload_budget=self._frame_payload_budget,
                    sent_timestamp_ms=send_started_ms,
                    sender_socket=raw_sender,
                    ether_prefix=self._ether_prefix,
                )
                send_ms = (time.perf_counter() - send_start) * 1000.0
                self._stats.set("last_send_ms", send_ms)
                self._stats.set("last_pipeline_ms", encode_ms + send_ms)
                self._stats.add("sent_frames", 1)
                self._stats.add("sent_fragments", sent_fragments)
                self._stats.add("sent_bytes", len(jpeg_bytes))
                self._stats.set("sender_backend_l2socket", 1.0 if raw_sender is not None else 0.0)

                frame_id = (frame_id + 1) & 0xFFFFFFFF
                self._adjust_quality(encode_ms, send_ms, len(jpeg_bytes), sent_fragments)
            except Exception:
                self._stats.add("send_errors", 1)
                if raw_sender is not None:
                    try:
                        raw_sender.close()
                    except Exception:
                        pass
                    raw_sender = None
                    sender_backend = "scapy-sendp"
                    self._stats.set("sender_backend_l2socket", 0.0)
                time.sleep(0.010)
        if raw_sender is not None:
            try:
                raw_sender.close()
            except Exception:
                pass

    def _adjust_quality(self, encode_ms: float, send_ms: float, jpeg_size: int, sent_fragments: int) -> None:
        self._recent_encode_ms.append(encode_ms)
        self._recent_send_ms.append(send_ms)
        self._recent_fragments.append(sent_fragments)
        self._recent_sizes.append(jpeg_size)
        if len(self._recent_encode_ms) > 12:
            self._recent_encode_ms.pop(0)
            self._recent_send_ms.pop(0)
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
        avg_send = sum(self._recent_send_ms) / len(self._recent_send_ms)
        avg_fragments = sum(self._recent_fragments) / len(self._recent_fragments)
        avg_size = sum(self._recent_sizes) / len(self._recent_sizes)
        updated = self._quality

        if avg_send > 8.0 or avg_encode > 13.0 or avg_fragments > 28 or avg_size > target_bytes * 1.08:
            updated -= 2
        elif avg_send > 4.0 or avg_encode > 9.0 or avg_fragments > 22:
            updated -= 1
        elif avg_send < 2.5 and avg_encode < 6.0 and avg_fragments < 16 and avg_size < target_bytes * 0.72:
            updated += 1
        updated = max(35, min(92, updated))
        self._quality = updated
        self._stats.set("jpeg_quality", updated)


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
    parser.add_argument("--fps", type=int, default=30, help="Capture FPS")
    parser.add_argument("--quality", type=int, default=80, help="Initial JPEG quality")
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

    stop_event = threading.Event()
    stats = Stats()
    frame_buffer = DoubleFrameBuffer()

    capture_worker = CaptureWorker(
        stop_event=stop_event,
        frame_buffer=frame_buffer,
        bbox=bbox,
        target_size=target_size,
        fps=args.fps,
        stats=stats,
    )
    encode_send_worker = EncodeSendWorker(
        stop_event=stop_event,
        frame_buffer=frame_buffer,
        interface_name=interface_name,
        source_mac=source_mac,
        target_mac=target_mac,
        initial_quality=args.quality,
        frame_payload_budget=args.frame_payload,
        stats=stats,
    )

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

    capture_worker.start()
    encode_send_worker.start()

    try:
        while not stop_event.is_set():
            time.sleep(1.0)
            snapshot = stats.snapshot()
            print(
                "captured={captured:.0f} encoded={encoded:.0f} sent={sent:.0f} "
                "frags={frags:.0f} q={quality:.0f} enc_ms={enc_ms:.1f}".format(
                    captured=snapshot.get("captured_frames", 0),
                    encoded=snapshot.get("encoded_frames", 0),
                    sent=snapshot.get("sent_frames", 0),
                    frags=snapshot.get("sent_fragments", 0),
                    quality=snapshot.get("jpeg_quality", 0),
                    enc_ms=snapshot.get("last_encode_ms", 0.0),
                )
            )
    finally:
        stop_event.set()
        capture_worker.join(timeout=2.0)
        encode_send_worker.join(timeout=2.0)

    return 0


def _maybe_resize(frame: Image.Image, target_size: Optional[Tuple[int, int]]) -> Image.Image:
    if not target_size:
        return frame.convert("RGB")
    return frame.convert("RGB").resize(target_size, Image.Resampling.BILINEAR)


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
