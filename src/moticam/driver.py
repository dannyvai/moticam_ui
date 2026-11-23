"""
Helpers for the Motic camera driver: is it installed, and installing the copy
that ships with the standalone build (folder "driver" next to the .exe).
"""

import json
import os
import subprocess
import sys

MOTIC_VID = "VID_232F"
CREATE_NO_WINDOW = 0x08000000


def app_dir():
    """Folder of the standalone .exe, or the project root when run from source."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def bundled_driver_dir():
    d = os.path.join(app_dir(), "driver")
    if os.path.isdir(d) and any(f.lower().endswith(".inf") for _, _, fs in os.walk(d) for f in fs):
        return d
    return None


def installer_script():
    for name in ("install_driver.ps1", os.path.join("scripts", "install_driver.ps1")):
        p = os.path.join(app_dir(), name)
        if os.path.isfile(p):
            return p
    return None


def motic_devices():
    """[(status, friendly name, instance id)] for Motic USB devices that are plugged in."""
    if sys.platform != "win32":
        return []
    ps = ("Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
          f"Where-Object {{ $_.InstanceId -match '{MOTIC_VID}' }} | "
          "Select-Object Status, FriendlyName, InstanceId | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=30, creationflags=CREATE_NO_WINDOW).stdout.strip()
    except Exception:
        return []
    if not out:
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    return [(d.get("Status") or "", d.get("FriendlyName") or "", d.get("InstanceId") or "") for d in data]


def driver_missing():
    """True when a Motic camera is plugged in but Windows has no working driver for it."""
    devs = motic_devices()
    return bool(devs) and all(s.upper() != "OK" for s, _, _ in devs)


def run_installer(force=False):
    """Start the driver installer with administrator rights (shows the UAC prompt)."""
    script = installer_script()
    if not script:
        raise FileNotFoundError("install_driver.ps1 not found next to the program.")
    import ctypes
    args = f'-NoProfile -ExecutionPolicy Bypass -File "{script}"' + (" -Force" if force else "")
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", args, os.path.dirname(script), 1)
    if rc <= 32:
        raise OSError("The installer did not start (administrator permission was refused?).")
