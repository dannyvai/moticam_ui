"""
Build a standalone Windows version of the Moticam live imaging app.

Run it on a PC where the camera works (Motic Images Plus 3.0 installed):

    build.bat
    (or)  uv run --group build python scripts/build_standalone.py

It produces:

    dist/MoticamCapture/                 the program folder
        MoticamCapture.exe               the app (no Python needed)
        _internal/motic/MUCam32.dll ...  Motic camera SDK + the DLLs it needs
        driver/<name>/                   Motic camera driver packages (.inf/.sys/.cat), one per folder
        install_driver.bat / .ps1        installs that driver if a PC doesn't have it
        README.txt
    dist/MoticamCapture-<version>-win64.zip

Options:
    --motic-dll PATH     MUCam DLL to bundle (default: found automatically)
    --driver-dir PATH    driver package folder to bundle (default: the driver the connected camera
                         uses, plus every other Motic driver in the Windows driver store)
    --drivers-from PATH  also search PATH for Motic drivers, e.g. the "Drivers" folder of the
                         Motic Images Plus installer (works without the camera plugged in)
    --no-driver          do not bundle the driver
    --no-zip             skip the zip file

Note: the Motic DLL and driver are Motic's software. Bundling them is fine for your own lab's
computers; ask Motic before distributing the package to others.
"""

import argparse
import glob
import json
import os
import re
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


# --------------------------------------------------------------------------- driver packages
MOTIC_VID = "232F"
PID_RE = re.compile(r"USB\\VID_232F&PID_([0-9A-F]{4})", re.I)


def inf_text(path):
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[1:2] == b"\0":
        return raw.decode("utf-16", "replace")
    return raw.decode("latin-1", "replace")


def inf_source_files(text):
    """File names listed in the [SourceDisksFiles...] sections - everything the package needs."""
    files, in_section = set(), False
    for line in text.splitlines():
        line = line.split(";", 1)[0].strip()
        if line.startswith("["):
            in_section = line.lower().startswith("[sourcedisksfiles")
        elif in_section and "=" in line:
            files.add(line.split("=", 1)[0].strip().strip('"'))
    return files


def find_driver_packages(sources=()):
    """Every Motic (USB VID 232F) driver package in the Windows driver store and in `sources`
    (for example the Drivers folder of the Motic Images Plus installer)."""
    store = os.path.join(SYSTEM32, "DriverStore", "FileRepository")
    infs = [(inf, "driver store") for inf in glob.glob(os.path.join(store, "*", "*.inf"))]
    for src in sources:
        infs += [(inf, src) for inf in glob.glob(os.path.join(src, "**", "*.inf"), recursive=True)]
    pkgs = []
    for inf, origin in infs:
        d = os.path.dirname(inf)
        try:
            if os.path.getsize(inf) > 4_000_000:
                continue
            text = inf_text(inf)
        except OSError:
            continue
        pids = {p.upper() for p in PID_RE.findall(text)}
        if not pids:
            continue
        present = {f.lower() for _, _, fs in os.walk(d) for f in fs}
        missing = sorted(f for f in inf_source_files(text) if f.lower() not in present)
        ver = re.search(r"DriverVer\s*=\s*([^\r\n;]+)", text, re.I)
        pkgs.append({"dir": d, "inf": inf, "name": os.path.basename(inf), "pids": pids, "missing": missing,
                     "origin": origin,
                     "version": ver.group(1).strip() if ver else "?", "bytes": open(inf, "rb").read()})
    return pkgs


