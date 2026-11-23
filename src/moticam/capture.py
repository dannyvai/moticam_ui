"""
moticam.capture - Capture still images from a Moticam (or any USB/UVC camera) on Windows.

Setup (once):
    uv sync

Usage:
    uv run moticam                     # Motic camera via Motic's SDK (falls back to webcams)
    uv run moticam --diag              # is Windows seeing the camera, and how?
    uv run moticam --list              # find which camera index is the Moticam
    uv run moticam                     # live preview, SPACE to capture
    uv run moticam --camera 1          # use a specific camera index
    uv run moticam --width 2592 --height 1944
    uv run moticam --interval 10 --count 30 --no-preview   # time-lapse

Preview window keys:
    SPACE / C   capture a full-resolution image
    S           open the camera's own settings dialog (webcams / DirectShow only)
    + / -       exposure longer / shorter (Motic SDK)
    B           try the next Bayer colour pattern if colours look wrong (Motic SDK)
    R           cycle through resolutions
    G           toggle a centre crosshair (preview only, never saved)
    Q / ESC     quit
"""

import argparse
import datetime as dt
import os
import subprocess
import sys
import time

# Hide OpenCV's noisy backend warnings while probing cameras.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

try:
    import cv2
except ImportError:
    sys.exit("OpenCV is missing. Run:  uv sync")

BACKENDS = {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF, "any": cv2.CAP_ANY}

# Common sensor sizes used by Moticam models, largest first.
RESOLUTIONS = [
    (5440, 3648), (4096, 3072), (3840, 2160), (2592, 1944), (2048, 1536),
    (1920, 1080), (1600, 1200), (1280, 1024), (1280, 960), (1280, 720),
    (1024, 768), (800, 600), (640, 480),
]


# --------------------------------------------------------------------------- diagnostics
def run_diag():
    """Ask Windows (PnP) what it thinks the camera is. No admin rights needed."""
    ps = r"""
$devs = Get-PnpDevice -PresentOnly | Where-Object {
    $_.FriendlyName -match 'Motic|Camera|Webcam|Imaging|Video|UVC' -or
    $_.Class -match 'Camera|Image' -or
    $_.Status -ne 'OK'
}
$devs | Sort-Object Class | Format-Table -AutoSize Status, Class, FriendlyName, InstanceId

Write-Output "Extra DirectShow capture filters (normal webcams are not listed here):"
$cat = '{860BB310-5D01-11d0-BD3B-00A0C911CE86}'
foreach ($bits in @(@('64-bit', "Registry::HKEY_CLASSES_ROOT\CLSID\$cat\Instance"),
                    @('32-bit', "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Classes\CLSID\$cat\Instance"))) {
    Get-ChildItem $bits[1] -ErrorAction SilentlyContinue | ForEach-Object {
        "  [{0}] {1}" -f $bits[0], (Get-ItemProperty $_.PSPath).FriendlyName }
}

Write-Output ""
Write-Output "Motic driver / software files on this PC:"
$motic = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_232F' }
foreach ($d in $motic) {
    $inf = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName DEVPKEY_Device_DriverInfPath -ErrorAction SilentlyContinue).Data
    $ver = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName DEVPKEY_Device_DriverVersion -ErrorAction SilentlyContinue).Data
    "  driver for $($d.FriendlyName): $inf  version $ver"
}
$roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData, "$env:SystemRoot\System32", "$env:SystemRoot\SysWOW64")
foreach ($r in $roots) {
    Get-ChildItem $r -Filter '*motic*' -ErrorAction SilentlyContinue | ForEach-Object { "  $($_.FullName)" }
}
"""
    print("Asking Windows for camera / imaging / problem devices...\n")
    text = ""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60)
        text = out.stdout or out.stderr
        print(text)
    except Exception as e:
        print("Could not run PowerShell:", e)
    for line in text.splitlines():
        if "VID_232F" in line.upper() and not line.strip().startswith("OK"):
            print(">>> Found a Motic camera (USB vendor 232F) with NO working driver.\n"
                  "    It uses Motic's own USB protocol, so it can't be read until the\n"
                  "    'Motic USB3 Camera' driver (moticusbcamera.inf) is installed.\n"
                  "    Get it from moticmicroscopes.com > Software Updates (it comes with\n"
                  "    Motic Images Plus 3.0), install, re-plug the camera, then run --list.\n")
    print("""How to read this:
  * Moticam listed under class 'Camera' or 'Image' with Status OK
      -> it's a standard UVC camera; this program will work. Run --list next.
  * Moticam (or 'Unknown USB Device') with Status Error / Unknown, class 'USB' or blank
      -> the camera is NOT a standard webcam (older Moticam 1000/2000/2300/2500/
         3000/5000 models). Windows cannot read it without Motic's driver,
         and no generic program can either. Install the driver from
         motic.com (Software > Motic Images Plus / Moticam drivers), then
         run --list again; the driver exposes it as a DirectShow camera.
  * Vendor ID: look for VID_xxxx in InstanceId if you need to identify it.
""")


