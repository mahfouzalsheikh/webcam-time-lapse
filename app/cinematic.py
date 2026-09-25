"""Keep changing detail sharp while quietly softening the static background."""

from math import sqrt

from PIL import Image, ImageChops, ImageFilter, ImageOps


def scene_signature(path):
    """Small spatial signature, invariant to uniform exposure/contrast changes."""
    with Image.open(path) as source:
        source.draft('RGB', (192, 144))
        with ImageOps.exif_transpose(source) as oriented:
            aspect = oriented.width / oriented.height
            small = oriented.convert('L').resize((96, 72), Image.Resampling.BOX)
    values = list(small.filter(ImageFilter.GaussianBlur(1)).get_flattened_data())
    mean = sum(values) / len(values)
    # Do not amplify noise in plain walls or nearly black frames.
    deviation = max(12., sqrt(sum((value - mean) ** 2 for value in values) / len(values)))
    return aspect, [(value - mean) / deviation for value in values]


def scene_changes(paths, check, progress=None):
    """Compare captures once; threshold changes can reuse these compact scores."""
    report = progress or (lambda *args: None)
    changes, previous = [], None
    report('scene_analysis', 0, len(paths))
    for index, path in enumerate(paths):
        check()
        aspect, current = scene_signature(path)
        # Signed spatial gradients distinguish moved contours from broad light
        # changes. Blank walls contribute very little to this comparison.
        edges = [(current[y * 96 + x + 1] - current[y * 96 + x - 1],
                  current[(y + 1) * 96 + x] - current[(y - 1) * 96 + x])
                 for y in range(1, 71) for x in range(1, 95)]
        if previous is not None:
            old_aspect, old, old_edges = previous
            difference = [abs(a - b) for a, b in zip(old, current)]
            tiles = [
                sum(difference[y * 96 + x] for y in range(top, top + 12)
                    for x in range(left, left + 12)) / 144
                for top in range(0, 72, 12) for left in range(0, 96, 12)]
            edge_energy = sum(abs(a) + abs(b) + abs(c) + abs(d)
                              for (a, b), (c, d) in zip(old_edges, edges))
            edge_change = sum(abs(a - c) + abs(b - d)
                              for (a, b), (c, d) in zip(old_edges, edges)) / max(edge_energy, 1e-6)
            # Score every transition. Hard-filtering weaker changes here made
            # the slider unable to discover them at lower thresholds. Use the
            # weakest supporting evidence as the score instead: contour change,
            # average change and its spatial spread. At 45%, this retains the
            # original safeguards (.22 average, .30 across ten tiles, or .75
            # across 27 tiles), while lower values can accept smaller changes.
            ranked = sorted(tiles, reverse=True)
            reframing_score = min(sum(tiles) / len(tiles) * 45 / .22, ranked[9] * 45 / .30)
            broad_score = ranked[26] * 45 / .75
            score = min(edge_change * 100, max(reframing_score, broad_score))
            changes.append(dict(photo=index + 1, score_percent=score,
                                orientation_change=abs(aspect / old_aspect - 1) > .05))
        previous = aspect, current, edges
        report('scene_analysis', index + 1, len(paths))
    return changes


def reset_photos(changes, threshold_percent=45):
    return [change['photo'] for change in changes
            if change['orientation_change'] or (change['score_percent'] > 0
                                                and change['score_percent'] >= threshold_percent)]


def scene_ranges(paths, check, progress=None, threshold_percent=45):
    """Find structural cuts, with the same threshold rules used by the preview."""
    changes = scene_changes(paths, check, progress)
    starts = [0] + [photo - 1 for photo in reset_photos(changes, threshold_percent)]
    return list(zip(starts, starts[1:] + [len(paths)])) if paths else []


def detail_map(path):
    with Image.open(path) as source:
        source.thumbnail((192, 192), Image.Resampling.LANCZOS)
        gray = source.convert("L").filter(ImageFilter.GaussianBlur(.6))
    # Broad illumination changes have little high-frequency detail. Comparing
    # this map after lighting correction avoids treating exposure as motion.
    return ImageChops.difference(gray, gray.filter(ImageFilter.GaussianBlur(3)))


def focus_mask(paths, check, report):
    reference = activity = None
    report('focus_analysis', 0, len(paths))
    for index, path in enumerate(paths, 1):
        check()
        detail = detail_map(path)
        if reference is None:
            reference = detail
            activity = Image.new('L', detail.size)
        elif detail.size != reference.size:
            # A framing/aspect change is not reliable subject-motion evidence.
            report('focus_analysis', len(paths), len(paths))
            return None
        else:
            change = ImageChops.difference(reference, detail)
            activity = ImageChops.lighter(activity, change)
        report('focus_analysis', index, len(paths))
    if activity is None:
        return None
    changed = activity.point(lambda value: 255 if value >= 10 else 0)
    if changed.getbbox() is None:
        return None
    # Protect the neighborhood of fine stems/leaves as well as their edges.
    # The same feathered mask is used for every frame: no focus pumping.
    return changed.filter(ImageFilter.MaxFilter(21)).filter(ImageFilter.GaussianBlur(3))


def prepare_frames(paths, check, progress=None, scenes=None):
    """Process only temporary, already normalized PNGs; originals stay intact."""
    report = progress or (lambda stage, completed, total: None)
    scenes = scenes if scenes is not None else [(0, len(paths))]
    masks = []
    for start, end in scenes:
        mask = focus_mask(paths[start:end], check,
                          lambda stage, completed, total: report(stage, start + completed, len(paths)))
        masks.append(mask)
    shots = []
    for (start, end), mask in zip(scenes, masks):
        bounds = mask.point(lambda value: 255 if value >= 128 else 0).getbbox() if mask else None
        target = (.5, .5)
        if bounds:
            left, top, right, bottom = bounds
            target = (left + right) / (2 * mask.width), (top + bottom) / (2 * mask.height)
        shots.append(dict(start=start, end=end, target=target))
    report('focusing', 0, len(paths))
    shot_index = 0
    for index, path in enumerate(paths, 1):
        check()
        while index > scenes[shot_index][1]:
            shot_index += 1
        mask = masks[shot_index]
        if mask is not None:
            temporary = path.with_suffix('.focus.png')
            try:
                with Image.open(path) as source, source.convert('RGB') as sharp:
                    radius = max(1., min(sharp.size) * .008)
                    with sharp.filter(ImageFilter.GaussianBlur(radius)) as blurred:
                        with blurred.point([round(value * .72) for value in range(256)] * 3) as background:
                            with mask.resize(sharp.size, Image.Resampling.LANCZOS) as full_mask:
                                with Image.composite(sharp, background, full_mask) as result:
                                    result.save(temporary, 'PNG', compress_level=1)
                check()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        report('focusing', index, len(paths))
    # Keep one fixed focus and camera target per shot; never chase growing leaves.
    return shots
