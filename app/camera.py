import math
from contextlib import contextmanager
import errno
import os
import fcntl
import re
import stat
import struct
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageDraw

from .models import Settings


def device_path(logical: str):
    return Path(os.environ.get("CAMERA_DEVICE_ROOT", "/dev")) / logical.removeprefix("/dev/")


def logical_path(path):
    root = os.environ.get("CAMERA_DEVICE_ROOT", "/dev").rstrip("/")
    return "/dev/" + str(path).removeprefix(root + "/")


def probe_device(path: Path):
    """Query capabilities without starting a stream or changing camera settings."""
    info = path.stat()
    if not stat.S_ISCHR(info.st_mode) or os.major(info.st_rdev) != 81:
        raise OSError("Not a Linux video device")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        capability = bytearray(104)
        fcntl.ioctl(descriptor, 0x80685600, capability, True)  # VIDIOC_QUERYCAP
    finally:
        os.close(descriptor)
    _, card, _, _, caps, device_caps, _, _, _ = struct.unpack("16s32s32s6I", capability)
    caps = device_caps if caps & 0x80000000 else caps
    return {"name": card.split(b"\0", 1)[0].decode(errors="replace"), "capture": bool(caps & 1)}


def list_devices(demo=False):
    if demo:
        return [{"id": "/dev/video0", "name": "Demo camera (synthetic)", "available": True, "error": None}]
    devices, seen = [], set()
    # Prefer a stable USB ID where the host provides one; retain numeric paths as aliases.
    paths = sorted(device_path("/dev/v4l/by-id").glob("*"), key=str) + sorted(device_path("/dev/").glob("video[0-9]*"), key=str)
    for path in paths:
        logical = logical_path(path)
        if not re.fullmatch(r"/dev/(video[0-9]+|v4l/by-id/[A-Za-z0-9_.+-]+)", logical):
            continue
        try:
            device_number = path.stat().st_rdev
            if device_number in seen:
                continue
            info = probe_device(path)
            if not info["capture"]:
                continue  # Metadata/output nodes are not webcams.
            available, error, name = True, None, info["name"] or path.name
        except FileNotFoundError:
            continue
        except OSError as exc:
            available, error, name = False, str(exc), path.name
            device_number = str(path)
        aliases = [logical]
        if available:
            for alias in device_path("/dev/").glob("video[0-9]*"):
                try:
                    if alias.stat().st_rdev == device_number:
                        aliases.append(logical_path(alias))
                except OSError:
                    pass
        seen.add(device_number)
        devices.append({"id": logical, "name": name, "available": available, "error": error,
                        "aliases": sorted(set(aliases))})
    return devices


PIXEL_FORMATS = {
    b"MJPG": "mjpeg", b"JPEG": "mjpeg", b"YUYV": "yuyv422", b"UYVY": "uyvy422",
    b"NV12": "nv12", b"NV21": "nv21", b"RGB3": "rgb24", b"BGR3": "bgr24",
    b"GREY": "gray", b"YU12": "yuv420p", b"H264": "h264",
}


def _enum_ioctl(fd, request, buffer):
    try:
        fcntl.ioctl(fd, request, buffer, True)
        return True
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTTY):
            return False
        raise


