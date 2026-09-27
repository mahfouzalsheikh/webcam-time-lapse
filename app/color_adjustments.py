"""Shared, non-destructive brightness/contrast adjustment for previews and exports."""

from io import BytesIO

from PIL import Image, ImageOps


def enabled(options):
    return options.get('brightness', 0) != 0 or options.get('contrast', 100) != 100


def adjustment_lut(brightness=0, contrast=100):
    return [max(0, min(255, round((value - 127.5) * contrast / 100 + 127.5 + brightness * 255 / 100)))
            for value in range(256)] * 3


def preview(jpeg, brightness=0, contrast=100):
    with Image.open(BytesIO(jpeg)) as source, source.convert('RGB') as image:
        with image.point(adjustment_lut(brightness, contrast)) as adjusted:
            output = BytesIO()
            adjusted.save(output, 'PNG')
            return output.getvalue()


def prepare_frames(paths, directory, check, brightness=0, contrast=100, progress=None):
    report = progress or (lambda *args: None)
    lut = adjustment_lut(brightness, contrast)
    report('grading', 0, len(paths))
    for index, path in enumerate(paths, 1):
        check()
        destination = directory / f'{path.stem}.png'
        temporary = directory / f'{path.stem}.grade.png'
        try:
            with Image.open(path) as source, ImageOps.exif_transpose(source) as oriented:
                with oriented.convert('RGB') as image, image.point(lut) as adjusted:
                    adjusted.save(temporary, 'PNG', compress_level=1)
            check()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        report('grading', index, len(paths))