def connected_motic_cameras():
    """[(pid, status, bound oemNN.inf)] for Motic cameras plugged in right now."""
    ps = ("Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -match 'VID_232F' } | "
          "ForEach-Object { [pscustomobject]@{ Id = $_.InstanceId; Status = [string]$_.Status; "
          "Inf = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_DriverInfPath "
          "-ErrorAction SilentlyContinue).Data } } | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=60).stdout.strip()
        data = json.loads(out) if out else []
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    cams = []
    for d in data:
        m = re.search(r"PID_([0-9A-F]{4})", d.get("Id") or "", re.I)
        if m:
            cams.append((m.group(1).upper(), d.get("Status") or "", d.get("Inf") or ""))
    return cams


def choose_driver_packages(sources=()):
    """Pick the packages to bundle. Fails loudly if the connected camera's driver is not among them."""
    pkgs = find_driver_packages(sources)
    cams = connected_motic_cameras()
    inf_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "INF")
    chosen = {}
    # 1. the exact package each connected camera is using (oemNN.inf is a byte copy of it)
    for pid, status, bound in cams:
        print(f"Connected Motic camera: PID {pid}, status {status}, driver {bound or 'none'}")
        bound_bytes = None
        if bound:
            try:
                bound_bytes = open(os.path.join(inf_dir, bound), "rb").read()
            except OSError:
                pass
        exact = [p for p in pkgs if bound_bytes is not None and p["bytes"] == bound_bytes]
        fits = [p for p in pkgs if pid in p["pids"]]
        # prefer: the exact package the camera uses, then complete packages, then the newest
        fits.sort(key=lambda p: (p not in exact, bool(p["missing"]), -os.path.getmtime(p["inf"])))
        if not fits:
            sys.exit(f"ERROR: no driver package supports this camera (USB\\VID_232F&PID_{pid}).\n"
                     "Pass --drivers-from with the Drivers folder of the Motic Images Plus installer.")
        best = fits[0]
        if best["missing"]:
            sys.exit(f"ERROR: the driver for this camera ({best['name']}) is incomplete, missing: "
                     f"{', '.join(best['missing'])}.\nPass --drivers-from with the Drivers folder of the "
                     "Motic Images Plus installer.")
        chosen[best["name"].lower()] = best
    if not cams:
        print("NOTE: no Motic camera is plugged in, so the build cannot check which driver it needs.")
    # 2. one complete copy of every other Motic driver, so other Motic camera models work too
    by_name = {}
    for p in pkgs:
        by_name.setdefault(p["name"].lower(), []).append(p)
    for name, group in sorted(by_name.items()):
        if name in chosen:
            continue
        complete = sorted((p for p in group if not p["missing"]), key=lambda p: -os.path.getmtime(p["inf"]))
        if complete:
            chosen[name] = complete[0]
        else:
            print(f"Skipping {group[0]['name']} (not for the connected camera): every copy found is incomplete, "
                  f"missing {', '.join(group[0]['missing'])}")
    for p in chosen.values():
        pids = ", ".join(sorted(p["pids"]))
        print(f"Bundling {p['name']} {p['version']}  (PID {pids})  from {p['dir']}")
    return list(chosen.values())


# --------------------------------------------------------------------------- build
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motic-dll")
    ap.add_argument("--driver-dir")
    ap.add_argument("--drivers-from", action="append", default=[],
                    help="folder to search for Motic drivers, e.g. the Motic Images Plus installer's Drivers folder")
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

    # 2. driver packages
    drivers = []                                  # (source folder, name of subfolder in the package)
    if not args.no_driver:
        if args.driver_dir:
            infs = glob.glob(os.path.join(args.driver_dir, "**", "*.inf"), recursive=True)
            if not infs:
                sys.exit(f"No .inf file in {args.driver_dir}")
            drivers.append((args.driver_dir, os.path.splitext(os.path.basename(infs[0]))[0]))
        else:
            for src in args.drivers_from:
                if not os.path.isdir(src):
                    sys.exit(f"--drivers-from: folder not found: {src}")
            used = set()
            for p in choose_driver_packages(args.drivers_from):
                sub = os.path.splitext(p["name"])[0]
                while sub.lower() in used:
                    sub += "_2"
                used.add(sub.lower())
                drivers.append((p["dir"], sub))
        if not drivers:
            print("WARNING: no Motic driver package found - building without a driver.")

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
    for src_dir, sub in drivers:
        shutil.copytree(src_dir, os.path.join(target, "driver", sub), dirs_exist_ok=True)
    if drivers:
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
