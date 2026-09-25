"""A small elapsed-capture-time dial, drawn after all photo interpolation."""

from math import ceil, cos, pi, sin, tan

from PIL import Image

from .video import output_frame_count


def elapsed_times(timestamps, interpolation, extra):
    """Map video frames to capture time, preserving real gaps and endpoints."""
    if not timestamps:
        return
    origin = timestamps[0]
    factor = extra + 1 if interpolation != "none" else 1
    for start, end in zip(timestamps, timestamps[1:]):
        for step in range(factor):
            fraction = step / factor if interpolation in ("blend", "motion") else 0
            yield max(0., start - origin + (end - start) * fraction)
    yield max(0., timestamps[-1] - origin)


def ass_time(frame, fps):
    # Floor boundaries to centiseconds so each video frame (up to 60 fps)
    # falls inside its own event, including at fractional frame rates like 24.
    ticks = frame * 100 // fps
    seconds, fraction = divmod(ticks, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"


def ring_path(cx, cy, radius, thickness, fraction):
    """Closed ring segment, using cubic arcs instead of thousands of vertices."""
    if fraction <= 0:
        return ""
    sweep = min(1., fraction) * 2 * pi
    count = max(1, ceil(sweep / (pi / 2)))

    def point(r, angle):
        return cx + r * cos(angle), cy + r * sin(angle)

    def coordinates(points):
        return " ".join(f"{value:.2f}" for pair in points for value in pair)

    path = "m " + coordinates([point(radius, -pi / 2)])
    for r, start, delta in ((radius, -pi / 2, sweep / count),
                             (radius - thickness, -pi / 2 + sweep, -sweep / count)):
        if r != radius:
            path += " l " + coordinates([point(r, start)])
        for segment in range(count):
            a, b = start + segment * delta, start + (segment + 1) * delta
            k = 4 / 3 * tan(delta / 4)
            x0, y0 = point(r, a)
            x1, y1 = point(r, b)
            path += " b " + coordinates([(x0 - k * r * sin(a), y0 + k * r * cos(a)),
                                         (x1 + k * r * sin(b), y1 - k * r * cos(b)), (x1, y1)])
    return path + " c"


def photo_bounds(paths, settings, check):
    """Intersection of fitted photo areas, excluding export letterbox bars."""
    width, height = settings.width, settings.height
    for path in paths:
        check()
        with Image.open(path) as source:
            w, h = source.size
            if source.getexif().get(274) in (5, 6, 7, 8):
                w, h = h, w
        ratio = min(settings.width / w, settings.height / h)
        width, height = min(width, w * ratio), min(height, h * ratio)
    return (settings.width - width) / 2, (settings.height - height) / 2, width, height


def overall_progress(elapsed, duration):
    return min(1., max(0., elapsed / duration)) if duration > 0 else 1.


def write_overlay(path, timestamps, job, settings, check, progress=None, content_bounds=None):
    """Write bounded-memory ASS events; repeated photos hold their capture time."""
    mode, extra, fps = job.get("interpolation", "none"), job.get("intermediate_frames", 0), job["fps"]
    total = output_frame_count(len(timestamps), mode, extra)
    report = progress or (lambda stage, completed, total: None)
    report('overlay', 0, total)
    left, top, width, height = content_bounds or (0, 0, settings.width, settings.height)
    # Keep the complete dial inside every selected photo, including portraits
    # and unusually narrow images. Leave a margin on all sides of the circle.
    scale = min(min(settings.width, settings.height) / 1080, width / 236, height / 236)
    margin = 24 * scale
    cx, cy = left + margin + 94 * scale, top + margin + 94 * scale
    duration = max(0., timestamps[-1] - timestamps[0]) if timestamps else 0
    # ASS colors use BGR: cyan for the day, amber for overall capture progress.
    day_color, progress_color, track_color = "DAC35B", "75C6FF", "61534A"

    def drawing(shape, color, alpha="00"):
        return rf"{{\an7\pos(0,0)\p1\1c&H{color}&\1a&H{alpha}&}}{shape}"

    def label(text, y, size, color="F4F6F7", bold=0):
        return (rf"{{\an5\pos({cx:.2f},{cy + y * scale:.2f})"
                rf"\fs{size * scale:.2f}\b{bold}\1c&H{color}&}}{text}")

    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {settings.width}
PlayResY: {settings.height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Dial,DejaVu Sans,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""")

        def event(layer, start, end, content):
            if content:
                handle.write(f"Dialogue: {layer},{ass_time(start, fps)},{ass_time(end, fps)},Dial,,0,0,0,,{content}\n")

        # A translucent disk stops at the inside edge of the outer ring.
        disk = ring_path(cx, cy, 87 * scale, 87 * scale, 1)
        event(0, 0, total, drawing(disk, "19130E", "50"))
        tracks = ring_path(cx, cy, 94 * scale, 7 * scale, 1) + " " + ring_path(cx, cy, 77 * scale, 7 * scale, 1)
        event(1, 0, total, drawing(tracks, track_color))
        event(2, 0, total, label("DAY", -31, 15))
        # Coalesce unchanged text/arcs, particularly when Repeat photos is used.
        active = {}
        for index, elapsed in enumerate(elapsed_times(timestamps, mode, extra)):
            if index % 120 == 0:
                check()
                report('overlay', index, total)
            minutes = int(elapsed // 60)
            days, minute_of_day = divmod(minutes, 1440)
            hours, minutes = divmod(minute_of_day, 60)
            content = (
                drawing(ring_path(cx, cy, 94 * scale, 7 * scale, elapsed % 86400 / 86400), day_color) if elapsed % 86400 else "",
                drawing(ring_path(cx, cy, 77 * scale, 7 * scale, overall_progress(elapsed, duration)), progress_color),
                label(str(days + 1), -4, 42 if days < 999 else 32, bold=1),
                label(f"{days}d {hours:02}h {minutes:02}m", 29, 16),
            )
            for layer, value in enumerate(content, 3):
                start, previous = active.get(layer, (index, value))
                if previous != value:
                    event(layer, start, index, previous)
                    start = index
                active[layer] = start, value
        for layer, (start, value) in active.items():
            event(layer, start, total, value)
        check()
        report('overlay', total, total)
