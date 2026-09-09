# Grow · Plant time-lapse

A self-hosted app for recording plant growth over weeks or months. Manage multiple projects, each with its own webcam, schedule, photos, and downloadable MP4 videos. FastAPI, SQLite, and FFmpeg run in a single Docker container; photos stay on your machine.

## Start the app

On a Linux host with Docker Engine and Docker Compose:

```bash
./rebuild-and-restart.sh
```

Open **http://localhost:8091**.

1. Select **New project**, enter a name, and choose a capture interval and duration.
2. Select a webcam. The app selects its largest supported resolution and matching camera format automatically. Click **Test camera** to check framing.
3. Click **Create project**, then **Start recording**.
4. Use the project's **Overview**, **Photos**, **Settings**, and **Videos** tabs to follow its progress or change its setup.

The Projects page shows every project's recording status. Started projects have two progress rings, also shown in Overview: a live countdown to the next scheduled photo and the percentage of time elapsed toward the project end date. Timers update every second from the recorder's clock, handle overnight capture windows and retries, and resynchronize after a restart or reconnect. Paused projects show “Paused” for the next photo; their end dates still advance. “Due now” means the scheduled time has arrived; camera warm-up and shared-camera work may delay the saved photo. If the recorder cannot be reached, the rings show “Reconnecting…” instead of an outdated countdown. **Pause recording** stops only that project's schedule; its photos are retained. **Capture a photo** saves an extra photo. Camera previews are not saved, and testing unsaved camera settings does not change the active recording configuration. Advanced settings are collapsed by default.

Multiple projects can run at once, including projects sharing a webcam. Camera operations are serialized across projects, so scheduled captures wait their turn. A manual preview/capture may report that the camera is busy; try again after the current shot. At large project counts, the time spent warming up cameras can delay captures.

## Focus before capture

In **Settings → Camera**, **Autofocus before each photo** is enabled by default. For cameras exposing V4L2 continuous autofocus, the recorder restarts autofocus before every scheduled photo, manual capture, and test preview. It streams and discards frames for at least **Focus settling time** (default 3 seconds, adjustable from 1 to 15), or the camera warm-up time if longer, before saving the photo. This gives the lens time to adjust; it does not confirm a focus lock or guarantee a sharp image. Increase the settling time and use **Test camera** to check the result.

Settings shows whether the selected camera exposes compatible autofocus. Cameras without that control continue capturing with their existing focus and normal warm-up; the focus option has no effect on them. One-shot-only autofocus is detected but is not controlled by this implementation. If a camera has a physical focus ring, it can be adjusted manually while checking test previews. See the [Linux camera control reference](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/ext-ctrls-camera.html) for the distinction between continuous and one-shot focus.

The recorder keeps autofocus enabled during the capture stream and restores the camera's original autofocus setting afterward, including on capture errors. When the option is off, it leaves the camera's focus controls unchanged. Focus preferences are saved per project and survive restarts; scheduled autofocus failures use the existing capture retry behavior. Demo mode has no physical focus control.

## Preview and edit a video

Open a project's **Videos** tab:

1. **Play preview** to watch the included frames in capture order. Drag **Video position**, use the frame arrows, or click a thumbnail to inspect a particular photo. **Open original photo** shows it at full quality.
2. Click **Remove this frame**, or check several thumbnails and click **Remove selected**. **Select this page** selects up to 24 frames at once. Removed frames stay visible with a label; select them and click **Restore selected** to include them again. Edits save automatically per project and survive restarts. All original photos remain available in **Photos**.
3. Click **Export final video** and download the MP4 when ready. The export saves the included frame IDs, resolution, and playback speed at the moment it is queued. Later frame edits or new captures do not alter that export, even if rendering restarts.

The preview takes a snapshot of the capture cutoff when opened. Click **Refresh frames** to include newer photos without leaving the editor. The browser loads thumbnails and paginated metadata to keep multi-month projects manageable. Preview playback targets the configured video frame rate but can slow down while images load; the MP4 uses the exact configured frame rate and resolution. If every frame is removed, restore at least one before exporting. Recording continues while reviewing, editing, and exporting.

