"""
Camera sources with one common interface, plus a background capture thread.

Sources:
  MoticSource  - Motic USB cameras through Motic's MUCam SDK
  CvSource     - any webcam / UVC camera through OpenCV (DirectShow)

All camera calls run on the worker thread (the Motic SDK expects to be used
from the thread that opened the camera); the UI talks to it through
CameraWorker.call(), and reads frames with CameraWorker.latest().
"""

import math
import queue
import threading
import time
from dataclasses import dataclass, field

import cv2

from moticam import motic_sdk


@dataclass
class DeviceInfo:
    label: str
    kind: str           # "motic" | "cv"
    index: int
    resolutions: list = field(default_factory=list)


# --------------------------------------------------------------------------- discovery
def discover(probe_cv=4, dll=None):
    """Return a list of DeviceInfo. Slow (opens cameras) - call from a thread."""
    found, errors = [], []
    try:
        for i, typ, res in motic_sdk.list_cameras(dll):
            top = f"{res[0][0]}x{res[0][1]}" if res else "?"
            found.append(DeviceInfo(f"Motic camera {i + 1} ({top})", "motic", i, res))
    except Exception as e:
        errors.append(f"Motic SDK: {e}")
    for i in range(probe_cv):
        try:
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            ok = cap.isOpened()
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) if ok else 0
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) if ok else 0
            cap.release()
        except Exception:
            ok = False
        if ok:
            found.append(DeviceInfo(f"Webcam {i} (DirectShow, {w}x{h})", "cv", i))
    return found, errors


# --------------------------------------------------------------------------- sources
class MoticSource:
    kind = "motic"
    exposure_unit = "ms"
    exposure_log = True
    has_settings_dialog = False

    def __init__(self, index, dll=None):
        self.cam = motic_sdk.MoticCamera.open(index, dll)
        self.name = f"Motic camera {index + 1}"
        self.gains = self.cam.gain_list()
        self.gain_index = 0
        self.offset_rng = self.cam.offset_range()
        self.offset = 0

    # resolution
    @property
    def resolutions(self):
        return self.cam.resolutions

    @property
    def res_index(self):
        return self.cam.res_index

    def set_resolution_index(self, i):
        return self.cam.set_resolution_index(i)

    # exposure
    @property
    def exposure(self):
        return self.cam.exposure

    def exposure_range(self):
        lo = max(self.cam.exp_min, 0.01)
        hi = self.cam.exp_max if self.cam.exp_max > lo else 5000.0
        return lo, hi

    def set_exposure(self, v):
        return self.cam.set_exposure(v)

    def scale_exposure(self, factor):
        return self.cam.set_exposure(self.cam.exposure * factor)

    # gain / offset
    def gain_labels(self):
        return [f"x{g:g}" for g in self.gains]

    def set_gain_index(self, i):
        if self.cam.set_gain_index(i):
            self.gain_index = i
        return self.gain_index

    def set_offset(self, v):
        if self.cam.set_offset(int(v)):
            self.offset = int(v)
        return self.offset

    # colour pattern
    @property
    def is_bayer(self):
        return self.cam.format in (0, 1, 2, 3)

    def set_bayer(self, idx):
        self.cam.bayer_override = idx

    def read(self, timeout=0.5):
        return self.cam.read(timeout)

    def info(self):
        return self.cam.describe()

    def release(self):
        self.cam.release()


class CvSource:
    kind = "cv"
    exposure_unit = "log2 s"
    exposure_log = False
    has_settings_dialog = True
    RES = [(3840, 2160), (2592, 1944), (2048, 1536), (1920, 1080), (1600, 1200),
           (1280, 1024), (1280, 720), (1024, 768), (800, 600), (640, 480)]

    def __init__(self, index):
        self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open webcam {index}")
        self.name = f"Webcam {index}"
        self._res = []
        for w, h in self.RES:                         # keep the modes the camera accepts
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            got = (int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            if got not in self._res:
                self._res.append(got)
        self._res.sort(key=lambda r: -r[0] * r[1])
        self._ri = 0
        self.set_resolution_index(0)
        self.gains = [str(g) for g in range(0, 256, 16)]
        self.gain_index = 0
        self.offset_rng = None
        self.offset = 0

    @property
    def resolutions(self):
        return self._res

    @property
    def res_index(self):
        return self._ri

    def set_resolution_index(self, i):
        i %= len(self._res)
        w, h = self._res[i]
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self._ri = i
        return w, h

    @property
    def exposure(self):
        return self.cap.get(cv2.CAP_PROP_EXPOSURE)

    def exposure_range(self):
        return -13.0, 0.0

    def set_exposure(self, v):
        self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)   # manual mode on DirectShow
        self.cap.set(cv2.CAP_PROP_EXPOSURE, float(v))
        return self.exposure

    def scale_exposure(self, factor):
        return self.set_exposure(round(self.exposure + math.log2(factor)))

    def gain_labels(self):
        return self.gains

    def set_gain_index(self, i):
        self.cap.set(cv2.CAP_PROP_GAIN, i * 16)
        self.gain_index = i
        return i

    def set_offset(self, v):
        return 0

    is_bayer = False

    def set_bayer(self, idx):
        pass

    def open_settings_dialog(self):
        self.cap.set(cv2.CAP_PROP_SETTINGS, 1)

    def read(self, timeout=0.5):
        try:
            return self.cap.read()
        except cv2.error:
            return False, None

    def info(self):
        w, h = self._res[self._ri]
        return f"{self.name}, {w}x{h}"

    def release(self):
        self.cap.release()


def open_source(dev: DeviceInfo, dll=None):
    return MoticSource(dev.index, dll) if dev.kind == "motic" else CvSource(dev.index)


# --------------------------------------------------------------------------- worker thread
class CameraWorker(threading.Thread):
    """Owns the camera: opens it, grabs frames, runs queued commands."""

    def __init__(self, dev: DeviceInfo, dll=None):
        super().__init__(daemon=True)
        self.dev, self.dll = dev, dll
        self.src = None
        self.commands = queue.Queue()
        self.events = queue.Queue()        # (kind, payload, callback) for the UI thread
        self._lock = threading.Lock()
        self._frame, self._frame_id = None, 0
        self.fps = 0.0
        self._running = True

    def run(self):
        try:
            self.src = open_source(self.dev, self.dll)
            self.events.put(("opened", self.src, None))
        except Exception as e:
            self.events.put(("error", f"Could not open {self.dev.label}: {e}", None))
            return
        t_last, n, fails = time.time(), 0, 0
        try:
            while self._running:
                while True:
                    try:
                        fn, cb = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        res = fn(self.src)
                        if cb:
                            self.events.put(("result", res, cb))
                    except Exception as e:
                        self.events.put(("warning", f"Camera command failed: {e}", None))
                ok, frame = self.src.read(0.5)
                if ok and frame is not None:
                    fails = 0
                    with self._lock:
                        self._frame = frame
                        self._frame_id += 1
                    n += 1
                    now = time.time()
                    if now - t_last >= 1.0:
                        self.fps, n, t_last = n / (now - t_last), 0, now
                else:
                    fails += 1
                    if fails == 20:
                        self.events.put(("warning", "No frames from the camera - is it unplugged?", None))
                    time.sleep(0.02)
        finally:
            try:
                self.src.release()
            except Exception:
                pass
            self.events.put(("closed", None, None))

    def call(self, fn, callback=None):
        self.commands.put((fn, callback))

    def latest(self):
        with self._lock:
            return self._frame_id, self._frame

    def stop(self, wait=True):
        self._running = False
        if wait and self.is_alive():
            self.join(timeout=3)
