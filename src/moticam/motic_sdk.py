"""
Thin ctypes wrapper around Motic's MUCam SDK (MUCam32.dll / MUCam64.dll).

Motic USB cameras such as the "Motic USB3 Camera" (USB VID 232F) are not
standard webcams, so OpenCV/DirectShow cannot see them. Motic Images Plus
ships a C DLL ("Motic Universal Camera API", MUCam) that talks to them; this
module loads that DLL and exposes an OpenCV-like object:

    cam = MoticCamera.open()          # first Motic camera
    ok, frame = cam.read()            # frame is a BGR numpy array
    cam.release()

Function signatures follow Motic's public header MoticUCam.h.
"""

import ctypes
import glob
import os
import sys
import time

import numpy as np

try:
    import cv2
except ImportError:  # only needed for Bayer conversion
    cv2 = None

# Where Motic Images Plus 3.0 installs the SDK DLL (64-bit build despite the name).
DEFAULT_DLL_CANDIDATES = [
    r"C:\Program Files\Motic\Motic Image Devices\MUCam32.dll",
    r"C:\Program Files\Motic Image Devices\MUCam32.dll",
    r"C:\Program Files\Motic\Motic Image Devices\MUCam64.dll",
    r"C:\Program Files\Motic Image Devices\MUCam64.dll",
]

# MUCam_Format enum (declaration order in MoticUCam.h)
FMT_BAYER_GR_BG, FMT_BAYER_BG_GR, FMT_BAYER_GB_RG, FMT_BAYER_RG_GB = 0, 1, 2, 3
FMT_RGB, FMT_BGR, FMT_MONO = 4, 5, 6
FORMAT_NAMES = {0: "Bayer GR/BG", 1: "Bayer BG/GR", 2: "Bayer GB/RG", 3: "Bayer RG/GB",
                4: "RGB", 5: "BGR", 6: "Mono"}

# MUCam_Trigger_Type
TRIGGER_FREE, TRIGGER_SOFTWARE = 0, 1


class MoticSDKError(RuntimeError):
    pass


def _bundled_dlls():
    """MUCam DLL shipped inside the standalone build (folder "motic")."""
    roots = [getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None]
    out = []
    for r in filter(None, roots):
        out += sorted(glob.glob(os.path.join(r, "motic", "MUCam*.dll")))
    return out


def find_dll(explicit=None):
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("MOTIC_SDK_DLL"):
        candidates.append(os.environ["MOTIC_SDK_DLL"])
    candidates += _bundled_dlls()
    candidates += DEFAULT_DLL_CANDIDATES
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
        candidates += glob.glob(os.path.join(root, "Motic*", "**", "MUCam*.dll"), recursive=True)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


_lib = None


def load_sdk(dll_path=None):
    """Load the MUCam DLL once and declare its function signatures."""
    global _lib
    if _lib is not None:
        return _lib
    if sys.platform != "win32":
        raise MoticSDKError("The Motic SDK is only available on Windows.")
    path = find_dll(dll_path)
    if not path:
        raise MoticSDKError("MUCam DLL not found. Install Motic Images Plus 3.0, "
                            "or pass --motic-dll <path to MUCam32.dll>.")
    folder = os.path.dirname(path)
    os.add_dll_directory(folder)                      # so its own dependencies resolve
    os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
    try:
        lib = ctypes.CDLL(path)
    except OSError as e:
        raise MoticSDKError(f"Could not load {path}: {e}\n"
                            "(A 32-bit DLL cannot be loaded from 64-bit Python.)") from e

    H = ctypes.c_void_p
    B = ctypes.c_bool
    I = ctypes.c_int
    F = ctypes.c_float
    P = ctypes.POINTER

    def sig(name, restype, *argtypes):
        fn = getattr(lib, name, None)
        if fn is not None:
            fn.restype, fn.argtypes = restype, list(argtypes)
        return fn

    sig("MUCam_findCamera", H)
    sig("MUCam_releaseCamera", None, H)
    sig("MUCam_getType", I, H)
    sig("MUCam_openCamera", B, H)
    sig("MUCam_closeCamera", None, H)
    sig("MUCam_getFrameFormat", I, H)
    sig("MUCam_getFrame", B, H, P(ctypes.c_ubyte), P(ctypes.c_ulong))
    sig("MUCam_setBitCount", B, H, I)
    sig("MUCam_getBinningCount", I, H)
    sig("MUCam_getBinningList", B, H, P(I), P(I))
    sig("MUCam_setBinningIndex", B, H, I)
    sig("MUCam_setFlip", B, H, B)
    sig("MUCam_setMirror", B, H, B)
    sig("MUCam_getGainCount", I, H)
    sig("MUCam_setRGBGainValue", B, H, F, F, F, P(I), P(I), P(I))
    sig("MUCam_getGainList", B, H, P(F))
    sig("MUCam_setRGBGainIndex", B, H, I, I, I)
    sig("MUCam_getOffsetRange", B, H, P(I), P(I))
    sig("MUCam_setRGBOffset", B, H, I, I, I)
    sig("MUCam_getExposureRange", B, H, P(F), P(F))
    sig("MUCam_setExposure", B, H, F)
    sig("MUCam_isConnected", B, H)
    sig("MUCam_setTriggerType", B, H, I)
    _lib = lib
    _lib._path = path
    return lib