## Recovery after restarts

- Project settings, the running/paused state, end date, capture schedule, and history are persisted in SQLite on the Docker data volume.
- At startup, each running project schedules **one fresh capture as soon as its capture window permits**, even if its previous interval had not elapsed. It then continues its normal interval. Missed intervals are not replayed as a burst. A paused project stays paused.
- If a camera is unavailable or free storage is below the reserve, the project remains running and retries every minute within its capture window. The interface shows the error. Successful capture clears it.
- USB cameras connected after the app starts appear when you click **Refresh cameras**. The container can start without a camera attached. Stable USB device IDs are preferred so that renumbering `/dev/videoN` does not change the selected camera. A camera without a stable ID may require reselection if Linux renumbers it.
- Interrupted MP4 exports are queued automatically after a restart. They render again from the saved frame selection, resolution, and frame rate. Older exports created before frame editing was available retain their original capture cutoff. A partial MP4 is not offered for download. One video renders at a time across all projects.
- Docker's `restart: unless-stopped` policy restarts the app after crashes and host reboots, provided Docker Engine starts at boot. Manually stopping the container prevents automatic restart until you start it again. A browser tab does not need to remain open. See [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/).
- A rebuild waits for the app to report healthy before reporting success. HTTP health checks verify that the project schedulers are running; individual camera/storage errors appear in the affected project.

A project ends at its original start time plus its configured number of days. Pauses and downtime count toward that duration. Ended projects remain available for browsing and export; extend their duration in Settings to resume. Daily windows follow the project's timezone and daylight saving changes. An in-progress shot may finish after pausing or changing settings.

## Existing installations and data

The first upgrade automatically preserves the original study as the first project, including its photos, settings, exports, and running state. Existing files are retained in place. The migration is safe to repeat on later startups.

The persistent `/data` volume contains:

```text
projects.sqlite3          Project catalog
state.sqlite3            Original project's settings and history
frames/ thumbs/ exports/ Original project's files
projects/<project-id>/   Additional projects, each with its own database and files
```

A separate database per project keeps photos, settings, activity, and exports isolated. Exactly one app instance/worker may own a data volume; file locks enforce this. APIs remain compatible with the original single-project endpoints, which refer to the first project. New endpoints use `/api/projects/{project_id}/...`; API documentation is at `/docs`.

**`docker compose down -v` deletes the data volume, including every project and photo.** Ordinary rebuilds, restarts, and `docker compose down` preserve data. Original photos are never automatically deleted. Captures are written before their database record; a sudden power loss at that boundary can leave an unindexed original photo on disk, retained for recovery. A disk reserve is a best-effort guard, so leave headroom for other processes and in-flight writes.

For a consistent backup, stop briefly and copy the entire data volume, including all databases and their sidecar files:

```bash
docker compose stop
docker compose cp grow:/data ./grow-backup
docker compose start
```

Store backups on another disk. Restore the whole directory into a clean data volume while the app is stopped, ensuring UID 10001 can write it. Running projects resume on the next startup. Export settings are now saved with new exports; an export interrupted during an upgrade from the old version uses its saved frame cutoff/fps and the project's current resolution.

## Camera access

Native Linux Docker Engine and V4L2-compatible USB webcams are supported, including a Linux Raspberry Pi with a USB webcam. Docker Desktop on Windows/macOS needs separate USB passthrough; it cannot automatically access the computer's webcam. Raspberry Pi CSI and network cameras are not implemented.

The Linux setup binds the host's device directory read-only at `/host-dev`, with a [device cgroup rule](https://docs.docker.com/reference/compose-file/services/#device_cgroup_rules) allowing access to V4L2 character devices (major 81). The application runs as an unprivileged user, validates video-device paths, and only probes/captures V4L2 devices. It does not run in privileged mode or mount the Docker socket. Directory binding lets camera nodes appear after boot or reconnect without recreating the container. See [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/).

