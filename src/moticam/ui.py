"""
moticam.ui - live imaging window for Moticam / USB microscope cameras.

    uv run moticam-ui

Layout follows the familiar live-imaging module style: image preview in the
middle, a control panel on the right with five pages (Basic, Colour, Advanced,
Capture, Measure), an album strip of captured images along the bottom and a
status bar with FPS, objective magnification, stamp and calibration.

Mouse in the image:
    wheel            zoom at the cursor          double-click   fit to window
    left drag        pan / draw with the active measuring tool
    right drag       move the scale cross        middle drag    pan
Keys: SPACE capture, F fit, 1 = 100 %, Z freeze, Esc cancel / back to live,
      Delete remove selected measurement, Left/Right previous/next album image.
"""

import csv
import datetime as dt
import glob
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
import tkinter.font as tkfont

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk

from moticam import cameras, driver

APP_TITLE = "Live Imaging - Moticam"
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".moticam", "ui_settings.json")
PRESET_DIR = os.path.join(os.path.expanduser("~"), ".moticam", "presets")
DEFAULTS_NAME = "(Factory defaults)"
# settings that describe the window/workspace rather than imaging parameters
NOT_IN_PRESET = {"folder", "panel_page", "album_visible", "geometry"}
DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Pictures", "Moticam")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")

# ---- theme ---------------------------------------------------------------------------
BG = "#1e2023"          # window / toolbars
PANEL = "#2a2d31"       # control panel
PANEL2 = "#353a40"      # inputs, buttons
HEADER = "#24272b"      # section headers
BORDER = "#43484f"
FG = "#e8e8e8"
FG_DIM = "#9aa1a9"
ACCENT = "#1f8ee6"      # blue accent
ACCENT_HI = "#4aa8f0"
CANVAS_BG = "#0f1012"
WARN = "#f0b429"

OVERLAY_COLORS = {"Green": "#3cff5a", "Red": "#ff3b30", "Yellow": "#ffe14d", "Cyan": "#2ee6ff",
                  "Magenta": "#ff4df0", "White": "#ffffff", "Black": "#000000", "Blue": "#3a7bff"}
OBJECTIVES = ["None", "4x", "10x", "20x", "40x", "60x", "100x"]
WB_PRESETS = {  # multipliers in B, G, R order (approximate correction for the light source)
    "Default": (1.0, 1.0, 1.0),
    "Halogen": (1.45, 1.0, 0.78),
    "LED 3000K": (1.35, 1.0, 0.82),
    "LED 5000K": (1.08, 1.0, 0.95),
}
BAYER_CHOICES = ["Auto", "GR/BG", "BG/GR", "GB/RG", "RG/GB"]
FILTERS = ["None", "Grayscale", "Invert", "Red", "Green", "Blue"]
CROSS_STYLES = ["Full lines", "Small cross", "Circle + cross", "Reticle"]
TOOLS = [("pan", "Pan"), ("line", "Line"), ("rect", "Rect"), ("circle", "Circle"), ("angle", "Angle")]


# =========================================================================== helpers
_font_cache = {}


def get_font(size):
    size = max(8, int(size))
    if size not in _font_cache:
        f = None
        for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        _font_cache[size] = f or ImageFont.load_default(size=size)
    return _font_cache[size]


def nice_length(x):
    if x <= 0:
        return 1
    e = 10 ** math.floor(math.log10(x))
    f = x / e
    return (1 if f < 1.5 else 2 if f < 3.5 else 5 if f < 7.5 else 10) * e


def fmt_len(um):
    if um >= 1000:
        return f"{um / 1000:g} mm"
    return f"{um:g} µm"


def read_image(path):
    data = np.fromfile(path, np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise IOError(f"Cannot read {path}")
    return img


def write_image(path, img_bgr):
    ext = os.path.splitext(path)[1].lower()
    params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext in (".jpg", ".jpeg") else []
    ok, buf = cv2.imencode(ext, img_bgr, params)
    if not ok:
        raise IOError("encoding failed")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    buf.tofile(path)


class TF:
    """Image -> target (canvas or image) coordinate transform."""

    def __init__(self, s=1.0, ox=0.0, oy=0.0):
        self.s, self.ox, self.oy = s, ox, oy

    def c(self, x, y):
        return (x - self.ox) * self.s, (y - self.oy) * self.s

    def i(self, cx, cy):
        return cx / self.s + self.ox, cy / self.s + self.oy


# =========================================================================== overlays
def measurement_text(m, um):
    k, unit = (um, "µm") if um else (1.0, "px")
    p = m["pts"]
    if m["type"] == "line":
        return f"{math.dist(p[0], p[1]) * k:.2f} {unit}"
    if m["type"] == "rect":
        w, h = abs(p[1][0] - p[0][0]) * k, abs(p[1][1] - p[0][1]) * k
        return f"{w:.1f} × {h:.1f} {unit}  A={w * h:.1f} {unit}²"
    if m["type"] == "circle":
        r = math.dist(p[0], p[1]) * k
        return f"r={r:.2f} {unit}  A={math.pi * r * r:.1f} {unit}²"
    if m["type"] == "angle" and len(p) == 3:
        a1 = math.atan2(p[0][1] - p[1][1], p[0][0] - p[1][0])
        a2 = math.atan2(p[2][1] - p[1][1], p[2][0] - p[1][0])
        d = abs(math.degrees(a1 - a2)) % 360
        return f"{min(d, 360 - d):.1f}°"
    return ""


def draw_overlays(img, w, h, tf, cfg, scale=1.0, view=None):
    """Draw crosshair, grid, scale bar, stamp and measurements onto PIL image `img`."""
    d = ImageDraw.Draw(img)
    W, H = img.size
    view = view or (0, 0, W, H)
    lw = lambda base: max(1, int(round(base * scale)))
    fs = max(12, int(13 * scale))
    font = get_font(fs)
    (ix0, iy0), (ix1, iy1) = tf.c(0, 0), tf.c(w, h)

    def text(xy, s, color, anchor="la", size=None):
        d.text(xy, s, fill=color, font=get_font(size) if size else font, anchor=anchor,
               stroke_width=max(1, fs // 7), stroke_fill="#000000")

    # grid
    if cfg["grid_on"]:
        n, col = max(2, cfg["grid_n"]), cfg["grid_color"]
        for k in range(1, n):
            x = tf.c(w * k / n, 0)[0]
            y = tf.c(0, h * k / n)[1]
            d.line([(x, iy0), (x, iy1)], fill=col, width=lw(1))
            d.line([(ix0, y), (ix1, y)], fill=col, width=lw(1))

    # scale cross
    if cfg["cross_on"]:
        cx, cy = tf.c(cfg["cross_frac"][0] * w, cfg["cross_frac"][1] * h)
        col, t = cfg["cross_color"], lw(cfg["cross_width"])
        r = cfg["cross_radius"] * tf.s
        style = cfg["cross_style"]
        if style == "Small cross":
            d.line([(cx - r, cy), (cx + r, cy)], fill=col, width=t)
            d.line([(cx, cy - r), (cx, cy + r)], fill=col, width=t)
        else:
            d.line([(ix0, cy), (ix1, cy)], fill=col, width=t)
            d.line([(cx, iy0), (cx, iy1)], fill=col, width=t)
        if style == "Circle + cross":
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=t)
        if style == "Reticle":
            for k in (1, 2, 3):
                d.ellipse([cx - k * r, cy - k * r, cx + k * r, cy + k * r], outline=col, width=t)
            tick = max(4, r * 0.12)
            for k in range(1, 13):
                for sx, sy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    px, py = cx + sx * k * r / 4, cy + sy * k * r / 4
                    big = tick * (1.8 if k % 4 == 0 else 1)
                    if sx:
                        d.line([(px, py - big), (px, py + big)], fill=col, width=max(1, t - 1))
                    else:
                        d.line([(px - big, py), (px + big, py)], fill=col, width=max(1, t - 1))

    um = cfg["um_per_px"]

    # measurements
    mcol = cfg["meas_color"]
    items = list(cfg["measurements"]) + ([cfg["pending"]] if cfg.get("pending") else [])
    for m in items:
        pts = [tf.c(*p) for p in m["pts"]]
        sel = m.get("id") is not None and m.get("id") == cfg.get("selected")
        col = "#ffffff" if sel else mcol
        t = lw(3 if sel else 2)
        if m["type"] == "line":
            d.line(pts, fill=col, width=t)
            for p in pts:
                d.line([(p[0] - 5 * scale, p[1]), (p[0] + 5 * scale, p[1])], fill=col, width=t)
                d.line([(p[0], p[1] - 5 * scale), (p[0], p[1] + 5 * scale)], fill=col, width=t)
            anchor_pt = ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2 - 8 * scale)
        elif m["type"] in ("rect", "wbrect"):
            (x0, y0), (x1, y1) = pts
            d.rectangle([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], outline=col, width=t)
            anchor_pt = (min(x0, x1), min(y0, y1) - 6 * scale)
        elif m["type"] == "circle":
            r = math.dist(pts[0], pts[1])
            d.ellipse([pts[0][0] - r, pts[0][1] - r, pts[0][0] + r, pts[0][1] + r], outline=col, width=t)
            d.line([pts[0], pts[1]], fill=col, width=max(1, t - 1))
            anchor_pt = (pts[0][0], pts[0][1] - r - 6 * scale)
        else:  # angle
            d.line(pts, fill=col, width=t)
            anchor_pt = (pts[1][0] + 10 * scale, pts[1][1] - 10 * scale)
        if cfg["meas_labels"] and m["type"] != "wbrect":
            label = measurement_text(m, um)
            if m.get("id") is not None:
                label = f"{m['id']}: {label}"
            text(anchor_pt, label, col, anchor="lb" if m["type"] != "line" else "mb")

    # scale bar (bottom-right of the visible part of the image)
    if cfg["sb_on"] and um:
        vx0, vy0 = max(view[0], ix0), max(view[1], iy0)
        vx1, vy1 = min(view[2], ix1), min(view[3], iy1)
        if vx1 - vx0 > 60 and vy1 - vy0 > 40:
            vis_um = (vx1 - vx0) / tf.s * um
            L_um = cfg["sb_len"] or nice_length(vis_um / 5)
            L = L_um / um * tf.s
            m = 18 * scale
            bh = max(4, 6 * scale)
            x1, y1 = vx1 - m, vy1 - m
            d.rectangle([x1 - L, y1 - bh, x1, y1], fill=cfg["sb_color"], outline="#000000")
            text((x1 - L / 2, y1 - bh - 4 * scale), fmt_len(L_um), cfg["sb_color"], anchor="mb")

    # magnification stamp
    if cfg["stamp"]:
        text((max(view[0], ix0) + 12 * scale, max(view[1], iy0) + 10 * scale), cfg["stamp"], "#ffffff",
             size=int(fs * 1.4))


# =========================================================================== widgets
class ScrollFrame(ttk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, bg=PANEL, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, style="Panel.TFrame")
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._update_region)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)

    def _update_region(self, _=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _bind_wheel(self, _):
        self.bind_all("<MouseWheel>", self._wheel)
        self.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))

    def _unbind_wheel(self, _):
        for s in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.unbind_all(s)

    def _wheel(self, e):
        if isinstance(e.widget, ttk.Combobox) or str(e.widget).endswith("popdown.f.l"):
            return
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def top(self):
        self.canvas.yview_moveto(0)


