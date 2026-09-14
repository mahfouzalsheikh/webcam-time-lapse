# Grow · Plant time-lapse

A self-hosted app for recording plant growth over weeks or months. Manage multiple projects, each with its own webcam or USB DSLR, schedule, photos, and downloadable MP4 videos. FastAPI, SQLite, FFmpeg, and gPhoto2 run in a single Docker container; photos stay on your machine.

## Start the app

On a Linux host with Docker Engine and Docker Compose:

```bash
./rebuild-and-restart.sh
```

Open **http://localhost:8091**.

1. Select **New project**, enter a name, and choose a capture interval and duration.
2. Select a camera. For webcams, the app selects its largest supported resolution and matching format automatically. Click **Test camera** to check framing. For a Canon DSLR, follow the USB setup below, then use **Take test photo**.
3. Click **Create project**, then **Start recording**.
4. Use the project's **Overview**, **Photos**, **Settings**, and **Videos** tabs to follow its progress or change its setup.

The Projects page shows every project's recording status. Started projects have two progress rings, also shown in Overview: a live countdown to the next scheduled photo and the percentage of time elapsed toward the project end date. Timers update every second from the recorder's clock, handle overnight capture windows and retries, and resynchronize after a restart or reconnect. Paused projects show “Paused” for the next photo; their end dates still advance. “Due now” means the scheduled time has arrived; camera warm-up and shared-camera work may delay the saved photo. If the recorder cannot be reached, the rings show “Reconnecting…” instead of an outdated countdown. **Pause recording** stops only that project's schedule; its photos are retained. **Capture a photo** saves an extra photo. Camera previews are not added to the project, and testing unsaved camera settings does not change the active recording configuration. Advanced settings are collapsed by default.

Multiple projects can run at once, including projects sharing a camera. Camera operations are serialized across projects, so scheduled captures wait their turn. A manual preview/capture may report that the camera is busy; try again after the current shot. At large project counts, the time spent warming up cameras can delay captures.

In **Photos** or **Recent photos**, click **Delete photo** and confirm to permanently remove that photo's original and thumbnail from the app. Counts, storage totals, the latest photo and future video exports update accordingly. Existing exported videos and copies on the camera's card are retained. Wait for any queued or running video export in that project to finish before deleting individual photos. **Remove this frame** in Videos remains reversible and only excludes a photo from future exports.

Click **Delete project** on a project card or beside its recording button and confirm to permanently remove its photos, thumbnails, videos, settings and activity history. Deletion stops the project's scheduler and exports and waits for an in-progress capture to finish before removing files. Other projects continue running. You can delete the original project or every project; deleted projects do not reappear after restart. Interrupted file cleanup resumes when the app next starts. Deletion cannot be undone and does not delete copies on a camera's card.

## Canon Rebel T7i / EOS 800D over USB

