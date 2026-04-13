from __future__ import annotations

import dataclasses
import json
import os
import queue
import re
import struct
import subprocess
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

ETHER_TYPE = 0x88B5
HEADER_STRUCT = struct.Struct("!IHHHQ")
HEADER_SIZE = HEADER_STRUCT.size
MAX_FRAME_PAYLOAD = 1400
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
GUID_RE = re.compile(r"\{?([0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12})\}?")
_SCAPY_HELPERS_LOCK = threading.Lock()
_SCAPY_GET_IF_LIST: Optional[Callable[[], List[str]]] = None
_SCAPY_GET_IF_HWADDR: Optional[Callable[[str], str]] = None
_SCAPY_HELPERS_LOADED = False
_INTERFACE_CACHE_LOCK = threading.Lock()
_INTERFACE_CACHE: Tuple[float, List["InterfaceInfo"]] = (0.0, [])
_ADAPTER_CACHE_LOCK = threading.Lock()
_ADAPTER_CACHE: Tuple[float, Dict[str, Dict[str, str]]] = (0.0, {})


def _clean_adapter_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "none" else text


@dataclasses.dataclass(frozen=True)
class FragmentHeader:
    frame_id: int
    fragment_index: int
    fragment_total: int
    payload_length: int
    sent_timestamp_ms: int


@dataclasses.dataclass(frozen=True)
class InterfaceInfo:
    raw_name: str
    friendly_name: str
    mac_address: str
    status: str = ""
    description: str = ""
    ipv4_address: str = ""

    @property
    def display_name(self) -> str:
        if self.friendly_name:
            return f"{self.friendly_name} [{self.mac_address}] :: {self.raw_name}"
        return f"{self.raw_name} [{self.mac_address}]"

    @property
    def concise_name(self) -> str:
        return self.friendly_name or self.raw_name

    @property
    def is_connected(self) -> bool:
        return self.status.strip().lower() == "up"

    @property
    def is_loopback(self) -> bool:
        text = f"{self.raw_name} {self.friendly_name} {self.description}".lower()
        return "loopback" in text

    @property
    def is_virtual(self) -> bool:
        text = f"{self.raw_name} {self.friendly_name} {self.description}".lower()
        keywords = (
            "vmware",
            "virtual",
            "tap-",
            "tap ",
            "sstap",
            "miniport",
            "npcap loopback",
            "loopback",
            "bluetooth",
            "hyper-v",
            "vethernet",
            "wireguard",
            "vpn",
            "ndis",
        )
        return any(keyword in text for keyword in keywords)

    @property
    def is_wifi(self) -> bool:
        text = f"{self.friendly_name} {self.description}".lower()
        keywords = (
            "wlan",
            "wi-fi",
            "wifi",
            "wireless",
            "802.11",
            "ax210",
            "ax211",
            "be200",
        )
        return any(keyword in text for keyword in keywords) and not self.is_virtual

    @property
    def is_wired_ethernet(self) -> bool:
        text = f"{self.friendly_name} {self.description}".lower()
        keywords = (
            "以太网",
            "ethernet",
            "realtek",
            "usb gbe",
            "family controller",
        )
        return any(keyword in text for keyword in keywords) and not self.is_virtual

    @property
    def is_physical_preferred(self) -> bool:
        return self.is_wired_ethernet

    @property
    def is_recommended_physical(self) -> bool:
        return (self.is_wired_ethernet or self.is_wifi) and not self.is_virtual and not self.is_loopback


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: Dict[str, float] = {}

    def add(self, key: str, value: float = 1.0) -> None:
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def set(self, key: str, value: float) -> None:
        with self._lock:
            self._values[key] = value

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._values)


