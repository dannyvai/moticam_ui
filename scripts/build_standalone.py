"""
Build a standalone Windows version of the Moticam live imaging app.

Run it on a PC where the camera works (Motic Images Plus 3.0 installed):

    build.bat
    (or)  uv run --group build python scripts/build_standalone.py

It produces:

    dist/MoticamCapture/                 the program folder
        MoticamCapture.exe               the app (no Python needed)
        _internal/motic/MUCam32.dll ...  Motic camera SDK + the DLLs it needs
        driver/                          Motic USB camera driver package (.inf/.sys/.cat)
        install_driver.bat / .ps1        installs that driver if a PC doesn't have it
        README.txt
    dist/MoticamCapture-<version>-win64.zip

Options:
    --motic-dll PATH     MUCam DLL to bundle (default: found automatically)
    --driver-dir PATH    driver package folder to bundle (default: from the Windows driver store)
    --no-driver          do not bundle the driver
    --no-zip             skip the zip file

Note: the Motic DLL and driver are Motic's software. Bundling them is fine for your own lab's
computers; ask Motic before distributing the package to others.
"""

import argparse
import glob
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

APP_NAME = "MoticamCapture"
SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
RUNTIME_PREFIXES = ("msvcp", "vcruntime", "concrt", "vcomp", "mfc", "libgcc", "libstdc++", "libwinpthread")


