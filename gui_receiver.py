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
        self.preview_enabled = False

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
            raise RuntimeError("接收端已经在运行")
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
        self.configure(bg="#eef3f8")
        self._set_initial_window_geometry()

        self.controller = ReceiverController()
        self.iface_map: Dict[str, str] = {}
        self.interface_lookup: Dict[str, InterfaceInfo] = {}
        self.recommended_iface_raw = ""
        self._stop_worker: Optional[threading.Thread] = None
        self._close_after_stop = False
        self._loaded_config: Dict[str, str] = {}
        self._tick_after_id: Optional[str] = None
        self._iface_loading = False
        self._layout_after_id: Optional[str] = None
        self._compact_layout: Optional[bool] = None
        self._stats_compact_layout: Optional[bool] = None
        self._height_compact_layout: Optional[bool] = None
        self._initial_refresh_after_id: Optional[str] = None
        self._initial_layout_after_id: Optional[str] = None
        self._iface_result_queue: "queue.Queue[tuple[str, str, list[InterfaceInfo], Optional[InterfaceInfo], bool]]" = queue.Queue()
        self._last_rate_sample_time = time.perf_counter()
        self._last_forwarded_frames = 0.0
        self._last_auto_udp_host = ""
        self._preview_photo: Optional[ImageTk.PhotoImage] = None
        self._last_preview_image: Optional[Image.Image] = None
        self._preview_refresh_after_id: Optional[str] = None
        self._stat_cards: list[ttk.Frame] = []
        self._scroll_canvas: Optional[tk.Canvas] = None
        self._scroll_body: Optional[ttk.Frame] = None
        self._scroll_window_id: Optional[int] = None
        self._help_text_full = (
            "快速上手：先选择推荐网卡，再检查 UDP 转发地址是否是接收程序实际监听的 IP:端口。\n"
            "如果第三方程序绑定的是物理网卡 IP，不要继续使用 127.0.0.1。"
        )
        self._help_text_compact = "先选推荐网卡，再核对 UDP 转发地址是否和第三方程序监听的 IP:端口一致。"

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
        self.preview_hint_var = tk.StringVar(value="实时预览默认关闭")
        self.preview_meta_var = tk.StringVar(value="预览分辨率：未启用")

        self._build_styles()
        self._build_ui()
        self._load_config()
        self._update_preview_enabled_ui()
        self._set_interface_loading("正在加载网卡，请稍候...")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Configure>", self._on_window_configure)
        self._initial_refresh_after_id = self.after(60, lambda: self._refresh_interfaces(initial=True))
        self._initial_layout_after_id = self.after(120, self._apply_responsive_layout)
        self._tick_after_id = self.after(100, self._tick)

    def _set_initial_window_geometry(self) -> None:
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = min(max(1060, int(screen_width * 0.93)), 1480)
        height = min(max(780, int(screen_height * 0.91)), 1040)
        self.geometry(f"{width}x{height}")
        self.minsize(min(width, 980), min(height, 720))

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
        style.configure("BadgeIdle.TLabel", background="#d8e1ec", foreground="#24405c", padding=(16, 8), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("BadgeRun.TLabel", background="#d9f5e4", foreground="#197245", padding=(16, 8), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("BadgeStop.TLabel", background="#fde6c8", foreground="#8a5412", padding=(16, 8), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(16, 8))
        style.configure("Secondary.TButton", font=("Microsoft YaHei UI", 11), padding=(16, 8))
        style.configure("StatCard.TFrame", background="#ffffff")
        style.configure("StatValue.TLabel", background="#ffffff", foreground="#12395d", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("StatName.TLabel", background="#ffffff", foreground="#6e7a88", font=("Microsoft YaHei UI", 10))
        style.configure("Preview.TCheckbutton", background="#ffffff")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        shell = ttk.Frame(self, style="Page.TFrame")
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(0, weight=1)

        self._scroll_canvas = tk.Canvas(shell, bg="#eef3f8", highlightthickness=0, bd=0)
        self._scroll_canvas.grid(row=0, column=0, sticky="nsew")
        scroll_bar = ttk.Scrollbar(shell, orient="vertical", command=self._scroll_canvas.yview)
        scroll_bar.grid(row=0, column=1, sticky="ns")
        self._scroll_canvas.configure(yscrollcommand=scroll_bar.set)

        self._scroll_body = ttk.Frame(self._scroll_canvas, style="Page.TFrame")
        self._scroll_window_id = self._scroll_canvas.create_window(0, 0, window=self._scroll_body, anchor="nw")
        self._scroll_body.bind("<Configure>", self._on_scroll_body_configure)
        self._scroll_canvas.bind("<Configure>", self._on_scroll_canvas_configure)
        self._scroll_canvas.bind("<Enter>", self._bind_mousewheel)
        self._scroll_canvas.bind("<Leave>", self._unbind_mousewheel)

        self.page = self._scroll_body
        self.page.columnconfigure(0, weight=1)

        self.hero = ttk.Frame(self.page, style="Hero.TFrame", padding=(26, 20))
        self.hero.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 14))
        self.hero.columnconfigure(0, weight=1)
        ttk.Label(self.hero, text="Ether Stream 接收端", style="HeroTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.hero_subtitle = ttk.Label(
            self.hero,
            text="抓取原始以太网分片，完成拼包后转发为单包 JPEG UDP，并可在界面内实时预览。",
            style="HeroSub.TLabel",
        )
        self.hero_subtitle.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.status_badge = ttk.Label(self.hero, textvariable=self.status_var, style="BadgeIdle.TLabel")
        self.status_badge.grid(row=0, column=1, rowspan=2, sticky="e")

        self.content = ttk.Frame(self.page, style="Page.TFrame")
        self.content.grid(row=1, column=0, sticky="ew", padx=18)
        self.content.columnconfigure(0, weight=8)
        self.content.columnconfigure(1, weight=7)

        self.config_card = ttk.Frame(self.content, style="Card.TFrame", padding=18)
        self.config_card.columnconfigure(1, weight=1)
        self.config_card.columnconfigure(2, minsize=124)
        ttk.Label(self.config_card, text="接收参数", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self.config_hint_label = ttk.Label(
            self.config_card,
            text="监听网卡会优先显示推荐的实体网卡。选择后会自动带出该网卡的本机 IPv4 地址。",
            style="CardHint.TLabel",
        )
        self.config_hint_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 8))

        field_specs = [
            ("监听网卡", self.iface_var, "combo", "选择接收原始以太网分片的网卡。程序会优先推荐已连接的有线网卡。"),
            ("UDP 转发地址", self.udp_target_var, "entry", "格式示例：192.168.2.1:4455。接收端会把完整 JPEG 作为单个 UDP 包转发到这里。"),
            ("队列长度", self.queue_size_var, "entry", "接收分片时的缓存队列长度。越大越能抗瞬时抖动，但堆积过多会增加旧帧排队。"),
            ("拼包超时(秒)", self.max_age_var, "entry", "同一帧分片在这个时间内没收齐就丢弃，避免半帧长期占用队列。"),
        ]

        row_index = 2
        for label_text, variable, kind, tip_text in field_specs:
            label = ttk.Label(self.config_card, text=label_text, style="Field.TLabel")
            label.grid(row=row_index, column=0, sticky="w", padx=(0, 12), pady=6)
            ToolTip(label, tip_text)
            if kind == "combo":
                self.iface_combo = ttk.Combobox(self.config_card, textvariable=variable, state="readonly")
                self.iface_combo.grid(row=row_index, column=1, sticky="ew", pady=6)
                self.iface_combo.bind("<<ComboboxSelected>>", self._on_iface_changed)
                ToolTip(self.iface_combo, tip_text)
                self.refresh_button = ttk.Button(self.config_card, text="刷新网卡", command=self._refresh_interfaces)
                self.refresh_button.grid(row=row_index, column=2, sticky="ew", padx=(10, 0), pady=6)
                ToolTip(self.refresh_button, "重新扫描本机可用网卡。")
            else:
                entry = ttk.Entry(self.config_card, textvariable=variable)
                entry.grid(row=row_index, column=1, columnspan=2, sticky="ew", pady=6)
                ToolTip(entry, tip_text)
            row_index += 1

        self.side_panel = ttk.Frame(self.content, style="Page.TFrame")
        self.side_panel.columnconfigure(0, weight=1)

        self.help_card = ttk.Frame(self.side_panel, style="Card.TFrame", padding=18)
        self.help_card.columnconfigure(0, weight=1)
        ttk.Label(self.help_card, text="运行状态", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.subtitle_label = ttk.Label(self.help_card, textvariable=self.subtitle_var, style="CardHint.TLabel", justify="left")
        self.subtitle_label.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self.iface_hint_label = ttk.Label(self.help_card, textvariable=self.iface_hint_var, style="CardHint.TLabel", justify="left")
        self.iface_hint_label.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.help_text_label = ttk.Label(
            self.help_card,
            text=(
                "快速上手：先选择推荐网卡，再检查 UDP 转发地址是否是接收程序实际监听的 IP:端口。\n"
                "如果第三方程序绑定的是物理网卡 IP，不要继续使用 127.0.0.1。"
            ),
            style="CardHint.TLabel",
            justify="left",
        )
        self.help_text_label.grid(row=3, column=0, sticky="ew")

        action_bar = ttk.Frame(self.help_card, style="Card.TFrame")
        action_bar.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        action_bar.columnconfigure(0, weight=1)
        action_bar.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(action_bar, text="开始接收", style="Primary.TButton", command=self._start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ToolTip(self.start_button, "按当前参数开始抓包、拼包并转发。")
        self.stop_button = ttk.Button(action_bar, text="停止接收", style="Secondary.TButton", command=self._stop)
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ToolTip(self.stop_button, "停止接收线程和处理线程。")
        self.stop_button.state(["disabled"])

        self.preview_card = ttk.Frame(self.side_panel, style="Card.TFrame", padding=18)
        self.preview_card.columnconfigure(0, weight=1)
        self.preview_card.rowconfigure(1, weight=1)
        preview_header = ttk.Frame(self.preview_card, style="Card.TFrame")
        preview_header.grid(row=0, column=0, sticky="ew")
        preview_header.columnconfigure(0, weight=1)
        ttk.Label(preview_header, text="实时预览", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.preview_check = ttk.Checkbutton(
            preview_header,
            variable=self.preview_enabled_var,
            style="Preview.TCheckbutton",
            command=self._on_preview_toggle,
            takefocus=False,
        )
        self.preview_check.grid(row=0, column=1, sticky="e")
        ToolTip(self.preview_check, "勾上后显示接收到的最新完整 JPEG 预览；关闭可减少界面解码和缩放负担。")

        self.preview_host = tk.Frame(self.preview_card, bg="#f4f7fb", highlightthickness=0, bd=0)
        self.preview_host.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self.preview_host.grid_propagate(False)
        self.preview_host.configure(height=320)
        self.preview_label = tk.Label(
            self.preview_host,
            textvariable=self.preview_hint_var,
            bg="#f4f7fb",
            fg="#6e7a88",
            font=("Microsoft YaHei UI", 10),
            justify="center",
        )
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
        self.preview_host.bind("<Configure>", self._on_preview_host_resize)
        self.preview_meta_label = ttk.Label(self.preview_card, textvariable=self.preview_meta_var, style="CardHint.TLabel", justify="left")
        self.preview_meta_label.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        self.detail_card = ttk.Frame(self.page, style="Card.TFrame", padding=(18, 14))
        self.detail_card.grid(row=2, column=0, sticky="ew", padx=18, pady=(14, 10))
        self.detail_card.columnconfigure(0, weight=1)
        ttk.Label(self.detail_card, text="实时详情", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.detail_label = ttk.Label(self.detail_card, textvariable=self.detail_var, style="CardHint.TLabel", justify="left")
        self.detail_label.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.stats_row = ttk.Frame(self.page, style="Page.TFrame")
        self.stats_row.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 14))
        self._build_stat_card(self.stats_row, "抓到分片", self.capture_stat_var)
        self._build_stat_card(self.stats_row, "处理分片", self.process_stat_var)
        self._build_stat_card(self.stats_row, "转发整帧", self.forward_stat_var)
        self._build_stat_card(self.stats_row, "当前队列", self.queue_stat_var)

        self.log_card = ttk.Frame(self.page, style="Card.TFrame", padding=18)
        self.log_card.grid(row=4, column=0, sticky="nsew", padx=18, pady=(0, 18))
        self.log_card.columnconfigure(0, weight=1)
        self.log_card.rowconfigure(1, weight=1)
        ttk.Label(self.log_card, text="运行日志", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.log_box = scrolledtext.ScrolledText(
            self.log_card,
            height=11,
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

    def _build_stat_card(self, parent: ttk.Frame, title: str, variable: tk.StringVar) -> None:
        card = ttk.Frame(parent, style="StatCard.TFrame", padding=(18, 14))
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text=title, style="StatName.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, textvariable=variable, style="StatValue.TLabel").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self._stat_cards.append(card)

    def _on_scroll_body_configure(self, _: tk.Event[tk.Misc]) -> None:
        if self._scroll_canvas is None:
            return
        self._scroll_canvas.configure(scrollregion=self._scroll_canvas.bbox("all"))

    def _on_scroll_canvas_configure(self, event: tk.Event[tk.Misc]) -> None:
        if self._scroll_canvas is None or self._scroll_window_id is None:
            return
        self._scroll_canvas.itemconfigure(self._scroll_window_id, width=max(1, int(event.width)))

    def _bind_mousewheel(self, _: tk.Event[tk.Misc]) -> None:
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

    def _unbind_mousewheel(self, _: tk.Event[tk.Misc]) -> None:
        self.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> None:
        if self._scroll_canvas is None:
            return
        delta = int(getattr(event, "delta", 0))
        if delta == 0:
            return
        self._scroll_canvas.yview_scroll(-1 * int(delta / 120), "units")

    def _apply_responsive_layout(self) -> None:
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        compact = width < 1000
        if compact != self._compact_layout:
            self._compact_layout = compact
            self.config_card.grid_forget()
            self.side_panel.grid_forget()
            self.help_card.grid_forget()
            self.preview_card.grid_forget()
            if compact:
                self.content.columnconfigure(0, weight=1)
                self.content.columnconfigure(1, weight=0)
                self.content.rowconfigure(0, weight=0)
                self.content.rowconfigure(1, weight=0)
                self.content.rowconfigure(2, weight=0)
                self.config_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
                self.side_panel.grid(row=1, column=0, sticky="ew")
                self.side_panel.rowconfigure(0, weight=0)
                self.side_panel.rowconfigure(1, weight=0)
                self.help_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
                self.preview_card.grid(row=1, column=0, sticky="ew")
            else:
                self.content.columnconfigure(0, weight=8)
                self.content.columnconfigure(1, weight=7)
                self.content.rowconfigure(0, weight=0)
                self.content.rowconfigure(1, weight=0)
                self.content.rowconfigure(2, weight=0)
                self.config_card.grid(row=0, column=0, sticky="ew", padx=(0, 10))
                self.side_panel.grid(row=0, column=1, sticky="ew")
                self.side_panel.rowconfigure(0, weight=0)
                self.side_panel.rowconfigure(1, weight=0)
                self.help_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
                self.preview_card.grid(row=1, column=0, sticky="ew")

        height_compact = height < 860
        if height_compact != self._height_compact_layout:
            self._height_compact_layout = height_compact
            self.hero.configure(padding=(20, 12) if height_compact else (22, 16))
            self.config_card.configure(padding=12 if height_compact else 14)
            self.help_card.configure(padding=12 if height_compact else 14)
            self.preview_card.configure(padding=10 if height_compact else 14)
            self.detail_card.configure(padding=(12, 8) if height_compact else (14, 10))
            self.log_card.configure(padding=12 if height_compact else 14)
            self.log_box.configure(height=3 if height_compact else 4)
            self.help_text_label.configure(text=self._help_text_compact if height_compact else self._help_text_full)

        stats_compact = width < 960
        if stats_compact != self._stats_compact_layout:
            self._stats_compact_layout = stats_compact
            for card in self._stat_cards:
                card.grid_forget()
            if stats_compact:
                self.stats_row.rowconfigure(0, weight=1)
                self.stats_row.rowconfigure(1, weight=1)
                for column in range(2):
                    self.stats_row.columnconfigure(column, weight=1)
                for index, card in enumerate(self._stat_cards):
                    row = index // 2
                    column = index % 2
                    padx = (0, 10) if column == 0 else (0, 0)
                    pady = (0, 10) if row == 0 else (0, 0)
                    card.grid(row=row, column=column, sticky="nsew", padx=padx, pady=pady)
            else:
                self.stats_row.rowconfigure(0, weight=1)
                self.stats_row.rowconfigure(1, weight=0)
                for column in range(4):
                    self.stats_row.columnconfigure(column, weight=1)
                for index, card in enumerate(self._stat_cards):
                    padx = (0, 10) if index < len(self._stat_cards) - 1 else (0, 0)
                    card.grid(row=0, column=index, sticky="nsew", padx=padx)

        for card in self._stat_cards:
            card.configure(padding=(10, 8) if height_compact else (12, 10))

        wrap_width = max(380, width - 120)
        detail_wrap = max(520, width - 120)
        side_wrap = 420 if not compact else max(480, width - 120)
        self.detail_label.configure(wraplength=detail_wrap)
        self.subtitle_label.configure(wraplength=side_wrap)
        self.iface_hint_label.configure(wraplength=side_wrap)
        self.help_text_label.configure(wraplength=side_wrap)
        self.config_hint_label.configure(wraplength=wrap_width // 2)
        self.preview_meta_label.configure(wraplength=side_wrap)
        self.hero_subtitle.configure(wraplength=max(440, width - 260))
        self.preview_card.grid_configure(pady=(0, 8) if height_compact else (0, 10))
        self.stats_row.grid_configure(pady=(0, 10) if height_compact else (0, 14))
        self.log_card.grid_configure(pady=(0, 12) if height_compact else (0, 18))
        self._apply_preview_card_state()
        self._schedule_preview_refresh()

    def _on_window_configure(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is not self:
            return
        if self._layout_after_id is not None:
            try:
                self.after_cancel(self._layout_after_id)
            except tk.TclError:
                pass
        self._layout_after_id = self.after(80, self._finish_layout_refresh)

    def _finish_layout_refresh(self) -> None:
        self._layout_after_id = None
        self._apply_responsive_layout()

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
        self._autofill_udp_target_from_selected(force=initial)
        if not self.controller.running:
            self.start_button.state(["!disabled"] if values else ["disabled"])
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
        self._autofill_udp_target_from_selected(force=False)

    def _update_iface_hint(self) -> None:
        current_raw = self.iface_map.get(self.iface_var.get().strip(), "")
        selected = self.interface_lookup.get(current_raw)
        recommended = self.interface_lookup.get(self.recommended_iface_raw)
        if selected is None and recommended is None:
            self.iface_hint_var.set("没有可用网卡。请确认已安装 Npcap，并用管理员权限运行程序。")
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

    def _autofill_udp_target_from_selected(self, force: bool) -> None:
        current_raw = self.iface_map.get(self.iface_var.get().strip(), "")
        selected = self.interface_lookup.get(current_raw)
        if selected is None or not selected.ipv4_address:
            return

        current_value = self.udp_target_var.get().strip()
        current_host = ""
        current_port = 4455
        if current_value:
            try:
                current_host, current_port = parse_udp_target(current_value)
            except Exception:
                current_host = current_value
                current_port = 4455

        auto_hosts = {"", "127.0.0.1", "localhost", "0.0.0.0"}
        should_replace = force or current_host in auto_hosts or current_host == self._last_auto_udp_host
        if not should_replace:
            return

        self.udp_target_var.set(f"{selected.ipv4_address}:{current_port}")
        self._last_auto_udp_host = selected.ipv4_address

    def _parse_udp_target(self) -> str:
        value = self.udp_target_var.get().strip()
        host, port = parse_udp_target(value)
        return f"{host}:{port}"

    def _on_preview_toggle(self) -> None:
        enabled = bool(self.preview_enabled_var.get())
        self.controller.set_preview_enabled(enabled)
        self._update_preview_enabled_ui()
        if enabled:
            self._schedule_preview_refresh()
            self._log("已开启实时预览")
        else:
            self._clear_preview("实时预览已关闭", "预览分辨率：未启用")
            self._log("已关闭实时预览")

    def _update_preview_enabled_ui(self) -> None:
        if self.preview_enabled_var.get():
            if self._last_preview_image is None:
                self.preview_hint_var.set("等待接收第一帧画面")
                self.preview_meta_var.set("预览分辨率：等待数据")
        else:
            self.preview_hint_var.set("实时预览默认关闭")
            self.preview_meta_var.set("预览分辨率：未启用")
        self.controller.set_preview_enabled(bool(self.preview_enabled_var.get()))
        self._apply_preview_card_state()

    def _on_preview_host_resize(self, _: tk.Event[tk.Misc]) -> None:
        self._schedule_preview_refresh()

    def _schedule_preview_refresh(self) -> None:
        if self._preview_refresh_after_id is not None:
            try:
                self.after_cancel(self._preview_refresh_after_id)
            except tk.TclError:
                pass
        self._preview_refresh_after_id = self.after(20, self._refresh_preview_image)

    def _refresh_preview_image(self) -> None:
        self._preview_refresh_after_id = None
        if not self.preview_enabled_var.get() or self._last_preview_image is None:
            return
        max_width = max(1, self.preview_host.winfo_width() - 20)
        max_height = max(1, self.preview_host.winfo_height() - 20)
        if max_width < 20 or max_height < 20:
            return
        image = ImageOps.contain(self._last_preview_image, (max_width, max_height), method=Image.Resampling.LANCZOS)
        self._preview_photo = ImageTk.PhotoImage(image)
        self.preview_label.configure(image=self._preview_photo, text="")
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")

    def _clear_preview(self, hint: str, meta: str) -> None:
        self._preview_photo = None
        self._last_preview_image = None
        self.preview_hint_var.set(hint)
        self.preview_meta_var.set(meta)
        self.preview_label.configure(image="", textvariable=self.preview_hint_var)
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")

    def _apply_preview_card_state(self) -> None:
        compact_height = bool(self._height_compact_layout)
        enabled = bool(self.preview_enabled_var.get())
        if enabled:
            preview_height = 150 if compact_height else 220
            self.preview_host.grid()
            self.preview_meta_label.grid()
            self.preview_host.configure(height=preview_height)
        else:
            self.preview_host.grid_remove()
            self.preview_meta_label.grid_remove()
            self.preview_card.configure(padding=12 if compact_height else 14)

    def _start(self) -> None:
        if self.controller.running:
            return
        try:
            iface = self._selected_iface()
            udp_target = self._parse_udp_target()
            queue_size = int(self.queue_size_var.get().strip())
            max_age = float(self.max_age_var.get().strip())
            self.controller.start(
                iface=iface,
                udp_target=udp_target,
                queue_size=queue_size,
                max_age=max_age,
                preview_enabled=bool(self.preview_enabled_var.get()),
            )
        except Exception as exc:
            messagebox.showerror("接收端", str(exc), parent=self)
            self._log(f"启动失败: {exc}")
            return

        self._save_config()
        self._last_rate_sample_time = time.perf_counter()
        self._last_forwarded_frames = 0.0
        if self.preview_enabled_var.get():
            self._clear_preview("等待接收第一帧画面", "预览分辨率：等待数据")
        else:
            self._clear_preview("实时预览默认关闭", "预览分辨率：未启用")
        self._set_status("running", "运行中", "正在持续接收、拼包并转发画面")
        self._log(f"接收端已启动，监听接口：{iface}")
        self._log(f"UDP 转发地址：{udp_target}")

    def _stop(self) -> None:
        if not self.controller.running:
            return
        if self._stop_worker is not None and self._stop_worker.is_alive():
            return
        self._set_status("stopping", "停止中", "正在等待工作线程退出")
        self._log("正在停止接收端...")
        self._stop_worker = threading.Thread(target=self._stop_in_background, daemon=True)
        self._stop_worker.start()

    def _stop_in_background(self) -> None:
        self.controller.stop()
        self.after(0, self._finish_stop)

    def _finish_stop(self) -> None:
        self._set_status("idle", "已停止", "接收端已停止，参数已保留")
        self._log("接收端已停止")
        if self._close_after_stop:
            self._cancel_tick()
            self.destroy()

    def _tick(self) -> None:
        self._drain_iface_results()
        snapshot = self.controller.snapshot()
        now = time.perf_counter()
        elapsed = max(0.001, now - self._last_rate_sample_time)
        forwarded_frames = snapshot.get("forwarded_frames", 0.0)
        forward_fps = max(0.0, (forwarded_frames - self._last_forwarded_frames) / elapsed)
        self._last_rate_sample_time = now
        self._last_forwarded_frames = forwarded_frames

        self.capture_stat_var.set(f"{snapshot.get('captured_packets', 0):.0f}")
        self.process_stat_var.set(f"{snapshot.get('processed_fragments', 0):.0f}")
        self.forward_stat_var.set(f"{snapshot.get('forwarded_frames', 0):.0f}")
        self.queue_stat_var.set(f"{snapshot.get('queue_depth', 0):.0f}")

        oversize_bytes = snapshot.get("last_udp_oversize_bytes", 0.0) / 1024.0
        self.detail_var.set(
            "转发帧率 {forward_fps:.1f} fps  |  当前队列 {queue_depth:.0f}  |  接收端处理 {process_ms:.1f} ms  |  "
            "UDP 丢弃 {udp_drops:.0f}  |  队列丢弃 {queue_drops:.0f}  |  队列裁剪 {queue_trims:.0f}  |  "
            "超大帧丢弃 {oversize_frames:.0f}  |  最近超大帧 {oversize_bytes:.1f} KB".format(
                forward_fps=forward_fps,
                queue_depth=snapshot.get("queue_depth", 0.0),
                process_ms=snapshot.get("last_receiver_process_ms", 0.0),
                udp_drops=snapshot.get("udp_drops", 0.0),
                queue_drops=snapshot.get("queue_drops", 0.0),
                queue_trims=snapshot.get("queue_trims", 0.0),
                oversize_frames=snapshot.get("udp_oversize_frames", 0.0),
                oversize_bytes=oversize_bytes,
            )
        )

        if self.preview_enabled_var.get():
            preview_bytes = self.controller.pop_preview_frame()
            if preview_bytes:
                try:
                    with Image.open(io.BytesIO(preview_bytes)) as image:
                        self._last_preview_image = image.convert("RGB").copy()
                    width, height = self._last_preview_image.size
                    self.preview_meta_var.set(f"预览分辨率：{width} x {height}")
                    self._refresh_preview_image()
                except Exception:
                    self.preview_hint_var.set("收到数据，但预览解码失败")
                    self.preview_meta_var.set("预览分辨率：解码失败")

        self._tick_after_id = self.after(100, self._tick)

    def _drain_iface_results(self) -> None:
        while True:
            try:
                selected_label, saved_raw, interfaces, recommended, initial = self._iface_result_queue.get_nowait()
            except queue.Empty:
                return
            self._apply_interface_results(selected_label, saved_raw, interfaces, recommended, initial)

    def _cancel_tick(self) -> None:
        if self._tick_after_id is not None:
            try:
                self.after_cancel(self._tick_after_id)
            except tk.TclError:
                pass
            self._tick_after_id = None
        if self._preview_refresh_after_id is not None:
            try:
                self.after_cancel(self._preview_refresh_after_id)
            except tk.TclError:
                pass
            self._preview_refresh_after_id = None

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
            self.start_button.state(["disabled"] if self._iface_loading or not self.iface_map else ["!disabled"])
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
        self._loaded_config = load_gui_config(RECEIVER_CONFIG)
        self.udp_target_var.set(self._loaded_config.get("udp_target", self.udp_target_var.get()))
        self.queue_size_var.set(self._loaded_config.get("queue_size", self.queue_size_var.get()))
        self.max_age_var.set(self._loaded_config.get("max_age", self.max_age_var.get()))
        preview_flag = (self._loaded_config.get("preview_enabled", "0").strip() == "1")
        self.preview_enabled_var.set(preview_flag)

    def _save_config(self) -> None:
        payload = {
            "iface_label": self.iface_var.get().strip(),
            "iface_raw": self.iface_map.get(self.iface_var.get().strip(), ""),
            "udp_target": self.udp_target_var.get().strip(),
            "queue_size": self.queue_size_var.get().strip(),
            "max_age": self.max_age_var.get().strip(),
            "preview_enabled": "1" if self.preview_enabled_var.get() else "0",
        }
        save_gui_config(RECEIVER_CONFIG, payload)

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
        for after_id_attr in ("_initial_refresh_after_id", "_initial_layout_after_id"):
            after_id = getattr(self, after_id_attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
                setattr(self, after_id_attr, None)
        if self._layout_after_id is not None:
            try:
                self.after_cancel(self._layout_after_id)
            except tk.TclError:
                pass
            self._layout_after_id = None
        super().destroy()


def main() -> int:
    app = ReceiverApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