def _largest_step(minimum, maximum, step):
    step = max(1, step)
    maximum = min(maximum, 16384)
    value = minimum + ((maximum - minimum) // step) * step
    if value % 2:
        value -= step
    return value if value >= minimum and value % 2 == 0 else 0


def capabilities(logical: str, demo=False):
    """Enumerate actual capture formats and sizes without changing the active stream."""
    if demo:
        modes = [{"width": w, "height": h, "input_format": "mjpeg"} for w, h in [(1920,1080), (1280,720), (640,480)]]
        return {"device": logical, "modes": modes, "recommended": modes[0]}
    path = device_path(logical)
    try:
        if not probe_device(path)["capture"]:
            raise OSError("This device does not support video capture")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        modes = set()
        try:
            for index in range(256):
                fmt = bytearray(64)
                struct.pack_into("II", fmt, 0, index, 1)  # VIDEO_CAPTURE
                if not _enum_ioctl(fd, 0xC0405602, fmt):  # VIDIOC_ENUM_FMT
                    break
                fourcc = bytes(fmt[44:48])
                if fourcc not in PIXEL_FORMATS:
                    continue
                for size_index in range(1024):
                    size = bytearray(44)
                    struct.pack_into("II", size, 0, size_index, struct.unpack_from("I", fmt, 44)[0])
                    if not _enum_ioctl(fd, 0xC02C564A, size):  # VIDIOC_ENUM_FRAMESIZES
                        break
                    kind = struct.unpack_from("I", size, 8)[0]
                    if kind == 1:
                        width, height = struct.unpack_from("II", size, 12)
                    elif kind in (2, 3):
                        min_w, max_w, step_w, min_h, max_h, step_h = struct.unpack_from("6I", size, 12)
                        width = _largest_step(min_w, max_w, step_w)
                        height = _largest_step(min_h, max_h, step_h)
                    else:
                        continue
                    if 320 <= width <= 16384 and 240 <= height <= 16384 and width % 2 == height % 2 == 0:
                        modes.add((width, height, PIXEL_FORMATS[fourcc]))
                    if kind in (2, 3):
                        break
        finally:
            os.close(fd)
    except OSError as exc:
        raise RuntimeError(f"Cannot detect camera resolution: {exc}. You can still choose a resolution manually.") from exc
    ordered = sorted(modes, key=lambda m: (m[0]*m[1], m[2] == "mjpeg", m[0], m[1]), reverse=True)
    results = [{"width": w, "height": h, "input_format": fmt} for w,h,fmt in ordered]
    return {"device": logical, "modes": results, "recommended": results[0] if results else None}


# Standard V4L2 camera controls (linux/v4l2-controls.h).
FOCUS_AUTO = 0x009A090C
FOCUS_START = 0x009A091C
FOCUS_ABSOLUTE = 0x009A090A
QUERYCTRL = 0xC0445624
G_CTRL = 0xC008561B
S_CTRL = 0xC008561C


def focus_control(fd, control_id):
    buffer = bytearray(68)
    struct.pack_into("I", buffer, 0, control_id)
    if not _enum_ioctl(fd, QUERYCTRL, buffer):
        return False
    flags = struct.unpack_from("I", buffer, 56)[0]
    return not flags & (0x0001 | 0x0004)  # Disabled or read-only.


def focus_capabilities(logical, demo=False):
    if demo:
        return {"autofocus": False, "single_shot": False, "manual": False, "demo": True}
    try:
        if not probe_device(device_path(logical))["capture"]:
            raise OSError("This device does not support video capture")
        fd = os.open(device_path(logical), os.O_RDONLY | os.O_NONBLOCK)
        try:
            return {"autofocus": focus_control(fd, FOCUS_AUTO),
                    "single_shot": focus_control(fd, FOCUS_START),
                    "manual": focus_control(fd, FOCUS_ABSOLUTE), "demo": False}
        finally:
            os.close(fd)
    except OSError as exc:
        raise RuntimeError(f"Cannot check camera focus controls: {exc}") from exc


def set_focus_control(fd, value):
    buffer = bytearray(struct.pack("Ii", FOCUS_AUTO, value))
    fcntl.ioctl(fd, S_CTRL, buffer, True)


@contextmanager
def prepare_focus(settings):
    """Enable continuous autofocus for the same stream used to take the photo.

    Keep the control handle open until capture completes, then restore its original
    value so previews and other projects do not inherit this project's choice.
    Unsupported cameras retain the existing capture behavior.
    """
    if not settings.autofocus:
        yield settings.warmup_seconds
        return
    fd = None
    original = None
    try:
        fd = os.open(device_path(settings.camera_device), os.O_RDWR | os.O_NONBLOCK)
        supported = focus_control(fd, FOCUS_AUTO)
        if supported:
            buffer = bytearray(struct.pack("Ii", FOCUS_AUTO, 0))
            fcntl.ioctl(fd, G_CTRL, buffer, True)
            original = struct.unpack_from("i", buffer, 4)[0]
            # Restart autofocus for every shot, including cameras shared by projects.
            set_focus_control(fd, 0)
            set_focus_control(fd, 1)
        yield max(settings.warmup_seconds, settings.focus_settle_seconds) if supported else settings.warmup_seconds
    except OSError as exc:
        raise RuntimeError(f"Camera autofocus failed: {exc}. Retry or turn off autofocus in Settings.") from exc
    finally:
        if fd is not None:
            try:
                if original is not None:
                    set_focus_control(fd, original)
            except OSError as exc:
                raise RuntimeError(f"Could not restore the camera's focus setting: {exc}") from exc
            finally:
                os.close(fd)


def capture(path: Path, settings: Settings, demo: bool):
    if demo:
        # A clearly labeled synthetic camera source for development and hardware-free setup.
        image = Image.new("RGB", (settings.width, settings.height), "#e1e7dc")
        draw = ImageDraw.Draw(image)
        w, h = image.size
        draw.rectangle((0, h * .77, w, h), fill="#c1cbb6")
        draw.ellipse((w * .33, h * .85, w * .67, h * .95), fill="#a3af99")
        draw.polygon([(w * .39, h * .64), (w * .61, h * .64), (w * .58, h * .89), (w * .42, h * .89)], fill="#b87250")
        draw.ellipse((w * .39, h * .60, w * .61, h * .68), fill="#744b35")
        sway = math.sin(time.time() / 10) * w * .015
        draw.line((w * .5, h * .64, w * .5 + sway, h * .23), fill="#456c3d", width=max(3, w // 180))
        for x, y, flip in [(.5, .31, -1), (.5, .44, 1), (.5, .55, -1)]:
            cx = w * x + sway
            cy = h * y
            draw.ellipse((cx - w * .16 if flip < 0 else cx, cy - h * .12,
                          cx if flip < 0 else cx + w * .16, cy + h * .02), fill="#507747")
        draw.text((20, 20), "DEMO CAMERA / " + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), fill="#35513c")
        image.save(path, "JPEG", quality=92)
        return
    try:
        info = probe_device(device_path(settings.camera_device))
        if not info["capture"]:
            raise OSError("The selected device does not support video capture")
    except OSError as exc:
        raise RuntimeError(f"Selected webcam {settings.camera_device} is unavailable: {exc}. Select an available webcam in Capture settings.") from exc
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "v4l2"]
    if settings.input_format != "auto":
        command += ["-input_format", settings.input_format]
    command += ["-video_size", f"{settings.width}x{settings.height}", "-i",
                str(device_path(settings.camera_device)), "-ss", str(settings.warmup_seconds),
                "-frames:v", "1", "-q:v", "2", "-threads", "1", "-update", "1", str(path)]
    try:
        with prepare_focus(settings) as settle_seconds:
            # -ss is after -i: discard frames while this stream is running, allowing
            # the lens and exposure to settle before saving the final frame.
            command[command.index("-ss") + 1] = str(settle_seconds)
            result = subprocess.run(command, capture_output=True, timeout=30 + settle_seconds, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Camera timed out. Check the USB connection and camera format.")
    if result.returncode:
        raise RuntimeError("Camera capture failed: " + result.stderr.decode(errors="replace")[-1500:])
    with Image.open(path) as image:
        image.verify()
