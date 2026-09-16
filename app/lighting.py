"""Match exposure and color between stills without modifying source photos."""

from statistics import median

from PIL import Image, ImageOps


def channel_levels(path):
    # Reduced JPEG decoding keeps analysis inexpensive for full-resolution DSLRs.
    with Image.open(path) as source:
        source.draft("RGB", (256, 256))
        source.thumbnail((256, 256))
        histogram = source.convert("RGB").histogram()
    levels = []
    for channel in range(3):
        counts = histogram[channel * 256:(channel + 1) * 256]
        total = sum(counts)
        low, high = total * .05, total * .95
        cumulative = weighted = 0
        # Trim the darkest/brightest 5% so small highlights don't drive correction.
        for value, count in enumerate(counts):
            kept = max(0, min(cumulative + count, high) - max(cumulative, low))
            weighted += value * kept
            cumulative += count
        levels.append(weighted / (high - low))
    return levels


def prepare_frames(paths, directory, size, check):
    """Write lossless, export-sized corrected frames; retain only stats in memory."""
    levels = []
    for path in paths:
        check()
        levels.append(channel_levels(path))
    # Preserve the scene's typical color cast rather than assuming it is gray.
    # Near-black/white frames cannot provide a useful reference.
    usable = [level for level in levels if 8 < sum(level) / 3 < 247]
    target = [median(channel) for channel in zip(*usable)] if usable else None
    for path, level in zip(paths, levels):
        check()
        gains = [max(.5, min(2., desired / max(actual, 1.)))
                 for desired, actual in zip(target, level)] if target else [1.] * 3
        lut = [min(255, round(value * gain)) for gain in gains for value in range(256)]
        with Image.open(path) as source, ImageOps.exif_transpose(source) as oriented:
            with oriented.convert("RGB") as rgb:
                with ImageOps.contain(rgb, size, Image.Resampling.LANCZOS) as resized:
                    with resized.point(lut) as corrected:
                        corrected.save(directory / f"{path.stem}.png", "PNG")
        check()
