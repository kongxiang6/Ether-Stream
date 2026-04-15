from __future__ import annotations

import argparse
import dataclasses
import queue
import signal
import socket
import threading
import time
from typing import Callable, Optional

from ether_stream.common import (
    ETHER_TYPE,
    FrameAssembler,
    FragmentHeader,
    Stats,
    list_interfaces,
    print_interfaces,
    parse_udp_target,
    resolve_interface_name,
    unpack_fragment,
)

_ASYNC_SNIFFER = None
_ASYNC_SNIFFER_LOADED = False
MAX_UDP_PAYLOAD_BYTES = 65507


@dataclasses.dataclass(frozen=True)
class QueuedFragment:
    header: FragmentHeader
    payload: bytes


def _drop_oldest_frame_groups(
    packet_queue: "queue.Queue[QueuedFragment]",
    *,
    target_depth: int,
) -> tuple[int, list[int]]:
    dropped_fragments = 0
    dropped_frame_ids: list[int] = []
    queue_mutex = getattr(packet_queue, "mutex", None)
    queued_items = getattr(packet_queue, "queue", None)
    if queue_mutex is None or queued_items is None:
        return 0, []

    with queue_mutex:
        while queued_items and len(queued_items) > target_depth:
            oldest_frame_id = queued_items[0].header.frame_id
            dropped_frame_ids.append(oldest_frame_id)
            while queued_items and queued_items[0].header.frame_id == oldest_frame_id:
                queued_items.popleft()
                dropped_fragments += 1

        if dropped_fragments:
            unfinished = getattr(packet_queue, "unfinished_tasks", 0)
            packet_queue.unfinished_tasks = max(0, unfinished - dropped_fragments)
            packet_queue.not_full.notify_all()

    return dropped_fragments, dropped_frame_ids


def _enqueue_latest_fragment(
    packet_queue: "queue.Queue[QueuedFragment]",
    fragment: QueuedFragment,
    stats: Stats,
) -> None:
    try:
        packet_queue.put_nowait(fragment)
        return
    except queue.Full:
        pass

    max_size = getattr(packet_queue, "maxsize", 0) or 1
    dropped_fragments, dropped_frame_ids = _drop_oldest_frame_groups(
        packet_queue,
        target_depth=max(0, max_size - 1),
    )
    if dropped_fragments:
        stats.add("queue_drops", dropped_fragments)
        stats.add("queue_drop_frames", len(dropped_frame_ids))

    try:
        packet_queue.put_nowait(fragment)
    except queue.Full:
        stats.add("queue_drops", 1)


class ReceiverThread(threading.Thread):
    def __init__(
        self,
        *,
        interface_name: str,
        packet_queue: "queue.Queue[QueuedFragment]",
        stop_event: threading.Event,
        stats: Stats,
    ) -> None:
        super().__init__(daemon=True)
        self._interface_name = interface_name
        self._packet_queue = packet_queue
        self._stop_event = stop_event
        self._stats = stats
        self._sniffer: object | None = None

    def run(self) -> None:
        try:
            sniffer_class = _load_async_sniffer()
            if sniffer_class is None:
                raise RuntimeError("scapy is not available")
            self._sniffer = sniffer_class(
                iface=self._interface_name,
                prn=self._on_packet,
                filter=f"ether proto {ETHER_TYPE:#06x}",
                store=False,
            )
            self._sniffer.start()
            while not self._stop_event.wait(1.0):
                pass
        except Exception:
            self._stats.add("capture_errors", 1)
            self._stop_event.set()
        finally:
            if self._sniffer is not None:
                try:
                    self._sniffer.stop(join=True)
                except Exception:
                    self._stats.add("capture_errors", 1)

    def _on_packet(self, packet: object) -> None:
        try:
            eth_type = getattr(packet, "type", None)
            if eth_type != ETHER_TYPE:
                return
            payload = bytes(packet.payload)
        except Exception:
            self._stats.add("capture_errors", 1)
            return

        self._stats.add("captured_packets", 1)
        self._stats.add("captured_bytes", len(payload))
        try:
            header, fragment_payload = unpack_fragment(payload)
        except Exception:
            self._stats.add("decode_errors", 1)
            return
        _enqueue_latest_fragment(
            self._packet_queue,
            QueuedFragment(header=header, payload=fragment_payload),
            self._stats,
        )