class FrameAssembler:
    def __init__(self, *, max_age: float = 0.25, clean_interval: int = 128, max_frames: int = 256) -> None:
        self._max_age = max_age
        self._clean_interval = clean_interval
        self._max_frames = max_frames
        self._frames: Dict[int, Dict[str, object]] = {}
        self._push_count = 0

    def push(self, header: FragmentHeader, payload: bytes) -> Optional[bytes]:
        self._push_count += 1
        now = time.perf_counter()
        if self._push_count % self._clean_interval == 0:
            self._cleanup(now)

        if header.fragment_total <= 0 or header.fragment_index >= header.fragment_total:
            return None

        if len(self._frames) >= self._max_frames and header.frame_id not in self._frames:
            oldest_frame_id = min(self._frames.items(), key=lambda item: item[1]["timestamp"])[0]
            self._frames.pop(oldest_frame_id, None)

        slot = self._frames.setdefault(
            header.frame_id,
            {"total": header.fragment_total, "fragments": {}, "timestamp": now},
        )
        slot["timestamp"] = now
        slot["total"] = header.fragment_total
        slot["fragments"][header.fragment_index] = payload

        fragments = slot["fragments"]
        total = slot["total"]
        if len(fragments) < total:
            return None

        assembled = b"".join(fragments[index] for index in range(total))
        self._frames.pop(header.frame_id, None)
        return assembled

    def _cleanup(self, now: float) -> None:
        expired = [
            frame_id
            for frame_id, slot in self._frames.items()
            if now - float(slot["timestamp"]) > self._max_age
        ]
        for frame_id in expired:
            self._frames.pop(frame_id, None)

    def clear(self) -> None:
        self._frames.clear()


def pack_fragment(
    frame_id: int,
    fragment_index: int,
    fragment_total: int,
    payload: bytes,
    sent_timestamp_ms: int,
) -> bytes:
    return HEADER_STRUCT.pack(frame_id, fragment_index, fragment_total, len(payload), sent_timestamp_ms) + payload


def unpack_fragment(packet_payload: bytes) -> Tuple[FragmentHeader, bytes]:
    if len(packet_payload) < HEADER_SIZE:
        raise ValueError("packet too short for fragment header")
    frame_id, fragment_index, fragment_total, payload_length, sent_timestamp_ms = HEADER_STRUCT.unpack(
        packet_payload[:HEADER_SIZE]
    )
    payload = packet_payload[HEADER_SIZE : HEADER_SIZE + payload_length]
    if len(payload) != payload_length:
        raise ValueError("packet payload length mismatch")
    return FragmentHeader(frame_id, fragment_index, fragment_total, payload_length, sent_timestamp_ms), payload


def normalize_mac(value: str) -> str:
    compact = value.strip().lower().replace("-", ":")
    if not MAC_RE.fullmatch(compact):
        raise ValueError(f"invalid MAC address: {value!r}")
    return compact


def parse_size(value: str) -> Tuple[int, int]:
    width_text, height_text = value.lower().split("x", 1)
    width, height = int(width_text), int(height_text)
    if width <= 0 or height <= 0:
        raise ValueError("size must be positive")
    return width, height