def probe(index, backend):
    try:
        return _probe(index, backend)
    except cv2.error:
        return None


def _probe(index, backend):
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        return None
    ok, frame = False, None
    for _ in range(5):
        ok, frame = safe_read(cap)
        if ok:
            break
        time.sleep(0.1)
    info = None
    if ok and frame is not None:
        h, w = frame.shape[:2]
        set_max_resolution(cap)
        ok2, f2 = safe_read(cap)
        mh, mw = (f2.shape[:2] if ok2 else (h, w))
        info = (w, h, mw, mh)
    cap.release()
    return info


def run_list(max_index=8, dll=None):
    print("Probing cameras (this takes a few seconds)...\n")
    found = False
    try:
        from moticam import motic_sdk
        cams = motic_sdk.list_cameras(dll)
        print(f"Motic SDK ({motic_sdk.load_sdk(dll)._path}):")
        if not cams:
            print("  no Motic cameras found (is Motic Images Plus running? close it)")
        for i, typ, res in cams:
            found = True
            sizes = ", ".join(f"{w}x{h}" for w, h in res) or "unknown"
            print(f"  --camera {i} --backend motic:  type {typ}, resolutions {sizes}")
        print()
    except Exception as e:
        print(f"Motic SDK not usable: {e}\n")
    print("DirectShow / Media Foundation cameras:")
    for name in ("dshow", "msmf"):
        for i in range(max_index):
            info = probe(i, BACKENDS[name])
            if info:
                found = True
                w, h, mw, mh = info
                print(f"  --camera {i} --backend {name}:  default {w}x{h}, max {mw}x{mh}")
    if not found:
        print("  No cameras could be opened. Run with --diag to see what Windows reports.")
    else:
        print("\nThe Moticam is usually the one with the highest max resolution"
              " (the laptop webcam is often index 0).")


# --------------------------------------------------------------------------- camera helpers
def safe_read(cap):
    """cap.read() that never raises (MSMF can throw on odd mode changes)."""
    try:
        return cap.read()
    except cv2.error:
        return False, None


def set_resolution(cap, w, h):
    if cap.getBackendName() == "MOTIC":
        return cap.set_resolution(w, h)
    # MJPG lets most UVC cameras deliver high resolutions at a usable frame rate.
    # Only request it on DirectShow; on Media Foundation it can produce broken frames.
    if cap.getBackendName().upper() == "DSHOW":
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    for _ in range(3):          # let the new mode settle
        safe_read(cap)
    return int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))


def set_max_resolution(cap):
    # Asking for something huge makes DirectShow pick the largest supported mode.
    return set_resolution(cap, 10000, 10000)


def open_motic(index, dll=None, width=None, height=None):
    from moticam import motic_sdk
    cam = motic_sdk.MoticCamera.open(index, dll)
    if width and height:
        cam.set_resolution(width, height)
    print("Opened via Motic SDK:", cam.describe())
    cam.read()                                  # first frame can take a moment
    return cam


def open_camera(index, backend_name, width=None, height=None, dll=None):
    if backend_name in ("auto", "motic"):
        try:
            return open_motic(index, dll, width, height)
        except Exception as e:
            if backend_name == "motic":
                sys.exit(f"Motic SDK: {e}")
            print(f"Motic SDK unavailable ({e}); trying DirectShow camera {index}.")
            backend_name = "dshow"
    cap = cv2.VideoCapture(index, BACKENDS[backend_name])
    if not cap.isOpened():
        sys.exit(f"Could not open camera {index} with backend '{backend_name}'.\n"
                 f"Try: uv run moticam --list   (or --diag)")
    if width and height:
        w, h = set_resolution(cap, width, height)
    else:
        w, h = set_max_resolution(cap)
    print(f"Camera {index} open via {backend_name} at {w}x{h}")
    # Warm-up: let auto-exposure / white balance settle.
    t0 = time.time()
    while time.time() - t0 < 1.0:
        safe_read(cap)
    return cap


def grab_frame(cap, retries=5):
    for _ in range(retries):
        ok, frame = safe_read(cap)
        if ok and frame is not None:
            return frame
        time.sleep(0.05)
    return None