The app supports DSLR still photography through gPhoto2 on Linux. The [gPhoto2 camera list](https://gphoto.sourceforge.io/proj/libgphoto2/support.php) lists the Canon EOS Rebel T7i (also named EOS 800D) with image capture support. No virtual webcam or HDMI capture adapter is needed. Full-resolution 6000 × 4000 JPEG capture has been verified on the connected T7i; test your own camera setup before a long recording.

1. Connect the camera to the Linux host with a USB data cable and turn it on in still-photo mode. Disable camera Wi-Fi/NFC for USB use. Insert a memory card with free space, select **JPEG Large/Fine** quality, and disable auto power off for long recordings. RAW+JPEG is also accepted; the app only retains the downloaded JPEG. RAW-only cannot be used.
2. Set exposure and focus on the camera/lens. For a fixed plant composition, focus once and switch the lens to **MF**. The app leaves DSLR settings unchanged; its webcam autofocus, settling time, warm-up and input format controls do not apply. Avoid Bulb exposures; each capture/download has a 90-second timeout. Use reliable continuous power for long studies.
3. Install host tools and give the container's supplementary `video` group access to Canon camera USB nodes. Desktop user ACLs alone do not grant access to the container user. On Debian/Ubuntu:

   ```bash
   sudo apt-get update
   sudo apt-get install gphoto2
   sudo tee /etc/udev/rules.d/99-grow-canon.rules >/dev/null <<'EOF'
   SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="04a9", ENV{ID_GPHOTO2}=="1", GROUP="video", MODE="0660"
   EOF
   sudo udevadm control --reload-rules
   ```

   Reconnect the camera so the rule applies. Close EOS Utility, photo importers and other camera apps; unmount/eject the camera's photo mount in the file manager if it holds the USB interface. `gphoto2 --auto-detect` should show **Canon EOS Rebel T7i** or **Canon EOS 800D**. On other Linux distributions, install the equivalent gphoto2/libgphoto2 packages and ensure the `video` group exists before applying the rule.
4. Run `./rebuild-and-restart.sh` to install the container dependency, mount USB devices, and refresh permission groups. In **Settings → Camera**, click **Refresh cameras**, select the Canon and click **Take test photo**, then **Save settings**. This also works for new projects. Start recording when ready.

Every scheduled capture, manual capture, and test photo fires the real shutter. Tests are not added to the project's photo history. Capture uses [`--capture-image-and-download --keep`](https://gphoto.sourceforge.io/doc/manual/ref-gphoto2-cli.html): photos stored on the camera card are left there, including test photos, so monitor card space. The app does not delete existing camera files. The camera's capture destination determines whether a copy is stored on the card. The host keeps full-resolution JPEGs and EXIF without recompressing them; **Video export resolution** controls MP4 output only (Full HD by default). Exports preserve the photo's aspect ratio with padding. Full-resolution photos require substantially more disk space than webcam frames; the initial DSLR estimate assumes 10 MB per photo until actual captures are available.

When Linux exposes a unique USB serial number, DSLR selection stores a stable `gphoto2:serial:…` ID derived from the vendor, product and serial. Each capture resolves that identity to the camera's current USB address, so a running project retries and resumes after reconnects or restarts without reselection. Discovery reads the kernel's USB metadata in `/sys/bus/usb/devices` ([Linux USB ABI](https://github.com/torvalds/linux/blob/master/Documentation/ABI/stable/sysfs-bus-usb)); it does not claim the camera or fire the shutter. The app never substitutes a different serial number or guesses between duplicate serials.

Older projects using an address such as `gphoto2:usb:001,006` upgrade when settings are saved or a saved capture succeeds while that address still identifies the connected camera. If it has already changed, **Refresh cameras**, select the Canon and **Save settings** once. Cameras without a readable, unique serial retain explicit USB-address selection and may need reselection after reconnects. Test a photo to verify permissions and capture support; other gPhoto2 cameras still need their own hardware validation.

If the desktop takes over the camera and capture reports **Could not claim USB**, run this on the Linux host as your desktop user (without `sudo`):

```bash
./fix-canon-usb.sh
```

This releases Canon photo mounts and persistently masks the current user's `gvfs-gphoto2-volume-monitor` service so the desktop does not automatically mount photo cameras on reconnect or login. It applies to photo-camera mounting for this login; it does not change USB device permissions. Close any photo importers that explicitly open the camera. The container cannot release a host desktop mount itself. To restore desktop camera mounting, run `./fix-canon-usb.sh --undo`.

To diagnose container access without taking a photo:

```bash
docker compose exec grow gphoto2 --auto-detect
docker compose exec grow id
ls -l /dev/bus/usb/*/*
```

If discovery works but capture says it cannot claim USB, check the udev rule and groups and close competing camera software. If JPEG validation fails, check camera image quality. Capture errors appear in the project and running projects retry through the existing scheduler. No camera hardware is needed for demo mode.

## Webcam focus before capture

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
- USB cameras connected after the app starts appear when you click **Refresh cameras**. The container can start without a camera attached. Stable webcam IDs and DSLR serial IDs survive device renumbering; running projects retry automatically after reconnect. A camera without a stable ID may require reselection if Linux renumbers it.
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

Native Linux Docker Engine supports V4L2-compatible USB webcams and USB DSLRs through gPhoto2, including on a Linux Raspberry Pi. Docker Desktop on Windows/macOS needs separate USB passthrough; it cannot automatically access the computer's camera. Raspberry Pi CSI and network cameras are not implemented.

The Linux setup binds the host's device directory read-only at `/host-dev` for V4L2 devices (major 81), and `/dev/bus/usb` at its standard path for gPhoto2/libusb (major 189). [Device cgroup rules](https://docs.docker.com/reference/compose-file/services/#device_cgroup_rules) permit camera access; host device permissions still apply. The application runs as an unprivileged user and validates V4L2 paths and DSLR USB addresses. It does not run in privileged mode or mount the Docker socket. USB passthrough exposes the USB bus, with access limited by Unix permissions. Directory binding lets camera nodes appear after boot or reconnect without recreating the container. See [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/).

The rebuild script generates an ignored `compose.override.yaml` with the host video and non-root USB permission groups. It replaces the previous version's fixed device mappings. The file is also used automatically by plain `docker compose` commands. Don't edit it manually. If a new camera uses a different permission group, rerun the rebuild script to add that group. Camera choice is made in the UI, not in `.env`.

For troubleshooting, install `v4l-utils` on the host and use:

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
```

When a webcam is selected, the app queries its V4L2 formats and frame sizes and chooses the largest compatible resolution by pixel count, preferring MJPEG when multiple formats offer that size. Resolution choices list reported modes; choosing a smaller size also picks a matching format. Camera detection does not start a stream or interrupt recording. Existing saved settings are preserved when opening the form; click **Use largest resolution** to update them, then **Save settings**. Capture and MP4 output share that resolution; video playback speed stays independently configurable in Advanced settings.

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

Python 3.12, Pipenv, and FFmpeg with libx264 are required; install gphoto2 for native DSLR capture. Development uses Linux file locking.

```bash
pipenv sync --dev
DEMO_MODE=1 DATA_DIR=./data pipenv run uvicorn app.main:app --host 127.0.0.1 --port 8092
pipenv run python -m pytest -q
```

Use `pipenv install <package>` or `pipenv install --dev <package>` to add dependencies. After manually editing `Pipfile`, run `pipenv lock` and `pipenv sync --dev`; commit `Pipfile` and `Pipfile.lock` together.

Tests cover DSLR discovery, full-resolution JPEG/EXIF preservation, portrait thumbnails, RAW-only rejection, USB errors/timeouts, DSLR scheduling and export, project isolation and migration, multiple recordings resuming after restart, paused and finished projects, shared-camera serialization, automatic retries, export recovery with the original settings and frame selection, reversible frame exclusions and pagination, maximum camera resolution detection, autofocus sequencing and restoration after failures, live camera discovery, request validation, real demo JPEGs and FFmpeg videos, capture windows/DST, and disk reserves. Integration checks also exercise Docker restart/recreation and the browser interface. A host reboot itself must be validated on your deployment; Docker must be enabled at boot.
