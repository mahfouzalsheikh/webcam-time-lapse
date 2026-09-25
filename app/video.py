"""Video timing shared by export metadata and the FFmpeg filter pipeline."""

import json

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


def export_filters(job, settings):
    filters = (f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease:flags=lanczos,"
               f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if job.get("interpolation", "none") != "none" and job["frames"] > 1:
        frames = export_details(job)["output_frames"]
        if job["interpolation"] == "repeat":
            # Hold each photo until the next one. No optical flow or crossfade;
            # trim the final hold to retain the shared between-photos duration.
            return (filters + ",tpad=stop_mode=clone:stop=1,"
                    f"fps=fps={job['fps']}:round=up,trim=end_frame={frames},setpts=PTS-STARTPTS")
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
