from __future__ import annotations

import io
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Dict, Optional

from PIL import Image, ImageOps, ImageTk

from ether_stream.common import (
    FrameAssembler,
    InterfaceInfo,
    Stats,
    list_interfaces,
    parse_udp_target,
    resolve_interface_name,
)
from ether_stream.gui_support import (
    RECEIVER_CONFIG,
    ToolTip,
    choose_recommended_interface,
    filter_display_interfaces,
    format_interface_label,
    load_gui_config,
    save_gui_config,
    summarize_interface,
)


class ReceiverController:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.stats = Stats()
        self.packet_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=4096)
        self.preview_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=2)
        self.assembler = FrameAssembler()
        self.receiver_thread: Optional[threading.Thread] = None
        self.processor_thread: Optional[threading.Thread] = None
        self.running = False
        self.preview_enabled = True

    def start(
        self,
        *,
        iface: str,
        udp_target: str,
        queue_size: int,
        max_age: float,
        preview_enabled: bool,
    ) -> None:
        from receiver import ProcessorThread, ReceiverThread

        if self.running:
            raise RuntimeError("接收端已经在运行中")
        if queue_size <= 0:
            raise ValueError("队列长度必须大于 0")
        if max_age <= 0:
            raise ValueError("拼包超时必须大于 0")
        if not udp_target.strip():
            raise ValueError("请填写 UDP 转发地址")

        canonical_iface = resolve_interface_name(iface)
        udp_host, udp_port = parse_udp_target(udp_target)
        self.stop_event = threading.Event()
        self.stats = Stats()
        self.preview_enabled = preview_enabled
        self.packet_queue = queue.Queue(maxsize=max(1, queue_size))
        self.preview_queue = queue.Queue(maxsize=2)
        self.assembler = FrameAssembler(max_age=max_age)
        self.receiver_thread = ReceiverThread(
            interface_name=canonical_iface,
            packet_queue=self.packet_queue,
            stop_event=self.stop_event,
            stats=self.stats,
        )
        self.processor_thread = ProcessorThread(
            packet_queue=self.packet_queue,
            udp_target=(udp_host, udp_port),
            stop_event=self.stop_event,
            assembler=self.assembler,
            stats=self.stats,
            preview_callback=self._push_preview_frame,
        )

        started_receiver = False
        started_processor = False
        try:
            self.receiver_thread.start()
            started_receiver = True
            self.processor_thread.start()
            started_processor = True
            self.running = True
        except Exception:
            self.stop_event.set()
            if started_receiver and self.receiver_thread is not None:
                self.receiver_thread.join(timeout=2.0)
            if started_processor and self.processor_thread is not None:
                self.processor_thread.join(timeout=2.0)
            self.receiver_thread = None
            self.processor_thread = None
            self.running = False
            raise

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        if self.receiver_thread is not None:
            self.receiver_thread.join(timeout=2.0)
        if self.processor_thread is not None:
            self.processor_thread.join(timeout=2.0)
        self.receiver_thread = None
        self.processor_thread = None
        self.running = False

    def snapshot(self) -> Dict[str, float]:
        data = self.stats.snapshot()
        data["queue_depth"] = float(self.packet_queue.qsize())
        return data

    def pop_preview_frame(self) -> Optional[bytes]:
        latest: Optional[bytes] = None
        while True:
            try:
                latest = self.preview_queue.get_nowait()
            except queue.Empty:
                return latest

    def set_preview_enabled(self, enabled: bool) -> None:
        self.preview_enabled = enabled
        if enabled:
            return
        while True:
            try:
                self.preview_queue.get_nowait()
            except queue.Empty:
                return

    def _push_preview_frame(self, jpeg_bytes: bytes) -> None:
        if not self.preview_enabled:
            return
        try:
            self.preview_queue.put_nowait(jpeg_bytes)
            return
        except queue.Full:
            pass
        try:
            self.preview_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.preview_queue.put_nowait(jpeg_bytes)
        except queue.Full:
            pass


class ReceiverApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Ether Stream 接收端")
        self.geometry("1340x960")
        self.minsize(1180, 820)
        self.configure(bg="#eef3f8")

        self.controller = ReceiverController()
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
        self._last_forwarded_frames = 0.0
        self._last_auto_udp_host = ""

        self.iface_var = tk.StringVar()
        self.udp_target_var = tk.StringVar(value="127.0.0.1:4455")
        self.queue_size_var = tk.StringVar(value="4096")
        self.max_age_var = tk.StringVar(value="0.25")
        self.preview_enabled_var = tk.BooleanVar(value=False)

        self.status_var = tk.StringVar(value="待机")
        self.subtitle_var = tk.StringVar(value="等待开始接收数据")
        self.iface_hint_var = tk.StringVar(value="正在识别推荐网卡")
        self.capture_stat_var = tk.StringVar(value="0")
        self.process_stat_var = tk.StringVar(value="0")
        self.forward_stat_var = tk.StringVar(value="0")
        self.queue_stat_var = tk.StringVar(value="0")
        self.detail_var = tk.StringVar(value="等待启动后显示统计信息")
        self.preview_hint_var = tk.StringVar(value="等待接收到第一帧画面")
        self.preview_meta_var = tk.StringVar(value="原始分辨率: 已关闭")
        self._preview_photo: Optional[ImageTk.PhotoImage] = None
        self._last_preview_image: Optional[Image.Image] = None

        self._build_styles()
        self._build_ui()
        self._load_config()
        self._set_interface_loading("正在加载网卡，请稍候...")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, lambda: self._refresh_interfaces(initial=True))
        self._tick_after_id = self.after(100, self._tick)

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
        style.configure("Hero.TFrame", background="#12395d")
        style.configure("HeroTitle.TLabel", background="#12395d", foreground="#ffffff", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("HeroSub.TLabel", background="#12395d", foreground="#d5e2f5", font=("Microsoft YaHei UI", 10))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#12395d", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("CardHint.TLabel", background="#ffffff", foreground="#6e7a88", font=("Microsoft YaHei UI", 10))
        style.configure("Field.TLabel", background="#ffffff", foreground="#25313d", font=("Microsoft YaHei UI", 11))
        style.configure("BadgeIdle.TLabel", background="#d8e1ec", foreground="#24405c", padding=(18, 10), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("BadgeRun.TLabel", background="#d9f5e4", foreground="#197245", padding=(18, 10), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("BadgeStop.TLabel", background="#fde6c8", foreground="#8a5412", padding=(18, 10), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(18, 10))
        style.configure("Secondary.TButton", font=("Microsoft YaHei UI", 11), padding=(18, 10))
        style.configure("StatCard.TFrame", background="#ffffff")
        style.configure("StatValue.TLabel", background="#ffffff", foreground="#12395d", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("StatName.TLabel", background="#ffffff", foreground="#6e7a88", font=("Microsoft YaHei UI", 10))

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=4, minsize=390)
        self.rowconfigure(4, weight=3, minsize=220)

        hero = ttk.Frame(self, style="Hero.TFrame", padding=(26, 20))
        hero.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 14))
        hero.columnconfigure(0, weight=1)
        ttk.Label(hero, text="Ether Stream 接收端", style="HeroTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            hero,
            text="监听原始以太网帧，完成分片重组后转发到本机或远程 UDP 服务。",
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
        ttk.Label(config_card, text="接收参数", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(config_card, text="网卡会自动排序并标出推荐项。鼠标停留可查看中文说明。", style="CardHint.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 8)
        )

        field_specs = [
            ("监听网卡", self.iface_var, "combo", "程序会把最可能正确的实体网卡排到最前面，并标记为推荐。"),
            ("UDP 转发地址", self.udp_target_var, "entry", "格式：IP:端口，例如 192.168.2.1:4455"),
            ("队列长度", self.queue_size_var, "entry", "抓包后暂存分片的队列大小。数值越大越抗抖动，但占用更多内存。"),
            ("拼包超时(秒)", self.max_age_var, "entry", "同一帧分片等待的最长时间。越小延迟越低，越大容错越强。"),
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
            else:
                entry = ttk.Entry(config_card, textvariable=variable)
                entry.grid(row=row_index, column=1, columnspan=2, sticky="ew", pady=6)
                ToolTip(entry, tip_text)
            row_index += 1

        help_card = ttk.Frame(content, style="Card.TFrame", padding=18)
        help_card.grid(row=0, column=1, sticky="nsew")
        help_card.columnconfigure(0, weight=1)
        help_card.rowconfigure(5, weight=1)
        ttk.Label(help_card, text="运行状态", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(help_card, textvariable=self.subtitle_var, style="CardHint.TLabel", wraplength=420, justify="left").grid(
            row=1, column=0, sticky="ew", pady=(2, 6)
        )
        ttk.Label(help_card, textvariable=self.iface_hint_var, style="CardHint.TLabel", wraplength=420, justify="left").grid(
            row=2, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Label(
            help_card,
            text="格式：IP:端口，例如 192.168.2.1:4455",
            style="CardHint.TLabel",
            wraplength=420,
            justify="left",
        ).grid(row=3, column=0, sticky="ew")

        action_bar = ttk.Frame(help_card, style="Card.TFrame")
        action_bar.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        action_bar.columnconfigure(0, weight=1)
        action_bar.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(action_bar, text="开始接收", style="Primary.TButton", command=self._start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ToolTip(self.start_button, "按当前参数开始抓包并重组以太网分片。")
        self.stop_button = ttk.Button(action_bar, text="停止接收", style="Secondary.TButton", command=self._stop)
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ToolTip(self.stop_button, "停止抓包和转发，保留当前参数。")
        self.stop_button.state(["disabled"])

        preview_frame = ttk.Frame(help_card, style="Card.TFrame")
        preview_frame.grid(row=5, column=0, sticky="nsew", pady=(16, 0))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(2, weight=1)
        preview_header = ttk.Frame(preview_frame, style="Card.TFrame")
        preview_header.grid(row=0, column=0, sticky="ew")
        preview_header.columnconfigure(2, weight=1)
        ttk.Label(preview_header, text="实时预览", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.preview_check = tk.Checkbutton(
            preview_header,
            variable=self.preview_enabled_var,
            command=self._on_preview_toggle,
            text="",
            width=2,
            height=2,
            bg="#ffffff",
            activebackground="#ffffff",
            selectcolor="#ffffff",
            relief="flat",
            bd=0,
            highlightthickness=0,
            anchor="center",
            font=("Segoe UI Symbol", 16, "bold"),
        )
        self.preview_check.grid(row=0, column=1, sticky="w", padx=(12, 0), pady=(0, 2))
        ToolTip(self.preview_check, "勾选后显示实时预览；取消勾选后仅保留抓包和 UDP 转发。")
        ttk.Label(preview_header, textvariable=self.preview_meta_var, style="CardHint.TLabel").grid(
            row=0, column=2, sticky="e"
        )
        self.preview_stage = tk.Frame(preview_frame, bg="#eef3f8", width=372, height=372, bd=0, highlightthickness=0)
        self.preview_stage.grid(row=2, column=0, sticky="n", pady=(12, 0))
        self.preview_stage.grid_propagate(False)
        self.preview_stage.columnconfigure(0, weight=1)
        self.preview_stage.rowconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(
            self.preview_stage,
            bg="#eef3f8",
            relief="flat",
            highlightthickness=0,
            bd=0,
        )
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        self.preview_canvas.bind("<Configure>", self._on_preview_canvas_configure)
        self.preview_label = tk.Label(
            self.preview_stage,
            textvariable=self.preview_hint_var,
            anchor="center",
            justify="center",
            bg="#eef3f8",
            fg="#52606d",
            relief="flat",
            font=("Microsoft YaHei UI", 11),
        )
        self.preview_label.grid(row=0, column=0, sticky="nsew")

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
        self._build_stat_card(stats_row, 0, "已捕获包数", self.capture_stat_var)
        self._build_stat_card(stats_row, 1, "已处理分片", self.process_stat_var)
        self._build_stat_card(stats_row, 2, "已转发帧数", self.forward_stat_var)
        self._build_stat_card(stats_row, 3, "当前队列深度", self.queue_stat_var)

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

        self._set_status("idle", "待机", "等待开始接收数据")

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
        self._sync_udp_target_to_selected_iface()
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
            raise ValueError("请选择有效的监听网卡")
        return self.iface_map[label]

    def _on_iface_changed(self, _: object = None) -> None:
        self._update_iface_hint()
        self._sync_udp_target_to_selected_iface(force=True)

    def _sync_udp_target_to_selected_iface(self, *, force: bool = False) -> None:
        current_raw = self.iface_map.get(self.iface_var.get().strip(), "")
        selected = self.interface_lookup.get(current_raw)
        if selected is None or not selected.ipv4_address:
            return

        current_value = self.udp_target_var.get().strip()
        current_host = ""
        port_text = "4455"
        if current_value:
            host_part, separator, port_part = current_value.rpartition(":")
            if separator and port_part.isdigit():
                current_host = host_part.strip()
                port_text = port_part.strip()
            else:
                current_host = current_value

        should_replace = force or not current_host or current_host in {"127.0.0.1", "192.168.2.1", self._last_auto_udp_host}
        if not should_replace:
            return

        self.udp_target_var.set(f"{selected.ipv4_address}:{port_text}")
        self._last_auto_udp_host = selected.ipv4_address

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

    def _start(self) -> None:
        if self.controller.running:
            return
        try:
            iface = self._selected_iface()
            self.controller.start(
                iface=iface,
                udp_target=self.udp_target_var.get().strip(),
                queue_size=int(self.queue_size_var.get().strip()),
                max_age=float(self.max_age_var.get().strip()),
                preview_enabled=self.preview_enabled_var.get(),
            )
        except Exception as exc:
            messagebox.showerror("接收端", str(exc), parent=self)
            self._log(f"启动失败: {exc}")
            return

        self._save_config()
        self._last_rate_sample_time = time.perf_counter()
        self._last_forwarded_frames = 0.0
        self._set_status("running", "运行中", "正在监听原始以太网帧")
        self.preview_meta_var.set("原始分辨率: 等待新画面")
        self._clear_preview("正在等待第一帧画面...")
        self._log(f"接收端已启动，接口: {iface}")
        self._log(f"UDP 转发目标: {self.udp_target_var.get().strip()}")

    def _stop(self) -> None:
        if not self.controller.running:
            return
        if self._stop_worker is not None and self._stop_worker.is_alive():
            return
        self._set_status("stopping", "停止中", "正在等待抓包和处理线程退出")
        self._log("正在停止接收端...")
        self._stop_worker = threading.Thread(target=self._stop_in_background, daemon=True)
        self._stop_worker.start()

    def _stop_in_background(self) -> None:
        self.controller.stop()
        self.after(0, self._finish_stop)

    def _finish_stop(self) -> None:
        self._set_status("idle", "已停止", "接收端已停止，参数已保留")
        self.preview_meta_var.set("原始分辨率: 暂无")
        self._clear_preview("已停止接收，预览已暂停")
        self._log("接收端已停止")
        if self._close_after_stop:
            self._cancel_tick()
            self.destroy()

    def _tick(self) -> None:
        self._drain_iface_results()
        self._drain_preview_frames()
        snapshot = self.controller.snapshot()
        now = time.perf_counter()
        elapsed = max(0.001, now - self._last_rate_sample_time)
        forwarded_frames = snapshot.get("forwarded_frames", 0.0)
        receive_fps = max(0.0, (forwarded_frames - self._last_forwarded_frames) / elapsed)
        self._last_rate_sample_time = now
        self._last_forwarded_frames = forwarded_frames
        self.capture_stat_var.set(f"{snapshot.get('captured_packets', 0):.0f}")
        self.process_stat_var.set(f"{snapshot.get('processed_fragments', 0):.0f}")
        self.forward_stat_var.set(f"{snapshot.get('forwarded_frames', 0):.0f}")
        self.queue_stat_var.set(f"{snapshot.get('queue_depth', 0):.0f}")
        self.detail_var.set(
            "接收帧率 {receive_fps:.1f} fps  |  接收端处理延迟 {process_ms:.1f} ms  |  队列丢弃 {queue_drop:.0f}  |  队列裁剪 {queue_trim:.0f}  |  UDP 丢弃 {udp_drop:.0f}  |  解码错误 {decode_err:.0f}".format(
                receive_fps=receive_fps,
                process_ms=snapshot.get("last_receiver_process_ms", 0.0),
                queue_drop=snapshot.get("queue_drops", 0),
                queue_trim=snapshot.get("queue_trims", 0),
                udp_drop=snapshot.get("udp_drops", 0),
                decode_err=snapshot.get("decode_errors", 0),
            )
        )
        self._tick_after_id = self.after(100, self._tick)

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

    def _on_preview_toggle(self) -> None:
        enabled = self.preview_enabled_var.get()
        self.controller.set_preview_enabled(enabled)
        if enabled:
            self.preview_meta_var.set("原始分辨率: 等待新画面")
            self._clear_preview("实时预览已开启，等待新画面...")
        else:
            self.preview_meta_var.set("原始分辨率: 已关闭")
            self._clear_preview("实时预览已关闭")

    def _drain_preview_frames(self) -> None:
        if not self.preview_enabled_var.get():
            return
        jpeg_bytes = self.controller.pop_preview_frame()
        if not jpeg_bytes:
            return
        try:
            image = Image.open(io.BytesIO(jpeg_bytes))
            image.load()
            image = image.convert("RGB")
        except Exception:
            self._clear_preview("预览解码失败，请检查发送画面数据")
            return

        self._last_preview_image = image
        self._render_preview_image()
        self.preview_meta_var.set(f"原始分辨率: {image.width}x{image.height}")
        self.preview_hint_var.set(f"按比例预览 {image.width}x{image.height}")

    def _clear_preview(self, hint: str) -> None:
        self._preview_photo = None
        self._last_preview_image = None
        if hasattr(self, "preview_canvas"):
            self.preview_canvas.delete("all")
        if hasattr(self, "preview_label"):
            self.preview_label.configure(image="", text=hint)
            self.preview_label.lift()
        self.preview_hint_var.set(hint)
        if not self.preview_enabled_var.get():
            self.preview_meta_var.set("原始分辨率: 已关闭")

    def _on_preview_canvas_configure(self, _: object = None) -> None:
        if not self.preview_enabled_var.get() or self._last_preview_image is None:
            return
        self._render_preview_image()

    def _render_preview_image(self) -> None:
        if self._last_preview_image is None:
            return
        target_width = self.preview_canvas.winfo_width()
        target_height = self.preview_canvas.winfo_height()
        if target_width <= 1 or target_height <= 1:
            self.after(30, self._render_preview_image)
            return
        fitted = ImageOps.contain(
            self._last_preview_image,
            (target_width, target_height),
            method=Image.Resampling.BILINEAR,
        )
        self._preview_photo = ImageTk.PhotoImage(fitted)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(target_width // 2, target_height // 2, image=self._preview_photo, anchor="center")
        self.preview_label.configure(image="", text="")
        self.preview_label.lower(self.preview_canvas)

    def _load_config(self) -> None:
        self._loaded_config = load_gui_config(RECEIVER_CONFIG)
        loaded_udp_target = self._loaded_config.get("udp_target", self.udp_target_var.get())
        if loaded_udp_target.strip() == "192.168.2.1:4455":
            loaded_udp_target = "127.0.0.1:4455"
        self.udp_target_var.set(loaded_udp_target)
        self.queue_size_var.set(self._loaded_config.get("queue_size", self.queue_size_var.get()))
        self.max_age_var.set(self._loaded_config.get("max_age", self.max_age_var.get()))
        preview_enabled_text = self._loaded_config.get("preview_enabled", "0").strip().lower()
        self.preview_enabled_var.set(preview_enabled_text not in {"0", "false", "off", "no"})
        self.controller.set_preview_enabled(self.preview_enabled_var.get())
        if not self.preview_enabled_var.get():
            self.preview_meta_var.set("原始分辨率: 已关闭")
            self._clear_preview("实时预览已关闭")

    def _save_config(self) -> None:
        save_gui_config(
            RECEIVER_CONFIG,
            {
                "iface_label": self.iface_var.get().strip(),
                "iface_raw": self.iface_map.get(self.iface_var.get().strip(), ""),
                "udp_target": self.udp_target_var.get().strip(),
                "queue_size": self.queue_size_var.get().strip(),
                "max_age": self.max_age_var.get().strip(),
                "preview_enabled": "1" if self.preview_enabled_var.get() else "0",
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
    app = ReceiverApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
