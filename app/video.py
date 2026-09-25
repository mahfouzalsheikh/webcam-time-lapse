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


def camera_filters(job, settings, bounds, frames=None):
    frames = frames if frames is not None else output_frame_count(job['frames'], job.get('interpolation', 'none'), job.get('intermediate_frames', 0))
    zoom = 1 + job.get('cinematic_zoom_percent', 20) / 100
    if frames < 2 or zoom == 1:
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
    # Move opposite crop edges inward by equal amounts. The image center stays
    # fixed even when growth or lighting activity is concentrated on one side.
    inset = (1 - 1 / zoom) / 2
    left_edge = f"W*{inset:.12f}*{ease}"
    right_edge = f"W*(1-{inset:.12f}*{ease})"
    top_edge = f"H*{inset:.12f}*{ease}"
    bottom_edge = f"H*(1-{inset:.12f}*{ease})"
    # zoompan rounds crop positions and dimensions to whole pixels, even with
    # supersampling. Perspective's cubic resampler retains fractional coordinates
    # for a smooth affine pan/zoom at every resolution, one output per input.
    # 4:4:4 also keeps color planes on the same sampling grid during the transform.
    return (f",crop={width}:{height}:{x}:{y},format=yuv444p,"
            f"perspective=x0='{left_edge}':y0='{top_edge}':"
            f"x1='{right_edge}':y1='{top_edge}':x2='{left_edge}':y2='{bottom_edge}':"
            f"x3='{right_edge}':y3='{bottom_edge}':sense=source:eval=frame:interpolation=cubic,"
            f"pad={settings.width}:{settings.height}:{x}:{y},setsar=1")


def interpolation_filters(job):
    filters = ''
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
    return filters


def export_filters(job, settings, content_bounds=None, shots=None):
    filters = (f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease:flags=lanczos:eval=frame,"
               f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2:eval=frame,setsar=1")
    if not job.get('cinematic_focus') or not shots or len(shots) == 1:
        filters += interpolation_filters(job)
        if job.get('cinematic_focus'):
            filters += camera_filters(job, settings, content_bounds)
        return filters

    # Interpolate within each shot, never across a detected cut. Keep the same
    # frame count/timeline by holding the outgoing photo for the inter-shot gap.
    # Each branch has its own frame counter, so the incoming shot starts at 1x.
    factor = job.get('intermediate_frames', 0) + 1 if job.get('interpolation', 'none') != 'none' else 1
    filters += f",split={len(shots)}" + ''.join(f"[shot{i}]" for i in range(len(shots)))
    for i, shot in enumerate(shots):
        count = shot['end'] - shot['start']
        local = {**job, 'frames': count}
        frames = output_frame_count(count, job.get('interpolation', 'none'), job.get('intermediate_frames', 0))
        hold = factor - 1 if i < len(shots) - 1 else 0
        filters += f";[shot{i}]trim=start_frame={shot['start']}:end_frame={shot['end']},setpts=PTS-STARTPTS"
        filters += interpolation_filters(local)
        if hold:
            filters += f",tpad=stop_mode=clone:stop={hold}"
        # fps establishes the correct final-frame duration for concat, including
        # one-photo shots. Reset PTS before it so no source frames get duplicated.
        filters += f",settb=AVTB,setpts=N/({job['fps']}*TB),fps={job['fps']}:round=near"
        filters += camera_filters(local, settings, shot.get('bounds', content_bounds), frames + hold)
        filters += f"[move{i}]"
    filters += ';' + ''.join(f"[move{i}]" for i in range(len(shots)))
    return filters + f"concat=n={len(shots)}:v=1:a=0,setsar=1"
