"""Match exposure and color between stills without modifying source photos."""

from statistics import median
from math import log2
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import os

from PIL import Image, ImageOps


def scene_samples(path):
    """Keep spatial correspondence; a growing subject must not meter the scene."""
    # Reduced JPEG decoding keeps analysis inexpensive for full-resolution DSLRs.
    with Image.open(path) as source:
        source.draft("RGB", (256, 256))
        with ImageOps.exif_transpose(source) as oriented:
            with oriented.convert("RGB") as rgb:
                with rgb.resize((24, 18), Image.Resampling.BOX) as small:
                    return list(small.get_flattened_data())


def lighting_gains(samples, check):
    if not samples:
        return []
    # A separate temporal reference for every tile retains scene structure.
    # Ratios from unchanged tiles agree even when exposure/white balance changes;
    # moving leaves and local shadows become outliers instead of changing a mean.
    reference = [[median(channel) for channel in zip(*tile)]
                 for tile in zip(*samples)]
    offsets = []
    for frame in samples:
        check()
        ratios = [tuple(log2(actual / desired) for actual, desired in zip(tile, target))
                  for tile, target in zip(frame, reference)
                  if all(12 < value < 240 for value in (*tile, *target))]
        if len(ratios) < 12:
            offsets.append(None)
            continue
        center = [median(channel) for channel in zip(*ratios)]
        inliers = [ratio for ratio in ratios
                   if max(abs(a - b) for a, b in zip(ratio, center)) < .12]
        # No coherent background evidence: leave this photo alone.
        offsets.append([median(channel) for channel in zip(*inliers)]
                       if len(inliers) >= max(12, len(ratios) // 3) else None)
    usable = [offset for offset in offsets if offset is not None]
    anchor = [median(channel) for channel in zip(*usable)] if usable else [0.] * 3
    return [[2 ** max(-1., min(1., typical - offset))
             for typical, offset in zip(anchor, row)] if row is not None else [1.] * 3
            for row in offsets]


def correction_lut(gains):
    lut = []
    for gain in gains:
        # Roll off amplified highlights instead of flattening them at 255.
        knee = 192 / gain if gain > 1 else 255
        for value in range(256):
            corrected = value * gain
            if value > knee:
                t = (value - knee) / (255 - knee)
                slope = gain * (255 - knee) / 63
                corrected = 192 + 63 * (slope * t / (1 + (slope - 1) * t))
            lut.append(max(0, min(255, round(corrected))))
    return lut


def correct_frame(path, gains, directory):
    lut = correction_lut(gains)
    with Image.open(path) as source, ImageOps.exif_transpose(source) as oriented:
        with oriented.convert("RGB") as rgb:
            # Keep original detail. Both effect-free and corrected exports are
            # resized once, by the same FFmpeg filter during encoding.
            with rgb.point(lut) as corrected:
                corrected.save(directory / f"{path.stem}.png", "PNG", compress_level=1)


def prepare_frames(paths, directory, check, progress=None, scenes=None):
    """Write corrected frames with at most four images being processed at once."""
    samples = []
    report = progress or (lambda stage, completed, total: None)
    report('analyzing', 0, len(paths))
    for index, path in enumerate(paths, 1):
        check()
        samples.append(scene_samples(path))
        report('analyzing', index, len(paths))
    # A new camera angle gets its own exposure reference, rather than matching
    # unrelated parts of the previous shot.
    gains = []
    for start, end in scenes if scenes is not None else [(0, len(paths))]:
        gains.extend(lighting_gains(samples[start:end], check))
    del samples
    report('normalizing', 0, len(paths))
    workers = min(4, max(1, os.cpu_count() or 1), max(1, len(paths)))
    remaining = iter(zip(paths, gains))
    completed = 0
    pending = set()
    # Keep only one image per worker in flight. Checks and progress stay on the
    # calling thread; shutdown waits for writers before export cleanup runs.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            while True:
                check()
                while len(pending) < workers:
                    item = next(remaining, None)
                    if item is None:
                        break
                    pending.add(pool.submit(correct_frame, *item, directory))
                if not pending:
                    break
                done, pending = wait(pending, timeout=.25, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                    completed += 1
                    check()
                    report('normalizing', completed, len(paths))
        finally:
            for future in pending:
                future.cancel()