# --------------------------------------------------------------------------- PE import reader
def pe_imports(path):
    """Names of the DLLs a Windows DLL/EXE imports (normal and delay-loaded)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd = opt + (96 if magic == 0x10B else 112)
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

    def cstr(rva):
        o = off(rva)
        return data[o:data.index(b"\0", o)].decode("ascii", "replace") if o is not None else None

    names = []
    imp_rva = struct.unpack_from("<I", data, dd + 8)[0]          # data directory 1: imports
    o = off(imp_rva) if imp_rva else None
    while o is not None and o + 20 <= len(data):
        name_rva = struct.unpack_from("<I", data, o + 12)[0]
        if not name_rva:
            break
        names.append(cstr(name_rva))
        o += 20
    dl_rva = struct.unpack_from("<I", data, dd + 13 * 8)[0]      # data directory 13: delay imports
    o = off(dl_rva) if dl_rva else None
    while o is not None and o + 32 <= len(data):
        name_rva = struct.unpack_from("<I", data, o + 4)[0]
        if not name_rva:
            break
        names.append(cstr(name_rva))
        o += 32
    return [n for n in names if n]


def collect_dlls(main_dll):
    """MUCam DLL plus every DLL it needs from the Motic folder, and VC++ runtimes from System32."""
    folder = os.path.dirname(main_dll)
    local = {f.lower(): os.path.join(folder, f) for f in os.listdir(folder)}
    found, todo = {}, [main_dll]
    while todo:
        path = todo.pop()
        key = os.path.basename(path).lower()
        if key in found:
            continue
        found[key] = path
        for name in pe_imports(path):
            n = name.lower()
            if n in found or n.startswith(("api-ms-", "ext-ms-")):
                continue
            if n in local:
                todo.append(local[n])
            elif n.startswith(RUNTIME_PREFIXES) and os.path.isfile(os.path.join(SYSTEM32, name)):
                found[n] = os.path.join(SYSTEM32, name)
    return list(found.values())


# --------------------------------------------------------------------------- driver package
def find_driver_package():
    """Folder of the installed Motic camera driver in the Windows driver store."""
    store = os.path.join(SYSTEM32, "DriverStore", "FileRepository")
    candidates = []
    for d in glob.glob(os.path.join(store, "*")):
        name = os.path.basename(d).lower()
        if "motic" not in name:
            continue
        for inf in glob.glob(os.path.join(d, "*.inf")):
            try:
                raw = open(inf, "rb").read()
            except OSError:
                continue
            text = raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "latin-1", "replace")
            if "232f" in text.lower():
                candidates.append(d)
                break
    if candidates:
        return max(candidates, key=os.path.getmtime)
    return export_driver_with_pnputil()


def export_driver_with_pnputil():
    """Fallback: find the oemNN.inf that mentions the Motic USB ID and export it (may need admin)."""
    inf_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "INF")
    for inf in glob.glob(os.path.join(inf_dir, "oem*.inf")):
        try:
            if b"232f" not in open(inf, "rb").read().lower().replace(b"\0", b""):
                continue
        except OSError:
            continue
        dest = tempfile.mkdtemp(prefix="motic_driver_")
        r = subprocess.run(["pnputil", "/export-driver", os.path.basename(inf), dest], capture_output=True, text=True)
        if r.returncode == 0 and glob.glob(os.path.join(dest, "**", "*.inf"), recursive=True):
            return dest
    return None


# --------------------------------------------------------------------------- build
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motic-dll")
    ap.add_argument("--driver-dir")
    ap.add_argument("--no-driver", action="store_true")
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--allow-no-sdk", action="store_true", help="build even if the Motic DLL is missing")
    args = ap.parse_args()

    if sys.platform != "win32" and not args.allow_no_sdk:
        sys.exit("Build the standalone version on Windows (the camera SDK and driver are Windows files).")

    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        version = tomllib.load(f)["project"]["version"]

    # 1. Motic SDK DLLs
    from moticam import motic_sdk
    dll = motic_sdk.find_dll(args.motic_dll)
    dlls = []
    if dll:
        dlls = collect_dlls(dll)
        print("Motic SDK files:")
        for d in dlls:
            print("   ", d)
    elif not args.allow_no_sdk:
        sys.exit("MUCam DLL not found. Install Motic Images Plus 3.0 or pass --motic-dll.")
    else:
        print("WARNING: building without the Motic SDK DLL.")

    # 2. driver package
    driver = None
    if not args.no_driver:
        driver = args.driver_dir or find_driver_package()
        if driver:
            print("Driver package:", driver)
        else:
            print("WARNING: Motic driver package not found - building without it "
                  "(use --driver-dir to point at the driver folder).")

    # 3. PyInstaller
    import PyInstaller.__main__
    dist, work = os.path.join(ROOT, "dist"), os.path.join(ROOT, "build")
    target = os.path.join(dist, APP_NAME)
    if os.path.isdir(target):
        shutil.rmtree(target)
    pyi = [
        os.path.join(ROOT, "scripts", "launcher.py"),
        "--name", APP_NAME, "--onedir", "--windowed", "--noconfirm", "--clean",
        "--distpath", dist, "--workpath", os.path.join(work, "pyinstaller"), "--specpath", work,
        "--paths", os.path.join(ROOT, "src"),
        "--collect-submodules", "moticam",
        "--hidden-import", "PIL._tkinter_finder",
    ]
    icon = os.path.join(ROOT, "scripts", "app.ico")
    if os.path.isfile(icon):
        pyi += ["--icon", icon]
    for d in dlls:
        pyi += ["--add-binary", f"{d}{os.pathsep}motic"]
    print("Running PyInstaller ...")
    PyInstaller.__main__.run(pyi)

    # 4. driver + installer + readme next to the exe
    if driver:
        shutil.copytree(driver, os.path.join(target, "driver"), dirs_exist_ok=True)
        for name in ("install_driver.ps1", "install_driver.bat"):
            shutil.copy2(os.path.join(ROOT, "scripts", name), target)
    with open(os.path.join(target, "README.txt"), "w", encoding="utf-8") as f:
        f.write(f"""Moticam Capture {version}

Start MoticamCapture.exe. Close Motic Images Plus first - only one program can use the camera.

First time on a new PC:
  If the camera is not found, run install_driver.bat (asks for administrator rights),
  then unplug and reconnect the camera. The program also offers to do this itself.

Images are saved to Pictures\\Moticam by default. Settings and presets are kept in
%USERPROFILE%\\.moticam.
""")

    # 5. zip
    if not args.no_zip:
        z = shutil.make_archive(os.path.join(dist, f"{APP_NAME}-{version}-win64"), "zip", dist, APP_NAME)
        print("Zip:", z)
    print("\nDone:", target)


if __name__ == "__main__":
    main()
