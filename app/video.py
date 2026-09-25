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
    fraction = f"min(on/{frames - 1},1)"
    ease = f"({fraction}*{fraction}*(3-2*{fraction}))"
    tx, ty = (max(0., min(1., value)) for value in target)
    # One output per input, including repeated/interpolated frames. Bound the
    # view to 1.00–1.20x and ease both ends; pan is limited to the available crop.
    zoom = f"1+0.2*{ease}"
    pan_x = f"clip(iw*(0.5+({tx:.6f}-0.5)*{ease})-iw/(2*zoom),0,iw-iw/zoom)"
    pan_y = f"clip(ih*(0.5+({ty:.6f}-0.5)*{ease})-ih/(2*zoom),0,ih-ih/zoom)"
    # 4:4:4 avoids chroma-grid jumps; modest supersampling smooths subpixel pans
    # at HD without allocating oversized 8K/16K working frames for 4K exports.
    sampling = 2 if max(width, height) <= 1920 else 1
    return (f",crop={width}:{height}:{x}:{y},format=yuv444p,"
            f"scale={width * sampling}:{height * sampling}:flags=lanczos,"
            f"zoompan=z='{zoom}':x='{pan_x}':y='{pan_y}':d=1:s={width}x{height}:fps={job['fps']},"
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
