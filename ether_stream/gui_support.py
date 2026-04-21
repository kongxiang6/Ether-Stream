from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from ether_stream.common import InterfaceInfo


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


SENDER_CONFIG = _base_dir() / "sender_gui_config.json"
RECEIVER_CONFIG = _base_dir() / "receiver_gui_config.json"


def load_gui_config(path: Path) -> Dict[str, str]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def save_gui_config(path: Path, payload: Dict[str, str]) -> None:
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except Exception:
        return


def choose_recommended_interface(interfaces: Iterable[InterfaceInfo]) -> Optional[InterfaceInfo]:
    interfaces = list(interfaces)
    for item in interfaces:
        if item.is_connected and item.is_wired_ethernet:
            return item
    for item in interfaces:
        if item.is_connected and item.is_wifi:
            return item
    for item in interfaces:
        if item.is_connected and item.is_recommended_physical:
            return item
    for item in interfaces:
        if item.is_wired_ethernet:
            return item
    for item in interfaces:
        if item.is_wifi:
            return item
    for item in interfaces:
        if item.is_recommended_physical:
            return item
    return interfaces[0] if interfaces else None


def filter_display_interfaces(interfaces: Iterable[InterfaceInfo]) -> list[InterfaceInfo]:
    interfaces = list(interfaces)
    preferred = [item for item in interfaces if item.is_recommended_physical]
    if preferred:
        return preferred
    fallback = [item for item in interfaces if not item.is_virtual and not item.is_loopback]
    if fallback:
        return fallback
    return interfaces


def _shorten_text(text: str, limit: int = 26) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_interface_label(item: InterfaceInfo, *, recommended: bool = False) -> str:
    status_text = "已连接" if item.is_connected else "未连接"
    name = item.concise_name
    if not item.friendly_name and item.raw_name.startswith("\\Device\\NPF_"):
        name = "未识别网卡"
    prefix = "【推荐】" if recommended else ""
    return f"{prefix}{_shorten_text(name)} | {status_text} | {item.mac_address}"


def summarize_interface(item: InterfaceInfo) -> Tuple[str, str]:
    title = item.concise_name
    if item.description and item.description != item.concise_name:
        title = f"{title} / {item.description}"
    detail_parts = [f"{'已连接' if item.is_connected else '未连接'}", f"MAC {item.mac_address}"]
    if item.ipv4_address:
        detail_parts.append(f"IP {item.ipv4_address}")
    detail = "，".join(detail_parts)
    return title, detail


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str, *, wraplength: int = 520) -> None:
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.tip_window: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None
        self._destroyed = False
        widget.bind("<Enter>", self._schedule_show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._on_destroy, add="+")

    def _schedule_show(self, _: tk.Event[tk.Misc]) -> None:
        if self._destroyed:
            return
        self._cancel()
        try:
            self._after_id = self.widget.after(450, self._show)
        except tk.TclError:
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        if self._destroyed or self.tip_window is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
            self.tip_window = tk.Toplevel(self.widget)
            self.tip_window.wm_overrideredirect(True)
            self.tip_window.wm_geometry(f"+{x}+{y}")
        except tk.TclError:
            self.tip_window = None
            return
        label = tk.Label(
            self.tip_window,
            text=self.text,
            justify="left",
            wraplength=self.wraplength,
            bg="#fff8d8",
            fg="#2c2c2c",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=6,
            font=("Microsoft YaHei UI", 9),
        )
        label.pack()

    def _hide(self, _: object = None) -> None:
        self._cancel()
        if self.tip_window is not None:
            try:
                self.tip_window.destroy()
            except tk.TclError:
                pass
            self.tip_window = None

    def _on_destroy(self, _: object = None) -> None:
        self._destroyed = True
        self._hide()

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
