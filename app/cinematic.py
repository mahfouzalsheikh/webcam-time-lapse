"""Keep changing detail sharp while quietly softening the static background."""

from PIL import Image, ImageChops, ImageFilter


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


def prepare_frames(paths, check, progress=None):
    """Process only temporary, already normalized PNGs; originals stay intact."""
    report = progress or (lambda stage, completed, total: None)
    mask = focus_mask(paths, check, report)
    report('focusing', 0, len(paths))
    for index, path in enumerate(paths, 1):
        check()
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