The rebuild script generates an ignored `compose.override.yaml` with the host video permission groups. It replaces the previous version's fixed device mappings. The file is also used automatically by plain `docker compose` commands. Don't edit it manually. If a new camera uses a different permission group, rerun the rebuild script to add that group. Camera choice is made in the UI, not in `.env`.

For troubleshooting, install `v4l-utils` on the host and use:

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
```

When a camera is selected, the app queries its V4L2 formats and frame sizes and chooses the largest compatible resolution by pixel count, preferring MJPEG when multiple formats offer that size. Resolution choices list reported modes; choosing a smaller size also picks a matching format. Camera detection does not start a stream or interrupt recording. Existing saved settings are preserved when opening the form; click **Use largest resolution** to update them, then **Save settings**. Capture and MP4 output share that resolution; video playback speed stays independently configurable in Advanced settings.

If the camera cannot report its modes, the form explains the issue and permits manual resolution selection. Test the camera before saving. Automatic selection supports common V4L2 formats with even dimensions up to 16384 pixels per side; bandwidth/driver limits can still make a reported mode fail to capture. See [Linux V4L2 frame-size enumeration](https://www.kernel.org/doc/html/v4.9/media/uapi/v4l/vidioc-enum-framesizes.html).

Choose a supported format and resolution, close other apps using the camera, and use Test camera. Metadata-only video nodes are excluded from the selector. A steady mount, consistent lighting, and reliable power help with long studies; manual focus positions and exposure controls are not currently exposed.

## Demo, configuration, and operations

Try synthetic plant images without camera hardware, using a separate project/data volume:

```bash
COMPOSE_PROJECT_NAME=grow-demo COMPOSE_FILE=compose.demo.yaml PORT=8092 ./rebuild-and-restart.sh
```

Open **http://localhost:8092**. Demo mode uses a clearly labeled synthetic image, not a plant-growth simulation. Continue using those same environment variables for demo rebuilds.

The default web port is 8091. Copy `.env.example` to `.env` to change `PORT` or `BIND_ADDRESS`. The app binds to `127.0.0.1` and has **no login**. Set `BIND_ADDRESS=0.0.0.0` only for a trusted LAN. Anyone who can access the app can view projects and change recordings. Use an authenticated HTTPS proxy or VPN for remote access; don't expose the unauthenticated port directly to the internet.

```bash
./rebuild-and-restart.sh          # Build, recreate, wait for health
docker compose logs --tail=100 -f
docker compose ps
docker compose restart           # Active projects resume automatically
```

The rebuild helper works from any directory and accepts `COMPOSE_FILE`. Production dependencies install from `Pipfile.lock`; an outdated lock fails the build before recreation. All original images and completed exports stay on the data volume. Logs rotate and event history is bounded per project.

## Development and tests

Python 3.12, Pipenv, and FFmpeg with libx264 are required. Development uses Linux file locking.

```bash
pipenv sync --dev
DEMO_MODE=1 DATA_DIR=./data pipenv run uvicorn app.main:app --host 127.0.0.1 --port 8092
pipenv run python -m pytest -q
```

Use `pipenv install <package>` or `pipenv install --dev <package>` to add dependencies. After manually editing `Pipfile`, run `pipenv lock` and `pipenv sync --dev`; commit `Pipfile` and `Pipfile.lock` together.

Tests cover project isolation and migration, multiple recordings resuming after restart, paused and finished projects, shared-camera serialization, automatic retries, export recovery with the original settings and frame selection, reversible frame exclusions and pagination, maximum camera resolution detection, autofocus sequencing and restoration after failures, live camera discovery, request validation, real demo JPEGs and FFmpeg videos, capture windows/DST, and disk reserves. Integration checks also exercise Docker restart/recreation and the browser interface. A host reboot itself must be validated on your deployment; Docker must be enabled at boot.