def save_frame(frame, outdir, fmt, prefix):
    os.makedirs(outdir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = os.path.join(outdir, f"{prefix}_{stamp}.{fmt}")
    params = [cv2.IMWRITE_JPEG_QUALITY, 97] if fmt in ("jpg", "jpeg") else []
    # cv2.imwrite can't handle non-ASCII paths on Windows; imencode + tofile can.
    ok, buf = cv2.imencode("." + fmt, frame, params)
    if not ok:
        raise IOError("Image encoding failed")
    buf.tofile(path)
    h, w = frame.shape[:2]
    print(f"Saved {path}  ({w}x{h})")
    return path


# --------------------------------------------------------------------------- modes
def run_preview(cap, args):
    motic = cap.getBackendName() == "MOTIC"
    win = ("Moticam  |  SPACE capture   +/- exposure   B colours   R resolution   G crosshair   Q quit"
           if motic else
           "Moticam  |  SPACE capture   S settings   R resolution   G crosshair   Q quit")
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    res_idx, crosshair, flash_until, count = -1, False, 0, 0
    next_auto = time.time() + args.interval if args.interval else None

    while True:
        frame = grab_frame(cap)
        if frame is None:
            print("Lost the camera feed.")
            break

        do_capture = False
        if next_auto and time.time() >= next_auto:
            do_capture, next_auto = True, next_auto + args.interval

        view = frame.copy()
        h, w = view.shape[:2]
        if crosshair:
            cv2.line(view, (w // 2, 0), (w // 2, h), (0, 255, 0), max(1, w // 800))
            cv2.line(view, (0, h // 2), (w, h // 2), (0, 255, 0), max(1, w // 800))
        status = f"{w}x{h}  saved: {count}"
        if motic:
            status += f"  exp: {cap.exposure:.1f} ms"
        cv2.putText(view, status, (10, 30 * max(1, w // 1280)), cv2.FONT_HERSHEY_SIMPLEX,
                    max(0.6, w / 1600), (0, 255, 255), max(1, w // 800), cv2.LINE_AA)
        if time.time() < flash_until:
            cv2.rectangle(view, (0, 0), (w - 1, h - 1), (255, 255, 255), max(4, w // 150))
        cv2.imshow(win, view)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), ord("Q"), 27):
            break
        if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
            break
        if key in (ord(" "), ord("c"), ord("C")):
            do_capture = True
        elif motic and key in (ord("+"), ord("=")):
            print(f"Exposure {cap.set_exposure(cap.exposure * 1.25 or 1):.2f} ms")
        elif motic and key in (ord("-"), ord("_")):
            print(f"Exposure {cap.set_exposure(cap.exposure / 1.25):.2f} ms")
        elif motic and key in (ord("b"), ord("B")):
            print("Bayer pattern now:", cap.cycle_bayer())
        elif motic and key in (ord("r"), ord("R")):
            got = cap.set_resolution_index(cap.res_index + 1)
            print(f"Resolution {got[0]}x{got[1]}")
        elif key in (ord("s"), ord("S")):
            # Opens the DirectShow property page (works with --backend dshow).
            if not cap.set(cv2.CAP_PROP_SETTINGS, 1):
                print("Settings dialog not available with this backend; try --backend dshow")
        elif key in (ord("r"), ord("R")):
            res_idx = (res_idx + 1) % len(RESOLUTIONS)
            rw, rh = RESOLUTIONS[res_idx]
            got = set_resolution(cap, rw, rh)
            print(f"Requested {rw}x{rh}, camera gave {got[0]}x{got[1]}")
        elif key in (ord("g"), ord("G")):
            crosshair = not crosshair

        if do_capture:
            save_frame(frame, args.out, args.format, args.prefix)
            count += 1
            flash_until = time.time() + 0.15
            if args.count and count >= args.count:
                break

    cv2.destroyAllWindows()


def run_headless(cap, args):
    n = args.count or 1
    for i in range(n):
        frame = grab_frame(cap)
        if frame is None:
            sys.exit("Failed to read a frame from the camera.")
        save_frame(frame, args.out, args.format, args.prefix)
        if i < n - 1 and args.interval:
            time.sleep(args.interval)


# --------------------------------------------------------------------------- Motic SDK inspection
def _pe_info(path):
    """Return (bits, [exported names]) for a Windows DLL/EXE, using only the stdlib."""
    import struct
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"MZ":
        return None, []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return None, []
    machine, nsec = struct.unpack_from("<HH", data, pe + 4)
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    bits = {0x14C: 32, 0x8664: 64, 0xAA64: 64}.get(machine, 0)
    dd = opt + (96 if magic == 0x10B else 112)
    exp_rva, exp_size = struct.unpack_from("<II", data, dd)
    if not exp_rva:
        return bits, []
    secs = []
    for i in range(nsec):
        o = opt + opt_size + 40 * i
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, o + 8)
        secs.append((va, max(vsize, rsize), raw))

    def off(rva):
        for va, size, raw in secs:
            if va <= rva < va + size:
                return rva - va + raw
        return None

    e = off(exp_rva)
    if e is None:
        return bits, []
    n_names = struct.unpack_from("<I", data, e + 24)[0]
    names_rva = struct.unpack_from("<I", data, e + 32)[0]
    names = []
    base = off(names_rva)
    for i in range(min(n_names, 400)):
        nrva = struct.unpack_from("<I", data, base + 4 * i)[0]
        no = off(nrva)
        end = data.index(b"\0", no)
        names.append(data[no:end].decode("ascii", "replace"))
    return bits, names


def run_inspect_motic():
    win = os.environ.get("SystemRoot", r"C:\Windows")
    roots = [r"C:\Program Files\Motic Image Devices", r"C:\Program Files\Motic"]
    files = []
    for r in roots:
        for dirpath, _, names in os.walk(r):
            for n in names:
                if n.lower().endswith((".dll", ".ax", ".exe", ".h", ".lib", ".txt", ".pdf", ".ini", ".xml")):
                    files.append(os.path.join(dirpath, n))
    for d in ("System32", "SysWOW64"):
        folder = os.path.join(win, d)
        try:
            files += [os.path.join(folder, n) for n in os.listdir(folder) if "motic" in n.lower()]
        except OSError:
            pass

    print(f"Found {len(files)} Motic files.\n")
    for path in sorted(files):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if path.lower().endswith((".dll", ".ax", ".exe")):
            try:
                bits, exports = _pe_info(path)
            except Exception:
                bits, exports = None, []
            print(f"[{bits or '?'}-bit] {path}  ({size // 1024} KB)")
            if exports:
                shown = ", ".join(exports[:60])
                more = f"  ... +{len(exports) - 60} more" if len(exports) > 60 else ""
                print(f"    exports: {shown}{more}")
        else:
            print(f"[file]   {path}  ({size // 1024} KB)")


# --------------------------------------------------------------------------- main
def main():
    default_out = os.path.join(os.path.expanduser("~"), "Pictures", "Moticam")
    p = argparse.ArgumentParser(description="Capture images from a Moticam / USB camera.")
    p.add_argument("--diag", action="store_true", help="show how Windows sees the camera")
    p.add_argument("--list", action="store_true", help="list cameras that can be opened")
    p.add_argument("--inspect-motic", action="store_true",
                   help="list Motic SDK files, their bitness and exported functions")
    p.add_argument("--camera", type=int, default=0, help="camera index (see --list)")
    p.add_argument("--backend", choices=["auto", "motic", *BACKENDS], default="auto",
                   help="auto (default: Motic SDK, else DirectShow), motic, dshow or msmf")
    p.add_argument("--motic-dll", help="path to MUCam32.dll if not in the default Motic folder")
    p.add_argument("--exposure", type=float, help="exposure in ms (Motic SDK)")
    p.add_argument("--width", type=int, help="requested width (default: max supported)")
    p.add_argument("--height", type=int, help="requested height")
    p.add_argument("--out", default=default_out, help=f"output folder (default {default_out})")
    p.add_argument("--format", choices=["png", "jpg", "tif", "bmp"], default="png")
    p.add_argument("--prefix", default="moticam", help="file name prefix")
    p.add_argument("--interval", type=float, default=0, help="seconds between auto captures")
    p.add_argument("--count", type=int, default=0, help="stop after this many images")
    p.add_argument("--no-preview", action="store_true", help="capture without a window")
    args = p.parse_args()

    if args.diag:
        return run_diag()
    if args.list:
        return run_list(dll=args.motic_dll)
    if args.inspect_motic:
        return run_inspect_motic()

    cap = open_camera(args.camera, args.backend, args.width, args.height, args.motic_dll)
    if args.exposure and cap.getBackendName() == "MOTIC":
        print(f"Exposure set to {cap.set_exposure(args.exposure):.2f} ms")
    try:
        if args.no_preview:
            run_headless(cap, args)
        else:
            run_preview(cap, args)
    finally:
        cap.release()


if __name__ == "__main__":
    main()