def list_cameras(dll_path=None):
    """Return [(index, type_id, [(w, h), ...])] without keeping cameras open."""
    lib = load_sdk(dll_path)
    handles = _enumerate(lib)
    out = []
    try:
        for i, h in enumerate(handles):
            res = []
            if lib.MUCam_openCamera(h):
                res = _binning_list(lib, h)
                lib.MUCam_closeCamera(h)
            out.append((i, lib.MUCam_getType(h), res))
    finally:
        for h in handles:
            lib.MUCam_releaseCamera(h)
    return out


def _enumerate(lib):
    handles = []
    for _ in range(16):                      # header: call until it returns 0
        h = lib.MUCam_findCamera()
        if not h:
            break
        handles.append(h)
    return handles


def _binning_list(lib, h):
    n = lib.MUCam_getBinningCount(h)
    if n <= 0:
        return []
    ws, hs = (ctypes.c_int * n)(), (ctypes.c_int * n)()
    if not lib.MUCam_getBinningList(h, ws, hs):
        return []
    return [(ws[i], hs[i]) for i in range(n)]


class MoticCamera:
    """OpenCV-VideoCapture-like wrapper around one MUCam camera."""

    def __init__(self, lib, handle, others):
        self.lib, self.h = lib, handle
        self._others = others                # other handles found during enumeration
        self.resolutions = _binning_list(lib, handle)
        self.res_index = 0
        self.width = self.height = 0
        self.exposure = 0.0
        self.exp_min = self.exp_max = 0.0
        self.bayer_override = None
        self._buf = None
        self._opened = True

    # ---------------------------------------------------------------- construction
    @classmethod
    def open(cls, index=0, dll_path=None):
        lib = load_sdk(dll_path)
        handles = _enumerate(lib)
        if index >= len(handles):
            for h in handles:
                lib.MUCam_releaseCamera(h)
            raise MoticSDKError(f"Motic SDK found {len(handles)} camera(s); index {index} not available. "
                                "Is Motic Images Plus still running? Close it and retry.")
        h = handles[index]
        others = [x for i, x in enumerate(handles) if i != index]
        if not lib.MUCam_openCamera(h):
            for x in handles:
                lib.MUCam_releaseCamera(x)
            raise MoticSDKError("MUCam_openCamera failed. Close Motic Images Plus / Motic Image "
                                "Devices (only one program can use the camera) and retry.")
        cam = cls(lib, h, others)
        cam._configure()
        return cam

    def _configure(self):
        lib, h = self.lib, self.h
        if lib.MUCam_setBitCount:
            lib.MUCam_setBitCount(h, 8)
        self.set_resolution_index(0)                    # full resolution
        if lib.MUCam_setRGBGainValue:
            ri, gi, bi = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
            lib.MUCam_setRGBGainValue(h, 1.0, 1.0, 1.0, ctypes.byref(ri), ctypes.byref(gi), ctypes.byref(bi))
        if lib.MUCam_setTriggerType:
            lib.MUCam_setTriggerType(h, TRIGGER_FREE)

    # ---------------------------------------------------------------- settings
    def set_resolution_index(self, idx):
        if not self.resolutions:
            raise MoticSDKError("Camera reported no resolutions.")
        idx %= len(self.resolutions)
        self.lib.MUCam_setBinningIndex(self.h, idx)
        self.res_index = idx
        self.width, self.height = self.resolutions[idx]
        # 3 bytes/pixel covers colour; extra headroom in case the SDK pads rows.
        self._buf = (ctypes.c_ubyte * (self.width * self.height * 3 + 4096))()
        self._update_exposure_range()
        return self.width, self.height

    def set_resolution(self, w, h):
        """Pick the supported resolution closest to w x h."""
        best = min(range(len(self.resolutions)),
                   key=lambda i: abs(self.resolutions[i][0] * self.resolutions[i][1] - w * h))
        return self.set_resolution_index(best)

    def _update_exposure_range(self):
        mn, mx = ctypes.c_float(), ctypes.c_float()
        if self.lib.MUCam_getExposureRange(self.h, ctypes.byref(mn), ctypes.byref(mx)):
            self.exp_min, self.exp_max = mn.value, mx.value
        target = self.exposure or 30.0                  # ms; header says set it after binning
        self.set_exposure(target)

    def set_exposure(self, ms):
        if self.exp_max > self.exp_min:
            ms = max(self.exp_min, min(self.exp_max, ms))
        if self.lib.MUCam_setExposure(self.h, float(ms)):
            self.exposure = ms
        return self.exposure

    def set_flip(self, flip=None, mirror=None):
        if flip is not None and self.lib.MUCam_setFlip:
            self.lib.MUCam_setFlip(self.h, bool(flip))
        if mirror is not None and self.lib.MUCam_setMirror:
            self.lib.MUCam_setMirror(self.h, bool(mirror))

    def gain_list(self):
        n = self.lib.MUCam_getGainCount(self.h) if self.lib.MUCam_getGainCount else 0
        if n <= 0 or not self.lib.MUCam_getGainList:
            return []
        arr = (ctypes.c_float * n)()
        return [round(v, 3) for v in arr] if self.lib.MUCam_getGainList(self.h, arr) else []

    def set_gain_index(self, i):
        return bool(self.lib.MUCam_setRGBGainIndex and self.lib.MUCam_setRGBGainIndex(self.h, i, i, i))

    def offset_range(self):
        mn, mx = ctypes.c_int(), ctypes.c_int()
        if self.lib.MUCam_getOffsetRange and self.lib.MUCam_getOffsetRange(self.h, ctypes.byref(mn), ctypes.byref(mx)):
            return mn.value, mx.value
        return None

    def set_offset(self, v):
        return bool(self.lib.MUCam_setRGBOffset and self.lib.MUCam_setRGBOffset(self.h, v, v, v))

    @property
    def format(self):
        return self.lib.MUCam_getFrameFormat(self.h)

    # ---------------------------------------------------------------- capture
    def read(self, timeout=3.0):
        """Return (ok, BGR frame) like cv2.VideoCapture.read()."""
        ts = ctypes.c_ulong(0)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.lib.MUCam_getFrame(self.h, self._buf, ctypes.byref(ts)):
                return True, self._to_bgr()
            time.sleep(0.005)
        return False, None

    def _to_bgr(self):
        w, h = self.width, self.height
        fmt = self.format
        raw = np.ctypeslib.as_array(self._buf)
        if fmt in (FMT_RGB, FMT_BGR):
            img = raw[: w * h * 3].reshape(h, w, 3).copy()
            return img[:, :, ::-1].copy() if fmt == FMT_RGB else img
        img = raw[: w * h].reshape(h, w).copy()
        if fmt == FMT_MONO:
            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if cv2 else np.dstack([img] * 3)
        # Bayer. OpenCV names patterns by the 2nd row/2nd column, hence the shift.
        codes = {FMT_BAYER_GR_BG: cv2.COLOR_BayerGB2BGR, FMT_BAYER_BG_GR: cv2.COLOR_BayerRG2BGR,
                 FMT_BAYER_GB_RG: cv2.COLOR_BayerGR2BGR, FMT_BAYER_RG_GB: cv2.COLOR_BayerBG2BGR}
        key = self.bayer_override if self.bayer_override is not None else fmt
        return cv2.cvtColor(img, codes[key])

    def cycle_bayer(self):
        """For fixing wrong colours on Bayer cameras: try the next pattern."""
        cur = self.bayer_override if self.bayer_override is not None else self.format
        self.bayer_override = (cur + 1) % 4 if cur in (0, 1, 2, 3) else 0
        return FORMAT_NAMES[self.bayer_override]

    # ---------------------------------------------------------------- OpenCV-ish API
    def isOpened(self):
        return self._opened

    def getBackendName(self):
        return "MOTIC"

    def describe(self):
        return (f"Motic camera type {self.lib.MUCam_getType(self.h)}, {self.width}x{self.height}, "
                f"format {FORMAT_NAMES.get(self.format, self.format)}, "
                f"exposure {self.exposure:.1f} ms (range {self.exp_min:g}-{self.exp_max:g})")

    def release(self):
        if not self._opened:
            return
        self._opened = False
        self.lib.MUCam_closeCamera(self.h)
        self.lib.MUCam_releaseCamera(self.h)
        for x in self._others:
            self.lib.MUCam_releaseCamera(x)