class Section(ttk.Frame):
    """Collapsible titled block in the control panel."""

    def __init__(self, master, title, opened=True):
        super().__init__(master, style="Panel.TFrame")
        self.pack(fill="x", pady=(0, 6))
        self._open = opened
        self.title = title
        self.header = tk.Label(self, text="", anchor="w", bg=HEADER, fg=FG, padx=8, pady=5,
                               font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.header.pack(fill="x")
        self.header.bind("<Button-1>", lambda e: self.toggle())
        self.body = ttk.Frame(self, style="Panel.TFrame", padding=(10, 6, 10, 4))
        self.body.columnconfigure(1, weight=1)
        self._update()

    def toggle(self):
        self._open = not self._open
        self._update()

    def _update(self):
        self.header.configure(text=("▾  " if self._open else "▸  ") + self.title)
        if self._open:
            self.body.pack(fill="x")
        else:
            self.body.pack_forget()


class SliderRow:
    """Label + slider + editable value in one grid row; optional log scale."""

    instances = []

    def __init__(self, parent, row, text, lo, hi, value, on_change, fmt="{:.0f}", log=False,
                 resolution=None, unit="", var=None):
        SliderRow.instances.append(self)
        self.var = var                      # tk variable this slider shows (used when loading presets)
        self.lo, self.hi, self.log, self.fmt, self.unit = lo, hi, log, fmt, unit
        self.on_change = on_change
        self.resolution = resolution
        self._suspend = False
        self.label = ttk.Label(parent, text=text, style="Panel.TLabel")
        self.label.grid(row=row, column=0, sticky="w", pady=2)
        self.pos = tk.DoubleVar()
        self.scale = ttk.Scale(parent, from_=0, to=1000, variable=self.pos, command=self._moved)
        self.scale.grid(row=row, column=1, sticky="ew", padx=6, pady=2)
        self.entry_var = tk.StringVar()
        self.entry = ttk.Entry(parent, textvariable=self.entry_var, width=8, justify="right")
        self.entry.grid(row=row, column=2, sticky="e", pady=2)
        self.entry.bind("<Return>", self._typed)
        self.entry.bind("<FocusOut>", self._typed)
        self.value = value
        self.set(value, notify=False)

    def _to_pos(self, v):
        if self.hi <= self.lo:
            return 0
        if self.log:
            lo, hi = math.log(max(self.lo, 1e-6)), math.log(max(self.hi, 1e-6))
            return (math.log(max(v, 1e-6)) - lo) / (hi - lo) * 1000
        return (v - self.lo) / (self.hi - self.lo) * 1000

    def _from_pos(self, p):
        if self.log:
            lo, hi = math.log(max(self.lo, 1e-6)), math.log(max(self.hi, 1e-6))
            v = math.exp(lo + p / 1000 * (hi - lo))
        else:
            v = self.lo + p / 1000 * (self.hi - self.lo)
        if self.resolution:
            v = round(v / self.resolution) * self.resolution
        return v

    def _moved(self, _=None):
        if self._suspend:
            return
        self.value = self._from_pos(self.pos.get())
        self.entry_var.set(self.fmt.format(self.value) + self.unit)
        self.on_change(self.value)

    def _typed(self, _=None):
        try:
            v = float(self.entry_var.get().replace(self.unit, "").strip())
        except ValueError:
            self.set(self.value, notify=False)
            return
        v = min(max(v, self.lo), self.hi)
        self.set(v, notify=True)

    def set(self, v, notify=False):
        self.value = v
        self._suspend = True
        self.pos.set(self._to_pos(v))
        self._suspend = False
        self.entry_var.set(self.fmt.format(v) + self.unit)
        if notify:
            self.on_change(v)

    def configure_range(self, lo, hi, log=None):
        self.lo, self.hi = lo, hi
        if log is not None:
            self.log = log
        self.set(min(max(self.value, lo), hi))

    def enable(self, on=True):
        st = "normal" if on else "disabled"
        self.scale.state(["!disabled"] if on else ["disabled"])
        self.entry.configure(state=st)


class ColorPick(tk.Button):
    instances = []

    def __init__(self, parent, var, on_change):
        ColorPick.instances.append(self)
        self.var, self.on_change = var, on_change
        super().__init__(parent, width=3, relief="flat", bd=0, cursor="hand2", command=self._pick,
                         highlightthickness=1, highlightbackground=BORDER)
        self.refresh()

    def _pick(self):
        c = colorchooser.askcolor(self.var.get(), title="Choose colour")[1]
        if c:
            self.var.set(c)
            self.refresh()
            self.on_change()

    def refresh(self):
        self.configure(bg=self.var.get(), activebackground=self.var.get())


# =========================================================================== application
class App:
    def __init__(self, root, dll=None):
        self.root, self.dll = root, dll
        self.settings = self._load_settings()
        try:
            self.ui_scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            self.ui_scale = 1.0
        self.worker = None
        self.src = None
        self.devices = []
        self.ui_q = queue.Queue()
        # image state
        self.mode = "live"
        self.frozen = False
        self.raw = None             # latest raw camera frame
        self.processed = None       # latest processed live frame
        self.still = None
        self.still_path = None
        self.last_fid = -1
        self.need_render = True
        self.src_size = None
        self.s, self.ox, self.oy, self.fit_mode = 1.0, 0.0, 0.0, True
        self._last_live_size = None
        self.photo = None
        self.flash_until = 0
        self.cursor = None
        # measurement state
        self.meas = {"live": []}
        self.meas_seq = 0
        self.pending = None
        self.selected = None
        self.drag = None
        # misc
        self.calib = self.settings.get("calib", {})
        self.cross_frac = tuple(self.settings.get("cross_frac", (0.5, 0.5)))
        self.wb_custom = tuple(self.settings.get("wb_custom", (1.0, 1.0, 1.0)))
        self._lut_key, self._lut_cache = None, None
        self.ae_busy = False
        self.timelapse = None
        self.recorder = None
        self.album_files = []
        self.album_items = {}
        self.thumb_q = queue.Queue()
        self.status_msg, self.status_until = "", 0
        self._t_hist = self._t_ae = self._t_status = 0

        self._init_vars()
        self._style()
        self._build()
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_devices(auto_open=True)
        self.load_album()
        self.root.after(20, self.tick)

    # ------------------------------------------------------------------ settings
    def _load_settings(self):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_settings(self):
        data = {k: v.get() for k, v in self.persist.items()}
        data.update(calib=self.calib, cross_frac=self.cross_frac, wb_custom=self.wb_custom,
                    last_device=self.v_device.get(), geometry=self.root.geometry(),
                    last_preset=self.v_preset.get())
        try:
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except Exception as e:
            print("Could not save settings:", e)

    def _init_vars(self):
        S = self.settings
        self.persist = {}

        self.defaults = {}

        def var(kind, name, default):
            v = kind(value=S.get(name, default))
            self.persist[name] = v
            self.defaults[name] = default
            return v

        B, D, I, Str = tk.BooleanVar, tk.DoubleVar, tk.IntVar, tk.StringVar
        self.v_device = Str(value="")
        self.v_res = Str(value="")
        self.v_auto_exp = var(B, "auto_exp", False)
        self.v_ae_target = var(I, "ae_target", 120)
        self.v_exposure = var(D, "exposure", 30.0)
        self.v_res_index = var(I, "res_index", 0)
        self.v_gain_index = var(I, "gain_index", 0)
        self.v_offset = var(I, "offset", 0)
        self.v_preset = Str(value=S.get("last_preset", ""))
        self.v_gamma = var(D, "gamma", 1.0)
        self.v_black = var(I, "black", 0)
        self.v_white = var(I, "white", 255)
        self.v_wb = var(Str, "wb_preset", "Default")
        self.v_mirror = var(B, "mirror", False)
        self.v_flip = var(B, "flip", False)
        self.v_r_gain = var(D, "r_gain", 1.0)
        self.v_g_gain = var(D, "g_gain", 1.0)
        self.v_b_gain = var(D, "b_gain", 1.0)
        self.v_r_bri = var(I, "r_bri", 0)
        self.v_g_bri = var(I, "g_bri", 0)
        self.v_b_bri = var(I, "b_bri", 0)
        self.v_hist = var(B, "histogram", True)
        self.v_bayer = var(Str, "bayer", "Auto")
        self.v_filter = var(Str, "filter", "None")
        self.v_sharp = var(I, "sharpness", 0)
        self.v_denoise = var(I, "denoise", 0)
        self.v_grid_on = var(B, "grid_on", False)
        self.v_grid_n = var(I, "grid_n", 4)
        self.v_grid_color = var(Str, "grid_color", "#ffe14d")
        self.v_cross_on = var(B, "cross_on", True)
        self.v_cross_style = var(Str, "cross_style", "Full lines")
        self.v_cross_color = var(Str, "cross_color", "#3cff5a")
        self.v_cross_width = var(I, "cross_width", 1)
        self.v_cross_radius = var(D, "cross_radius_pct", 6.0)
        self.v_sb_on = var(B, "scalebar_on", True)
        self.v_sb_len = var(D, "scalebar_len", 0.0)
        self.v_sb_color = var(Str, "scalebar_color", "#ffffff")
        self.v_folder = var(Str, "folder", DEFAULT_FOLDER)
        self.v_format = var(Str, "format", "png")
        self.v_prefix = var(Str, "prefix", "image")
        self.v_save_raw = var(B, "save_raw", False)
        self.v_save_overlay = var(B, "save_overlay_copy", True)
        self.v_tl_interval = var(D, "tl_interval", 10.0)
        self.v_tl_count = var(I, "tl_count", 30)
        self.v_rec_fps = var(D, "rec_fps", 10.0)
        self.v_tool = Str(value="pan")
        self.v_meas_color = var(Str, "meas_color", "#ffe14d")
        self.v_meas_labels = var(B, "meas_labels", True)
        self.v_objective = var(Str, "objective", "None")
        self.v_stamp = var(B, "stamp", False)
        self.v_album_on = var(B, "album_visible", True)
        self.v_panel = var(Str, "panel_page", "Basic")

    # ------------------------------------------------------------------ style
    def _style(self):
        r = self.root
        r.configure(bg=BG)
        family = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=family, size=9)
            except tk.TclError:
                pass
        st = ttk.Style(r)
        st.theme_use("clam")
        st.configure(".", background=PANEL, foreground=FG, fieldbackground=PANEL2, bordercolor=BORDER,
                     lightcolor=PANEL2, darkcolor=PANEL2, troughcolor="#191b1e", focuscolor=ACCENT,
                     selectbackground=ACCENT, selectforeground="#ffffff", insertcolor=FG, arrowcolor=FG,
                     font=(family, 9))
        st.map(".", foreground=[("disabled", FG_DIM)])
        st.configure("TFrame", background=PANEL)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("Bar.TFrame", background=BG)
        st.configure("Panel.TLabel", background=PANEL, foreground=FG)
        st.configure("Dim.TLabel", background=PANEL, foreground=FG_DIM)
        st.configure("Bar.TLabel", background=BG, foreground=FG)
        st.configure("BarDim.TLabel", background=BG, foreground=FG_DIM)
        st.configure("TButton", background=PANEL2, foreground=FG, padding=(8, 3), borderwidth=1, width=-4)
        st.map("TButton", background=[("pressed", ACCENT), ("active", "#41474e"), ("disabled", PANEL)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", padding=(14, 5), width=-4,
                     font=(family, 10, "bold"))
        st.map("Accent.TButton", background=[("pressed", "#176fb4"), ("active", ACCENT_HI)])
        st.configure("Bar.TButton", background=BG, padding=(8, 4), borderwidth=0, width=-3)
        st.map("Bar.TButton", background=[("pressed", ACCENT), ("active", PANEL2)])
        for name, bg in (("Tool.Toolbutton", BG), ("Tab.Toolbutton", HEADER)):
            st.configure(name, background=bg, foreground=FG, padding=(9, 5), borderwidth=0, anchor="center")
            st.map(name, background=[("selected", ACCENT), ("active", PANEL2)],
                   foreground=[("selected", "#ffffff")])
        st.configure("Bar.TCheckbutton", background=BG, foreground=FG)
        st.map("Bar.TCheckbutton", background=[("active", BG)])
        for name in ("TCheckbutton", "Bar.TCheckbutton", "TRadiobutton"):
            st.configure(name, indicatorbackground=PANEL2, indicatorforeground="#ffffff",
                         indicatormargin=(0, 0, 6, 0))
            st.map(name, indicatorbackground=[("selected", ACCENT), ("active", "#41474e")],
                   background=[("active", PANEL if not name.startswith("Bar") else BG)])
        st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=FG, arrowsize=12)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL2)], foreground=[("readonly", FG)],
               selectbackground=[("readonly", PANEL2)], selectforeground=[("readonly", FG)])
        st.configure("TMenubutton", background=PANEL2, foreground=FG, padding=(8, 3), arrowcolor=FG_DIM,
                     arrowsize=8, borderwidth=1, width=-4)
        st.map("TMenubutton", background=[("active", "#41474e"), ("pressed", ACCENT)])
        st.configure("TEntry", fieldbackground=PANEL2, foreground=FG, padding=2)
        st.configure("TSpinbox", fieldbackground=PANEL2, foreground=FG, arrowsize=10, padding=2)
        st.configure("Horizontal.TScale", background=ACCENT, troughcolor="#191b1e", sliderlength=14,
                     borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)
        st.map("Horizontal.TScale", background=[("active", ACCENT_HI), ("disabled", PANEL2)])
        for name in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
            st.configure(name, background="#4a5058", troughcolor="#1b1d20", arrowcolor=FG_DIM,
                         bordercolor="#1b1d20", lightcolor="#4a5058", darkcolor="#4a5058",
                         gripcount=0, borderwidth=0, arrowsize=int(14 * self.ui_scale))
            st.map(name, background=[("active", "#5d646d"), ("pressed", ACCENT)])
        st.configure("Treeview", background=PANEL2, fieldbackground=PANEL2, foreground=FG, rowheight=20,
                     borderwidth=0)
        st.map("Treeview", background=[("selected", ACCENT)])
        st.configure("Treeview.Heading", background=HEADER, foreground=FG_DIM, borderwidth=0)
        r.option_add("*TCombobox*Listbox.background", PANEL2)
        r.option_add("*TCombobox*Listbox.foreground", FG)
        r.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        r.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        r.option_add("*Menu.background", PANEL2)
        r.option_add("*Menu.foreground", FG)
        r.option_add("*Menu.activeBackground", ACCENT)

    # ------------------------------------------------------------------ layout
    def _build(self):
        r = self.root
        r.title(APP_TITLE)
        r.geometry(self.settings.get("geometry", "1400x900"))
        r.minsize(1000, 650)
        r.columnconfigure(0, weight=1)
        r.rowconfigure(1, weight=1)

        self._build_toolbar()

        main = ttk.Frame(r, style="Bar.TFrame")
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)
        main.rowconfigure(0, weight=0)
        main.rowconfigure(1, weight=1)
        # viewer bar (shown while looking at a saved image)
        self.nav = tk.Frame(main, bg="#173a5c", padx=8, pady=3)
        self.nav_label = tk.Label(self.nav, text="", bg="#173a5c", fg="#ffffff", font=("Segoe UI", 9, "bold"))
        self.nav_label.pack(side="left")
        for text, cmd in (("Back to live", self.back_to_live), ("Next >", lambda: self.step_image(1)),
                          ("< Previous", lambda: self.step_image(-1))):
            ttk.Button(self.nav, text=text, command=cmd).pack(side="right", padx=2)
        self.canvas = tk.Canvas(main, bg=CANVAS_BG, highlightthickness=0, bd=0, cursor="crosshair")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.img_item = self.canvas.create_image(0, 0, anchor="nw")
        self.msg_item = self.canvas.create_text(0, 0, text="", fill=FG_DIM, font=("Segoe UI", 13))
        self._bind_canvas()

        side = ttk.Frame(main, style="Panel.TFrame", width=int(375 * self.ui_scale))
        side.grid(row=0, column=1, rowspan=2, sticky="ns")
        side.grid_propagate(False)
        side.rowconfigure(2, weight=1)
        side.columnconfigure(0, weight=1)
        tabs = ttk.Frame(side, style="Panel.TFrame")
        tabs.grid(row=0, column=0, sticky="ew")
        self.pages = {}
        self.scroll = ScrollFrame(side)
        self.scroll.grid(row=2, column=0, sticky="nsew")
        self._build_preset_bar(side)
        for i, (name, glyph) in enumerate((("Basic", "☀"), ("Colour", "◑"), ("Advanced", "✚"),
                                           ("Capture", "◉"), ("Measure", "↔"))):
            b = ttk.Radiobutton(tabs, text=name, value=name, variable=self.v_panel,
                                style="Tab.Toolbutton", command=self.show_page)
            b.grid(row=0, column=i, sticky="ew")
            tabs.columnconfigure(i, weight=1)
            self.pages[name] = ttk.Frame(self.scroll.inner, style="Panel.TFrame", padding=(6, 8))
        self._page_basic(self.pages["Basic"])
        self._page_colour(self.pages["Colour"])
        self._page_advanced(self.pages["Advanced"])
        self._page_capture(self.pages["Capture"])
        self._page_measure(self.pages["Measure"])
        self.show_page()

        self._build_album()
        self._build_statusbar()
        self.update_calib_ui()

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, style="Bar.TFrame", padding=(6, 4))
        bar.grid(row=0, column=0, sticky="ew")
        self.toolbar = bar
        B = lambda text, cmd, **kw: ttk.Button(bar, text=text, command=cmd, style="Bar.TButton", **kw)
        self.b_live = B("Live", self.back_to_live)
        self.b_live.pack(side="left")
        self.b_freeze = B("Freeze", self.toggle_freeze)
        self.b_freeze.pack(side="left")
        ttk.Button(bar, text="Capture", style="Accent.TButton", command=self.capture).pack(side="left", padx=6)
        self._sep(bar)
        B("Fit", self.fit).pack(side="left")
        B("1:1", lambda: self.zoom_to(1.0)).pack(side="left")
        B("-", lambda: self.zoom_step(1 / 1.25)).pack(side="left")
        B("+", lambda: self.zoom_step(1.25)).pack(side="left")
        self._sep(bar)
        for key, label in TOOLS:
            ttk.Radiobutton(bar, text=label, value=key, variable=self.v_tool, style="Tool.Toolbutton",
                            command=self.tool_changed).pack(side="left")
        self._sep(bar)
        for text, var in (("Cross", self.v_cross_on), ("Grid", self.v_grid_on), ("Scale bar", self.v_sb_on)):
            ttk.Checkbutton(bar, text=text, variable=var, style="Bar.TCheckbutton",
                            command=self.redraw).pack(side="left", padx=4)

    @staticmethod
    def _sep(bar):
        tk.Frame(bar, width=1, bg=BORDER).pack(side="left", fill="y", padx=8, pady=2)

    def _build_preset_bar(self, side):
        bar = tk.Frame(side, bg=HEADER, padx=8, pady=6)
        bar.grid(row=1, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        tk.Label(bar, text="Preset", bg=HEADER, fg=FG, font=("Segoe UI", 9, "bold")).grid(row=0, column=0, padx=(0, 6))
        self.cb_preset = ttk.Combobox(bar, textvariable=self.v_preset, state="readonly")
        self.cb_preset.grid(row=0, column=1, sticky="ew")
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self.load_preset())
        btns = tk.Frame(bar, bg=HEADER)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        ttk.Button(btns, text="Load", command=self.load_preset).pack(side="left")
        ttk.Button(btns, text="Save\u2026", command=self.save_preset).pack(side="left", padx=3)
        ttk.Button(btns, text="Delete", command=self.delete_preset).pack(side="left")
        more = ttk.Menubutton(btns, text="More")
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="Import preset file\u2026", command=self.import_preset)
        menu.add_command(label="Export selected preset\u2026", command=self.export_preset)
        menu.add_separator()
        menu.add_command(label="Open presets folder", command=self.open_preset_folder)
        more["menu"] = menu
        more.pack(side="right")
        self.refresh_presets()

    def show_page(self):
        for p in self.pages.values():
            p.pack_forget()
        self.pages[self.v_panel.get()].pack(fill="both", expand=True)
        self.scroll.top()

    # ---- Basic page
    def _page_basic(self, page):
        s = Section(page, "Video device")
        b = s.body
        self.cb_device = ttk.Combobox(b, textvariable=self.v_device, state="readonly", width=30)
        self.cb_device.grid(row=0, column=0, columnspan=3, sticky="ew")
        row = ttk.Frame(b, style="Panel.TFrame")
        row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.b_open = ttk.Button(row, text="Open", command=self.toggle_open)
        self.b_open.pack(side="left")
        self.b_refresh = ttk.Button(row, text="Refresh", command=lambda: self.refresh_devices(False))
        self.b_refresh.pack(side="left", padx=4)
        self.b_props = ttk.Button(row, text="Properties…", command=self.open_props)
        self.b_props.pack(side="left")
        ttk.Button(row, text="Driver…", command=self.check_driver).pack(side="left", padx=4)
        self.l_devinfo = ttk.Label(b, text="", style="Dim.TLabel", wraplength=300)
        self.l_devinfo.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        s = Section(page, "Resolution")
        self.cb_res = ttk.Combobox(s.body, textvariable=self.v_res, state="readonly")
        self.cb_res.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.cb_res.bind("<<ComboboxSelected>>", self.res_selected)
        ttk.Label(s.body, text="Higher resolution gives a lower frame rate.", style="Dim.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))

        s = Section(page, "Exposure")
        b = s.body
        ttk.Checkbutton(b, text="Auto exposure", variable=self.v_auto_exp).grid(row=0, column=0, columnspan=2, sticky="w")
        self.sl_target = SliderRow(b, 1, "Target", 20, 235, self.v_ae_target.get(),
                                   lambda v: self.v_ae_target.set(int(v)), resolution=1, var=self.v_ae_target)
        self.sl_exp = SliderRow(b, 2, "Time", 0.1, 2000, self.v_exposure.get(), self.exposure_changed,
                                fmt="{:.2f}", log=True, unit="")
        self.l_exp_unit = ttk.Label(b, text="ms", style="Dim.TLabel")
        self.l_exp_unit.grid(row=2, column=3, sticky="w", padx=(2, 0))
        ttk.Label(b, text="Gain", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        self.cb_gain = ttk.Combobox(b, state="readonly", width=10)
        self.cb_gain.grid(row=3, column=1, sticky="w", padx=6, pady=2)
        self.cb_gain.bind("<<ComboboxSelected>>", self.gain_selected)
        self.sl_offset = SliderRow(b, 4, "Offset", 0, 100, self.v_offset.get(), self.offset_changed, resolution=1)

        s = Section(page, "Image adjustments")
        b = s.body
        self.sl_black = SliderRow(b, 0, "Enhance min", 0, 254, self.v_black.get(),
                                  lambda v: self._setv(self.v_black, int(v)), resolution=1, var=self.v_black)
        self.sl_white = SliderRow(b, 1, "Enhance max", 1, 255, self.v_white.get(),
                                  lambda v: self._setv(self.v_white, int(v)), resolution=1, var=self.v_white)
        self.sl_gamma = SliderRow(b, 2, "Gamma", 0.3, 3.0, self.v_gamma.get(),
                                  lambda v: self._setv(self.v_gamma, v), fmt="{:.2f}", resolution=0.01,
                                  var=self.v_gamma)
        ttk.Button(b, text="Reset", command=self.reset_adjust).grid(row=3, column=2, sticky="e", pady=(4, 0))

        s = Section(page, "White balance")
        b = s.body
        ttk.Button(b, text="Auto (whole image)", command=self.auto_wb).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(b, text="Pick white area", command=lambda: self.set_tool("wbrect")).grid(
            row=0, column=1, columnspan=2, sticky="e")
        ttk.Label(b, text="Light source", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        cb = ttk.Combobox(b, textvariable=self.v_wb, state="readonly", values=list(WB_PRESETS) + ["Custom"], width=12)
        cb.grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self.redraw(True))
        ttk.Label(b, text="Pick an empty, bright area of the slide for the most accurate result.",
                  style="Dim.TLabel", wraplength=300).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        s = Section(page, "View")
        b = s.body
        ttk.Checkbutton(b, text="Mirror", variable=self.v_mirror, command=lambda: self.redraw(True)).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(b, text="Flip", variable=self.v_flip, command=lambda: self.redraw(True)).grid(row=0, column=1, sticky="w")
        row = ttk.Frame(b, style="Panel.TFrame")
        row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(row, text="Fit to window", command=self.fit).pack(side="left")
        ttk.Button(row, text="Full (1:1)", command=lambda: self.zoom_to(1.0)).pack(side="left", padx=4)
        self._set_camera_controls(False)

    # ---- Colour page
    def _page_colour(self, page):
        s = Section(page, "Histogram")
        b = s.body
        ttk.Checkbutton(b, text="Histogram enabled", variable=self.v_hist).grid(row=0, column=0, sticky="w")
        self.hist_canvas = tk.Canvas(b, width=int(300 * self.ui_scale), height=int(110 * self.ui_scale),
                                     bg="#15171a", highlightthickness=0)
        self.hist_canvas.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.hist_item = self.hist_canvas.create_image(0, 0, anchor="nw")
        self.l_clip = ttk.Label(b, text="", style="Dim.TLabel")
        self.l_clip.grid(row=2, column=0, columnspan=3, sticky="w")

        s = Section(page, "Colour adjustments")
        b = s.body
        self.colour_sliders = []
        for i, (lab, var, lo, hi, fmt, res) in enumerate((
                ("Red gain", self.v_r_gain, 0.25, 4.0, "{:.2f}", 0.01),
                ("Red brightness", self.v_r_bri, -100, 100, "{:.0f}", 1),
                ("Green gain", self.v_g_gain, 0.25, 4.0, "{:.2f}", 0.01),
                ("Green brightness", self.v_g_bri, -100, 100, "{:.0f}", 1),
                ("Blue gain", self.v_b_gain, 0.25, 4.0, "{:.2f}", 0.01),
                ("Blue brightness", self.v_b_bri, -100, 100, "{:.0f}", 1))):
            sl = SliderRow(b, i, lab, lo, hi, var.get(), lambda v, var=var: self._setv(var, v), fmt=fmt,
                           resolution=res, log=(res == 0.01), var=var)
            self.colour_sliders.append((sl, var))
        ttk.Button(b, text="Reset", command=self.reset_colour).grid(row=6, column=2, sticky="e", pady=(4, 0))

        s = Section(page, "Sensor colour pattern")
        b = s.body
        ttk.Label(b, text="Bayer pattern", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.cb_bayer = ttk.Combobox(b, textvariable=self.v_bayer, values=BAYER_CHOICES, state="readonly", width=10)
        self.cb_bayer.grid(row=0, column=1, sticky="w", padx=6)
        self.cb_bayer.bind("<<ComboboxSelected>>", self.bayer_selected)
        ttk.Label(b, text="Only for raw-colour cameras. Change it if red and blue look swapped or the "
                          "image looks green.", style="Dim.TLabel", wraplength=300).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

    # ---- Advanced page
    def _page_advanced(self, page):
        s = Section(page, "Filters")
        b = s.body
        ttk.Label(b, text="Filter", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(b, textvariable=self.v_filter, values=FILTERS, state="readonly", width=12)
        cb.grid(row=0, column=1, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.redraw(True))
        SliderRow(b, 1, "Sharpness", 0, 100, self.v_sharp.get(), lambda v: self._setv(self.v_sharp, int(v)), resolution=1, var=self.v_sharp)
        SliderRow(b, 2, "Remove noise", 0, 4, self.v_denoise.get(), lambda v: self._setv(self.v_denoise, int(v)), resolution=1, var=self.v_denoise)

        s = Section(page, "Scale cross")
        b = s.body
        ttk.Checkbutton(b, text="Show scale cross", variable=self.v_cross_on, command=self.redraw).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(b, text="Style", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        cb = ttk.Combobox(b, textvariable=self.v_cross_style, values=CROSS_STYLES, state="readonly", width=14)
        cb.grid(row=1, column=1, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.redraw())
        ttk.Label(b, text="Colour", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        ColorPick(b, self.v_cross_color, self.redraw).grid(row=2, column=1, sticky="w", padx=6)
        SliderRow(b, 3, "Line width", 1, 8, self.v_cross_width.get(), lambda v: self._setv(self.v_cross_width, int(v), False), resolution=1, var=self.v_cross_width)
        SliderRow(b, 4, "Radius (%)", 1, 50, self.v_cross_radius.get(), lambda v: self._setv(self.v_cross_radius, v, False), fmt="{:.1f}", resolution=0.5, var=self.v_cross_radius)
        ttk.Button(b, text="Centre", command=self.center_cross).grid(row=5, column=2, sticky="e", pady=(4, 0))
        ttk.Label(b, text="Right-drag in the image to move the cross.", style="Dim.TLabel").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        s = Section(page, "Grid")
        b = s.body
        ttk.Checkbutton(b, text="Show grid", variable=self.v_grid_on, command=self.redraw).grid(row=0, column=0, columnspan=2, sticky="w")
        SliderRow(b, 1, "Cells", 2, 20, self.v_grid_n.get(), lambda v: self._setv(self.v_grid_n, int(v), False), resolution=1, var=self.v_grid_n)
        ttk.Label(b, text="Colour", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        ColorPick(b, self.v_grid_color, self.redraw).grid(row=2, column=1, sticky="w", padx=6)

        s = Section(page, "Scale bar")
        b = s.body
        ttk.Checkbutton(b, text="Show scale bar (needs calibration)", variable=self.v_sb_on,
                        command=self.redraw).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(b, text="Length (µm, 0 = auto)", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        e = ttk.Entry(b, textvariable=self.v_sb_len, width=8)
        e.grid(row=1, column=1, sticky="w", padx=6)
        e.bind("<Return>", lambda ev: self.redraw())
        ttk.Label(b, text="Colour", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        ColorPick(b, self.v_sb_color, self.redraw).grid(row=2, column=1, sticky="w", padx=6)

        s = Section(page, "Calibration")
        b = s.body
        self.l_calib = ttk.Label(b, text="", style="Panel.TLabel", wraplength=300)
        self.l_calib.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(b, text="µm / pixel", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 2))
        self.v_um_entry = tk.StringVar()
        e = ttk.Entry(b, textvariable=self.v_um_entry, width=10)
        e.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 2))
        ttk.Button(b, text="Set", command=self.set_calib_manual).grid(row=1, column=2, sticky="e", pady=(6, 2))
        row = ttk.Frame(b, style="Panel.TFrame")
        row.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(row, text="From measured line…", command=self.calibrate_from_line).pack(side="left")
        ttk.Button(row, text="Clear", command=self.clear_calib).pack(side="left", padx=4)
        ttk.Label(b, text="Each objective (status bar) keeps its own calibration. Draw a line over a "
                          "known distance on a calibration slide, then use “From measured line”.",
                  style="Dim.TLabel", wraplength=300).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.calib_tree = ttk.Treeview(b, columns=("obj", "val"), show="headings", height=4)
        self.calib_tree.heading("obj", text="Objective")
        self.calib_tree.heading("val", text="µm/px (at width)")
        self.calib_tree.column("obj", width=80)
        self.calib_tree.column("val", width=180)
        self.calib_tree.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))

    # ---- Capture page
    def _page_capture(self, page):
        s = Section(page, "Capture")
        b = s.body
        ttk.Button(b, text="Capture image", style="Accent.TButton", command=self.capture).grid(
            row=0, column=0, columnspan=3, sticky="ew")
        ttk.Label(b, text="Folder", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 2))
        ttk.Entry(b, textvariable=self.v_folder).grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 2))
        ttk.Button(b, text="…", width=3, command=self.choose_folder).grid(row=1, column=2, sticky="e", pady=(8, 2))
        ttk.Label(b, text="Format", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Combobox(b, textvariable=self.v_format, values=["png", "jpg", "tif", "bmp"], state="readonly",
                     width=6).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(b, text="File name", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Entry(b, textvariable=self.v_prefix, width=16).grid(row=3, column=1, sticky="w", padx=6)
        ttk.Checkbutton(b, text="Also save a copy with overlays", variable=self.v_save_overlay).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(b, text="Save raw sensor image (no adjustments)", variable=self.v_save_raw).grid(
            row=5, column=0, columnspan=3, sticky="w")
        ttk.Button(b, text="Open folder", command=self.open_folder).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

        s = Section(page, "Auto capture (time-lapse)")
        b = s.body
        ttk.Label(b, text="Every (s)", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Spinbox(b, textvariable=self.v_tl_interval, from_=0.5, to=86400, increment=1, width=8).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(b, text="Images", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Spinbox(b, textvariable=self.v_tl_count, from_=1, to=100000, increment=1, width=8).grid(row=1, column=1, sticky="w", padx=6)
        self.b_tl = ttk.Button(b, text="Start", command=self.toggle_timelapse)
        self.b_tl.grid(row=0, column=2, rowspan=2, sticky="e")
        self.l_tl = ttk.Label(b, text="", style="Dim.TLabel")
        self.l_tl.grid(row=2, column=0, columnspan=3, sticky="w")

        s = Section(page, "Record video")
        b = s.body
        ttk.Label(b, text="Frame rate", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Spinbox(b, textvariable=self.v_rec_fps, from_=1, to=60, increment=1, width=6).grid(row=0, column=1, sticky="w", padx=6)
        self.b_rec = ttk.Button(b, text="Record", command=self.toggle_record)
        self.b_rec.grid(row=0, column=2, sticky="e")
        self.l_rec = ttk.Label(b, text="Saves an .mp4 in the capture folder.", style="Dim.TLabel")
        self.l_rec.grid(row=1, column=0, columnspan=3, sticky="w")

    # ---- Measure page
    def _page_measure(self, page):
        s = Section(page, "Tools")
        b = s.body
        grid = ttk.Frame(b, style="Panel.TFrame")
        grid.grid(row=0, column=0, columnspan=3, sticky="ew")
        for i, (key, label) in enumerate(TOOLS):
            ttk.Radiobutton(grid, text=label, value=key, variable=self.v_tool, style="Tab.Toolbutton",
                            command=self.tool_changed).grid(row=0, column=i, sticky="ew", padx=1)
            grid.columnconfigure(i, weight=1)
        ttk.Label(b, text="Colour", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 2))
        ColorPick(b, self.v_meas_color, self.redraw).grid(row=1, column=1, sticky="w", padx=6, pady=(8, 2))
        ttk.Checkbutton(b, text="Show values on image", variable=self.v_meas_labels, command=self.redraw).grid(
            row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(b, text="Line/Rect/Circle: drag.  Angle: click 3 points (arm, vertex, arm).",
                  style="Dim.TLabel", wraplength=300).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        s = Section(page, "Results")
        b = s.body
        self.meas_tree = ttk.Treeview(b, columns=("n", "type", "value"), show="headings", height=10,
                                      selectmode="browse")
        for c, t, w in (("n", "#", 30), ("type", "Type", 60), ("value", "Value", 200)):
            self.meas_tree.heading(c, text=t)
            self.meas_tree.column(c, width=w, anchor="w")
        self.meas_tree.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.meas_tree.bind("<<TreeviewSelect>>", self.meas_selected)
        row = ttk.Frame(b, style="Panel.TFrame")
        row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(row, text="Delete", command=self.delete_measurement).pack(side="left")
        ttk.Button(row, text="Clear all", command=self.clear_measurements).pack(side="left", padx=4)
        ttk.Button(row, text="Export CSV…", command=self.export_csv).pack(side="left")

    # ---- album
    def _build_album(self):
        wrap = ttk.Frame(self.root, style="Bar.TFrame")
        wrap.grid(row=2, column=0, sticky="ew")
        head = ttk.Frame(wrap, style="Bar.TFrame", padding=(6, 2))
        head.pack(fill="x")
        self.b_album = ttk.Button(head, text="", style="Bar.TButton", command=self.toggle_album)
        self.b_album.pack(side="left")
        self.l_album = ttk.Label(head, text="", style="BarDim.TLabel")
        self.l_album.pack(side="left", padx=6)
        ttk.Button(head, text="Open image…", style="Bar.TButton", command=self.open_file).pack(side="right")
        ttk.Button(head, text="Open folder", style="Bar.TButton", command=self.open_folder).pack(side="right")
        ttk.Button(head, text="Refresh", style="Bar.TButton", command=self.load_album).pack(side="right")
        self.album_body = ttk.Frame(wrap, style="Bar.TFrame")
        self.album_canvas = tk.Canvas(self.album_body, height=132, bg=BG, highlightthickness=0)
        self.album_inner = tk.Frame(self.album_canvas, bg=BG)
        self.album_canvas.create_window(0, 0, window=self.album_inner, anchor="nw")
        hsb = ttk.Scrollbar(self.album_body, orient="horizontal", command=self.album_canvas.xview)
        self.album_canvas.configure(xscrollcommand=hsb.set)
        self.album_canvas.pack(fill="x")
        hsb.pack(fill="x")
        self.album_inner.bind("<Configure>", lambda e: self.album_canvas.configure(scrollregion=self.album_canvas.bbox("all")))
        self.album_canvas.bind("<MouseWheel>", lambda e: self.album_canvas.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        self._album_visible()

    def _album_visible(self):
        if self.v_album_on.get():
            self.album_body.pack(fill="x")
            self.b_album.configure(text="▾ Album")
        else:
            self.album_body.pack_forget()
            self.b_album.configure(text="▸ Album")

    def toggle_album(self):
        self.v_album_on.set(not self.v_album_on.get())
        self._album_visible()

    # ---- status bar
    def _build_statusbar(self):
        bar = ttk.Frame(self.root, style="Bar.TFrame", padding=(8, 3))
        bar.grid(row=3, column=0, sticky="ew")
        self.l_fps = ttk.Label(bar, text="FPS --", style="Bar.TLabel", width=9)
        self.l_fps.pack(side="left")
        self._sep(bar)
        self.l_res = ttk.Label(bar, text="", style="Bar.TLabel")
        self.l_res.pack(side="left")
        self._sep(bar)
        ttk.Label(bar, text="Magnification", style="BarDim.TLabel").pack(side="left")
        cb = ttk.Combobox(bar, textvariable=self.v_objective, values=OBJECTIVES, state="readonly", width=6)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: (self.update_calib_ui(), self.redraw()))
        ttk.Checkbutton(bar, text="Stamp", variable=self.v_stamp, style="Bar.TCheckbutton",
                        command=self.redraw).pack(side="left", padx=4)
        self.l_cal = ttk.Label(bar, text="", style="BarDim.TLabel")
        self.l_cal.pack(side="left", padx=4)
        self._sep(bar)
        self.l_msg = ttk.Label(bar, text="", style="Bar.TLabel")
        self.l_msg.pack(side="left")
        self.l_zoom = ttk.Label(bar, text="", style="BarDim.TLabel", width=8, anchor="e")
        self.l_zoom.pack(side="right")
        self.l_cursor = ttk.Label(bar, text="", style="BarDim.TLabel")
        self.l_cursor.pack(side="right", padx=8)

    # ------------------------------------------------------------------ presets
    @staticmethod
    def _preset_file(name):
        safe = "".join(c if c.isalnum() or c in " -_.()" else "_" for c in name).strip() or "preset"
        return os.path.join(PRESET_DIR, safe + ".json")

    def preset_names(self):
        names = []
        for p in sorted(glob.glob(os.path.join(PRESET_DIR, "*.json")), key=lambda x: x.lower()):
            try:
                with open(p, encoding="utf-8") as f:
                    names.append(json.load(f).get("name") or os.path.splitext(os.path.basename(p))[0])
            except Exception:
                continue
        return names

    def refresh_presets(self):
        self.cb_preset.configure(values=[DEFAULTS_NAME] + self.preset_names())

    def snapshot(self):
        """All imaging parameters from the control panel as a plain dict."""
        vals = {k: v.get() for k, v in self.persist.items() if k not in NOT_IN_PRESET}
        vals.update(wb_custom=list(self.wb_custom), cross_frac=list(self.cross_frac))
        return vals

    def save_preset(self):
        current = self.v_preset.get()
        name = simpledialog.askstring("Save preset", "Preset name:", parent=self.root,
                                      initialvalue="" if current in ("", DEFAULTS_NAME) else current)
        if not name or not name.strip():
            return
        name = name.strip()
        if name == DEFAULTS_NAME:
            messagebox.showinfo("Save preset", "That name is reserved. Choose another one.")
            return
        path = self._preset_file(name)
        if os.path.exists(path) and not messagebox.askyesno("Save preset", f"Replace the preset \u201c{name}\u201d?"):
            return
        data = {"name": name, "app": "moticam-ui", "version": 1,
                "saved": dt.datetime.now().isoformat(timespec="seconds"),
                "camera": self.src.name if self.src else None, "values": self.snapshot()}
        try:
            os.makedirs(PRESET_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except OSError as e:
            messagebox.showerror("Save preset", f"Could not save the preset:\n{e}")
            return
        self.refresh_presets()
        self.v_preset.set(name)
        self.message(f"Preset \u201c{name}\u201d saved.")

    def load_preset(self, name=None):
        name = name or self.v_preset.get()
        if not name:
            self.message("Choose a preset first.")
            return
        if name == DEFAULTS_NAME:
            values = dict(self.defaults)
            values.update(wb_custom=[1.0, 1.0, 1.0], cross_frac=[0.5, 0.5])
        else:
            try:
                with open(self._preset_file(name), encoding="utf-8") as f:
                    values = json.load(f)["values"]
            except Exception as e:
                messagebox.showerror("Load preset", f"Could not read the preset \u201c{name}\u201d:\n{e}")
                self.refresh_presets()
                return
        self.apply_values(values)
        self.v_preset.set(name)
        self.message(f"Preset \u201c{name}\u201d loaded.")

    def apply_values(self, values):
        for k, v in values.items():
            var = self.persist.get(k)
            if var is None or k in NOT_IN_PRESET:
                continue
            try:
                var.set(v)
            except tk.TclError:
                pass
        if "wb_custom" in values:
            self.wb_custom = tuple(values["wb_custom"])
        if "cross_frac" in values:
            self.cross_frac = tuple(values["cross_frac"])
        for sl in SliderRow.instances:            # move sliders to the new values
            if sl.var is not None:
                try:
                    sl.set(sl.var.get())
                except tk.TclError:
                    pass
        self.sl_exp.set(self.v_exposure.get())
        self.sl_offset.set(self.v_offset.get())
        for cp in ColorPick.instances:
            cp.refresh()
        self._lut_key = None
        self.apply_camera_params(exposure=not self.v_auto_exp.get())
        self.update_calib_ui()
        self.redraw(True)

    def delete_preset(self):
        name = self.v_preset.get()
        if not name or name == DEFAULTS_NAME:
            self.message("Choose one of your presets to delete.")
            return
        if not messagebox.askyesno("Delete preset", f"Delete the preset \u201c{name}\u201d?\nThis cannot be undone."):
            return
        try:
            os.remove(self._preset_file(name))
        except OSError as e:
            messagebox.showerror("Delete preset", str(e))
        self.v_preset.set("")
        self.refresh_presets()
        self.message(f"Preset \u201c{name}\u201d deleted.")

    def export_preset(self):
        name = self.v_preset.get()
        if not name or name == DEFAULTS_NAME:
            self.message("Choose one of your presets to export.")
            return
        dst = filedialog.asksaveasfilename(defaultextension=".json", initialfile=os.path.basename(self._preset_file(name)),
                                           filetypes=[("Preset", "*.json")])
        if dst:
            import shutil
            shutil.copyfile(self._preset_file(name), dst)
            self.message(f"Exported to {dst}")

    def import_preset(self):
        src = filedialog.askopenfilename(filetypes=[("Preset", "*.json"), ("All", "*.*")])
        if not src:
            return
        try:
            with open(src, encoding="utf-8") as f:
                data = json.load(f)
            assert isinstance(data.get("values"), dict)
        except Exception:
            messagebox.showerror("Import preset", "That file is not a preset saved by this program.")
            return
        name = data.get("name") or os.path.splitext(os.path.basename(src))[0]
        if os.path.exists(self._preset_file(name)) and \
                not messagebox.askyesno("Import preset", f"Replace your preset \u201c{name}\u201d?"):
            return
        os.makedirs(PRESET_DIR, exist_ok=True)
        with open(self._preset_file(name), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        self.refresh_presets()
        self.v_preset.set(name)
        self.message(f"Imported preset \u201c{name}\u201d. Press Load to use it.")

    def open_preset_folder(self):
        os.makedirs(PRESET_DIR, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(PRESET_DIR)
        else:
            self.message(PRESET_DIR)

    # ------------------------------------------------------------------ small utils
    def _setv(self, var, value, reprocess=True):
        var.set(value)
        self.redraw(reprocess)

    def message(self, text, seconds=4, warn=False):
        self.status_msg, self.status_until = text, time.time() + seconds
        self.l_msg.configure(text=text, foreground=WARN if warn else FG)

    def redraw(self, reprocess=False):
        if reprocess and self.raw is not None and self.mode == "live":
            self.processed = self.process(self.raw)
        self.need_render = True

    def um_per_px(self, width):
        c = self.calib.get(self.v_objective.get())
        if not c or not width:
            return None
        return c[0] * c[1] / width

    # ------------------------------------------------------------------ devices
    def refresh_devices(self, auto_open=False):
        if self.worker:
            self.message("Close the camera before searching for devices.")
            return
        self.v_device.set("Searching for cameras…")
        self.cb_device.configure(values=[])
        self.b_open.state(["disabled"])

        def work():
            devs, errs = cameras.discover(dll=self.dll)
            self.ui_q.put(("devices", (devs, errs, auto_open)))

        threading.Thread(target=work, daemon=True).start()

    def _devices_found(self, devs, errs, auto_open):
        self.devices = devs
        labels = [d.label for d in devs]
        self.cb_device.configure(values=labels)
        self.b_open.state(["!disabled"])
        if not any(d.kind == "motic" for d in devs) and sys.platform == "win32":
            threading.Thread(target=lambda: self.ui_q.put(("driver", driver.driver_missing())),
                             daemon=True).start()
        if not devs:
            self.v_device.set("No camera found")
            self.l_devinfo.configure(text="\n".join(errs) or "Plug in the camera and press Refresh.")
            self.show_canvas_message("No camera found.\nPlug in the Moticam, close other camera programs, "
                                     "then press Refresh.")
            return
        last = self.settings.get("last_device")
        pick = next((d for d in devs if d.label == last), None) or \
            next((d for d in devs if d.kind == "motic"), None) or devs[0]
        self.v_device.set(pick.label)
        self.l_devinfo.configure(text="\n".join(errs))
        if auto_open and (pick.kind == "motic" or last == pick.label):
            self.open_device()
        else:
            self.show_canvas_message("Choose a camera and press Open.")

    def offer_driver_install(self):
        if driver.bundled_driver_dir() and driver.installer_script():
            if messagebox.askyesno("Camera driver", "A Motic camera is plugged in, but its driver is not "
                                   "installed.\n\nInstall the driver now? Windows will ask for administrator "
                                   "permission."):
                self._run_driver_installer()
        else:
            messagebox.showwarning("Camera driver", "A Motic camera is plugged in, but its driver is not "
                                   "installed.\n\nInstall Motic Images Plus 3.0 (it includes the driver), "
                                   "then unplug and reconnect the camera.")

    def _run_driver_installer(self, force=False):
        try:
            driver.run_installer(force)
            self.message("Driver installer started. When it finishes, reconnect the camera and press Refresh.")
        except Exception as e:
            messagebox.showerror("Camera driver", str(e))

    def check_driver(self):
        if sys.platform != "win32":
            self.message("Driver check is only available on Windows.")
            return
        devs = driver.motic_devices()
        bundled = driver.bundled_driver_dir() and driver.installer_script()
        if not devs:
            text = "No Motic camera is plugged in (USB ID 232F)."
        else:
            text = "\n".join(f"{name or 'Unknown device'}: {status}" for status, name, _ in devs)
        if devs and all(s.upper() != "OK" for s, _, _ in devs):
            self.offer_driver_install()
            return
        if bundled and messagebox.askyesno("Camera driver", text + "\n\nReinstall the bundled driver anyway?"):
            self._run_driver_installer(force=True)
        elif not bundled:
            messagebox.showinfo("Camera driver", text)

    def toggle_open(self):
        if self.worker:
            self.close_device()
        else:
            self.open_device()

    def open_device(self):
        dev = next((d for d in self.devices if d.label == self.v_device.get()), None)
        if not dev:
            return
        self.show_canvas_message(f"Opening {dev.label}…")
        self.worker = cameras.CameraWorker(dev, self.dll)
        self.worker.start()
        self.b_open.configure(text="Close")
        self.b_refresh.state(["disabled"])

    def close_device(self):
        if self.recorder:
            self.toggle_record()
        if self.timelapse:
            self.toggle_timelapse()
        w, self.worker, self.src = self.worker, None, None
        if w:
            w.stop(wait=True)
        self.raw = self.processed = None
        self.b_open.configure(text="Open")
        self.b_refresh.state(["!disabled"])
        self._set_camera_controls(False)
        self.l_devinfo.configure(text="")
        if self.mode == "live":
            self.show_canvas_message("Camera closed.")

    def _camera_opened(self, src):
        self.src = src
        self.l_devinfo.configure(text=src.info())
        self._set_camera_controls(True)
        self.cb_res.configure(values=[f"{w} × {h}" for w, h in src.resolutions])
        if src.resolutions:
            self.cb_res.current(src.res_index)
        want_res = self.v_res_index.get()
        lo, hi = src.exposure_range()
        self.sl_exp.configure_range(lo, hi, log=src.exposure_log)
        self.l_exp_unit.configure(text=src.exposure_unit)
        self.cb_gain.configure(values=src.gain_labels())
        if not src.gain_labels():
            self.cb_gain.set("n/a")
            self.cb_gain.state(["disabled"])
        if src.offset_rng:
            self.sl_offset.configure_range(*src.offset_rng)
        else:
            self.sl_offset.enable(False)
        if src.kind == "motic":
            self.apply_camera_params()        # restore last resolution / exposure / gain / offset
        else:
            self.sl_exp.set(src.exposure)
            if src.resolutions and 0 < want_res < len(src.resolutions):
                self.apply_camera_params(exposure=False)
        if not src.has_settings_dialog:
            self.b_props.state(["disabled"])
        self.cb_bayer.state(["!disabled"] if src.is_bayer else ["disabled"])
        if src.is_bayer and self.v_bayer.get() != "Auto":
            self.bayer_selected()
        self.hide_canvas_message()
        self.message(f"Opened {src.name}")
        self.src_size = None

    def apply_camera_params(self, exposure=True):
        """Send resolution, exposure, gain, offset and Bayer pattern from the panel to the camera."""
        if not (self.worker and self.src):
            return
        src = self.src
        if src.resolutions:
            ri = min(max(self.v_res_index.get(), 0), len(src.resolutions) - 1)
            self.cb_res.current(ri)
            if ri != src.res_index:
                self.worker.call(lambda s: s.set_resolution_index(ri), self._resolution_applied)
        if exposure:
            want = self.v_exposure.get()
            self.worker.call(lambda s: s.set_exposure(want), self._exposure_applied)
        labels = src.gain_labels()
        if labels:
            gi = min(max(self.v_gain_index.get(), 0), len(labels) - 1)
            self.cb_gain.current(gi)
            self.worker.call(lambda s: s.set_gain_index(gi))
        if src.offset_rng:
            off = int(min(max(self.v_offset.get(), src.offset_rng[0]), src.offset_rng[1]))
            self.sl_offset.set(off)
            self.worker.call(lambda s: s.set_offset(off))
        if src.is_bayer:
            self.bayer_selected()

    def _resolution_applied(self, res):
        lo, hi = self.src.exposure_range()
        self.sl_exp.configure_range(lo, hi)
        self.message(f"Resolution {res[0]} × {res[1]}")

    def _set_camera_controls(self, on):
        for sl in (getattr(self, "sl_exp", None), getattr(self, "sl_offset", None)):
            if sl:
                sl.enable(on)
        for w in ("cb_res", "cb_gain", "b_props"):
            if hasattr(self, w):
                getattr(self, w).state(["!disabled" if on else "disabled"])

    def res_selected(self, _=None):
        if not self.worker:
            return
        idx = self.cb_res.current()
        self.v_res_index.set(idx)
        if self.recorder:
            self.toggle_record()
        self.worker.call(lambda s: s.set_resolution_index(idx), self._resolution_applied)

    def exposure_changed(self, v):
        self.v_exposure.set(v)
        if self.worker and self.src:
            self.worker.call(lambda s: s.set_exposure(v))

    def _exposure_applied(self, v):
        self.v_exposure.set(v)
        self.sl_exp.set(v)

    def gain_selected(self, _=None):
        i = self.cb_gain.current()
        if i >= 0:
            self.v_gain_index.set(i)
        if self.worker and i >= 0:
            self.worker.call(lambda s: s.set_gain_index(i))

    def offset_changed(self, v):
        self.v_offset.set(int(v))
        if self.worker and self.src and self.src.offset_rng:
            self.worker.call(lambda s: s.set_offset(int(v)))

    def bayer_selected(self, _=None):
        idx = BAYER_CHOICES.index(self.v_bayer.get()) - 1
        if self.worker:
            self.worker.call(lambda s: s.set_bayer(None if idx < 0 else idx))

    def open_props(self):
        if self.worker and self.src and self.src.has_settings_dialog:
            self.worker.call(lambda s: s.open_settings_dialog())

    # ------------------------------------------------------------------ processing
    def _lut(self):
        preset = WB_PRESETS.get(self.v_wb.get(), self.wb_custom if self.v_wb.get() == "Custom" else (1, 1, 1))
        gains = (self.v_b_gain.get() * preset[0], self.v_g_gain.get() * preset[1], self.v_r_gain.get() * preset[2])
        bri = (self.v_b_bri.get(), self.v_g_bri.get(), self.v_r_bri.get())
        black, white, gamma = self.v_black.get(), max(self.v_white.get(), self.v_black.get() + 1), self.v_gamma.get()
        key = (gains, bri, black, white, round(gamma, 3))
        if key == self._lut_key:
            return self._lut_cache
        self._lut_key = key
        if all(abs(g - 1) < 1e-6 for g in gains) and bri == (0, 0, 0) and black == 0 and white == 255 \
                and abs(gamma - 1) < 1e-3:
            self._lut_cache = None
            return None
        x = np.arange(256, dtype=np.float32)
        x = np.clip((x - black) / (white - black), 0, 1) ** (1.0 / gamma) * 255
        lut = np.zeros((1, 256, 3), np.uint8)
        for c in range(3):
            lut[0, :, c] = np.clip(x * gains[c] + bri[c], 0, 255).astype(np.uint8)
        self._lut_cache = lut
        return lut

    def process(self, frame):
        f = frame
        m, fl = self.v_mirror.get(), self.v_flip.get()
        if m or fl:
            f = cv2.flip(f, -1 if (m and fl) else (1 if m else 0))
        lut = self._lut()
        if lut is not None:
            f = cv2.LUT(f, lut)
        dn = self.v_denoise.get()
        if dn:
            f = cv2.medianBlur(f, 3 if dn < 3 else 5)
            if dn >= 2:
                f = cv2.GaussianBlur(f, (0, 0), 0.6 * (dn - 1))
        sh = self.v_sharp.get()
        if sh:
            a = sh / 40.0
            blur = cv2.GaussianBlur(f, (0, 0), 2.0)
            f = cv2.addWeighted(f, 1 + a, blur, -a, 0)
        flt = self.v_filter.get()
        if flt == "Grayscale":
            f = cv2.cvtColor(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        elif flt == "Invert":
            f = cv2.bitwise_not(f)
        elif flt in ("Red", "Green", "Blue"):
            ch = {"Blue": 0, "Green": 1, "Red": 2}[flt]
            out = np.zeros_like(f)
            out[:, :, ch] = f[:, :, ch]
            f = out
        return f

    def reset_adjust(self):
        for sl, var, v in ((self.sl_black, self.v_black, 0), (self.sl_white, self.v_white, 255),
                           (self.sl_gamma, self.v_gamma, 1.0)):
            var.set(v)
            sl.set(v)
        self.redraw(True)

    def reset_colour(self):
        for sl, var in self.colour_sliders:
            v = 1.0 if isinstance(var, tk.DoubleVar) else 0
            var.set(v)
            sl.set(v)
        self.v_wb.set("Default")
        self.redraw(True)

    def auto_wb(self, rect=None):
        if self.raw is None:
            self.message("No live image for white balance.", warn=True)
            return
        f = self.raw
        if rect:
            (x0, y0), (x1, y1) = rect
            # rect is in displayed (flipped) coordinates; map back if flipped
            h, w = f.shape[:2]
            if self.v_mirror.get():
                x0, x1 = w - x1, w - x0
            if self.v_flip.get():
                y0, y1 = h - y1, h - y0
            x0, x1 = sorted((int(max(0, x0)), int(min(w, x1))))
            y0, y1 = sorted((int(max(0, y0)), int(min(h, y1))))
            if x1 - x0 < 4 or y1 - y0 < 4:
                return
            f = f[y0:y1, x0:x1]
        mean = f.reshape(-1, 3).mean(axis=0) + 1e-3
        g = mean[1]
        self.wb_custom = (float(g / mean[0]), 1.0, float(g / mean[2]))
        self.v_wb.set("Custom")
        for (sl, var), v in zip(self.colour_sliders[::2], (1.0, 1.0, 1.0)):
            var.set(v)
            sl.set(v)
        self.redraw(True)
        self.message("White balance set (Custom).")

    # ------------------------------------------------------------------ view
    def current_image(self):
        if self.mode == "image":
            return self.still
        return self.processed

    def fit(self):
        self.fit_mode = True
        self._apply_fit()
        self.need_render = True

    def _apply_fit(self):
        img = self.current_image()
        if img is None:
            return
        h, w = img.shape[:2]
        cw, ch = max(2, self.canvas.winfo_width()), max(2, self.canvas.winfo_height())
        self.s = min(cw / w, ch / h)
        self.ox = -(cw / self.s - w) / 2
        self.oy = -(ch / self.s - h) / 2

    def zoom_to(self, s, at=None):
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        cx, cy = at or (cw / 2, ch / 2)
        ix, iy = cx / self.s + self.ox, cy / self.s + self.oy
        self.s = min(max(s, 0.02), 40)
        self.ox, self.oy = ix - cx / self.s, iy - cy / self.s
        self.fit_mode = False
        self.need_render = True

    def zoom_step(self, k, at=None):
        self.zoom_to(self.s * k, at)

    def show_canvas_message(self, text):
        self.canvas.itemconfigure(self.msg_item, text=text, state="normal")
        self.canvas.coords(self.msg_item, self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2)
        self.canvas.itemconfigure(self.img_item, state="hidden")

    def hide_canvas_message(self):
        self.canvas.itemconfigure(self.msg_item, state="hidden")
        self.canvas.itemconfigure(self.img_item, state="normal")

    def overlay_cfg(self, width, live_overlays=True):
        try:
            sb_len = float(self.v_sb_len.get() or 0)
        except (tk.TclError, ValueError):
            sb_len = 0
        obj = self.v_objective.get()
        if live_overlays and self.mode == "image" and self.still_path and \
                "_overlay" in os.path.basename(self.still_path):
            live_overlays = False      # that file already has its overlays burned in
        on = lambda v: bool(v.get()) and live_overlays
        return dict(
            grid_on=on(self.v_grid_on), grid_n=self.v_grid_n.get(), grid_color=self.v_grid_color.get(),
            cross_on=on(self.v_cross_on), cross_style=self.v_cross_style.get(),
            cross_color=self.v_cross_color.get(), cross_width=self.v_cross_width.get(),
            cross_radius=self.v_cross_radius.get() / 100.0 * width, cross_frac=self.cross_frac,
            sb_on=on(self.v_sb_on), sb_len=sb_len, sb_color=self.v_sb_color.get(),
            um_per_px=self.um_per_px(width),
            stamp=obj if (on(self.v_stamp) and obj != "None") else "",
            measurements=list(self.cur_meas()), pending=self.pending, selected=self.selected,
            meas_color=self.v_meas_color.get(), meas_labels=self.v_meas_labels.get())

    def render(self):
        self.need_render = False
        img = self.current_image()
        if img is None:
            return
        self.hide_canvas_message()
        h, w = img.shape[:2]
        cw, ch = max(2, self.canvas.winfo_width()), max(2, self.canvas.winfo_height())
        if self.src_size != (w, h):
            old = self.src_size
            self.src_size = (w, h)
            self.fit_mode = True
            if old and self.mode == "live" and self._last_live_size == old:
                kx, ky = w / old[0], h / old[1]
                for m in self.meas.get("live", []):
                    m["pts"] = [(x * kx, y * ky) for x, y in m["pts"]]
            if self.mode == "live":
                self._last_live_size = (w, h)
            self.update_calib_ui()
        if self.fit_mode:
            self._apply_fit()
        s, ox, oy = self.s, self.ox, self.oy
        out = np.empty((ch, cw, 3), np.uint8)
        out[:] = (18, 16, 15)
        x0, y0 = max(0, int(math.floor(ox))), max(0, int(math.floor(oy)))
        x1, y1 = min(w, int(math.ceil(ox + cw / s))), min(h, int(math.ceil(oy + ch / s)))
        if x1 > x0 and y1 > y0:
            dx0, dy0 = int(round((x0 - ox) * s)), int(round((y0 - oy) * s))
            dw, dh = max(1, int(round((x1 - x0) * s))), max(1, int(round((y1 - y0) * s)))
            interp = cv2.INTER_NEAREST if s >= 2 else (cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
            part = cv2.resize(img[y0:y1, x0:x1], (dw, dh), interpolation=interp)
            sx0, sy0 = max(0, -dx0), max(0, -dy0)
            tx0, ty0 = max(0, dx0), max(0, dy0)
            tw, th = min(dw - sx0, cw - tx0), min(dh - sy0, ch - ty0)
            if tw > 0 and th > 0:
                out[ty0:ty0 + th, tx0:tx0 + tw] = part[sy0:sy0 + th, sx0:sx0 + tw]
        pil = Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
        draw_overlays(pil, w, h, TF(s, ox, oy), self.overlay_cfg(w))
        if time.time() < self.flash_until:
            ImageDraw.Draw(pil).rectangle([2, 2, cw - 3, ch - 3], outline="#ffffff", width=6)
        if self.photo is None or (self.photo.width(), self.photo.height()) != (cw, ch):
            self.photo = ImageTk.PhotoImage(pil)
            self.canvas.itemconfigure(self.img_item, image=self.photo)
        else:
            self.photo.paste(pil)
        self.l_zoom.configure(text=f"{s * 100:.0f} %")

    # ------------------------------------------------------------------ main loop
    def tick(self):
        try:
            self._tick()
        except Exception as e:  # keep the UI alive whatever happens
            import traceback
            traceback.print_exc()
            self.message(f"Error: {e}", warn=True)
        self.root.after(15, self.tick)

    def _tick(self):
        now = time.time()
        while True:
            try:
                kind, payload = self.ui_q.get_nowait()
            except queue.Empty:
                break
            if kind == "devices":
                self._devices_found(*payload)
            elif kind == "saved":
                self._saved(*payload)
            elif kind == "error":
                self.message(payload, warn=True)
            elif kind == "driver" and payload:
                self.offer_driver_install()
        while True:
            try:
                path, thumb = self.thumb_q.get_nowait()
            except queue.Empty:
                break
            self._add_thumb_image(path, thumb)
        if self.worker:
            while True:
                try:
                    kind, payload, cb = self.worker.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "opened":
                    self._camera_opened(payload)
                elif kind == "result" and cb:
                    cb(payload)
                elif kind == "error":
                    self.message(payload, warn=True)
                    self.show_canvas_message(payload)
                    self.worker = None
                    self.b_open.configure(text="Open")
                    self.b_refresh.state(["!disabled"])
                    break
                elif kind == "warning":
                    self.message(payload, warn=True)
        if self.worker:
            fid, frame = self.worker.latest()
            if frame is not None and fid != self.last_fid:
                self.last_fid = fid
                if not self.frozen:
                    self.raw = frame
                    self.processed = self.process(frame)
                    if self.mode == "live":
                        self.need_render = True
                    if self.recorder:
                        self._record_frame(self.processed)
        if now - self._t_ae > 0.4:
            self._t_ae = now
            self._auto_exposure()
        if now - self._t_hist > 0.25:
            self._t_hist = now
            self._histogram()
        if now - self._t_status > 0.5:
            self._t_status = now
            self._status()
        if self.need_render:
            self.render()

    def _auto_exposure(self):
        if not (self.v_auto_exp.get() and self.worker and self.src and self.raw is not None) or self.ae_busy:
            return
        small = self.raw[::8, ::8]
        mean = float(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).mean()) + 0.5
        target = self.v_ae_target.get()
        if abs(mean - target) < 6:
            return
        k = min(max(target / mean, 0.5), 2.0) ** 0.8
        self.ae_busy = True

        def done(v):
            self.ae_busy = False
            self._exposure_applied(v)

        self.worker.call(lambda s: s.scale_exposure(k), done)

    def _histogram(self):
        img = self.current_image()
        if not self.v_hist.get() or img is None or self.v_panel.get() != "Colour":
            return
        W, H = max(50, self.hist_canvas.winfo_width()), max(50, self.hist_canvas.winfo_height())
        small = img[::4, ::4]
        canvas = Image.new("RGB", (W, H), "#15171a")
        d = ImageDraw.Draw(canvas)
        for c, col in ((2, (255, 80, 80)), (1, (80, 230, 80)), (0, (90, 140, 255))):
            hist = cv2.calcHist([small], [c], None, [256], [0, 256]).ravel()
            hist = hist / (hist.max() + 1e-6)
            pts = [(i * (W - 1) / 255, H - 1 - v * (H - 6)) for i, v in enumerate(hist)]
            d.line(pts, fill=col, width=1)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        clip_hi = (gray >= 250).mean() * 100
        clip_lo = (gray <= 5).mean() * 100
        self.hist_photo = ImageTk.PhotoImage(canvas)
        self.hist_canvas.itemconfigure(self.hist_item, image=self.hist_photo)
        self.l_clip.configure(text=f"Mean {gray.mean():.0f}   Over-exposed {clip_hi:.1f} %   Under {clip_lo:.1f} %",
                              foreground=WARN if clip_hi > 1 else FG_DIM)

    def _status(self):
        if self.worker and self.src:
            self.l_fps.configure(text=f"FPS {self.worker.fps:4.1f}")
        else:
            self.l_fps.configure(text="FPS --")
        img = self.current_image()
        if img is not None:
            h, w = img.shape[:2]
            extra = ""
            if self.mode == "live" and self.src:
                extra = f"   Exposure {self.src.exposure:.2f} {self.src.exposure_unit}"
            self.l_res.configure(text=f"{w} × {h}{extra}")
        if self.status_msg and time.time() > self.status_until:
            self.status_msg = ""
            self.l_msg.configure(text="")
        self.b_freeze.configure(text="Resume" if self.frozen else "Freeze")

    # ------------------------------------------------------------------ canvas input
    def _bind_canvas(self):
        c = self.canvas
        c.bind("<Configure>", self._canvas_resized)
        c.bind("<ButtonPress-1>", self.on_down)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_up)
        c.bind("<Double-Button-1>", lambda e: self.fit() if self.v_tool.get() == "pan" else None)
        c.bind("<ButtonPress-2>", self.on_pan_start)
        c.bind("<B2-Motion>", self.on_pan_move)
        c.bind("<ButtonPress-3>", self.on_cross)
        c.bind("<B3-Motion>", self.on_cross)
        c.bind("<Motion>", self.on_motion)
        c.bind("<Leave>", lambda e: self.l_cursor.configure(text=""))
        c.bind("<MouseWheel>", lambda e: self.zoom_step(1.2 if e.delta > 0 else 1 / 1.2, (e.x, e.y)))
        c.bind("<Button-4>", lambda e: self.zoom_step(1.2, (e.x, e.y)))
        c.bind("<Button-5>", lambda e: self.zoom_step(1 / 1.2, (e.x, e.y)))

    def _canvas_resized(self, e):
        self.canvas.coords(self.msg_item, e.width / 2, e.height / 2)
        self.redraw()

    def to_img(self, x, y):
        return x / self.s + self.ox, y / self.s + self.oy

    def on_pan_start(self, e):
        self.drag = ("pan", e.x, e.y, self.ox, self.oy)

    def on_pan_move(self, e):
        if self.drag and self.drag[0] == "pan":
            _, x, y, ox, oy = self.drag
            self.ox, self.oy = ox - (e.x - x) / self.s, oy - (e.y - y) / self.s
            self.fit_mode = False
            self.need_render = True

    def on_down(self, e):
        tool = self.v_tool.get()
        p = self.to_img(e.x, e.y)
        if tool == "pan":
            self.on_pan_start(e)
        elif tool == "angle":
            if not self.pending:
                self.pending = {"type": "angle", "pts": [p, p]}
            elif len(self.pending["pts"]) == 2:
                self.pending["pts"][1] = p
                self.pending["pts"].append(p)
            else:
                self.pending["pts"][2] = p
                self._finish_measurement()
        else:
            self.pending = {"type": tool, "pts": [p, p]}
        self.need_render = True

    def on_drag(self, e):
        if self.v_tool.get() == "pan":
            self.on_pan_move(e)
        elif self.pending and self.pending["type"] != "angle":
            self.pending["pts"][-1] = self.to_img(e.x, e.y)
            self.need_render = True
        self.on_motion(e)

    def on_up(self, e):
        self.drag = None
        if self.pending and self.pending["type"] != "angle":
            p0, p1 = self.pending["pts"]
            if math.dist(p0, p1) * self.s < 3:
                self.pending = None
            elif self.pending["type"] == "wbrect":
                rect = (p0, p1)
                self.pending = None
                self.set_tool("pan")
                self.auto_wb(rect)
            else:
                self._finish_measurement()
            self.need_render = True

    def on_motion(self, e):
        p = self.to_img(e.x, e.y)
        if self.pending and self.pending["type"] == "angle":
            self.pending["pts"][-1] = p
            self.need_render = True
        img = self.current_image()
        if img is None:
            return
        h, w = img.shape[:2]
        x, y = int(p[0]), int(p[1])
        if 0 <= x < w and 0 <= y < h:
            b, g, r = img[y, x]
            um = self.um_per_px(w)
            pos = f"x {x}  y {y}" + (f"  ({x * um:.1f}, {y * um:.1f} µm)" if um else "")
            self.l_cursor.configure(text=f"{pos}   R {r}  G {g}  B {b}")
        else:
            self.l_cursor.configure(text="")

    def on_cross(self, e):
        img = self.current_image()
        if img is None or not self.v_cross_on.get():
            return
        h, w = img.shape[:2]
        x, y = self.to_img(e.x, e.y)
        self.cross_frac = (min(max(x / w, 0), 1), min(max(y / h, 0), 1))
        self.need_render = True

    def center_cross(self):
        self.cross_frac = (0.5, 0.5)
        self.redraw()

    def set_tool(self, tool):
        self.v_tool.set(tool)
        self.tool_changed()

    def tool_changed(self):
        self.pending = None
        tool = self.v_tool.get()
        self.canvas.configure(cursor="fleur" if tool == "pan" else "crosshair")
        if tool == "wbrect":
            self.message("Drag a rectangle over an empty white/grey area of the slide.")
        self.need_render = True

    # ------------------------------------------------------------------ measurements
    def meas_key(self):
        return self.still_path if self.mode == "image" else "live"

    def cur_meas(self):
        return self.meas.setdefault(self.meas_key(), [])

    def _finish_measurement(self):
        m = self.pending
        self.pending = None
        self.meas_seq += 1
        m["id"] = self.meas_seq
        self.cur_meas().append(m)
        self.refresh_meas_tree()
        self.need_render = True

    def refresh_meas_tree(self):
        img = self.current_image()
        um = self.um_per_px(img.shape[1]) if img is not None else None
        self.meas_tree.delete(*self.meas_tree.get_children())
        for m in self.cur_meas():
            self.meas_tree.insert("", "end", iid=str(m["id"]),
                                  values=(m["id"], m["type"].capitalize(), measurement_text(m, um)))

    def meas_selected(self, _=None):
        sel = self.meas_tree.selection()
        self.selected = int(sel[0]) if sel else None
        self.need_render = True

    def delete_measurement(self):
        if self.selected is None:
            return
        lst = self.cur_meas()
        lst[:] = [m for m in lst if m["id"] != self.selected]
        self.selected = None
        self.refresh_meas_tree()
        self.need_render = True

    def clear_measurements(self):
        self.cur_meas().clear()
        self.selected = None
        self.refresh_meas_tree()
        self.need_render = True

    def export_csv(self):
        lst = self.cur_meas()
        if not lst:
            self.message("No measurements to export.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialdir=self.v_folder.get(), initialfile="measurements.csv")
        if not path:
            return
        img = self.current_image()
        um = self.um_per_px(img.shape[1]) if img is not None else None
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.writer(f)
            wr.writerow(["id", "type", "value", "points (px)", "um_per_px", "objective", "image"])
            for m in lst:
                pts = " ".join(f"({x:.1f},{y:.1f})" for x, y in m["pts"])
                wr.writerow([m["id"], m["type"], measurement_text(m, um), pts, um or "",
                             self.v_objective.get(), self.still_path or "live"])
        self.message(f"Exported {len(lst)} measurements.")

    # ------------------------------------------------------------------ calibration
    def update_calib_ui(self):
        obj = self.v_objective.get()
        img = self.current_image()
        w = img.shape[1] if img is not None else None
        um = self.um_per_px(w) if w else None
        c = self.calib.get(obj)
        if c:
            self.l_calib.configure(text=f"Objective {obj}: {c[0]:.4f} µm/px at width {c[1]} px")
            self.l_cal.configure(text=f"{um:.4f} µm/px" if um else f"{c[0]:.4f} µm/px")
        else:
            self.l_calib.configure(text=f"Objective {obj}: not calibrated")
            self.l_cal.configure(text="not calibrated")
        if um:
            self.v_um_entry.set(f"{um:.5f}")
        self.calib_tree.delete(*self.calib_tree.get_children())
        for k in OBJECTIVES:
            if k in self.calib:
                v, rw = self.calib[k]
                self.calib_tree.insert("", "end", values=(k, f"{v:.5f}  (@{rw} px)"))
        if hasattr(self, "meas_tree"):
            self.refresh_meas_tree()

    def set_calib_manual(self):
        img = self.current_image()
        if img is None:
            self.message("Need an image to calibrate against.", warn=True)
            return
        try:
            v = float(self.v_um_entry.get())
        except ValueError:
            self.message("Enter a number of µm per pixel.", warn=True)
            return
        self.calib[self.v_objective.get()] = [v, img.shape[1]]
        self.update_calib_ui()
        self.redraw()

    def calibrate_from_line(self):
        lines = [m for m in self.cur_meas() if m["type"] == "line"]
        sel = [m for m in lines if m["id"] == self.selected]
        m = (sel or lines[-1:] or [None])[0]
        img = self.current_image()
        if not m or img is None:
            messagebox.showinfo("Calibration", "First draw a line (Line tool) across a known distance, "
                                               "e.g. on a calibration slide.")
            return
        px = math.dist(*m["pts"])
        known = simpledialog.askfloat("Calibration", f"The line is {px:.1f} pixels long.\n"
                                                     f"How long is it in micrometres (µm)?",
                                      parent=self.root, minvalue=1e-6)
        if not known:
            return
        self.calib[self.v_objective.get()] = [known / px, img.shape[1]]
        self.update_calib_ui()
        self.redraw()
        self.message(f"Calibrated {self.v_objective.get()}: {known / px:.4f} µm/px")

    def clear_calib(self):
        self.calib.pop(self.v_objective.get(), None)
        self.update_calib_ui()
        self.redraw()

    # ------------------------------------------------------------------ capture / save
    def _next_path(self, suffix=""):
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        name = f"{self.v_prefix.get() or 'image'}_{stamp}{suffix}.{self.v_format.get()}"
        return os.path.join(self.v_folder.get(), name)

    def capture(self):
        if self.mode == "image":
            if self.still is None:
                return
            base = os.path.splitext(self.still_path)[0]
            img, path = self.still, None
            overlay_path = f"{base}_overlay.{self.v_format.get()}"
        else:
            if self.processed is None:
                self.message("No live image to capture.", warn=True)
                return
            img = self.raw if self.v_save_raw.get() else self.processed
            path = self._next_path()
            overlay_path = os.path.splitext(path)[0] + "_overlay." + self.v_format.get() \
                if self.v_save_overlay.get() else None
        cfg = self.overlay_cfg(img.shape[1]) if overlay_path else None
        if cfg:
            cfg["pending"] = None
            cfg["selected"] = None
        img = img.copy()
        self.flash_until = time.time() + 0.15
        self.need_render = True

        def work():
            try:
                saved = []
                if path:
                    write_image(path, img)
                    saved.append(path)
                if overlay_path:
                    h, w = img.shape[:2]
                    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                    draw_overlays(pil, w, h, TF(1, 0, 0), cfg, scale=max(1.0, w / 1100))
                    write_image(overlay_path, cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR))
                    saved.append(overlay_path)
                self.ui_q.put(("saved", (saved, img)))
            except Exception as e:
                self.ui_q.put(("error", f"Save failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _saved(self, paths, img):
        if not paths:
            return
        self.message("Saved " + ", ".join(os.path.basename(p) for p in paths))
        for p in paths:
            if os.path.dirname(os.path.abspath(p)) == os.path.abspath(self.v_folder.get()):
                self.album_files.insert(0, p)
                self._add_thumb_widget(p, at_front=True)
                th = Image.open(p) if p.endswith(("_overlay.png", "_overlay.jpg")) else \
                    Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                th.thumbnail((150, 100))
                self._add_thumb_image(p, th)
        self._album_count()

    def choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.v_folder.get())
        if d:
            self.v_folder.set(d)
            self.load_album()

    def open_folder(self):
        d = self.v_folder.get()
        os.makedirs(d, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(d)
        else:
            self.message(d)

    # time-lapse
    def toggle_timelapse(self):
        if self.timelapse:
            self.root.after_cancel(self.timelapse["after"])
            self.timelapse = None
            self.b_tl.configure(text="Start")
            self.l_tl.configure(text="Stopped.")
            return
        if not self.worker:
            self.message("Open the camera first.", warn=True)
            return
        try:
            interval, count = float(self.v_tl_interval.get()), int(self.v_tl_count.get())
        except (tk.TclError, ValueError):
            self.message("Check the time-lapse interval and count.", warn=True)
            return
        self.timelapse = {"n": 0, "count": count, "interval": interval, "after": None}
        self.b_tl.configure(text="Stop")
        self._timelapse_step()

    def _timelapse_step(self):
        tl = self.timelapse
        if not tl:
            return
        if self.mode == "live":
            self.capture()
        tl["n"] += 1
        self.l_tl.configure(text=f"Captured {tl['n']} of {tl['count']}")
        if tl["n"] >= tl["count"]:
            self.timelapse = None
            self.b_tl.configure(text="Start")
            self.l_tl.configure(text=f"Done: {tl['n']} images.")
            return
        tl["after"] = self.root.after(int(tl["interval"] * 1000), self._timelapse_step)

    # video
    def toggle_record(self):
        if self.recorder:
            self.recorder["writer"].release()
            n, path = self.recorder["n"], self.recorder["path"]
            self.recorder = None
            self.b_rec.configure(text="Record")
            self.l_rec.configure(text=f"Saved {os.path.basename(path)} ({n} frames)")
            return
        if self.processed is None:
            self.message("Open the camera first.", warn=True)
            return
        h, w = self.processed.shape[:2]
        path = os.path.splitext(self._next_path())[0] + ".mp4"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fps = float(self.v_rec_fps.get() or 10)
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        if not writer.isOpened():
            self.message("Could not start video recording.", warn=True)
            return
        self.recorder = {"writer": writer, "path": path, "size": (w, h), "n": 0,
                         "next": time.time(), "dt": 1.0 / fps}
        self.b_rec.configure(text="Stop")
        self.l_rec.configure(text="Recording…")

    def _record_frame(self, frame):
        r = self.recorder
        h, w = frame.shape[:2]
        now = time.time()
        if (w, h) != r["size"] or now < r["next"]:
            return
        r["next"] += r["dt"]
        if r["next"] < now:
            r["next"] = now + r["dt"]
        r["writer"].write(frame)
        r["n"] += 1
        self.l_rec.configure(text=f"Recording… {r['n']} frames")

    # ------------------------------------------------------------------ album / viewer
    def load_album(self):
        folder = self.v_folder.get()
        files = [p for p in glob.glob(os.path.join(folder, "*")) if p.lower().endswith(IMAGE_EXTS)]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        self.album_files = files[:300]
        for w in self.album_inner.winfo_children():
            w.destroy()
        self.album_items = {}
        for p in self.album_files:
            self._add_thumb_widget(p)
        self._album_count()

        paths = list(self.album_files)

        def work():
            for p in paths:
                try:
                    im = Image.open(p)
                    im.draft("RGB", (300, 200))
                    im = im.convert("RGB")
                    im.thumbnail((150, 100))
                    self.thumb_q.put((p, im))
                except Exception:
                    continue

        threading.Thread(target=work, daemon=True).start()

    def _album_count(self):
        self.l_album.configure(text=f"{self.v_folder.get()}   ({len(self.album_files)} images)")

    def _add_thumb_widget(self, path, at_front=False):
        f = tk.Frame(self.album_inner, bg=BG, padx=3, pady=3, highlightthickness=2, highlightbackground=BG)
        lab = tk.Label(f, bg="#15171a", width=20, height=6, text="…", fg=FG_DIM)
        lab.pack()
        name = os.path.basename(path)
        tk.Label(f, text=name if len(name) < 26 else name[:11] + "…" + name[-12:], bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 7)).pack()
        for w in (f, lab):
            w.bind("<Button-1>", lambda e, p=path: self.open_image(p))
            w.bind("<MouseWheel>", lambda e: self.album_canvas.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        kids = self.album_inner.winfo_children()
        if at_front and len(kids) > 1:
            f.pack(side="left", before=kids[0])
        else:
            f.pack(side="left")
        self.album_items[path] = (f, lab)

    def _add_thumb_image(self, path, pil):
        item = self.album_items.get(path)
        if not item:
            return
        ph = ImageTk.PhotoImage(pil)
        item[1].configure(image=ph, width=150, height=100, text="")
        item[1].image = ph

    def _highlight_album(self):
        for p, (f, _) in self.album_items.items():
            f.configure(highlightbackground=ACCENT if p == self.still_path and self.mode == "image" else BG)

    def open_file(self):
        p = filedialog.askopenfilename(initialdir=self.v_folder.get(),
                                       filetypes=[("Images", " ".join("*" + e for e in IMAGE_EXTS)), ("All", "*.*")])
        if p:
            self.open_image(p)

    def open_image(self, path):
        try:
            self.still = read_image(path)
        except Exception as e:
            self.message(str(e), warn=True)
            return
        self.still_path = path
        self.mode = "image"
        self.pending = None
        self.selected = None
        self.src_size = None
        self.nav_label.configure(text=f"Viewing  {os.path.basename(path)}")
        self.nav.grid(row=0, column=0, sticky="ew")
        self.refresh_meas_tree()
        self.update_calib_ui()
        self._highlight_album()
        self.need_render = True

    def step_image(self, k):
        if self.mode != "image" or not self.album_files:
            return
        try:
            i = self.album_files.index(self.still_path)
        except ValueError:
            i = -k
        i = (i + k) % len(self.album_files)
        self.open_image(self.album_files[i])

    def back_to_live(self):
        self.nav.grid_remove()
        self.mode = "live"
        self.frozen = False
        self.pending = None
        self.selected = None
        self.src_size = None
        self.refresh_meas_tree()
        self.update_calib_ui()
        self._highlight_album()
        if self.processed is None:
            self.show_canvas_message("No live image." if self.worker else "Camera closed. Choose a camera and press Open.")
        self.need_render = True

    def toggle_freeze(self):
        if self.mode == "image":
            self.back_to_live()
            return
        self.frozen = not self.frozen
        self.message("Frozen - press Resume or Z for live" if self.frozen else "Live")

    # ------------------------------------------------------------------ keys / close
    def _bind_keys(self):
        r = self.root

        def guard(fn):
            def h(e):
                if isinstance(e.widget, (tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox)):
                    return
                fn(e)
            return h

        r.bind("<space>", guard(lambda e: self.capture()))
        r.bind("<KeyPress-f>", guard(lambda e: self.fit()))
        r.bind("<KeyPress-1>", guard(lambda e: self.zoom_to(1.0)))
        r.bind("<KeyPress-z>", guard(lambda e: self.toggle_freeze()))
        r.bind("<Escape>", guard(self._escape))
        r.bind("<Delete>", guard(lambda e: self.delete_measurement()))
        r.bind("<Left>", guard(lambda e: self.step_image(-1)))
        r.bind("<Right>", guard(lambda e: self.step_image(1)))

    def _escape(self, _):
        if self.pending:
            self.pending = None
            self.need_render = True
        elif self.mode == "image" or self.frozen:
            self.back_to_live()

    def on_close(self):
        self._save_settings()
        if self.recorder:
            self.toggle_record()
        if self.worker:
            self.worker.stop(wait=True)
        self.root.destroy()


def main():
    import argparse
    p = argparse.ArgumentParser(description="Live imaging window for Moticam cameras")
    p.add_argument("--motic-dll", help="path to MUCam32.dll if not in the default Motic folder")
    args = p.parse_args()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root, dll=args.motic_dll)
    root.mainloop()


if __name__ == "__main__":
    main()
