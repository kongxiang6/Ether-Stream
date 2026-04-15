from __future__ import annotations

import ctypes
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Dict, Optional

from PIL import ImageGrab, ImageTk

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

MAX_CAPTURE_SIZE = 640
MAX_CAPTURE_FPS = 360
DEFAULT_CAPTURE_SIZE = 320
CAPTURE_MODE_LABELS = {
    "center": "屏幕中心",
    "manual": "自由移动",
}
CAPTURE_MODE_CODES = {label: code for code, label in CAPTURE_MODE_LABELS.items()}


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


class CaptureRegionSelector(tk.Toplevel):
    def __init__(
        self,
        master: tk.Tk,
        *,
        screen_bounds: tuple[int, int, int, int],
        capture_size: int,
        initial_bbox: tuple[int, int, int, int],
    ) -> None:
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg="#0b1220")
        self._screen_left, self._screen_top, self._screen_width, self._screen_height = screen_bounds
        self._capture_size = capture_size
        self._selection = [
            max(0, initial_bbox[0] - self._screen_left),
            max(0, initial_bbox[1] - self._screen_top),
            max(0, initial_bbox[2] - self._screen_left),
            max(0, initial_bbox[3] - self._screen_top),
        ]
        self._drag_offset = (capture_size // 2, capture_size // 2)
        self.result: Optional[tuple[int, int, int, int]] = None
        self._background_photo: Optional[ImageTk.PhotoImage] = None

        self.geometry(f"{self._screen_width}x{self._screen_height}+{self._screen_left}+{self._screen_top}")
        self._canvas = tk.Canvas(self, highlightthickness=0, bd=0, relief="flat", cursor="fleur")
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Button-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<Double-Button-1>", lambda _: self._confirm())
        self.bind("<Return>", lambda _: self._confirm())
        self.bind("<Escape>", lambda _: self._cancel())

        toolbar = tk.Frame(self._canvas, bg="#14263d", padx=14, pady=10)
        self._canvas.create_window(18, 18, window=toolbar, anchor="nw")
        tk.Label(toolbar, text="自由移动采集框", bg="#14263d", fg="#ffffff", font=("Microsoft YaHei UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        tk.Label(
            toolbar,
            text="拖动方框到想要的位置，按 Enter 或点击确认后保存。",
            bg="#14263d",
            fg="#d4e1ef",
            font=("Microsoft YaHei UI", 9),
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 8))
        self._coord_var = tk.StringVar()
        tk.Label(toolbar, textvariable=self._coord_var, bg="#14263d", fg="#8fd3ff", font=("Consolas", 10)).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Button(toolbar, text="确认", command=self._confirm).grid(row=3, column=0, sticky="ew")
        ttk.Button(toolbar, text="取消", command=self._cancel).grid(row=3, column=1, sticky="ew", padx=8)
        ttk.Button(toolbar, text="回到居中", command=self._recenter).grid(row=3, column=2, sticky="ew")

        self._create_background()
        self._redraw()

    def show(self) -> Optional[tuple[int, int, int, int]]:
        self.deiconify()
        self.focus_force()
        self.grab_set()
        self.wait_window()
        return self.result

    def _create_background(self) -> None:
        try:
            screenshot = ImageGrab.grab(
                bbox=(
                    self._screen_left,
                    self._screen_top,
                    self._screen_left + self._screen_width,
                    self._screen_top + self._screen_height,
                ),
                all_screens=True,
            )
        except Exception:
            screenshot = None
        if screenshot is not None:
            self._background_photo = ImageTk.PhotoImage(screenshot)
            self._canvas.create_image(0, 0, image=self._background_photo, anchor="nw")
        else:
            self._canvas.configure(bg="#22354b")
        self._canvas.create_rectangle(
            0,
            0,
            self._screen_width,
            self._screen_height,
            fill="#000000",
            stipple="gray25",
            outline="",
        )

    def _selection_inside(self, x: int, y: int) -> bool:
        left, top, right, bottom = self._selection
        return left <= x <= right and top <= y <= bottom

    def _on_press(self, event: tk.Event[tk.Misc]) -> None:
        x = int(event.x)
        y = int(event.y)
        if self._selection_inside(x, y):
            self._drag_offset = (x - self._selection[0], y - self._selection[1])
        else:
            self._drag_offset = (self._capture_size // 2, self._capture_size // 2)
            self._move_to(x - self._drag_offset[0], y - self._drag_offset[1])

    def _on_drag(self, event: tk.Event[tk.Misc]) -> None:
        self._move_to(int(event.x) - self._drag_offset[0], int(event.y) - self._drag_offset[1])

    def _move_to(self, left: int, top: int) -> None:
        max_left = max(0, self._screen_width - self._capture_size)
        max_top = max(0, self._screen_height - self._capture_size)
        left = max(0, min(max_left, left))
        top = max(0, min(max_top, top))
        self._selection = [left, top, left + self._capture_size, top + self._capture_size]
        self._redraw()

    def _recenter(self) -> None:
        left = (self._screen_width - self._capture_size) // 2
        top = (self._screen_height - self._capture_size) // 2
        self._move_to(left, top)

    def _redraw(self) -> None:
        left, top, right, bottom = self._selection
        self._canvas.delete("selection")
        self._canvas.create_rectangle(left, top, right, bottom, outline="#1de9b6", width=3, tags="selection")
        self._canvas.create_line((left + right) // 2, top, (left + right) // 2, bottom, fill="#1de9b6", width=1, dash=(6, 4), tags="selection")
        self._canvas.create_line(left, (top + bottom) // 2, right, (top + bottom) // 2, fill="#1de9b6", width=1, dash=(6, 4), tags="selection")
        self._canvas.create_text(
            left + 12,
            top - 10 if top > 28 else bottom + 14,
            text=f"{self._capture_size} x {self._capture_size}",
            fill="#ffffff",
            font=("Consolas", 11, "bold"),
            anchor="w",
            tags="selection",
        )
        abs_left = self._screen_left + left
        abs_top = self._screen_top + top
        abs_right = self._screen_left + right
        abs_bottom = self._screen_top + bottom
        self._coord_var.set(f"屏幕坐标: {abs_left},{abs_top} -> {abs_right},{abs_bottom}")

    def _confirm(self) -> None:
        left, top, right, bottom = self._selection
        self.result = (
            self._screen_left + left,
            self._screen_top + top,
            self._screen_left + right,
            self._screen_top + bottom,
        )
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.grab_release()
        self.destroy()


class SenderController:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.stats = Stats()
        self.capture_store = None
        self.encoded_store = None
        self.capture_worker: Optional[threading.Thread] = None
        self.encode_worker: Optional[threading.Thread] = None
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
        lock_quality: bool,
        frame_payload: int,
        bbox: Optional[tuple[int, int, int, int]],
        target_size: Optional[tuple[int, int]],
    ) -> None:
        from sender import CaptureWorker, EncodeWorker, LatestValueStore, SendWorker, get_primary_display_refresh_rate

        if self.running:
            raise RuntimeError("发射端已在运行")
        if fps <= 0:
            raise ValueError("采集帧率必须大于 0")
        if fps > MAX_CAPTURE_FPS:
            raise ValueError(f"采集帧率最大只能设置为 {MAX_CAPTURE_FPS}")
        if not 30 <= quality <= 95:
            raise ValueError("JPEG 质量建议设置在 30 到 95 之间")
        if frame_payload <= 64:
            raise ValueError("单帧负载预算过小，建议至少大于 64")
        if not dst_mac.strip():
            raise ValueError("请填写目标 MAC 地址")
        if target_size is None:
            raise ValueError("请填写采集区域尺寸")
        if target_size[0] > MAX_CAPTURE_SIZE or target_size[1] > MAX_CAPTURE_SIZE:
            raise ValueError(f"采集区域最大只能设置为 {MAX_CAPTURE_SIZE}x{MAX_CAPTURE_SIZE}")

        canonical_iface = resolve_interface_name(iface)
        normalized_dst = normalize_mac(dst_mac)
        source_override = src_mac.strip() if src_mac and src_mac.strip() else None
        normalized_src = resolve_source_mac(canonical_iface, source_override)
        display_refresh_hz = max(1, int(round(get_primary_display_refresh_rate())))
        effective_capture_fps = max(1, min(fps, display_refresh_hz, MAX_CAPTURE_FPS))

        self.stop_event = threading.Event()
        self.stats = Stats()
        self.capture_store = LatestValueStore()
        self.encoded_store = LatestValueStore()
        self.capture_worker = CaptureWorker(
            stop_event=self.stop_event,
            frame_store=self.capture_store,
            bbox=bbox,
            target_size=target_size,
            fps=effective_capture_fps,
            stats=self.stats,
        )
        self.encode_worker = EncodeWorker(
            stop_event=self.stop_event,
            capture_store=self.capture_store,
            encoded_store=self.encoded_store,
            initial_quality=quality,
            lock_quality=lock_quality,
            frame_payload_budget=frame_payload,
            stats=self.stats,
        )
        self.send_worker = SendWorker(
            stop_event=self.stop_event,
            encoded_store=self.encoded_store,
            interface_name=canonical_iface,
            source_mac=normalized_src,
            target_mac=normalized_dst,
            send_fps=fps,
            frame_payload_budget=frame_payload,
            stats=self.stats,
        )
        self.stats.set("display_refresh_fps", float(display_refresh_hz))

        started_capture = False
        started_encode = False
        started_send = False
        try:
            self.capture_worker.start()
            started_capture = True
            self.encode_worker.start()
            started_encode = True
            self.send_worker.start()
            started_send = True
            self.running = True
        except Exception:
            self.stop_event.set()
            if started_capture and self.capture_worker is not None:
                self.capture_worker.join(timeout=2.0)
            if started_encode and self.encode_worker is not None:
                self.encode_worker.join(timeout=2.0)
            if started_send and self.send_worker is not None:
                self.send_worker.join(timeout=2.0)
            self.capture_store = None
            self.encoded_store = None
            self.capture_worker = None
            self.encode_worker = None
            self.send_worker = None
            self.running = False
            raise

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        if self.capture_worker is not None:
            self.capture_worker.join(timeout=2.0)
        if self.encode_worker is not None:
            self.encode_worker.join(timeout=2.0)
        if self.send_worker is not None:
            self.send_worker.join(timeout=2.0)
        self.capture_store = None
        self.encoded_store = None
        self.capture_worker = None
        self.encode_worker = None
        self.send_worker = None
        self.running = False

    def snapshot(self) -> Dict[str, float]:
        return self.stats.snapshot()


class SenderApp(tk.Tk):
    def __init__(self) -> None:
        _enable_windows_dpi_awareness()
        super().__init__()
        self.title("Ether Stream 发射端")
        self.configure(bg="#eef3f8")
        self._set_initial_window_geometry()

        self.controller = SenderController()
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
        self._last_sent_frames = 0.0
        self._last_captured_frames = 0.0
        self._last_skipped_frames = 0.0
        self._last_reused_frames = 0.0
        self._screen_bounds = _get_primary_screen_bounds()
        self._manual_capture_bbox: Optional[tuple[int, int, int, int]] = None
        self._stat_cards: list[ttk.Frame] = []
        self._scroll_canvas: Optional[tk.Canvas] = None
        self._scroll_body: Optional[ttk.Frame] = None
        self._scroll_window_id: Optional[int] = None
        self._help_text_full = (
            f"先在接收端启动监听，再回到这里选择推荐网卡并填写目标 MAC。\n"
            f"采集区域建议先用 {DEFAULT_CAPTURE_SIZE}，如果需要更大区域，最大不要超过 {MAX_CAPTURE_SIZE}。"
        )
        self._help_text_compact = f"先开接收端，再选推荐网卡并填目标 MAC。采集区域建议先用 {DEFAULT_CAPTURE_SIZE}。"
        self._hero_subtitle_full = "截取屏幕指定区域并编码为 JPEG 分片，通过原始以太网帧发送到目标设备。"
        self._hero_subtitle_compact = "截取屏幕区域并发送 JPEG 以太网分片。"

        self.iface_var = tk.StringVar()
        self.dst_mac_var = tk.StringVar()
        self.fps_var = tk.StringVar(value="60")
        self.quality_var = tk.StringVar(value="80")
        self.quality_lock_var = tk.BooleanVar(value=True)
        self.payload_var = tk.StringVar(value=str(MAX_FRAME_PAYLOAD))
        self.capture_size_var = tk.StringVar(value=str(DEFAULT_CAPTURE_SIZE))
        self.capture_mode_var = tk.StringVar(value=CAPTURE_MODE_LABELS["center"])
        self.capture_position_var = tk.StringVar(value="默认截取屏幕正中间区域")

        self.status_var = tk.StringVar(value="待机")
        self.subtitle_var = tk.StringVar(value="准备开始推流")
        self.iface_hint_var = tk.StringVar(value="正在识别推荐网卡")
        self.target_stat_var = tk.StringVar(value="0")
        self.capture_stat_var = tk.StringVar(value="0")
        self.encode_stat_var = tk.StringVar(value="0")
        self.send_stat_var = tk.StringVar(value="0")
        self.quality_stat_var = tk.StringVar(value="0")
        self.drop_stat_var = tk.StringVar(value="0")
        self.detail_var = tk.StringVar(value="等待启动后显示统计信息")

        self._build_styles()
        self._build_ui()
        self._load_config()
        self._update_capture_mode_ui()
        self._set_interface_loading("正在加载网卡，请稍候...")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Configure>", self._on_window_configure)
        self._initial_refresh_after_id = self.after(60, lambda: self._refresh_interfaces(initial=True))
        self._initial_layout_after_id = self.after(120, self._apply_responsive_layout)
        self._tick_after_id = self.after(250, self._tick)

    def _set_initial_window_geometry(self) -> None:
        bounds = _get_primary_screen_bounds()
        if bounds is None:
            screen_width = self.winfo_screenwidth()
            screen_height = self.winfo_screenheight()
        else:
            _, _, screen_width, screen_height = bounds
        width = min(max(1020, int(screen_width * 0.92)), 1440)
        height = min(max(760, int(screen_height * 0.90)), 980)
        self.geometry(f"{width}x{height}")
        self.minsize(min(width, 960), min(height, 700))

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
        style.configure("BadgeIdle.TLabel", background="#d8e1ec", foreground="#24405c", padding=(16, 8), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("BadgeRun.TLabel", background="#d9f5e4", foreground="#197245", padding=(16, 8), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("BadgeStop.TLabel", background="#fde6c8", foreground="#8a5412", padding=(16, 8), font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(16, 8))
        style.configure("Secondary.TButton", font=("Microsoft YaHei UI", 11), padding=(16, 8))
        style.configure("CardCheck.TCheckbutton", background="#ffffff", foreground="#25313d", font=("Microsoft YaHei UI", 10))
        style.configure("StatCard.TFrame", background="#ffffff")
        style.configure("StatValue.TLabel", background="#ffffff", foreground="#183a66", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("StatName.TLabel", background="#ffffff", foreground="#6e7a88", font=("Microsoft YaHei UI", 10))

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
        ttk.Label(self.hero, text="Ether Stream 发射端", style="HeroTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.hero_subtitle = ttk.Label(
            self.hero,
            text=self._hero_subtitle_full,
            style="HeroSub.TLabel",
        )
        self.hero_subtitle.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.status_badge = ttk.Label(self.hero, textvariable=self.status_var, style="BadgeIdle.TLabel")
        self.status_badge.grid(row=0, column=1, rowspan=2, sticky="e")

        self.content = ttk.Frame(self.page, style="Page.TFrame")
        self.content.grid(row=1, column=0, sticky="ew", padx=18)
        self.content.columnconfigure(0, weight=8)
        self.content.columnconfigure(1, weight=6)

        self.config_card = ttk.Frame(self.content, style="Card.TFrame", padding=18)
        self.config_card.columnconfigure(1, weight=1)
        self.config_card.columnconfigure(2, minsize=124)
        ttk.Label(self.config_card, text="发送参数", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self.config_hint_label = ttk.Label(
            self.config_card,
            text="网卡会自动排序并标出推荐项。默认建议勾选“锁定 JPEG 质量”。鼠标停留可查看中文说明。",
            style="CardHint.TLabel",
        )
        self.config_hint_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 8))

        field_specs = [
            ("网卡接口", self.iface_var, "combo", "程序会自动把最合适的实体网卡排到前面，并标记为推荐。"),
            ("目标 MAC", self.dst_mac_var, "entry", "必填。支持 80:fa:5b:60:12:34 或 80-FA-5B-60-12-34 这两种格式。"),
            ("采集帧率", self.fps_var, "fps_combo", f"默认 60，最大 {MAX_CAPTURE_FPS}。值越高，对 CPU、编码和网卡压力越大。"),
            ("JPEG 质量", self.quality_var, "entry", "建议 60 到 85。数值越大越清晰，但带宽占用越高。"),
            ("单帧负载预算", self.payload_var, "entry", "每个以太网分片允许装载的数据量。默认 1400，一般不用改。"),
            ("采集区域", self.capture_size_var, "entry", f"填写正方形边长。默认 {DEFAULT_CAPTURE_SIZE}，最大 {MAX_CAPTURE_SIZE}。"),
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
            elif kind == "fps_combo":
                self.fps_combo = ttk.Combobox(
                    self.config_card,
                    textvariable=variable,
                    state="normal",
                    values=("60", "75", "90", "120", "144", "150", "165", "240", "300", "360"),
                )
                self.fps_combo.grid(row=row_index, column=1, columnspan=2, sticky="ew", pady=6)
                ToolTip(self.fps_combo, tip_text)
            else:
                entry = ttk.Entry(self.config_card, textvariable=variable)
                if label_text == "JPEG 质量":
                    entry.grid(row=row_index, column=1, sticky="ew", pady=6)
                    self.quality_lock_check = ttk.Checkbutton(
                        self.config_card,
                        text="锁定 JPEG 质量",
                        variable=self.quality_lock_var,
                        style="CardCheck.TCheckbutton",
                        takefocus=False,
                    )
                    self.quality_lock_check.grid(row=row_index, column=2, sticky="w", padx=(10, 0), pady=6)
                    ToolTip(self.quality_lock_check, "推荐开启。开启后只会在单帧过大时临时压低质量，不会再自动涨到更高数值。")
                else:
                    entry.grid(row=row_index, column=1, columnspan=2, sticky="ew", pady=6)
                ToolTip(entry, tip_text)
                if label_text == "目标 MAC":
                    self.dst_mac_entry = entry
                    self.dst_mac_entry.bind("<FocusOut>", self._on_dst_mac_focus_out)
                    self.dst_mac_entry.bind("<Return>", self._on_dst_mac_commit)
                if label_text == "采集区域":
                    self.capture_size_entry = entry
                    self.capture_size_entry.bind("<FocusOut>", self._on_capture_size_commit)
                    self.capture_size_entry.bind("<Return>", self._on_capture_size_commit)
            row_index += 1

        mode_label = ttk.Label(self.config_card, text="截取位置", style="Field.TLabel")
        mode_label.grid(row=row_index, column=0, sticky="w", padx=(0, 12), pady=6)
        ToolTip(mode_label, "默认截取屏幕正中间区域。切换到自由移动后，可拖动方框指定采集位置。")
        self.capture_mode_combo = ttk.Combobox(
            self.config_card,
            textvariable=self.capture_mode_var,
            state="readonly",
            values=tuple(CAPTURE_MODE_LABELS.values()),
        )
        self.capture_mode_combo.grid(row=row_index, column=1, sticky="ew", pady=6)
        self.capture_mode_combo.bind("<<ComboboxSelected>>", self._on_capture_mode_changed)
        ToolTip(self.capture_mode_combo, "center 表示屏幕中心，manual 表示自由移动选区。")
        self.capture_select_button = ttk.Button(self.config_card, text="选择位置", command=self._choose_manual_capture_region)
        self.capture_select_button.grid(row=row_index, column=2, sticky="ew", padx=(10, 0), pady=6)
        ToolTip(self.capture_select_button, "打开全屏选区工具，拖动到想要的位置后确认。")
        row_index += 1

        self.capture_position_label = ttk.Label(
            self.config_card,
            textvariable=self.capture_position_var,
            style="CardHint.TLabel",
            justify="left",
        )
        self.capture_position_label.grid(row=row_index, column=0, columnspan=3, sticky="ew", pady=(0, 2))

        self.help_card = ttk.Frame(self.content, style="Card.TFrame", padding=18)
        self.help_card.columnconfigure(0, weight=1)
        ttk.Label(self.help_card, text="运行状态", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.subtitle_label = ttk.Label(self.help_card, textvariable=self.subtitle_var, style="CardHint.TLabel", justify="left")
        self.subtitle_label.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self.iface_hint_label = ttk.Label(self.help_card, textvariable=self.iface_hint_var, style="CardHint.TLabel", justify="left")
        self.iface_hint_label.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.help_text_label = ttk.Label(
            self.help_card,
            text=(
                "快速上手：先在接收端启动监听，再回到这里选择推荐网卡并填写目标 MAC。\n"
                f"采集区域建议先用 {DEFAULT_CAPTURE_SIZE}，如果需要更大区域，最大不要超过 {MAX_CAPTURE_SIZE}。"
            ),
            style="CardHint.TLabel",
            justify="left",
        )
        self.help_text_label.grid(row=3, column=0, sticky="ew")

        action_bar = ttk.Frame(self.help_card, style="Card.TFrame")
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

        self.detail_card = ttk.Frame(self.page, style="Card.TFrame", padding=(18, 14))
        self.detail_card.grid(row=2, column=0, sticky="ew", padx=18, pady=(14, 10))
        self.detail_card.columnconfigure(0, weight=1)
        ttk.Label(self.detail_card, text="实时详情", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.detail_label = ttk.Label(self.detail_card, textvariable=self.detail_var, style="CardHint.TLabel", justify="left")
        self.detail_label.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.stats_row = ttk.Frame(self.page, style="Page.TFrame")
        self.stats_row.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 14))
        self._build_stat_card(self.stats_row, "设定帧率", self.target_stat_var)
        self._build_stat_card(self.stats_row, "已采集帧数", self.capture_stat_var)
        self._build_stat_card(self.stats_row, "已发送帧数", self.send_stat_var)
        self._build_stat_card(self.stats_row, "当前实际 JPEG", self.quality_stat_var)
        self._build_stat_card(self.stats_row, "编码跳帧", self.drop_stat_var)

        self.log_card = ttk.Frame(self.page, style="Card.TFrame", padding=18)
        self.log_card.grid(row=4, column=0, sticky="nsew", padx=18, pady=(0, 18))
        self.log_card.columnconfigure(0, weight=1)
        self.log_card.rowconfigure(1, weight=1)
        ttk.Label(self.log_card, text="运行日志", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.log_box = scrolledtext.ScrolledText(
            self.log_card,
            height=10,
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
            self.help_card.grid_forget()
            if compact:
                self.content.columnconfigure(0, weight=1)
                self.content.columnconfigure(1, weight=0)
                self.content.rowconfigure(0, weight=0)
                self.content.rowconfigure(1, weight=0)
                self.config_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
                self.help_card.grid(row=1, column=0, sticky="ew")
            else:
                self.content.columnconfigure(0, weight=8)
                self.content.columnconfigure(1, weight=6)
                self.content.rowconfigure(0, weight=0)
                self.content.rowconfigure(1, weight=0)
                self.config_card.grid(row=0, column=0, sticky="ew", padx=(0, 10))
                self.help_card.grid(row=0, column=1, sticky="ew")

        height_compact = height < 1120
        if height_compact != self._height_compact_layout:
            self._height_compact_layout = height_compact
            self.hero.configure(padding=(20, 12) if height_compact else (22, 16))
            self.config_card.configure(padding=12 if height_compact else 14)
            self.help_card.configure(padding=12 if height_compact else 14)
            self.detail_card.configure(padding=(12, 8) if height_compact else (14, 10))
            self.log_card.configure(padding=12 if height_compact else 14)
            self.log_box.configure(height=2 if height_compact else 3)
            self.help_text_label.configure(text=self._help_text_compact if height_compact else self._help_text_full)
            self.hero_subtitle.configure(text=self._hero_subtitle_compact if height_compact else self._hero_subtitle_full)

        stats_compact = width < max(960, 220 * len(self._stat_cards))
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
                for column in range(len(self._stat_cards)):
                    self.stats_row.columnconfigure(column, weight=1)
                for index, card in enumerate(self._stat_cards):
                    padx = (0, 10) if index < len(self._stat_cards) - 1 else (0, 0)
                    card.grid(row=0, column=index, sticky="nsew", padx=padx)

        for card in self._stat_cards:
            card.configure(padding=(10, 8) if height_compact else (12, 10))

        wrap_width = max(360, width - 110)
        detail_wrap = max(460, width - 120)
        side_wrap = 420 if not compact else max(460, width - 120)
        self.detail_label.configure(wraplength=detail_wrap)
        self.capture_position_label.configure(wraplength=wrap_width // 2)
        self.subtitle_label.configure(wraplength=side_wrap)
        self.iface_hint_label.configure(wraplength=side_wrap)
        self.help_text_label.configure(wraplength=side_wrap)
        self.config_hint_label.configure(wraplength=wrap_width // 2)
        self.hero_subtitle.configure(wraplength=max(420, width - 260))
        self.stats_row.grid_configure(pady=(0, 10) if height_compact else (0, 14))
        self.log_card.grid_configure(pady=(0, 12) if height_compact else (0, 18))

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
            raise ValueError("请选择有效的网卡接口")
        return self.iface_map[label]

    def _on_iface_changed(self, _: object = None) -> None:
        self._update_iface_hint()

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

    def _parse_capture_size(self) -> int:
        text = self.capture_size_var.get().strip()
        if not text:
            self.capture_size_var.set(str(DEFAULT_CAPTURE_SIZE))
            return DEFAULT_CAPTURE_SIZE
        size = int(text)
        if size <= 0:
            raise ValueError("采集区域必须是正整数，例如 320")
        if size > MAX_CAPTURE_SIZE:
            raise ValueError(f"采集区域最大只能设置为 {MAX_CAPTURE_SIZE}")
        return size

    def _screen_geometry(self) -> tuple[int, int, int, int]:
        if self._screen_bounds is not None:
            return self._screen_bounds
        return (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())

    def _center_bbox(self, size: int) -> tuple[int, int, int, int]:
        screen_left, screen_top, screen_width, screen_height = self._screen_geometry()
        left = screen_left + (screen_width - size) // 2
        top = screen_top + (screen_height - size) // 2
        return (left, top, left + size, top + size)

    def _normalize_manual_bbox(self, bbox: tuple[int, int, int, int], size: int) -> tuple[int, int, int, int]:
        screen_left, screen_top, screen_width, screen_height = self._screen_geometry()
        center_x = (bbox[0] + bbox[2]) // 2
        center_y = (bbox[1] + bbox[3]) // 2
        left = center_x - size // 2
        top = center_y - size // 2
        max_left = screen_left + screen_width - size
        max_top = screen_top + screen_height - size
        left = max(screen_left, min(max_left, left))
        top = max(screen_top, min(max_top, top))
        return (left, top, left + size, top + size)

    def _capture_mode_code(self) -> str:
        return CAPTURE_MODE_CODES.get(self.capture_mode_var.get().strip(), "center")

    def _update_capture_mode_ui(self) -> None:
        try:
            size = self._parse_capture_size()
        except Exception:
            size = DEFAULT_CAPTURE_SIZE
        mode = self._capture_mode_code()
        if self.capture_mode_var.get().strip() not in CAPTURE_MODE_CODES:
            self.capture_mode_var.set(CAPTURE_MODE_LABELS[mode])
        if mode == "manual":
            self.capture_select_button.state(["!disabled"])
            if self._manual_capture_bbox is not None:
                self._manual_capture_bbox = self._normalize_manual_bbox(self._manual_capture_bbox, size)
                left, top, right, bottom = self._manual_capture_bbox
                self.capture_position_var.set(f"自由移动位置：{left},{top} -> {right},{bottom}，尺寸 {size}x{size}")
            else:
                self.capture_position_var.set("自由移动模式尚未选择位置，请点击“选择位置”")
        else:
            self.capture_select_button.state(["disabled"])
            left, top, right, bottom = self._center_bbox(size)
            self.capture_position_var.set(f"默认居中：{left},{top} -> {right},{bottom}，尺寸 {size}x{size}")

    def _on_capture_mode_changed(self, _: object = None) -> None:
        self._update_capture_mode_ui()

    def _on_capture_size_commit(self, _: object = None) -> str:
        try:
            size = self._parse_capture_size()
        except Exception as exc:
            messagebox.showerror("发射端", str(exc), parent=self)
            self.capture_size_var.set(str(DEFAULT_CAPTURE_SIZE))
            size = DEFAULT_CAPTURE_SIZE
        if self._manual_capture_bbox is not None:
            self._manual_capture_bbox = self._normalize_manual_bbox(self._manual_capture_bbox, size)
        self._update_capture_mode_ui()
        return "break"

    def _choose_manual_capture_region(self) -> None:
        try:
            size = self._parse_capture_size()
        except Exception as exc:
            messagebox.showerror("发射端", str(exc), parent=self)
            return
        bounds = self._screen_geometry()
        initial_bbox = self._manual_capture_bbox or self._center_bbox(size)
        selector = CaptureRegionSelector(self, screen_bounds=bounds, capture_size=size, initial_bbox=initial_bbox)
        selected = selector.show()
        if selected is None:
            return
        self._manual_capture_bbox = selected
        self.capture_mode_var.set(CAPTURE_MODE_LABELS["manual"])
        self._update_capture_mode_ui()

    def _parse_capture_region(self) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
        size = self._parse_capture_size()
        if self._capture_mode_code() == "manual":
            if self._manual_capture_bbox is None:
                raise ValueError("当前是自由移动模式，请先点击“选择位置”")
            bbox = self._normalize_manual_bbox(self._manual_capture_bbox, size)
            self._manual_capture_bbox = bbox
        else:
            bbox = self._center_bbox(size)
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
            fps = int(self.fps_var.get().strip())
            self.controller.start(
                iface=iface,
                dst_mac=self.dst_mac_var.get().strip(),
                src_mac="",
                fps=fps,
                quality=int(self.quality_var.get().strip()),
                lock_quality=bool(self.quality_lock_var.get()),
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
        self._last_skipped_frames = 0.0
        self._last_reused_frames = 0.0
        self._set_status("running", "运行中", "正在持续采集并发送画面")
        self._log(f"发射端已启动，接口 {iface}")
        self._log(f"目标 MAC: {self.dst_mac_var.get().strip().lower()}")
        self._log(f"采集区域: {target_size[0]}x{target_size[1]}")
        self._log(f"采集坐标: {bbox[0]},{bbox[1]} -> {bbox[2]},{bbox[3]}")
        self._log(f"截取模式: {self.capture_mode_var.get().strip() or CAPTURE_MODE_LABELS['center']}")
        self._log(
            f"JPEG 模式: {'固定' if self.quality_lock_var.get() else '自适应'}，设定质量 {self.quality_var.get().strip()}"
        )
        self._log("运行后端将自动检测：优先 dxcam 抓屏、turbojpeg 编码；不可用时自动回退")

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
        skipped_frames = snapshot.get("capture_to_encode_skips", snapshot.get("latest_frame_skips", 0.0))
        reused_frames = snapshot.get("reused_frame_sends", 0.0)
        send_fps = max(0.0, (sent_frames - self._last_sent_frames) / elapsed)
        capture_fps = max(0.0, (captured_frames - self._last_captured_frames) / elapsed)
        skipped_fps = max(0.0, (skipped_frames - self._last_skipped_frames) / elapsed)
        reused_fps = max(0.0, (reused_frames - self._last_reused_frames) / elapsed)
        self._last_rate_sample_time = now
        self._last_sent_frames = sent_frames
        self._last_captured_frames = captured_frames
        self._last_skipped_frames = skipped_frames
        self._last_reused_frames = reused_frames
        send_target_fps = snapshot.get("send_target_fps", 0.0)
        if send_target_fps <= 0:
            try:
                send_target_fps = float(int(self.fps_var.get().strip()))
            except Exception:
                send_target_fps = 0.0
        capture_target_fps = snapshot.get("capture_target_fps", 0.0)
        capture_limit_fps = snapshot.get("display_refresh_fps", 60.0)
        capture_coverage = (capture_fps / capture_target_fps * 100.0) if capture_target_fps > 0 else 0.0
        send_coverage = (send_fps / send_target_fps * 100.0) if send_target_fps > 0 else 0.0
        quality_target = snapshot.get("jpeg_target_quality", 0.0)
        if quality_target <= 0:
            try:
                quality_target = float(int(self.quality_var.get().strip()))
            except Exception:
                quality_target = 0.0
        quality_locked = snapshot.get("quality_locked", 1.0) >= 0.5
        actual_quality = snapshot.get("jpeg_quality", 0.0)
        capture_ms = snapshot.get("last_capture_ms", 0.0)
        encode_ms = snapshot.get("last_encode_ms", 0.0)
        send_ms = snapshot.get("last_send_ms", 0.0)
        frame_age_ms = snapshot.get("last_frame_age_ms", 0.0)
        total_ms = snapshot.get("last_pipeline_ms", capture_ms + encode_ms + send_ms)
        encoded_frames = snapshot.get("encoded_frames", 0.0)
        send_errors = snapshot.get("send_errors", 0.0)
        fragment_count = snapshot.get("last_fragment_count", 0.0)
        if snapshot.get("capture_backend_dxcam", 0.0) >= 0.5:
            capture_backend = "dxcam"
        elif snapshot.get("capture_backend_mss", 0.0) >= 0.5:
            capture_backend = "mss"
        elif snapshot.get("capture_backend_pillow", 0.0) >= 0.5:
            capture_backend = "pillow"
        else:
            capture_backend = "检测中"
        if snapshot.get("jpeg_encoder_turbojpeg", 0.0) >= 0.5:
            jpeg_backend = "turbojpeg"
        elif snapshot.get("jpeg_encoder_opencv", 0.0) >= 0.5:
            jpeg_backend = "opencv"
        elif snapshot.get("jpeg_encoder_pillow", 0.0) >= 0.5:
            jpeg_backend = "pillow"
        else:
            jpeg_backend = "检测中"
        self.capture_stat_var.set(f"{snapshot.get('captured_frames', 0):.0f}")
        self.send_stat_var.set(f"{snapshot.get('sent_frames', 0):.0f}")
        self.target_stat_var.set(f"{send_target_fps:.0f}")
        self.encode_stat_var.set(f"{encoded_frames:.0f}")
        self.quality_stat_var.set(f"{actual_quality:.0f}")
        self.drop_stat_var.set(f"{skipped_frames:.0f}")
        self.detail_var.set(
            "发送目标 {send_target_fps:.0f} fps  |  自动采集上限 {capture_target_fps:.0f} fps（主屏约 {capture_limit_fps:.0f} Hz）  |  实采 {capture_fps:.1f} fps ({capture_cover:.0f}%)  |  实发 {send_fps:.1f} fps ({send_cover:.0f}%)\n"
            "重复发包 {reuse_fps:.1f} fps / 累计 {reuse_total:.0f}  |  编码跳帧 {skip_fps:.1f} fps / 累计 {skip_total:.0f}  |  画面年龄 {frame_age_ms:.1f} ms\n"
            "抓屏后端 {capture_backend}  |  编码后端 {jpeg_backend}  |  采集 {capture_ms:.1f} ms  |  编码 {enc_ms:.1f} ms  |  发包 {send_ms:.1f} ms  |  单次链路 {total_ms:.1f} ms  |  JPEG {quality_mode} 设定 {quality_target:.0f} / 实际 {actual_quality:.0f}  |  分片 {fragment_count:.0f}  |  发送错误 {send_err:.0f}  |  JPEG {jpeg_kb:.1f} KB".format(
                send_target_fps=send_target_fps,
                capture_target_fps=capture_target_fps,
                capture_limit_fps=capture_limit_fps,
                capture_fps=capture_fps,
                capture_cover=capture_coverage,
                send_fps=send_fps,
                send_cover=send_coverage,
                reuse_fps=reused_fps,
                reuse_total=reused_frames,
                skip_fps=skipped_fps,
                skip_total=skipped_frames,
                frame_age_ms=frame_age_ms,
                capture_backend=capture_backend,
                jpeg_backend=jpeg_backend,
                capture_ms=capture_ms,
                enc_ms=encode_ms,
                send_ms=send_ms,
                total_ms=total_ms,
                jpeg_kb=snapshot.get("last_jpeg_bytes", 0.0) / 1024.0,
                fragment_count=fragment_count,
                quality_mode="固定" if quality_locked else "自适应",
                quality_target=quality_target,
                actual_quality=actual_quality,
                send_err=send_errors,
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
        self._loaded_config = load_gui_config(SENDER_CONFIG)
        self.dst_mac_var.set(self._loaded_config.get("dst_mac", ""))
        loaded_fps = (self._loaded_config.get("fps", "") or "").strip()
        if not loaded_fps.isdigit():
            loaded_fps = "60"
        self.fps_var.set(str(min(MAX_CAPTURE_FPS, max(1, int(loaded_fps)))))
        self.quality_var.set(self._loaded_config.get("quality", self.quality_var.get()))
        quality_lock_text = str(self._loaded_config.get("quality_lock", "1")).strip().lower()
        self.quality_lock_var.set(quality_lock_text not in {"0", "false", "off", "no"})
        self.payload_var.set(self._loaded_config.get("payload", self.payload_var.get()))
        capture_size = self._loaded_config.get("capture_size", str(DEFAULT_CAPTURE_SIZE)).strip()
        if not capture_size.isdigit():
            capture_size = str(DEFAULT_CAPTURE_SIZE)
        self.capture_size_var.set(str(min(MAX_CAPTURE_SIZE, max(1, int(capture_size)))))
        capture_mode = self._loaded_config.get("capture_mode", "center").strip().lower()
        self.capture_mode_var.set(CAPTURE_MODE_LABELS.get(capture_mode, CAPTURE_MODE_LABELS["center"]))
        bbox_text = self._loaded_config.get("manual_bbox", "").strip()
        if bbox_text:
            try:
                parts = [int(part.strip()) for part in bbox_text.split(",")]
                if len(parts) == 4:
                    self._manual_capture_bbox = tuple(parts)  # type: ignore[assignment]
            except Exception:
                self._manual_capture_bbox = None

    def _save_config(self) -> None:
        payload = {
            "iface_label": self.iface_var.get().strip(),
            "iface_raw": self.iface_map.get(self.iface_var.get().strip(), ""),
            "dst_mac": self.dst_mac_var.get().strip(),
            "fps": self.fps_var.get().strip(),
            "quality": self.quality_var.get().strip(),
            "quality_lock": "1" if self.quality_lock_var.get() else "0",
            "payload": self.payload_var.get().strip(),
            "capture_size": self.capture_size_var.get().strip(),
            "capture_mode": self._capture_mode_code(),
            "manual_bbox": ",".join(str(part) for part in self._manual_capture_bbox) if self._manual_capture_bbox else "",
        }
        save_gui_config(SENDER_CONFIG, payload)

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
    app = SenderApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