def parse_bbox(value: str) -> Tuple[int, int, int, int]:
    parts = [int(part.strip()) for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be left,top,right,bottom")
    left, top, right, bottom = parts
    if right <= left or bottom <= top:
        raise ValueError("bbox must have positive width and height")
    return left, top, right, bottom


def parse_udp_target(value: str) -> Tuple[str, int]:
    host, port_text = value.rsplit(":", 1)
    port = int(port_text)
    if not (1 <= port <= 65535):
        raise ValueError("port must be 1-65535")
    return host, port


def split_chunks(data: bytes, chunk_size: int) -> List[bytes]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [data[index : index + chunk_size] for index in range(0, len(data), chunk_size)] or [b""]


def save_json(path: str, payload: Dict[str, object]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def load_json(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_interface_name(interface_name: str) -> str:
    candidate = interface_name.strip()
    if not candidate:
        raise ValueError("请选择有效的网卡接口")

    scapy_interfaces = _get_scapy_interfaces()
    if not scapy_interfaces:
        raise RuntimeError("未检测到可用的 Npcap 网卡，请先安装或重装 Npcap，然后重新打开软件再试。")

    exact_matches: List[str] = []
    guid_matches: List[str] = []
    friendly_matches: List[str] = []
    description_matches: List[str] = []
    candidate_lower = candidate.lower()
    candidate_guid = _extract_guid(candidate)

    for interface in scapy_interfaces:
        raw_name = _scapy_interface_raw_name(interface)
        if not raw_name:
            continue

        if raw_name.lower() == candidate_lower:
            exact_matches.append(raw_name)
            continue

        interface_guid = _extract_guid(f"{getattr(interface, 'guid', '')} {raw_name}")
        if candidate_guid and candidate_guid == interface_guid:
            guid_matches.append(raw_name)
            continue

        friendly_name = str(getattr(interface, "name", "")).strip()
        if friendly_name and friendly_name.lower() == candidate_lower:
            friendly_matches.append(raw_name)
            continue

        description = str(getattr(interface, "description", "")).strip()
        if description and description.lower() == candidate_lower:
            description_matches.append(raw_name)

    for matches in (exact_matches, guid_matches, friendly_matches, description_matches):
        unique_matches = list(dict.fromkeys(matches))
        if len(unique_matches) == 1:
            return unique_matches[0]

    raise RuntimeError(
        f"当前选择的网卡没有被 Npcap 识别：{interface_name}\n"
        "请先安装或重装 Npcap，然后重新打开软件后再试。"
    )


def resolve_source_mac(interface_name: str, explicit_mac: Optional[str] = None) -> str:
    if explicit_mac:
        return normalize_mac(explicit_mac)
    interface_name = resolve_interface_name(interface_name)
    _, get_if_hwaddr = _load_scapy_helpers()
    if get_if_hwaddr is None:
        raise RuntimeError("scapy is not available")
    try:
        return normalize_mac(get_if_hwaddr(interface_name))
    except Exception:
        adapter_details = _windows_adapter_details()
        details = adapter_details.get(_extract_guid(interface_name), {})
        try:
            return normalize_mac(str(details.get("mac", "")))
        except Exception as exc:
            raise RuntimeError(f"无法读取当前网卡的 MAC 地址：{interface_name}") from exc


def list_interfaces(*, force_refresh: bool = False) -> List[InterfaceInfo]:
    global _INTERFACE_CACHE
    now = time.monotonic()
    with _INTERFACE_CACHE_LOCK:
        cache_expiry, cached_items = _INTERFACE_CACHE
        if not force_refresh and cached_items and now < cache_expiry:
            return list(cached_items)

    adapter_details = _windows_adapter_details()
    _, get_if_hwaddr = _load_scapy_helpers()
    interfaces: List[InterfaceInfo] = []
    seen_raw_names: set[str] = set()
    for interface in _get_scapy_interfaces():
        raw_name = _scapy_interface_raw_name(interface)
        if not raw_name or raw_name.lower() in seen_raw_names:
            continue
        seen_raw_names.add(raw_name.lower())

        guid = _extract_guid(f"{getattr(interface, 'guid', '')} {raw_name}")
        details = adapter_details.get(guid, {})
        try:
            mac_address = normalize_mac(get_if_hwaddr(raw_name)) if get_if_hwaddr is not None else ""
        except Exception:
            mac_address = ""
        if not mac_address:
            try:
                mac_address = normalize_mac(str(details.get("mac", "")))
            except Exception:
                mac_address = "unavailable"

        interfaces.append(
            InterfaceInfo(
                raw_name=raw_name,
                friendly_name=str(details.get("name") or getattr(interface, "name", "")).strip(),
                mac_address=mac_address,
                status=str(details.get("status", "")).strip(),
                description=str(details.get("description") or getattr(interface, "description", "")).strip(),
                ipv4_address=str(details.get("ipv4", "")).strip(),
            )
        )

    sorted_items = sorted(interfaces, key=_interface_sort_key)
    with _INTERFACE_CACHE_LOCK:
        _INTERFACE_CACHE = (time.monotonic() + 2.0, list(sorted_items))
    return sorted_items


def drop_oldest_put(pkt_queue: "queue.Queue[bytes]", payload: bytes, stats: Optional[Stats] = None) -> None:
    try:
        pkt_queue.put_nowait(payload)
        return
    except queue.Full:
        if stats:
            stats.add("queue_drops", 1)
    try:
        pkt_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        pkt_queue.put_nowait(payload)
    except queue.Full:
        if stats:
            stats.add("queue_drops", 1)


def print_interfaces(interfaces: Iterable[InterfaceInfo]) -> None:
    for item in interfaces:
        print(item.display_name)


def _interface_sort_key(item: InterfaceInfo) -> Tuple[int, int, int, str]:
    return (
        0 if item.is_connected and item.is_wired_ethernet else
        1 if item.is_connected and item.is_wifi else
        2 if item.is_connected and item.is_recommended_physical else
        3 if item.is_connected and not item.is_virtual else
        4 if item.is_wired_ethernet else
        5 if item.is_wifi else
        6 if item.is_recommended_physical else
        7 if not item.is_virtual else
        8,
        0 if item.friendly_name else 1,
        0 if item.mac_address not in {"00:00:00:00:00:00", "unavailable"} else 1,
        item.concise_name.lower(),
    )


def _windows_adapter_details() -> Dict[str, Dict[str, str]]:
    global _ADAPTER_CACHE
    now = time.monotonic()
    with _ADAPTER_CACHE_LOCK:
        cache_expiry, cached_items = _ADAPTER_CACHE
        if cached_items and now < cache_expiry:
            return dict(cached_items)

    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$ips=@{}; "
        "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Where-Object { $_.IPAddress -and $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } | "
        "ForEach-Object { if (-not $ips.ContainsKey($_.InterfaceIndex)) { $ips[$_.InterfaceIndex] = $_.IPAddress } }; "
        "Get-NetAdapter | "
        "Select-Object -Property Name,InterfaceGuid,InterfaceIndex,Status,InterfaceDescription,MacAddress,"
        "@{Name='IPv4';Expression={$ips[$_.InterfaceIndex]}} | "
        "ConvertTo-Json -Compress"
    )
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", command],
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
    except Exception:
        return {}

    try:
        items = json.loads(output.decode("utf-8", errors="ignore"))
    except Exception:
        return {}

    if isinstance(items, dict):
        items = [items]

    mapping: Dict[str, Dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        guid = str(item.get("InterfaceGuid", "")).strip().upper().strip("{}")
        if guid:
            mapping[guid] = {
                "name": _clean_adapter_text(item.get("Name", "")),
                "status": _clean_adapter_text(item.get("Status", "")),
                "description": _clean_adapter_text(item.get("InterfaceDescription", "")),
                "mac": _clean_adapter_text(item.get("MacAddress", "")),
                "ipv4": _clean_adapter_text(item.get("IPv4", "")),
            }
    with _ADAPTER_CACHE_LOCK:
        _ADAPTER_CACHE = (time.monotonic() + 5.0, dict(mapping))
    return mapping


def _extract_guid(value: str) -> str:
    match = GUID_RE.search(value or "")
    return match.group(1).upper() if match else ""


def _get_scapy_interfaces() -> List[object]:
    try:
        from scapy.all import conf
    except Exception:
        return []
    try:
        return list(conf.ifaces.values())
    except Exception:
        return []


def _scapy_interface_raw_name(interface: object) -> str:
    return str(getattr(interface, "network_name", "") or getattr(interface, "name", "")).strip()


def _load_scapy_helpers() -> Tuple[Optional[Callable[[], List[str]]], Optional[Callable[[str], str]]]:
    global _SCAPY_GET_IF_LIST, _SCAPY_GET_IF_HWADDR, _SCAPY_HELPERS_LOADED
    if _SCAPY_HELPERS_LOADED:
        return _SCAPY_GET_IF_LIST, _SCAPY_GET_IF_HWADDR

    with _SCAPY_HELPERS_LOCK:
        if _SCAPY_HELPERS_LOADED:
            return _SCAPY_GET_IF_LIST, _SCAPY_GET_IF_HWADDR
        try:
            from scapy.all import get_if_hwaddr as imported_get_if_hwaddr
            from scapy.all import get_if_list as imported_get_if_list
        except Exception:
            imported_get_if_list = None
            imported_get_if_hwaddr = None
        _SCAPY_GET_IF_LIST = imported_get_if_list
        _SCAPY_GET_IF_HWADDR = imported_get_if_hwaddr
        _SCAPY_HELPERS_LOADED = True
    return _SCAPY_GET_IF_LIST, _SCAPY_GET_IF_HWADDR
