"""USB still capture via gPhoto2; no V4L2 stream or camera setting changes."""

import hashlib
import logging
import os
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

logger = logging.getLogger(__name__)
PREFIX = "gphoto2:"
USB_SYSFS = Path("/sys/bus/usb/devices")


def is_dslr(device):
    return device.startswith(PREFIX)


def run(arguments, *, timeout=15, cwd=None):
    try:
        result = subprocess.run(
            ["gphoto2", *arguments], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False,
            cwd=cwd, env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError as exc:
        raise RuntimeError("DSLR support requires gphoto2. Rebuild the Docker image or install gphoto2 on the host for native use.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("DSLR timed out. Check USB, camera power, focus and exposure time; close other camera apps.") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot run gphoto2: {exc}") from exc
    # Some Canon capture failures print errors but gPhoto2 still exits with 0.
    if result.returncode or re.search(r"(?m)^\s*(?:\*\*\* Error \*\*\*|ERROR:)", result.stderr):
        detail = (result.stderr or result.stdout)[-1500:].strip()
        if "Could not claim" in detail and "USB" in detail:
            raise RuntimeError("Camera USB connection is busy. Close photo importers and run ./fix-canon-usb.sh on the host to release the desktop camera mount and prevent automatic mounting. Recording will retry automatically. Check USB permissions if the problem persists.")
        raise RuntimeError(f"DSLR operation failed: {detail}. Check USB permissions, close camera apps or unmount the camera in your file manager, and check that its card has space.")
    return result.stdout


def usb_serial_ids():
    """Read kernel USB metadata without opening or claiming any camera."""
    identities = {}
    for device in USB_SYSFS.glob("*"):
        try:
            serial = (device / "serial").read_text().strip()
            if not serial or not serial.strip("0"):
                continue
            vendor = (device / "idVendor").read_text().strip().lower()
            product = (device / "idProduct").read_text().strip().lower()
            bus = int((device / "busnum").read_text())
            address = int((device / "devnum").read_text())
            if not re.fullmatch(r"[0-9a-f]{4}", vendor) or not re.fullmatch(r"[0-9a-f]{4}", product):
                continue
            identity = hashlib.sha256(f"{vendor}:{product}:{serial}".encode()).hexdigest()
            identities[f"usb:{bus:03},{address:03}"] = PREFIX + "serial:" + identity
        except (OSError, ValueError):
            continue  # Unplugged mid-enumeration, unreadable, or no serial number.
    # Duplicate serials cannot safely identify one physical camera.
    return {port: identity for port, identity in identities.items()
            if list(identities.values()).count(identity) == 1}


def stable_device(device):
    if device.startswith(PREFIX + "usb:"):
        return usb_serial_ids().get(device.removeprefix(PREFIX), device)
    return device


def discover():
    """USB enumeration only: do not claim the camera or trigger the shutter."""
    output = run(["--auto-detect"])
    identities = usb_serial_ids()
    devices = []
    for line in output.splitlines():
        match = re.fullmatch(r"(.+?)\s+(usb:[0-9]{3},[0-9]{3})\s*", line)
        if match:
            model, port = match.groups()
            legacy_id = PREFIX + port
            devices.append({"id": identities.get(port, legacy_id), "name": model.strip(),
                            "port": port, "aliases": [legacy_id],
                            "backend": "gphoto2", "available": True, "error": None})
    return devices


def list_devices():
    try:
        return discover()
    except RuntimeError as exc:
        # A missing DSLR dependency must not prevent webcam use.
        logger.warning("DSLR discovery unavailable: %s", exc)
        return []


def capabilities(device):
    # These are video output presets, not claims about the camera's sensor modes.
    modes = [{"width": w, "height": h, "input_format": "auto"}
             for w, h in [(3840, 2160), (1920, 1080), (1280, 720), (640, 480)]]
    return {"device": device, "backend": "gphoto2", "modes": modes,
            "recommended": modes[1], "resolution_scope": "export"}


def capture(path: Path, settings):
    selected = next((c for c in discover() if c["id"] == settings.camera_device
                     or settings.camera_device in c.get("aliases", [])), None)
    if selected is None:
        if settings.camera_device.startswith(PREFIX + "serial:"):
            raise RuntimeError("Selected DSLR is disconnected or its USB serial number is unavailable or ambiguous. Reconnect the same camera and turn it on; recording will retry automatically.")
        raise RuntimeError("Selected DSLR is unavailable. Turn it on, connect USB, then Refresh cameras and reselect it if its USB address changed.")
    # Keep all downloads separate from published frames, including RAW+JPEG pairs.
    # A relative pattern avoids interpreting percent signs in the data directory.
    with TemporaryDirectory(prefix=".dslr-", dir=path.parent) as directory:
        run(["--port", selected.get("port", selected["id"].removeprefix(PREFIX)),
             "--camera", selected["name"], "--filename", "capture-%n.%C",
             "--force-overwrite", "--keep", "--capture-image-and-download"],
            timeout=90, cwd=directory)
        jpegs = [p for p in Path(directory).iterdir() if p.suffix.lower() in {".jpg", ".jpeg"}]
        if len(jpegs) != 1:
            raise RuntimeError("DSLR did not return one JPEG photo. Set image quality to JPEG (Large/Fine recommended) or RAW+JPEG on the camera; RAW-only is unsupported.")
        try:
            with Image.open(jpegs[0]) as image:
                if image.format != "JPEG":
                    raise ValueError("Downloaded file is not JPEG")
                image.load()  # Detect truncated downloads before publishing.
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"DSLR returned an invalid JPEG: {exc}. Retry the capture.") from exc
        # Preserve full resolution and EXIF without another lossy JPEG encode.
        jpegs[0].replace(path)
