"""Video timing shared by export metadata and the FFmpeg filter pipeline."""

import json
from math import ceil, floor

from .export_progress import progress_details


def output_frame_count(photos, interpolation="none", intermediate_frames=0):
    extra = intermediate_frames if interpolation != "none" else 0
    return photos + max(0, photos - 1) * extra


def export_details(job):
    job = dict(job)
    settings = json.loads(job.pop("settings", None) or "{}")
    frames = output_frame_count(job["frames"], job.get("interpolation", "none"),
                                job.get("intermediate_frames", 0))
    return {**job, "width": settings.get("width"), "height": settings.get("height"),
            "output_frames": frames, "duration_seconds": frames / job["fps"],
            "progress": progress_details(job)}


def camera_filters(job, settings, target, bounds):
    frames = output_frame_count(job['frames'], job.get('interpolation', 'none'), job.get('intermediate_frames', 0))
    if frames < 2:
        return ""
    left, top, width, height = bounds or (0, 0, settings.width, settings.height)
    # Crop only inside the fitted photo, leaving letterbox bars stationary.
    x, y = 2 * ceil(left / 2), 2 * ceil(top / 2)
    width = 2 * floor((left + width) / 2) - x
    height = 2 * floor((top + height) / 2) - y
    if min(width, height) < 2:
        return ""
    # Perspective counts output frames from one, unlike zoompan's zero-based on.
    fraction = f"clip((on-1)/{frames - 1},0,1)"
    ease = f"({fraction}*{fraction}*(3-2*{fraction}))"
    tx, ty = (max(0., min(1., value)) for value in target)
    # Clamp the destination view once, then ease all four source corners toward
    # it. This keeps the path inside the photo without hitting a pan limit partway
    # through the move. The final view is 1/1.2 of the original in both dimensions.
    inset = 1 - 1 / 1.2
    end_x = max(0., min(inset, tx - 1 / 2.4))
    end_y = max(0., min(inset, ty - 1 / 2.4))
    left_edge = f"W*{end_x:.12f}*{ease}"
    right_edge = f"W*(1-{inset - end_x:.12f}*{ease})"
    top_edge = f"H*{end_y:.12f}*{ease}"
    bottom_edge = f"H*(1-{inset - end_y:.12f}*{ease})"
    # zoompan rounds crop positions and dimensions to whole pixels, even with
    # supersampling. Perspective's cubic resampler retains fractional coordinates
    # for a smooth affine pan/zoom at every resolution, one output per input.
    # 4:4:4 also keeps color planes on the same sampling grid during the transform.
    return (f",crop={width}:{height}:{x}:{y},format=yuv444p,"
            f"perspective=x0='{left_edge}':y0='{top_edge}':"
            f"x1='{right_edge}':y1='{top_edge}':x2='{left_edge}':y2='{bottom_edge}':"
            f"x3='{right_edge}':y3='{bottom_edge}':sense=source:eval=frame:interpolation=cubic,"
            f"pad={settings.width}:{settings.height}:{x}:{y},setsar=1")


def export_filters(job, settings, cinematic_target=(.5, .5), content_bounds=None):
    filters = (f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease:flags=lanczos,"
               f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if job.get("interpolation", "none") != "none" and job["frames"] > 1:
        frames = export_details(job)["output_frames"]
        if job["interpolation"] == "repeat":
            # Hold each photo until the next one. No optical flow or crossfade;
            # trim the final hold to retain the shared between-photos duration.
            filters += (",tpad=stop_mode=clone:stop=1,"
                        f"fps=fps={job['fps']}:round=up,trim=end_frame={frames},setpts=PTS-STARTPTS")
        else:
            mode = "blend" if job["interpolation"] == "blend" else "mci"
            factor = job['intermediate_frames'] + 1
            # Supply context at both ends, including motion estimation for the first
            # gap. Smaller bilateral blocks follow fine edges; detected cuts use
            # captured frames instead of inventing motion between scenes.
            options = (":mc_mode=aobmc:me_mode=bilat:me=epzs:mb_size=8:vsbmc=1"
                       ":scd=fdiff:scd_threshold=10" if mode == "mci" else ":scd=none")
            filters += (",format=yuv420p,tpad=start_mode=clone:start=1:stop_mode=clone:stop=2,"
                        f"minterpolate=fps={job['fps']}:mi_mode={mode}{options},"
                        f"trim=start_frame={factor}:end_frame={frames + factor},setpts=PTS-STARTPTS")
    if job.get('cinematic_focus'):
        filters += camera_filters(job, settings, cinematic_target, content_bounds)
    return filters
