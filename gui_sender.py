from __future__ import annotations

import ctypes
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Dict, Optional

from ether_stream.common import (
    InterfaceInfo,
    MAX_FRAME_PAYLOAD,
    Stats,
    list_interfaces,
    normalize_mac,
    resolve_interface_name,
    resolve_source_mac,
)
from ether_stream.gui_support import (
    SENDER_CONFIG,
    ToolTip,
    choose_recommended_interface,
    filter_display_interfaces,
    format_interface_label,
    load_gui_config,
    save_gui_config,
    summarize_interface,
)


def _enable_windows_dpi_awareness() -> None:
    if not hasattr(ctypes, "windll"):
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _get_primary_screen_bounds() -> Optional[tuple[int, int, int, int]]:
    try:
        import mss

        with mss.mss() as capture:
            monitor = capture.monitors[1]
            return (
                int(monitor["left"]),
                int(monitor["top"]),
                int(monitor["width"]),
                int(monitor["height"]),
            )
    except Exception:
        pass

    if not hasattr(ctypes, "windll"):
        return None
    try:
        user32 = ctypes.windll.user32
        return (0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
    except Exception:
        return None


class SenderController:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.stats = Stats()
        self.frame_buffer = None
        self.capture_worker: Optional[threading.Thread] = None
        self.send_worker: Optional[threading.Thread] = None
        self.running = False

    def start(
        self,
        *,
        iface: str,
        dst_mac: str,
        src_mac: Optional[str],
        fps: int,
        quality: int,
        frame_payload: int,
        bbox: Optional[tuple[int, int, int, int]],
        target_size: Optional[tuple[int, int]],
    ) -> None:
        from sender import CaptureWorker, DoubleFrameBuffer, EncodeSendWorker

        if self.running:
            raise RuntimeError("发射端已经在运行中")
        if fps <= 0:
            raise ValueError("采集帧率必须大于 0")
        if not 30 <= quality <= 95:
            raise ValueError("JPEG 质量建议设置在 30 到 95 之间")
        if frame_payload <= 64:
            raise ValueError("单帧负载预算过小，建议至少大于 64")
        if not dst_mac.strip():
            raise ValueError("请填写目标 MAC 地址")

        canonical_iface = resolve_interface_name(iface)
        normalized_dst = normalize_mac(dst_mac)
        source_override = src_mac.strip() if src_mac and src_mac.strip() else None
        normalized_src = resolve_source_mac(canonical_iface, source_override)

        self.stop_event = threading.Event()
        self.stats = Stats()
        self.frame_buffer = DoubleFrameBuffer()
        self.capture_worker = CaptureWorker(
            stop_event=self.stop_event,
            frame_buffer=self.frame_buffer,
            bbox=bbox,
            target_size=target_size,
            fps=fps,
            stats=self.stats,
        )
        self.send_worker = EncodeSendWorker(
            stop_event=self.stop_event,
            frame_buffer=self.frame_buffer,
            interface_name=canonical_iface,
            source_mac=normalized_src,
            target_mac=normalized_dst,
            initial_quality=quality,
            frame_payload_budget=frame_payload,
            stats=self.stats,
        )

        started_capture = False
        started_send = False
        try:
            self.capture_worker.start()
            started_capture = True
            self.send_worker.start()
            started_send = True
            self.running = True
        except Exception:
            self.stop_event.set()
            if started_capture and self.capture_worker is not None:
                self.capture_worker.join(timeout=2.0)
            if started_send and self.send_worker is not None:
                self.send_worker.join(timeout=2.0)
            self.capture_worker = None
            self.send_worker = None
            self.running = False
            raise

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        if self.capture_worker is not None:
            self.capture_worker.join(timeout=2.0)
        if self.send_worker is not None:
            self.send_worker.join(timeout=2.0)
        self.capture_worker = None
        self.send_worker = None
        self.running = False

    def snapshot(self) -> Dict[str, float]:
        return self.stats.snapshot()


class SenderApp(tk.Tk):
    def __init__(self) -> None:
        _enable_windows_dpi_awareness()
        super().__init__()
        self.title("Ether Stream 发射端")
        self.geometry("1340x960")
        self.minsize(1180, 820)
        self.configure(bg="#eef3f8")

        self.controller = SenderController()
        self.iface_map: Dict[str, str] = {}
        self.interface_lookup: Dict[str, InterfaceInfo] = {}
        self.recommended_iface_raw = ""
        self._stop_worker: Optional[threading.Thread] = None
        self._close_after_stop = False
        self._loaded_config: Dict[str, str] = {}
        self._tick_after_id: Optional[str] = None
        self._iface_loading = False
        self._iface_result_queue: "queue.Queue[tuple[str, str, list[InterfaceInfo], Optional[InterfaceInfo], bool]]" = queue.Queue()
        self._last_rate_sample_time = time.perf_counter()
        self._last_sent_frames = 0.0
        self._last_captured_frames = 0.0

        self.iface_var = tk.StringVar()
        self.dst_mac_var = tk.StringVar()
        self.fps_var = tk.StringVar(value="60")
        self.quality_var = tk.StringVar(value="80")
        self.payload_var = tk.StringVar(value=str(MAX_FRAME_PAYLOAD))
        self.capture_size_var = tk.StringVar(value="320")

        self.status_var = tk.StringVar(value="待机")
        self.subtitle_var = tk.StringVar(value="准备开始推流")
        self.iface_hint_var = tk.StringVar(value="正在识别推荐网卡")
        self.capture_stat_var = tk.StringVar(value="0")
        self.encode_stat_var = tk.StringVar(value="0")
        self.send_stat_var = tk.StringVar(value="0")
        self.quality_stat_var = tk.StringVar(value="0")
        self.detail_var = tk.StringVar(value="等待启动后显示统计信息")

        self._build_styles()
        self._build_ui()
        self._load_config()
        self._set_interface_loading("正在加载网卡，请稍候...")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, lambda: self._refresh_interfaces(initial=True))
        self._tick_after_id = self.after(250, self._tick)

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        base_font = ("Microsoft YaHei UI", 10)
        style.configure(".", font=base_font)
        style.configure("Page.TFrame", background="#eef3f8")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Hero.TFrame", background="#183a66")
        style.configure("HeroTitle.TLabel", background="#183a66", foreground="#ffffff", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("HeroSub.TLabel", background="#183a66", foreground="#d5e2f5", font=("Microsoft YaHei UI", 10))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#183a66", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("CardHint.TLabel", background="#ffffff", foreground="#6e7a88", font=("Microsoft YaHei UI", 10))
        style.configure("Field.TLabel", background="#ffffff", foreground="#25313d", font=("Microsoft YaHei UI", 11))
        style.configure("BadgeIdle.TLabel", background="#d8e1ec", foreground="#24405c", padding=(18, 10), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("BadgeRun.TLabel", background="#d9f5e4", foreground="#197245", padding=(18, 10), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("BadgeStop.TLabel", background="#fde6c8", foreground="#8a5412", padding=(18, 10), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(18, 10))
        style.configure("Secondary.TButton", font=("Microsoft YaHei UI", 11), padding=(18, 10))
        style.configure("StatCard.TFrame", background="#ffffff")
        style.configure("StatValue.TLabel", background="#ffffff", foreground="#183a66", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("StatName.TLabel", background="#ffffff", foreground="#6e7a88", font=("Microsoft YaHei UI", 10))

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=4, minsize=390)
        self.rowconfigure(4, weight=3, minsize=220)

        hero = ttk.Frame(self, style="Hero.TFrame", padding=(26, 20))
        hero.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 14))
        hero.columnconfigure(0, weight=1)
        ttk.Label(hero, text="Ether Stream 发射端", style="HeroTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            hero,
            text="抓取屏幕中心区域并编码为 JPEG 分片，通过原始以太网帧发送到目标设备。",
            style="HeroSub.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.status_badge = ttk.Label(hero, textvariable=self.status_var, style="BadgeIdle.TLabel")
        self.status_badge.grid(row=0, column=1, rowspan=2, sticky="e")

        content = ttk.Frame(self, style="Page.TFrame")
        content.grid(row=1, column=0, sticky="nsew", padx=18)
        content.columnconfigure(0, weight=8)
        content.columnconfigure(1, weight=6)
        content.rowconfigure(0, weight=1)

        config_card = ttk.Frame(content, style="Card.TFrame", padding=18)
        config_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        config_card.columnconfigure(1, weight=1)
        config_card.columnconfigure(2, minsize=124)
        ttk.Label(config_card, text="发送参数", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(config_card, text="网卡会自动排序并标出推荐项。鼠标停留可查看中文说明。", style="CardHint.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 8)
        )

        field_specs = [
            ("网卡接口", self.iface_var, "combo", "程序会自动把最合适的实体网卡排到前面，并标记为推荐"),
            ("目标 MAC", self.dst_mac_var, "entry", "必填。这里填接收端网卡的 MAC 地址，支持 80:fa:5b:60:12:34 或 80-FA-5B-60-12-34 这两种常见格式。"),
            ("采集帧率", self.fps_var, "fps_combo", "默认 60。可选 120、150、300 或手动输入更高值，但数值越高越吃 CPU、编码和发送带宽，实际效果还会受显示器刷新率限制。"),
            ("JPEG 质量", self.quality_var, "entry", "画面压缩质量。数值越大越清晰，但带宽占用越高。常用 70 到 85。"),
            ("单帧负载预算", self.payload_var, "entry", "每个以太网分片允许装载的数据量。默认 1400，一般不要改。"),
            ("采集区域", self.capture_size_var, "entry", "填写中心方形区域边长。例如填 320，表示抓屏幕正中间 320x320。留空则抓全屏。"),
        ]

        row_index = 2
        for label_text, variable, kind, tip_text in field_specs:
            label = ttk.Label(config_card, text=label_text, style="Field.TLabel")
            label.grid(row=row_index, column=0, sticky="w", padx=(0, 12), pady=6)
            ToolTip(label, tip_text)
            if kind == "combo":
                self.iface_combo = ttk.Combobox(config_card, textvariable=variable, state="readonly", width=42)
                self.iface_combo.grid(row=row_index, column=1, sticky="ew", pady=6)
                self.iface_combo.bind("<<ComboboxSelected>>", self._on_iface_changed)
                ToolTip(self.iface_combo, tip_text)
                self.refresh_button = ttk.Button(config_card, text="刷新网卡", command=self._refresh_interfaces)
                self.refresh_button.grid(row=row_index, column=2, sticky="ew", padx=(10, 0), pady=6)
                ToolTip(self.refresh_button, "重新扫描一次本机所有可用网卡。")
            elif kind == "fps_combo":
                self.fps_combo = ttk.Combobox(
                    config_card,
                    textvariable=variable,
                    state="normal",
                    values=("60", "75", "90", "120", "144", "150", "165", "240", "300"),
                )
                self.fps_combo.grid(row=row_index, column=1, columnspan=2, sticky="ew", pady=6)
                ToolTip(self.fps_combo, tip_text)
            else:
                entry = ttk.Entry(config_card, textvariable=variable)
                entry.grid(row=row_index, column=1, columnspan=2, sticky="ew", pady=6)
                ToolTip(entry, tip_text)
                if label_text == "目标 MAC":
                    self.dst_mac_entry = entry
                    self.dst_mac_entry.bind("<FocusOut>", self._on_dst_mac_focus_out)
                    self.dst_mac_entry.bind("<Return>", self._on_dst_mac_commit)
            row_index += 1

        help_card = ttk.Frame(content, style="Card.TFrame", padding=18)
        help_card.grid(row=0, column=1, sticky="nsew")
        help_card.columnconfigure(0, weight=1)
        ttk.Label(help_card, text="运行状态", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(help_card, textvariable=self.subtitle_var, style="CardHint.TLabel", wraplength=420, justify="left").grid(
            row=1, column=0, sticky="ew", pady=(2, 6)
        )
        ttk.Label(help_card, textvariable=self.iface_hint_var, style="CardHint.TLabel", wraplength=420, justify="left").grid(
            row=2, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Label(
            help_card,
            text=(
                "快速上手：先在接收端启动监听，再回到这里直接选【推荐】网卡并填写目标 MAC。\n"
                "采集区域填 320，表示抓屏幕正中 320x320。"
            ),
            style="CardHint.TLabel",
            wraplength=420,
            justify="left",
        ).grid(row=3, column=0, sticky="ew")

        action_bar = ttk.Frame(help_card, style="Card.TFrame")
        action_bar.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        action_bar.columnconfigure(0, weight=1)
        action_bar.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(action_bar, text="开始发射", style="Primary.TButton", command=self._start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ToolTip(self.start_button, "按当前参数开始采集、编码并发送画面。")
        self.stop_button = ttk.Button(action_bar, text="停止发射", style="Secondary.TButton", command=self._stop)
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ToolTip(self.stop_button, "停止采集和发送，保留当前参数。")
        self.stop_button.state(["disabled"])

        detail_card = ttk.Frame(self, style="Card.TFrame", padding=(18, 14))
        detail_card.grid(row=2, column=0, sticky="ew", padx=18, pady=(14, 10))
        detail_card.columnconfigure(0, weight=1)
        ttk.Label(detail_card, text="实时详情", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(detail_card, textvariable=self.detail_var, style="CardHint.TLabel", wraplength=1200, justify="left").grid(
            row=1, column=0, sticky="ew", pady=(8, 0)
        )

        stats_row = ttk.Frame(self, style="Page.TFrame")
        stats_row.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 14))
        for column in range(4):
            stats_row.columnconfigure(column, weight=1)
        self._build_stat_card(stats_row, 0, "已采集帧数", self.capture_stat_var)
        self._build_stat_card(stats_row, 1, "已编码帧数", self.encode_stat_var)
        self._build_stat_card(stats_row, 2, "已发送帧数", self.send_stat_var)
        self._build_stat_card(stats_row, 3, "当前 JPEG 质量", self.quality_stat_var)

        log_card = ttk.Frame(self, style="Card.TFrame", padding=18)
        log_card.grid(row=4, column=0, sticky="nsew", padx=18, pady=(0, 18))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        ttk.Label(log_card, text="运行日志", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.log_box = scrolledtext.ScrolledText(
            log_card,
            height=12,
            bg="#0f1b2d",
            fg="#dbe5f0",
            insertbackground="#ffffff",
            relief="flat",
            font=("Consolas", 10),
            padx=10,
            pady=10,
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.log_box.configure(state="disabled")

        self._set_status("idle", "待机", "准备开始推流")

    def _build_stat_card(self, parent: ttk.Frame, column: int, title: str, variable: tk.StringVar) -> None:
        card = ttk.Frame(parent, style="StatCard.TFrame", padding=(18, 14))
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 10, 0))
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text=title, style="StatName.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, textvariable=variable, style="StatValue.TLabel").grid(row=1, column=0, sticky="w", pady=(10, 0))

    def _refresh_interfaces(self, initial: bool = False) -> None:
        if self._iface_loading and not initial:
            return
        self._set_interface_loading("正在扫描网卡，请稍候...")
        selected_label = self.iface_var.get().strip()
        saved_raw = self._loaded_config.get("iface_raw", "").strip()
        threading.Thread(
            target=self._refresh_interfaces_worker,
            args=(selected_label, saved_raw, initial),
            daemon=True,
        ).start()

    def _refresh_interfaces_worker(self, selected_label: str, saved_raw: str, initial: bool) -> None:
        try:
            interfaces = list_interfaces(force_refresh=not initial)
            recommended = choose_recommended_interface(interfaces)
        except Exception:
            interfaces = []
            recommended = None
        self._iface_result_queue.put((selected_label, saved_raw, interfaces, recommended, initial))

    def _apply_interface_results(
        self,
        selected_label: str,
        saved_raw: str,
        interfaces: list[InterfaceInfo],
        recommended: Optional[InterfaceInfo],
        initial: bool,
    ) -> None:
        if not self.winfo_exists():
            return

        all_count = len(interfaces)
        interfaces = filter_display_interfaces(interfaces)
        self.iface_map.clear()
        self.interface_lookup.clear()
        self.recommended_iface_raw = recommended.raw_name if recommended else ""

        values: list[str] = []
        for item in interfaces:
            label = format_interface_label(item, recommended=item.raw_name == self.recommended_iface_raw)
            values.append(label)
            self.iface_map[label] = item.raw_name
            self.interface_lookup[item.raw_name] = item

        self.iface_combo["values"] = values
        if selected_label in self.iface_map:
            self.iface_var.set(selected_label)
        else:
            chosen_raw = saved_raw if saved_raw in self.interface_lookup else self.recommended_iface_raw
            chosen_label = next((label for label, raw in self.iface_map.items() if raw == chosen_raw), "")
            if not chosen_label and values:
                chosen_label = values[0]
            self.iface_var.set(chosen_label)

        self._iface_loading = False
        self.iface_combo.configure(state="readonly" if values else "disabled")
        self.refresh_button.state(["!disabled"])
        self._update_iface_hint()
        if not self.controller.running:
            if values:
                self.start_button.state(["!disabled"])
            else:
                self.start_button.state(["disabled"])
        if not initial:
            self._log(f"已刷新网卡列表，当前显示 {len(values)} 项，共识别到 {all_count} 项")
        elif values:
            self._log(f"检测到 {all_count} 个接口，默认仅显示 {len(values)} 个推荐实体网卡")
        else:
            self._log("未检测到可用网卡，请确认已安装 Npcap")

    def _selected_iface(self) -> str:
        if self._iface_loading:
            raise ValueError("网卡正在加载，请稍等后再试")
        label = self.iface_var.get().strip()
        if not label or label not in self.iface_map:
            raise ValueError("请选择有效的网卡接口")
        return self.iface_map[label]

    def _on_iface_changed(self, _: object = None) -> None:
        self._update_iface_hint()

    def _update_iface_hint(self) -> None:
        current_raw = self.iface_map.get(self.iface_var.get().strip(), "")
        selected = self.interface_lookup.get(current_raw)
        recommended = self.interface_lookup.get(self.recommended_iface_raw)
        if selected is None and recommended is None:
            self.iface_hint_var.set("没有可用网卡。请确认已安装 Npcap，并用管理员运行程序。")
            return
        if selected is not None and current_raw == self.recommended_iface_raw:
            title, detail = summarize_interface(selected)
            self.iface_hint_var.set(f"当前已选推荐网卡：{title}\n{detail}")
            return
        if recommended is not None:
            title, detail = summarize_interface(recommended)
            hint = f"推荐优先选择：{title}\n{detail}"
            if selected is not None:
                current_title, _ = summarize_interface(selected)
                hint += f"\n当前选择：{current_title}"
            self.iface_hint_var.set(hint)
            return
        title, detail = summarize_interface(selected) if selected is not None else ("", "")
        self.iface_hint_var.set(f"当前选择：{title}\n{detail}")

    def _parse_capture_region(self) -> tuple[Optional[tuple[int, int, int, int]], Optional[tuple[int, int]]]:
        text = self.capture_size_var.get().strip()
        if not text:
            return None, None
        size = int(text)
        if size <= 0:
            raise ValueError("采集区域必须是正整数，例如 320")
        screen_bounds = _get_primary_screen_bounds()
        if screen_bounds is None:
            screen_left = 0
            screen_top = 0
            screen_width = self.winfo_screenwidth()
            screen_height = self.winfo_screenheight()
        else:
            screen_left, screen_top, screen_width, screen_height = screen_bounds
        max_side = min(screen_width, screen_height)
        if size > max_side:
            raise ValueError(f"采集区域过大，当前屏幕最大建议不超过 {max_side}")
        left = screen_left + (screen_width - size) // 2
        top = screen_top + (screen_height - size) // 2
        bbox = (left, top, left + size, top + size)
        return bbox, (size, size)

    def _normalize_dst_mac_value(self) -> None:
        text = self.dst_mac_var.get().strip()
        if not text:
            return
        try:
            normalized = normalize_mac(text)
        except ValueError:
            return
        self.dst_mac_var.set(normalized)

    def _on_dst_mac_focus_out(self, _: object = None) -> None:
        self._normalize_dst_mac_value()

    def _on_dst_mac_commit(self, _: object = None) -> str:
        self._normalize_dst_mac_value()
        return "break"

    def _start(self) -> None:
        if self.controller.running:
            return
        try:
            self._normalize_dst_mac_value()
            iface = self._selected_iface()
            bbox, target_size = self._parse_capture_region()
            self.controller.start(
                iface=iface,
                dst_mac=self.dst_mac_var.get().strip(),
                src_mac="",
                fps=int(self.fps_var.get().strip()),
                quality=int(self.quality_var.get().strip()),
                frame_payload=int(self.payload_var.get().strip()),
                bbox=bbox,
                target_size=target_size,
            )
        except Exception as exc:
            messagebox.showerror("发射端", str(exc), parent=self)
            self._log(f"启动失败: {exc}")
            return

        self._save_config()
        self._last_rate_sample_time = time.perf_counter()
        self._last_sent_frames = 0.0
        self._last_captured_frames = 0.0
        self._set_status("running", "运行中", "正在持续采集并发送画面")
        self._log(f"发射端已启动，接口: {iface}")
        self._log(f"目标 MAC: {self.dst_mac_var.get().strip().lower()}")
        if self.capture_size_var.get().strip():
            size = self.capture_size_var.get().strip()
            self._log(f"中心采集区域: {size}x{size}")
            if bbox is not None:
                self._log(f"采集坐标: {bbox[0]},{bbox[1]} -> {bbox[2]},{bbox[3]}")
            self._log("提示: 如果发射端窗口本身放在屏幕正中间，预览画面里也会把这个窗口一起采进去")
        else:
            self._log("中心采集区域: 全屏")

    def _stop(self) -> None:
        if not self.controller.running:
            return
        if self._stop_worker is not None and self._stop_worker.is_alive():
            return
        self._set_status("stopping", "停止中", "正在等待工作线程退出")
        self._log("正在停止发射端...")
        self._stop_worker = threading.Thread(target=self._stop_in_background, daemon=True)
        self._stop_worker.start()

    def _stop_in_background(self) -> None:
        self.controller.stop()
        self.after(0, self._finish_stop)

    def _finish_stop(self) -> None:
        self._set_status("idle", "已停止", "发射端已停止，参数已保留")
        self._log("发射端已停止")
        if self._close_after_stop:
            self._cancel_tick()
            self.destroy()

    def _tick(self) -> None:
        self._drain_iface_results()
        snapshot = self.controller.snapshot()
        now = time.perf_counter()
        elapsed = max(0.001, now - self._last_rate_sample_time)
        sent_frames = snapshot.get("sent_frames", 0.0)
        captured_frames = snapshot.get("captured_frames", 0.0)
        send_fps = max(0.0, (sent_frames - self._last_sent_frames) / elapsed)
        capture_fps = max(0.0, (captured_frames - self._last_captured_frames) / elapsed)
        self._last_rate_sample_time = now
        self._last_sent_frames = sent_frames
        self._last_captured_frames = captured_frames
        self.capture_stat_var.set(f"{snapshot.get('captured_frames', 0):.0f}")
        self.encode_stat_var.set(f"{snapshot.get('encoded_frames', 0):.0f}")
        self.send_stat_var.set(f"{snapshot.get('sent_frames', 0):.0f}")
        self.quality_stat_var.set(f"{snapshot.get('jpeg_quality', 0):.0f}")
        self.detail_var.set(
            "采集帧率 {capture_fps:.1f} fps  |  发送帧率 {send_fps:.1f} fps  |  本机处理延迟 {pipe_ms:.1f} ms  |  编码 {enc_ms:.1f} ms  |  发包 {send_ms:.1f} ms  |  发送错误 {send_err:.0f}".format(
                capture_fps=capture_fps,
                send_fps=send_fps,
                pipe_ms=snapshot.get("last_pipeline_ms", 0.0),
                enc_ms=snapshot.get("last_encode_ms", 0.0),
                send_ms=snapshot.get("last_send_ms", 0.0),
                send_err=snapshot.get("send_errors", 0),
            )
        )
        self._tick_after_id = self.after(250, self._tick)

    def _drain_iface_results(self) -> None:
        while True:
            try:
                selected_label, saved_raw, interfaces, recommended, initial = self._iface_result_queue.get_nowait()
            except queue.Empty:
                return
            self._apply_interface_results(selected_label, saved_raw, interfaces, recommended, initial)

    def _cancel_tick(self) -> None:
        if self._tick_after_id is None:
            return
        try:
            self.after_cancel(self._tick_after_id)
        except tk.TclError:
            pass
        self._tick_after_id = None

    def _set_status(self, kind: str, text: str, subtitle: str) -> None:
        self.status_var.set(text)
        self.subtitle_var.set(subtitle)
        if kind == "running":
            self.status_badge.configure(style="BadgeRun.TLabel")
            self.start_button.state(["disabled"])
            self.stop_button.state(["!disabled"])
        elif kind == "stopping":
            self.status_badge.configure(style="BadgeStop.TLabel")
            self.start_button.state(["disabled"])
            self.stop_button.state(["disabled"])
        else:
            self.status_badge.configure(style="BadgeIdle.TLabel")
            if self._iface_loading or not self.iface_map:
                self.start_button.state(["disabled"])
            else:
                self.start_button.state(["!disabled"])
            self.stop_button.state(["disabled"])

    def _set_interface_loading(self, hint_text: str) -> None:
        self._iface_loading = True
        self.iface_hint_var.set(hint_text)
        self.iface_combo.configure(state="disabled")
        self.iface_combo["values"] = ("正在加载网卡...",)
        if not self.iface_var.get().strip() or self.iface_var.get().strip() not in self.iface_map:
            self.iface_var.set("正在加载网卡...")
        self.refresh_button.state(["disabled"])
        if not self.controller.running:
            self.start_button.state(["disabled"])

    def _load_config(self) -> None:
        self._loaded_config = load_gui_config(SENDER_CONFIG)
        self.dst_mac_var.set(self._loaded_config.get("dst_mac", ""))
        loaded_fps = (self._loaded_config.get("fps", "") or "").strip()
        self.fps_var.set("60" if loaded_fps in {"", "30"} else loaded_fps)
        self.quality_var.set(self._loaded_config.get("quality", self.quality_var.get()))
        self.payload_var.set(self._loaded_config.get("payload", self.payload_var.get()))
        capture_size = self._loaded_config.get("capture_size", "")
        if not capture_size:
            old_bbox = self._loaded_config.get("bbox", "")
            if old_bbox.isdigit():
                capture_size = old_bbox
            old_size = self._loaded_config.get("size", "")
            if not capture_size and "x" in old_size:
                width_text, _, height_text = old_size.partition("x")
                if width_text == height_text and width_text.isdigit():
                    capture_size = width_text
        self.capture_size_var.set(capture_size or self.capture_size_var.get())

    def _save_config(self) -> None:
        save_gui_config(
            SENDER_CONFIG,
            {
                "iface_label": self.iface_var.get().strip(),
                "iface_raw": self.iface_map.get(self.iface_var.get().strip(), ""),
                "dst_mac": self.dst_mac_var.get().strip(),
                "fps": self.fps_var.get().strip(),
                "quality": self.quality_var.get().strip(),
                "payload": self.payload_var.get().strip(),
                "capture_size": self.capture_size_var.get().strip(),
            },
        )

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state="disabled")

    def _on_close(self) -> None:
        self._save_config()
        if self.controller.running:
            self._close_after_stop = True
            self._stop()
            return
        self._cancel_tick()
        self.destroy()

    def destroy(self) -> None:
        self._cancel_tick()
        super().destroy()


def main() -> int:
    app = SenderApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
