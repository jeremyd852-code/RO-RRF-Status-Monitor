"""Independent, lightweight Tk overlay for the currently observed own pet."""

from __future__ import annotations

import math
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable


DEFAULT_SETTINGS = {
    "pet_overlay_x": None,
    "pet_overlay_y": None,
    "pet_overlay_width": 280,
    "pet_overlay_font_size": 12,
    "pet_overlay_opacity": 0.9,
    "pet_overlay_locked": False,
}
CHANGE_DELAY_MS = 350
CARD_BACKGROUND = "#FFFFFF"
HEADER_BACKGROUND = "#DCEBF5"
TEXT_COLOR = "#26384A"
STALE_COLOR = "#536171"


def _number(value: object, fallback: float, lower: float, upper: float) -> float:
    try:
        number = float(value)
        if not math.isfinite(number):
            return fallback
        return max(lower, min(upper, number))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _normalize(settings: dict, previous: dict | None = None) -> dict:
    result = dict(DEFAULT_SETTINGS if previous is None else previous)
    for key in result:
        if key in settings:
            result[key] = settings[key]
    for key in ("pet_overlay_x", "pet_overlay_y"):
        value = result[key]
        if value is not None:
            try:
                coordinate = float(value)
                result[key] = int(max(-(2**30), min(2**30, coordinate))) if math.isfinite(coordinate) else None
            except (TypeError, ValueError, OverflowError):
                result[key] = None
    result["pet_overlay_width"] = round(_number(result["pet_overlay_width"], 280, 220, 600))
    result["pet_overlay_font_size"] = round(_number(result["pet_overlay_font_size"], 12, 10, 20))
    result["pet_overlay_opacity"] = round(_number(result["pet_overlay_opacity"], 0.9, 0.4, 1.0), 3)
    result["pet_overlay_locked"] = result["pet_overlay_locked"] is True
    return result