class ProcessorThread(threading.Thread):
    def __init__(
        self,
        *,
        packet_queue: "queue.Queue[QueuedFragment]",
        udp_target: tuple[str, int],
        stop_event: threading.Event,
        assembler: FrameAssembler,
        stats: Stats,
        preview_callback: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        super().__init__(daemon=True)
        self._packet_queue = packet_queue
        self._udp_target = udp_target
        self._stop_event = stop_event
        self._assembler = assembler
        self._stats = stats
        self._preview_callback = preview_callback

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        except OSError:
            pass
        try:
            sock.connect(self._udp_target)
        except OSError:
            pass
        try:
            while not self._stop_event.is_set():
                self._trim_backlog_if_needed()
                try:
                    fragment = self._packet_queue.get(timeout=0.05)
                except queue.Empty:
                    continue

                self._stats.add("processed_fragments", 1)
                frame_process_started = time.perf_counter()
                assembled = self._assembler.push(fragment.header, fragment.payload)
                if assembled is None:
                    continue

                if len(assembled) > MAX_UDP_PAYLOAD_BYTES:
                    self._stats.add("udp_oversize_frames", 1)
                    self._stats.add("udp_drops", 1)
                    self._stats.set("last_udp_oversize_bytes", float(len(assembled)))
                    continue

                try:
                    sock.send(assembled)
                except (BlockingIOError, OSError):
                    try:
                        sock.sendto(assembled, self._udp_target)
                    except (BlockingIOError, OSError):
                        self._stats.add("udp_drops", 1)
                        continue

                self._stats.add("forwarded_frames", 1)
                self._stats.add("forwarded_bytes", len(assembled))
                self._stats.set("last_receiver_process_ms", (time.perf_counter() - frame_process_started) * 1000.0)

                if self._preview_callback is not None:
                    try:
                        self._preview_callback(assembled)
                    except Exception:
                        pass
        finally:
            sock.close()

    def _trim_backlog_if_needed(self) -> None:
        try:
            depth = self._packet_queue.qsize()
        except NotImplementedError:
            return

        max_size = getattr(self._packet_queue, "maxsize", 0) or 0
        threshold = max(192, max_size // 3 if max_size > 0 else 0)
        target_depth = max(64, threshold // 2)
        if depth <= threshold:
            return

        dropped_fragments, dropped_frame_ids = _drop_oldest_frame_groups(
            self._packet_queue,
            target_depth=target_depth,
        )
        if dropped_fragments:
            for frame_id in dropped_frame_ids:
                self._assembler.drop_frame(frame_id)
            self._stats.add("queue_trims", dropped_fragments)
            self._stats.add("queue_trim_frames", len(dropped_frame_ids))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Low-latency Ethernet frame receiver")
    parser.add_argument("--iface", help="Scapy interface raw name")
    parser.add_argument("--udp-target", default="127.0.0.1:4455", help="UDP target host:port")
    parser.add_argument("--queue-size", type=int, default=4096, help="Packet queue size")
    parser.add_argument("--max-age", type=float, default=0.25, help="Fragment assembly timeout seconds")
    parser.add_argument("--list-ifaces", action="store_true", help="List interfaces and exit")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.list_ifaces:
        print_interfaces(list_interfaces())
        return 0

    if not args.iface:
        parser.error("--iface is required unless --list-ifaces is used")

    interface_name = resolve_interface_name(args.iface)
    udp_target = parse_udp_target(args.udp_target)
    stop_event = threading.Event()
    stats = Stats()
    packet_queue: "queue.Queue[QueuedFragment]" = queue.Queue(maxsize=max(1, args.queue_size))
    assembler = FrameAssembler(max_age=max(0.05, args.max_age))

    receiver_thread = ReceiverThread(
        interface_name=interface_name,
        packet_queue=packet_queue,
        stop_event=stop_event,
        stats=stats,
    )
    processor_thread = ProcessorThread(
        packet_queue=packet_queue,
        udp_target=udp_target,
        stop_event=stop_event,
        assembler=assembler,
        stats=stats,
    )

    def _shutdown(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"receiver start iface={interface_name}")
    print(f"udp_target={udp_target[0]}:{udp_target[1]}")

    receiver_thread.start()
    processor_thread.start()

    try:
        while not stop_event.is_set():
            time.sleep(1.0)
            snapshot = stats.snapshot()
            print(
                "captured_pkts={captured:.0f} processed_frags={processed:.0f} "
                "forwarded_frames={forwarded:.0f} queue_drops={queue_drops:.0f} udp_drops={udp_drops:.0f}".format(
                    captured=snapshot.get("captured_packets", 0),
                    processed=snapshot.get("processed_fragments", 0),
                    forwarded=snapshot.get("forwarded_frames", 0),
                    queue_drops=snapshot.get("queue_drops", 0),
                    udp_drops=snapshot.get("udp_drops", 0),
                )
            )
    finally:
        stop_event.set()
        receiver_thread.join(timeout=2.0)
        processor_thread.join(timeout=2.0)

def _load_async_sniffer():
    global _ASYNC_SNIFFER, _ASYNC_SNIFFER_LOADED
    if _ASYNC_SNIFFER_LOADED:
        return _ASYNC_SNIFFER
    try:
        from scapy.all import AsyncSniffer as imported_async_sniffer
    except Exception:
        imported_async_sniffer = None
    _ASYNC_SNIFFER = imported_async_sniffer
    _ASYNC_SNIFFER_LOADED = True
    return _ASYNC_SNIFFER


if __name__ == "__main__":
    raise SystemExit(main())