def _work_area(master: tk.Misc, x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
    """Use the nearest Windows monitor's work area, including negative origins."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class MonitorInfo(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            user32 = ctypes.windll.user32
            user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
            user32.MonitorFromRect.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            rectangle = wintypes.RECT(x, y, x + width, y + height)
            monitor = user32.MonitorFromRect(ctypes.byref(rectangle), 2)
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                area = info.rcWork
                return area.left, area.top, area.right, area.bottom
        except (AttributeError, OSError, ValueError):
            pass
    return 0, 0, max(1, master.winfo_screenwidth()), max(1, master.winfo_screenheight())


class PetOverlay:
    """A pet-only Toplevel. Its caller owns enabled state and persistence.

    All public methods and callbacks run on the constructing Tk thread.
    update_settings accepts a full application settings dictionary, but this
    object reads and exports only its six pet_overlay_* presentation settings.
    """

    def __init__(
        self,
        master: tk.Misc,
        settings: dict,
        on_change: Callable[[dict], None],
        on_close: Callable[[], None],
    ) -> None:
        self._thread_id = threading.get_ident()
        self._master = master
        self._on_change = on_change
        self._on_close = on_close
        self._settings = _normalize(settings)
        self._change_job: str | None = None
        self._dirty = False
        self._destroyed = False
        self._visible = False
        self._drag_offset: tuple[int, int] | None = None
        self._content: tuple[str, int | None, int | None, str, bool, bool] | None = None

        self.window = tk.Toplevel(master)
        self.window.withdraw()
        self.window.title("自身寵物")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(background="#93AABD", borderwidth=1)
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.window.bind("<Destroy>", self._on_destroy, add="+")

        self._body = tk.Frame(self.window, background=CARD_BACKGROUND)
        self._body.pack(fill="both", expand=True)
        highlight = tk.Frame(self._body, background="#F7FCFF", height=1)
        highlight.pack(fill="x")
        header = tk.Frame(self._body, background=HEADER_BACKGROUND, padx=6, pady=1)
        header.pack(fill="x")
        self._name_font = tkfont.Font(root=self.window, family="Microsoft JhengHei", size=12, weight="bold")
        self._satiety_font = tkfont.Font(root=self.window, family="Microsoft JhengHei", size=12, weight="bold")
        self._intimacy_font = tkfont.Font(root=self.window, family="Microsoft JhengHei", size=12)
        self._name = tk.Label(header, anchor="w", background=HEADER_BACKGROUND, font=self._name_font, borderwidth=0, padx=0, pady=0)
        self._name.pack(side="left", fill="x", expand=True)
        self._close_button = self._button(header, "×", self._close)
        self._close_button.pack(side="right", padx=(4, 0))
        self._lock_button = self._button(header, "鎖定", self._toggle_lock)
        self._lock_button.pack(side="right")

        separator = tk.Frame(self._body, background="#B8CBD9", height=1)
        separator.pack(fill="x")
        content = tk.Frame(self._body, background=CARD_BACKGROUND, padx=7, pady=5)
        content.pack(fill="both", expand=True)
        self._satiety_row = tk.Frame(content, background=CARD_BACKGROUND, padx=4, pady=2)
        self._satiety_row.pack(fill="x")
        self._satiety_caption = tk.Label(self._satiety_row, text="飽食度", anchor="w", borderwidth=0, padx=0, pady=0)
        self._satiety_caption.pack(side="left", padx=(0, 12))
        self._satiety = tk.Label(self._satiety_row, anchor="e", borderwidth=0, padx=0, pady=0)
        self._satiety.pack(side="right", fill="x", expand=True)
        self._intimacy_row = tk.Frame(content, background=CARD_BACKGROUND, padx=4, pady=2)
        self._intimacy_row.pack(fill="x")
        self._intimacy_caption = tk.Label(self._intimacy_row, text="親密度", anchor="w", borderwidth=0, padx=0, pady=0)
        self._intimacy_caption.pack(side="left", padx=(0, 12))
        self._intimacy = tk.Label(self._intimacy_row, anchor="e", borderwidth=0, padx=0, pady=0)
        self._intimacy.pack(side="right", fill="x", expand=True)
        self._message = tk.Label(content, anchor="nw", justify="left", height=2, background=CARD_BACKGROUND, borderwidth=0, padx=4, pady=1)
        self._message.pack(fill="x")

        # Buttons keep their own actions; the rest of the card is a drag surface.
        self._drag_widgets = (self._body, highlight, header, separator, content, self._name,
                              self._satiety_row, self._satiety_caption, self._satiety,
                              self._intimacy_row, self._intimacy_caption, self._intimacy, self._message)
        for widget in self._drag_widgets:
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        self._apply_style()
        self.set_content(name="自身寵物", satiety=None, intimacy=None, message="等待寵物資料", low=False, stale=True)

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None]) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, font=("Microsoft JhengHei", 10),
            relief="flat", borderwidth=0, highlightthickness=0, padx=4, pady=0,
            foreground="#27465F", background=HEADER_BACKGROUND, activeforeground="#18364F",
            activebackground="#B8D1E5", cursor="hand2", takefocus=False,
        )

    def _assert_thread(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("PetOverlay must be operated on its Tk thread")

    def show(self) -> None:
        self._assert_thread()
        if self._destroyed or self._visible:
            return
        self._place()
        self.window.deiconify()
        self.window.attributes("-topmost", True)
        self.window.lift()
        self._visible = True

    def hide(self) -> None:
        self._assert_thread()
        if self._destroyed:
            return
        self._drag_offset = None
        self._visible = False
        self.window.withdraw()
        self._flush_change()

    def winfo_exists(self) -> bool:
        self._assert_thread()
        try:
            return not self._destroyed and bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def destroy(self) -> None:
        self._assert_thread()
        if self._destroyed:
            return
        self._cancel_change()
        self._dirty = False
        self._destroyed = True
        self._visible = False
        self._drag_offset = None
        self.window.destroy()

    def set_content(
        self, *, name: str, satiety: int | None, intimacy: int | None,
        message: str, low: bool, stale: bool,
    ) -> None:
        self._assert_thread()
        if self._destroyed:
            return
        satiety = satiety if isinstance(satiety, int) and not isinstance(satiety, bool) and 0 <= satiety <= 100 else None
        intimacy = intimacy if isinstance(intimacy, int) and not isinstance(intimacy, bool) and 0 <= intimacy <= 1000 else None
        content = (str(name or "自身寵物"), satiety, intimacy, str(message or ""), bool(low), bool(stale))
        if content == self._content:
            return
        self._content = content
        self._render_content()

    def update_settings(self, settings: dict) -> None:
        self._assert_thread()
        if self._destroyed:
            return
        normalized = _normalize(settings, self._settings)
        if normalized == self._settings:
            return
        self._settings = normalized
        if normalized["pet_overlay_locked"]:
            self._drag_offset = None
        self._apply_style()
        self._render_content()
        if self._visible:
            self._place()

    def export_settings(self) -> dict:
        self._assert_thread()
        return dict(self._settings)

    def _apply_style(self) -> None:
        size = self._settings["pet_overlay_font_size"]
        self._name_font.configure(size=size)
        self._satiety_font.configure(size=size)
        self._intimacy_font.configure(size=size)
        self._satiety.configure(font=self._satiety_font)
        self._satiety_caption.configure(font=self._satiety_font)
        self._intimacy.configure(font=self._intimacy_font)
        self._intimacy_caption.configure(font=self._intimacy_font)
        # Preserve all numeric digits at large font sizes. Extra space covers
        # the card padding, window border, and the labels' own pixel insets.
        minimum_width = min(600, max(
            220,
            self._satiety_font.measure("飽食度") + self._satiety_font.measure("100 / 100") + 36,
            self._intimacy_font.measure("親密度") + self._intimacy_font.measure("1000 / 1000") + 36,
        ))
        width_changed = self._settings["pet_overlay_width"] < minimum_width
        if width_changed:
            self._settings["pet_overlay_width"] = minimum_width
        self._message.configure(font=("Microsoft JhengHei", max(10, size - 1)), wraplength=self._settings["pet_overlay_width"] - 24)
        self._lock_button.configure(text="解鎖" if self._settings["pet_overlay_locked"] else "鎖定")
        for widget in self._drag_widgets:
            widget.configure(cursor="arrow" if self._settings["pet_overlay_locked"] else "fleur")
        try:
            self.window.attributes("-alpha", self._settings["pet_overlay_opacity"])
        except tk.TclError:
            pass
        if width_changed:
            self._schedule_change()

    def _render_content(self) -> None:
        if self._content is None:
            return
        name, satiety, intimacy, message, low, stale = self._content
        color = STALE_COLOR if stale else TEXT_COLOR
        alert_color, alert_background = color, CARD_BACKGROUND
        if low and not stale:
            critical = satiety is not None and satiety <= 10
            alert_color = "#8D2830" if critical else "#6C480C"
            alert_background = "#FCE5E6" if critical else "#FFF3D4"
        controls_width = self._lock_button.winfo_reqwidth() + self._close_button.winfo_reqwidth()
        available = max(20, self._settings["pet_overlay_width"] - controls_width - 24)
        shown_name = name
        while shown_name and self._name_font.measure(shown_name) > available:
            shown_name = shown_name[:-1]
        if shown_name != name:
            while shown_name and self._name_font.measure(shown_name + "…") > available:
                shown_name = shown_name[:-1]
            shown_name += "…"
        self._name.configure(text=shown_name, foreground=STALE_COLOR if stale else "#24445E")
        self._satiety_row.configure(background=alert_background)
        self._satiety_caption.configure(foreground=alert_color, background=alert_background)
        self._satiety.configure(text=f"{satiety} / 100" if satiety is not None else "等待更新", foreground=alert_color, background=alert_background)
        self._intimacy_caption.configure(foreground=color, background=CARD_BACKGROUND)
        self._intimacy.configure(text=f"{intimacy} / 1000" if intimacy is not None else "等待更新", foreground=color, background=CARD_BACKGROUND)
        self._message.configure(text=message, foreground=alert_color if low or stale else STALE_COLOR, background=CARD_BACKGROUND)

    def _place(self, x: int | None = None, y: int | None = None) -> None:
        self.window.update_idletasks()
        width = self._settings["pet_overlay_width"]
        height = self._body.winfo_reqheight() + 2
        x = self._settings["pet_overlay_x"] if x is None else x
        y = self._settings["pet_overlay_y"] if y is None else y
        anchor_x = self._master.winfo_rootx() if x is None else x
        anchor_y = self._master.winfo_rooty() if y is None else y
        left, top, right, bottom = _work_area(self._master, anchor_x, anchor_y, width, height)
        width, height = min(width, right - left), min(height, bottom - top)
        x = max(left, min(right - width, right - width - 24 if x is None else x))
        y = max(top, min(bottom - height, top + 64 if y is None else y))
        changed = (x, y) != (self._settings["pet_overlay_x"], self._settings["pet_overlay_y"])
        self._settings["pet_overlay_x"], self._settings["pet_overlay_y"] = x, y
        # The explicit '+' anchors to the virtual desktop origin. A bare '-'
        # would mean an offset from the screen's far edge in Tk geometry syntax.
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        if changed:
            self._schedule_change()

    def _drag_start(self, event: tk.Event) -> None:
        if not self._settings["pet_overlay_locked"]:
            self._drag_offset = event.x_root - self.window.winfo_x(), event.y_root - self.window.winfo_y()

    def _drag_move(self, event: tk.Event) -> None:
        if self._drag_offset is not None and not self._settings["pet_overlay_locked"]:
            self._place(event.x_root - self._drag_offset[0], event.y_root - self._drag_offset[1])

    def _drag_end(self, _event: tk.Event) -> None:
        self._drag_offset = None

    def _toggle_lock(self) -> None:
        self._settings["pet_overlay_locked"] = not self._settings["pet_overlay_locked"]
        self._drag_offset = None
        self._apply_style()
        self._schedule_change()

    def _schedule_change(self) -> None:
        self._dirty = True
        self._cancel_change()
        self._change_job = self._master.after(CHANGE_DELAY_MS, self._flush_change)

    def _cancel_change(self) -> None:
        if self._change_job is not None:
            try:
                self._master.after_cancel(self._change_job)
            except tk.TclError:
                pass
            self._change_job = None

    def _flush_change(self) -> None:
        self._cancel_change()
        if self._dirty and not self._destroyed:
            self._dirty = False
            self._on_change(self.export_settings())

    def _close(self) -> None:
        self.hide()
        self._on_close()

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self.window:
            self._destroyed = True
            self._drag_offset = None
            self._cancel_change()
