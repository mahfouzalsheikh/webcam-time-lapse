const $ = (id) => document.getElementById(id);
let projects = [],
  defaults,
  cameras = [],
  demo = false,
  currentId = null,
  tab = "overview";
let editingKey = null,
  dirty = false,
  photoPage = 0,
  refreshing = false,
  routeVersion = 0;
let latestKey, previewUrl, testUrl, noticeTimer;
const numeric = [
  "interval_minutes",
  "duration_days",
  "warmup_seconds",
  "focus_settle_seconds",
  "reserve_mb",
  "export_fps",
];
const bytes = (value) =>
  value >= 1073741824
    ? `${(value / 1073741824).toFixed(1)} GB`
    : `${(value / 1048576).toFixed(1)} MB`;
const project = () => projects.find((p) => p.id === currentId);
const endpoint = (id, path) => `projects/${encodeURIComponent(id)}/${path}`;
const media = (id, kind, name) =>
  `/media/projects/${encodeURIComponent(id)}/${kind}/${name}`;
const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const date = (
  stamp,
  p = project(),
  opts = { dateStyle: "medium", timeStyle: "short" },
) =>
  stamp
    ? new Date(stamp * 1000).toLocaleString(undefined, {
        timeZone: p?.settings.timezone || "UTC",
        ...opts,
      })
    : "Not started";
function status(p) {
  return p.runtime.ends_at && p.runtime.ends_at <= projectNow(p)
    ? ["Finished", ""]
    : p.runtime.running
      ? p.runtime.last_error
        ? ["Retrying", "error"]
        : ["Recording", "running"]
      : p.runtime.started_at
        ? ["Paused", ""]
        : ["Ready", ""];
}
async function api(path, method = "GET", body) {
  const response = await fetch(`/api/${path}`, {
    method,
    headers: method === "GET" ? {} : { "Content-Type": "application/json" },
    body: method === "GET" ? undefined : JSON.stringify(body ?? {}),
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    const detail = Array.isArray(result.detail)
      ? result.detail
          .map((e) => `${e.loc.slice(1).join(".")}: ${e.msg}`)
          .join("\n")
      : result.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.headers.get("content-type")?.includes("image/")
    ? response.blob()
    : response.json();
}
function notify(message, error = false) {
  clearTimeout(noticeTimer);
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
  $("notice").hidden = false;
  noticeTimer = setTimeout(
    () => ($("notice").hidden = true),
    error ? 15000 : 6000,
  );
}
async function action(button, callback) {
  button.disabled = true;
  try {
    await callback();
    await refresh();
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
    if (button.id === "export-button") updateVideoButtons();
  }
}
function go(hash) {
  if (dirty && !confirm("You have unsaved settings. Leave without saving?"))
    return;
  dirty = false;
  location.hash = hash;
}
function newProject() {
  go("new");
}
$("new-project").onclick = newProject;
document
  .querySelectorAll("[data-new]")
  .forEach((button) => (button.onclick = newProject));
document
  .querySelectorAll("[data-tab]")
  .forEach(
    (button) =>
      (button.onclick = () => go(`project/${currentId}/${button.dataset.tab}`)),
  );
window.addEventListener("beforeunload", (event) => {
  if (dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
document.addEventListener("click", (event) => {
  const link = event.target.closest('a[href^="#"]');
  if (link && dirty && link.hash !== location.hash) {
    event.preventDefault();
    go(link.hash.slice(1));
  }
});
function route() {
  resetTimeline();
  cameraRequest++;
  editingKey = null;
  dirty = false;
  $("settings-fields").disabled = true;
  const parts = location.hash.slice(1).split("/");
  currentId = parts[0] === "project" ? parts[1] : null;
  tab = ["overview", "photos", "settings", "videos"].includes(parts[2])
    ? parts[2]
    : "overview";
  const isNew = parts[0] === "new";
  $("projects-view").hidden = Boolean(currentId) || isNew;
  $("project-view").hidden = !currentId;
  $("editor-view").hidden = !(isNew || (currentId && tab === "settings"));
  $("new-heading").hidden = !isNew;
  document
    .querySelectorAll(".tab-panel")
    .forEach((node) => (node.hidden = node.id !== `${tab}-panel`));
  document
    .querySelectorAll(".tabs [data-tab]")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.tab === tab),
    );
  routeVersion++;
  photoPage = 0;
  latestKey = undefined;
  refresh().catch((error) => notify(error.message, true));
}
window.addEventListener("hashchange", route);

function renderProjects() {
  $("connection").textContent = demo
    ? "● Connected · demo camera"
    : "● Recorder online";
  $("mode").textContent = demo ? "Demo mode" : "";
  $("project-summary").textContent =
    `${projects.length} project${projects.length === 1 ? "" : "s"} · ${projects.filter((p) => p.runtime.running).length} recording`;
  $("disk-summary").textContent = projects.length
    ? `${bytes(projects[0].disk.free)} disk space available`
    : "";
  $("project-nav").replaceChildren();
  $("project-grid").replaceChildren();
  for (const p of projects) {
    const [label, cls] = status(p),
      href = `#project/${p.id}/overview`;
    const link = el("a");
    link.href = href;
    link.classList.toggle("active", p.id === currentId);
    link.append(el("i", undefined, cls), el("span", p.settings.name));
    $("project-nav").append(link);
    const card = el("article", undefined, "project-card"),
      cover = el("a", undefined, "project-cover");
    cover.href = href;
    cover.setAttribute("aria-label", `Open ${p.settings.name}`);
    if (p.latest) {
      const img = el("img");
      img.src = media(p.id, "thumbs", `${p.latest.id}.jpg`);
      img.alt = `Latest photo: ${p.settings.name}`;
      img.loading = "lazy";
      cover.append(img);
    } else cover.append(el("span", "❧"));
    const body = el("div", undefined, "project-card-body"),
      title = el("div", undefined, "project-card-title");
    title.append(el("h2", p.settings.name), el("span", label, `badge ${cls}`));
    body.append(
      title,
      el(
        "p",
        `${p.frames.count.toLocaleString()} photos · Every ${p.settings.interval_minutes} min · ${p.settings.duration_days} days`,
      ),
    );
    body.append(
      el(
        "p",
        p.runtime.last_error && p.runtime.running
          ? "Capture needs attention. Retrying automatically."
          : p.runtime.running
            ? "Recording resumes automatically after restarts."
            : p.runtime.started_at
              ? "Paused. Resume when you are ready."
              : "Set up your camera, then start recording.",
      ),
    );
    if (p.runtime.started_at || p.runtime.running) {
      const rings = el("div", undefined, "project-rings");
      rings.append(progressRing(p, "capture"), progressRing(p, "completion"));
      body.append(rings);
    }
    const open = el("a", "Open project", "outline");
    open.href = href;
    body.append(open);
    card.append(cover, body);
    $("project-grid").append(card);
  }
}
function renderProject(p) {
  const s = p.settings,
    r = p.runtime,
    [label, cls] = status(p);
  document.title = `${s.name} · Grow`;
  $("preview-button").textContent = isDslr(s.camera_device) ? "Take test photo" : "Preview camera";
  $("preview-button").title = isDslr(s.camera_device) ? "Fires the DSLR shutter; not added to project photos. A copy may remain on the camera card." : "";
  $("project-title").textContent = s.name;
  $("project-status").textContent = label;
  $("project-status").className = `badge ${cls}`;
  $("project-subtitle").textContent =
    `One photo every ${s.interval_minutes} minutes · ${s.duration_days} days · ${s.width} × ${s.height}`;
  $("record-button").textContent = r.running
    ? "Pause recording"
    : r.started_at
      ? "Resume recording"
      : "Start recording";
  $("record-button").className = r.running ? "outline" : "primary";
  $("record-button").disabled = label === "Finished";
  const error = Boolean(r.last_error && r.running),
    finished = label === "Finished";
  $("project-alert").hidden = !error && !finished;
  $("project-alert").className = `info-panel${error ? " error" : ""}`;
  $("project-alert").textContent = error
    ? `${r.last_error} The project is still running and retries automatically every minute during its capture hours.`
    : finished
      ? "This project has reached its end date. Increase its duration in Settings to continue recording."
      : "";
  $("photo-count").textContent = p.frames.count.toLocaleString();
  $("next-capture-metric").replaceChildren(progressRing(p, "capture"));
  $("completion-metric").replaceChildren(progressRing(p, "completion"));
  $("video-duration").textContent =
    `${(p.frames.included / s.export_fps).toFixed(1)} seconds`;
  const cameraName = demo
    ? "Demo camera"
    : cameras.find(
        (c) => c.id === s.camera_device || c.aliases?.includes(s.camera_device),
      )?.name || s.camera_device.split("/").pop();
  $("schedule").replaceChildren();
  for (const [key, value] of [
    ["Camera", cameraName],
    [
      "Capture hours",
      s.daylight_only ? `${s.day_start}–${s.day_end}` : "All day",
    ],
    ["Timezone", s.timezone],
    ["Started", date(r.started_at, p)],
    ["Ends", date(r.ends_at, p)],
    ["Photo storage", bytes(p.frames.bytes)],
  ]) {
    const row = el("div");
    row.append(el("dt", key), el("dd", value));
    $("schedule").append(row);
  }
  const key = `${p.id}:${p.latest?.id || "empty"}`;
  if (latestKey !== key) {
    latestKey = key;
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = null;
    }
    $("latest-image").hidden = !p.latest;
    $("image-empty").hidden = Boolean(p.latest);
    if (p.latest)
      $("latest-image").src = media(p.id, "frames", `${p.latest.id}.jpg`);
    $("image-title").textContent = "Latest photo";
    $("image-time").textContent = p.latest ? date(p.latest.captured_at, p) : "";
    $("preview-note").textContent =
      isDslr(s.camera_device) ? "Test takes a real photo; it is not added to this project. A copy may remain on the camera card." : "Preview checks the camera without saving a photo.";
  }
}
function renderPhotos(target, photos, id) {
  target.replaceChildren();
  if (!photos.length) {
    target.append(
      el(
        "div",
        "No photos yet. Start recording or capture a photo from Overview.",
        "empty",
      ),
    );
    return;
  }
  for (const photo of photos) {
    const a = el("a", undefined, "photo-card");
    a.href = media(id, "frames", `${photo.id}.jpg`);
    a.target = "_blank";
    a.rel = "noopener";
    const img = el("img");
    img.src = media(id, "thumbs", `${photo.id}.jpg`);
    img.alt = `Photo taken ${date(photo.captured_at)}`;
    img.loading = "lazy";
    a.append(img, el("span", date(photo.captured_at)));
    target.append(a);
  }
}
function renderExports(jobs, id) {
  $("export-list").replaceChildren();
  exportBusy = jobs.some((j) => ["queued", "running"].includes(j.status));
  updateVideoButtons();
  if (!jobs.length)
    $("export-list").append(
      el(
        "div",
        "No videos yet. Review your frames above, then click Export final video.",
        "empty",
      ),
    );
  for (const job of jobs) {
    const row = el("article", undefined, "card export-row"),
      info = el("div");
    info.append(
      el("h3", date(job.created_at)),
      el(
        "p",
        `${job.frames} photos · ${job.fps} fps · ${(job.frames / job.fps).toFixed(1)} seconds`,
      ),
    );
    if (job.error) info.append(el("p", job.error, "error"));
    row.append(info);
    if (job.status === "complete") {
      const link = el("a", "Download MP4", "outline");
      link.href = media(id, "exports", `${job.id}.mp4`);
      link.download = "";
      row.append(link);
    } else
      row.append(
        el(
          "span",
          job.status === "queued"
            ? "Queued"
            : job.status === "running"
              ? "Rendering…"
              : "Failed",
          "badge",
        ),
      );
    $("export-list").append(row);
  }
}
function fillForm(s, key) {
  if (editingKey === key) return;
  editingKey = key;
  dirty = false;
  $("settings-fields").disabled = false;
  const form = $("settings-form");
  for (const [name, value] of Object.entries(s)) {
    const field = form.elements.namedItem(name);
    if (!field) continue;
    if (field.type === "checkbox") field.checked = value;
    else {
      if (
        field.tagName === "SELECT" &&
        ![...field.options].some((o) => o.value === String(value))
      )
        field.add(
          new Option(
            name === "interval_minutes" ? `${value} minutes` : value,
            value,
          ),
        );
      field.value = value;
    }
  }
  const resolution = `${s.width}x${s.height}`;
  if (![...$("resolution").options].some((o) => o.value === resolution))
    $("resolution").add(new Option(`${s.width} × ${s.height}`, resolution));
  $("resolution").value = resolution;
  $("test-image").hidden = true;
  $("test-note").hidden = true;
  $("save-message").textContent = "Your changes apply to this project only.";
  $("save-settings").textContent =
    key === "new" ? "Create project" : "Save settings";
  renderCameraOptions(s.camera_device);
  updateEstimate();
  detectResolution(key === "new");
}
function renderCameraOptions(selected = $("camera-device").value) {
  const match = cameras.find(
    (c) => c.id === selected || c.aliases?.includes(selected),
  );
  const select = $("camera-device");
  select.replaceChildren();
  cameras.forEach((c) =>
    select.add(
      new Option(
        `${c.name.trim()}${c.available ? "" : " (unavailable)"}`,
        c.id,
      ),
    ),
  );
  if (!match && selected)
    select.add(
      new Option(`${selected.split("/").pop()} (unavailable)`, selected),
    );
  if (!cameras.length && !selected)
    select.add(new Option("No cameras found", ""));
  select.value = match?.id || selected || cameras[0]?.id || "";
  updateCameraHelp();
}
function isDslr(device) {
  return !demo && device?.startsWith("gphoto2:");
}
function updateCameraHelp() {
  const c = cameras.find((c) => c.id === $("camera-device").value);
  const dslr = isDslr($("camera-device").value);
  for (const id of ["autofocus-field", "focus-settle-field", "focus-settle-help", "camera-format-field", "camera-warmup-field"])
    $(id).hidden = dslr;
  $("resolution-label").textContent = dslr ? "Video export resolution" : "Photo resolution";
  $("detect-resolution").textContent = dslr ? "Use Full HD video" : "Use largest resolution";
  $("test-camera").textContent = dslr ? "Take test photo" : "Test camera";
  $("test-note").textContent = dslr ? "Test photo is not added to this project. A copy may remain on the camera card." : "Preview only — this photo is not saved.";
  $("test-camera").disabled = !c?.available;
  $("camera-help").textContent = demo
    ? "Demo mode: previews use a synthetic plant image."
    : !cameras.length
      ? "No cameras found. Connect a USB camera and click Refresh cameras. For a DSLR, check gphoto2 installation and USB permissions (see README)."
      : !c
        ? $("camera-device").value.startsWith("gphoto2:serial:")
          ? "The saved camera is disconnected. Reconnect the same camera and turn it on; running projects retry automatically."
          : "The saved camera is disconnected. Reconnect it, refresh, and reselect it if its USB address changed."
        : c.error
          ? `Camera unavailable: ${c.error}`
          : dslr
            ? `USB DSLR: select JPEG quality on the camera. Tests fire the shutter. Full-resolution JPEGs are downloaded; copies on the camera card are kept, so monitor card space. ${c.id.startsWith("gphoto2:serial:") ? "Save settings to remember this camera across reconnects." : "This camera has no unique readable USB serial; reselect it if its USB address changes."}`
            : "Test the camera to check the angle and resolution. Save settings to use it for this project.";
}
function formSettings() {
  const form = $("settings-form"),
    values = Object.fromEntries(new FormData(form));
  numeric.forEach((key) => (values[key] = Number(values[key])));
  values.daylight_only = form.elements.daylight_only.checked;
  values.autofocus = form.elements.autofocus.checked;
  [values.width, values.height] = $("resolution").value.split("x").map(Number);
  return values;
}
function updateEstimate() {
  const s = formSettings();
  $("capture-window").hidden = !s.daylight_only;
  $("window-help").hidden = !s.daylight_only;
  const minutes = (v) => {
    const [h, m] = v.split(":").map(Number);
    return h * 60 + m;
  };
  const daily = s.daylight_only
    ? (minutes(s.day_end) - minutes(s.day_start) + 1440) % 1440
    : 1440;
  const count = Math.ceil(daily / s.interval_minutes) * s.duration_days;
  const p = project(),
    average = p?.frames.count ? p.frames.bytes / p.frames.count : isDslr(s.camera_device) ? 10000000 : 350000;
  $("estimate").textContent =
    `Estimated result: ${count.toLocaleString()} photos → ${(count / s.export_fps).toFixed(0)} seconds of video. Approximately ${bytes(count * average)} for original photos, plus thumbnails and videos.`;
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  const version = routeVersion,
    id = currentId,
    selectedTab = tab;
  try {
    const data = await api("projects");
    if (version !== routeVersion) return;
    const receivedAt = performance.now();
    projects = data.projects.map((p) => ({ ...p, receivedAt }));
    demo = data.demo;
    renderProjects();
    const p = project();
    if (id && !p) {
      notify("This project could not be found.", true);
      location.hash = "projects";
      return;
    }
    if (p) renderProject(p);
    const isNew = location.hash === "#new";
    if (isNew || (id && selectedTab === "settings")) {
      const key = isNew ? "new" : id;
      if (editingKey !== key) {
        const [d, inventory] = await Promise.all([
          defaults ? Promise.resolve(defaults) : api("defaults"),
          api("cameras"),
        ]);
        if (version !== routeVersion) return;
        defaults = d;
        cameras = inventory.cameras;
        const settings = isNew
          ? {
              ...defaults,
              name: "",
              timezone:
                Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
              camera_device:
                cameras.find((c) => c.available)?.id || defaults.camera_device,
            }
          : p.settings;
        fillForm(settings, key);
      }
    } else if (id && selectedTab === "overview") {
      const [photos, events] = await Promise.all([
        api(endpoint(id, "frames?limit=4")),
        api(endpoint(id, "events")),
      ]);
      if (version !== routeVersion) return;
      renderPhotos($("recent-photos"), photos, id);
      $("events").replaceChildren();
      events.slice(0, 8).forEach((event) => {
        const row = el("div", undefined, `event ${event.level}`);
        row.append(
          el("time", date(event.created_at)),
          el("span", event.message),
        );
        $("events").append(row);
      });
      if (!events.length)
        $("events").append(el("div", "No activity yet.", "event"));
    } else if (id && selectedTab === "photos") {
      const photos = await api(
        endpoint(id, `frames?limit=24&offset=${photoPage * 24}`),
      );
      if (version !== routeVersion) return;
      renderPhotos($("all-photos"), photos, id);
      $("previous").disabled = photoPage === 0;
      $("next").disabled = (photoPage + 1) * 24 >= p.frames.count;
      $("page-label").textContent =
        `Page ${photoPage + 1} · ${p.frames.count} photos`;
    } else if (id && selectedTab === "videos") {
      const jobs = await api(endpoint(id, "exports"));
      if (version === routeVersion) {
        renderExports(jobs, id);
        if (!timeline && !timelineLoading) await loadTimeline(true);
      }
    }
  } catch (error) {
    $("connection").textContent = "Reconnecting to recorder…";
    throw error;
  } finally {
    refreshing = false;
    if (version !== routeVersion)
      refresh().catch((error) => notify(error.message, true));
  }
}
$("record-button").onclick = (event) => {
  const p = project();
  if (!p) return;
  action(event.currentTarget, () =>
    api(endpoint(p.id, p.runtime.running ? "pause" : "start"), "POST"),
  );
};
$("capture-button").onclick = (event) => {
  const id = currentId;
  action(event.currentTarget, async () => {
    await api(endpoint(id, "capture"), "POST");
    notify("Photo saved to this project.");
  });
};
$("preview-button").onclick = (event) => {
  const id = currentId;
  action(event.currentTarget, async () => {
    const blob = await api(endpoint(id, "preview"), "POST");
    if (currentId !== id) return;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    $("latest-image").src = previewUrl;
    $("latest-image").hidden = false;
    $("image-empty").hidden = true;
    $("image-title").textContent = "Camera preview";
    $("image-time").textContent = "Not saved";
    $("preview-note").textContent =
      isDslr(project()?.settings.camera_device) ? "Test photo is not added to this project. A copy may remain on the camera card." : "This is a test preview. Your saved photos are unchanged.";
  });
};
$("export-button").onclick = (event) => {
  const id = currentId;
  action(event.currentTarget, async () => {
    if (!timeline || timeline.id !== id) return;
    await api(endpoint(id, "exports"), "POST", { cutoff: timeline.cutoff });
    notify("Video queued. The download will appear when it is ready.");
  });
};
$("previous").onclick = () => {
  photoPage = Math.max(0, photoPage - 1);
  refresh().catch((error) => notify(error.message, true));
};
$("next").onclick = () => {
  photoPage++;
  refresh().catch((error) => notify(error.message, true));
};
$("refresh-cameras").onclick = (event) =>
  action(event.currentTarget, async () => {
    const result = await api("cameras");
    cameras = result.cameras;
    renderCameraOptions();
    await detectResolution(false);
  });
$("camera-device").onchange = () => {
  updateCameraHelp();
  detectResolution(true);
  $("test-image").hidden = true;
  $("test-note").hidden = true;
};
$("test-camera").onclick = (event) => {
  if (!$("settings-form").reportValidity()) return;
  const key = editingKey;
  action(event.currentTarget, async () => {
    const blob = await api(
      endpoint(currentId || "default", "preview"),
      "POST",
      { settings: formSettings() },
    );
    if (key !== editingKey) return;
    if (testUrl) URL.revokeObjectURL(testUrl);
    testUrl = URL.createObjectURL(blob);
    $("test-image").src = testUrl;
    $("test-image").hidden = false;
    $("test-note").hidden = false;
  });
};
$("settings-form").oninput = () => {
  dirty = true;
  $("save-message").textContent = "You have unsaved changes.";
  updateEstimate();
};
$("settings-form").onsubmit = (event) => {
  event.preventDefault();
  const key = editingKey,
    settings = formSettings();
  action(event.submitter, async () => {
    if (key === "new") {
      const p = await api("projects", "POST", settings);
      dirty = false;
      editingKey = null;
      location.hash = `project/${p.id}/overview`;
      notify("Project created. Click Start recording when you are ready.");
    } else {
      await api(endpoint(key, "settings"), "PUT", settings);
      dirty = false;
      $("save-message").textContent = "Settings saved.";
      notify("Project settings saved.");
    }
  });
};
let cameraRequest = 0, detectedModes = [];
function setCameraFormat(format) {
  const select = $("settings-form").elements.input_format;
  if (![...select.options].some((option) => option.value === format))
    select.add(new Option(format.toUpperCase(), format));
  select.value = format;
}
async function detectResolution(apply) {
  const request = ++cameraRequest, version = routeVersion, device = $("camera-device").value;
  detectedModes = [];
  checkFocus(device, version, request);
  $("resolution-help").textContent = "Checking camera resolutions…";
  $("detect-resolution").disabled = true;
  $("save-settings").disabled = true;
  $("test-camera").disabled = true;
  try {
    const result = await api(`cameras/capabilities?device=${encodeURIComponent(device)}`);
    if (request !== cameraRequest || version !== routeVersion) return;
    detectedModes = result.modes;
    if (!result.recommended) throw new Error("This camera does not report supported resolutions. Choose one manually and test the camera.");
    const previous = $("resolution").value;
    $("resolution").replaceChildren();
    const sizes = new Set();
    for (const mode of detectedModes) {
      const value = `${mode.width}x${mode.height}`;
      if (sizes.has(value)) continue;
      sizes.add(value);
      $("resolution").add(new Option(`${mode.width} × ${mode.height}${sizes.size === 1 && result.resolution_scope !== "export" ? " (largest)" : ""}`, value));
    }
    if (!apply && !sizes.has(previous)) $("resolution").add(new Option(`${previous.replace("x", " × ")} (saved)`, previous));
    const best = result.recommended;
    $("resolution").value = apply ? `${best.width}x${best.height}` : previous;
    if (apply) {
      setCameraFormat(best.input_format);
      dirty = true;
      $("save-message").textContent = "Camera settings updated. Save to apply.";
      updateEstimate();
    }
    $("resolution-help").textContent = result.resolution_scope === "export"
      ? "Original JPEGs keep the size and quality chosen on the camera. This setting controls MP4 output only; photos are fitted with padding to preserve their shape. Save settings to apply."
      : `${apply ? "Selected" : "Largest available:"} ${best.width} × ${best.height} (${best.input_format.toUpperCase()}). Photos and exported videos use the selected resolution.${apply ? " Save settings to apply." : " Your saved resolution is unchanged."}`;
  } catch (error) {
    if (request === cameraRequest && version === routeVersion) $("resolution-help").textContent = error.message;
  } finally {
    if (request === cameraRequest && version === routeVersion) {
      $("detect-resolution").disabled = false;
      $("save-settings").disabled = false;
      updateCameraHelp();
    }
  }
}
async function checkFocus(device, version, request) {
  $("focus-help").textContent = "Checking camera focus support…";
  try {
    const focus = await api(`cameras/focus?device=${encodeURIComponent(device)}`);
    if (version !== routeVersion || request !== cameraRequest) return;
    $("focus-help").textContent = focus.demo
      ? "Demo image: there is no physical lens to focus."
      : focus.backend === "gphoto2"
        ? "Set focus and exposure on the camera and lens. The app does not change DSLR focus, image quality or exposure settings. For fixed framing, focus once and switch the lens to MF."
      : focus.autofocus
        ? "Autofocus supported. When enabled, the app restarts autofocus before each photo and preview, then gives the lens time to settle."
        : focus.single_shot
          ? "This camera exposes only one-shot autofocus, which this app does not control yet. Photos will use the camera's existing focus."
          : "This camera does not expose software autofocus. The option has no effect on it; photos use the camera's existing focus. If the lens has a manual focus ring, adjust it while using Test camera.";
  } catch (error) {
    if (version === routeVersion && request === cameraRequest) $("focus-help").textContent = error.message;
  }
}
$("detect-resolution").onclick = () => detectResolution(true);
$("resolution").onchange = () => {
  const mode = detectedModes.find((m) => `${m.width}x${m.height}` === $("resolution").value);
  if (mode) setCameraFormat(mode.input_format);
  $("resolution-help").textContent = isDslr($("camera-device").value)
    ? "Exported videos will use this resolution. Original JPEGs keep the camera's resolution. Save settings to apply."
    : "Photos and exported videos will use this resolution. Save settings to apply.";
};

let timeline = null, timelinePage = 0, timelineLoading = false, timelineRequest = 0;
let selectedFrames = new Set(), frameCache = new Map(), currentFrame = null, frameIndex = 0;
let playing = false, playTimer, frameRequest = 0, exportBusy = false, selectionBusy = false;
function stopPlayback() {
  playing = false;
  clearTimeout(playTimer);
  frameRequest++;
  $("play-timeline").textContent = "Play preview";
}
function resetTimeline() {
  stopPlayback();
  timeline = null;
  timelinePage = 0;
  timelineLoading = false;
  selectionBusy = false;
  exportBusy = false;
  timelineRequest++;
  frameCache.clear();
  selectedFrames.clear();
  currentFrame = null;
  frameIndex = 0;
  $("timeline-image").hidden = true;
  $("timeline-empty").hidden = false;
  $("frame-original").hidden = true;
  $("timeline-grid").replaceChildren();
  $("timeline-summary").textContent = "Loading frames…";
  updateVideoButtons();
}
function updateVideoButtons() {
  const blocked = !timeline || timelineLoading || selectionBusy;
  $("export-button").disabled = blocked || exportBusy || !timeline?.included;
  $("play-timeline").disabled = blocked || !timeline?.included;
  $("timeline-scrubber").disabled = blocked || !timeline?.included;
  $("frame-back").disabled = blocked || !timeline?.included || frameIndex <= 0;
  $("frame-forward").disabled = blocked || !timeline?.included || frameIndex >= timeline.included - 1;
  $("remove-current").disabled = blocked || !currentFrame;
  $("remove-selected").disabled = blocked || !selectedFrames.size;
  $("restore-selected").disabled = blocked || !selectedFrames.size;
  $("select-page").disabled = blocked || !timeline?.frames.length;
  $("refresh-timeline").disabled = timelineLoading || selectionBusy;
  $("timeline-previous").disabled = blocked || timelinePage === 0;
  $("timeline-next").disabled = blocked || (timelinePage + 1) * 24 >= timeline.total;
  $("selection-count").textContent = `${selectedFrames.size} selected`;
}
async function loadTimeline(fresh = false) {
  stopPlayback();
  const id = currentId, version = routeVersion, request = ++timelineRequest;
  if (fresh) timelinePage = 0;
  const cutoff = fresh ? "" : `&cutoff=${timeline.cutoff}`;
  const previousIndex = fresh ? 0 : frameIndex;
  timelineLoading = true;
  updateVideoButtons();
  try {
    const data = await api(endpoint(id, `timeline?limit=24&offset=${timelinePage * 24}${cutoff}`));
    if (version !== routeVersion || request !== timelineRequest) return;
    timeline = { ...data, id };
    selectedFrames.clear();
    frameCache.clear();
    currentFrame = null;
    frameIndex = 0;
    $("frame-original").hidden = true;
    $("timeline-image").hidden = true;
    $("current-frame-label").textContent = "Select a frame below to inspect it.";
    $("timeline-scrubber").max = Math.max(0, data.included - 1);
    $("timeline-scrubber").value = 0;
    $("timeline-empty").hidden = Boolean(data.included);
    $("timeline-empty").textContent = data.total ? "All frames are removed. Restore frames below to preview and export." : "No photos yet. Capture a photo from Overview to begin.";
    $("timeline-summary").textContent = `${data.included.toLocaleString()} frames included · ${data.excluded.toLocaleString()} removed · ${(data.included / project().settings.export_fps).toFixed(1)} seconds at ${project().settings.export_fps} fps. Reviewed through ${date(data.cutoff)}.`;
    $("timeline-page").textContent = `Page ${timelinePage + 1} of ${Math.max(1, Math.ceil(data.total / 24))}`;
    renderTimelineFrames();
    if (data.included) await showVideoFrame(previousIndex);
  } finally {
    if (version === routeVersion && request === timelineRequest) {
      timelineLoading = false;
      updateVideoButtons();
    }
  }
}
function renderTimelineFrames() {
  $("timeline-grid").replaceChildren();
  $("select-page").checked = false;
  $("select-page").indeterminate = false;
  for (const [index, frame] of timeline.frames.entries()) {
    const card = el("article", undefined, `timeline-card${frame.excluded ? " removed" : ""}`);
    const inspect = el("button"), img = el("img"), label = el("label"), check = el("input");
    inspect.setAttribute("aria-label", `Inspect frame ${timelinePage * 24 + index + 1}`);
    img.src = media(timeline.id, "thumbs", `${frame.id}.jpg`);
    img.alt = `Photo taken ${date(frame.captured_at)}`;
    img.loading = "lazy";
    inspect.append(img);
    inspect.onclick = () => { stopPlayback(); displayFrame(frame); };
    check.type = "checkbox";
    check.value = frame.id;
    check.onchange = () => {
      if (check.checked) selectedFrames.add(frame.id); else selectedFrames.delete(frame.id);
      card.classList.toggle("selected", check.checked);
      $("select-page").checked = selectedFrames.size === timeline.frames.length;
      $("select-page").indeterminate = selectedFrames.size > 0 && selectedFrames.size < timeline.frames.length;
      updateVideoButtons();
    };
    const text = el("span", `Frame ${timelinePage * 24 + index + 1}`);
    text.append(el("small", date(frame.captured_at)), el("small", frame.excluded ? "Removed from video" : "Included in video"));
    label.append(check, text);
    card.append(inspect, label);
    $("timeline-grid").append(card);
  }
  if (!timeline.total) $("timeline-grid").append(el("div", "Your captured photos will appear here.", "empty"));
}
function displayFrame(frame, imageSource) {
  currentFrame = frame;
  frameIndex = Math.max(0, frame.video_index);
  $("timeline-scrubber").value = frameIndex;
  $("timeline-scrubber").setAttribute("aria-valuetext", `Frame ${frameIndex + 1} of ${timeline.included}`);
  $("timeline-image").src = imageSource || media(timeline.id, "thumbs", `${frame.id}.jpg`);
  $("timeline-image").hidden = false;
  $("timeline-empty").hidden = true;
  $("frame-original").hidden = false;
  $("frame-original").href = media(timeline.id, "frames", `${frame.id}.jpg`);
  $("current-frame-label").textContent = `${date(frame.captured_at, project(), {dateStyle: "medium", timeStyle: "medium"})} · ${frame.excluded ? "Removed from video" : "Included in video"}`;
  $("remove-current").textContent = frame.excluded ? "Restore this frame" : "Remove this frame";
  updateVideoButtons();
}
async function showVideoFrame(index) {
  if (!timeline?.included) return;
  const request = ++frameRequest, version = routeVersion, data = timeline;
  index = Math.min(Math.max(0, index), data.included - 1);
  const block = Math.floor(index / 100);
  if (!frameCache.has(block)) {
    const page = await api(endpoint(data.id, `timeline?included_only=true&limit=100&offset=${block * 100}&cutoff=${data.cutoff}`));
    if (request !== frameRequest || version !== routeVersion) return;
    if (page.included !== data.included || page.excluded !== data.excluded) {
      stopPlayback();
      await loadTimeline();
      notify("The frame selection changed in another window. Preview refreshed.");
      return;
    }
    if (frameCache.size >= 3) frameCache.delete(frameCache.keys().next().value);
    frameCache.set(block, page.frames);
  }
  const frame = frameCache.get(block)[index % 100];
  if (!frame) return;
  const image = new Image();
  image.src = media(data.id, "thumbs", `${frame.id}.jpg`);
  await image.decode();
  if (request !== frameRequest || version !== routeVersion) return;
  frameIndex = index;
  $("timeline-scrubber").value = index;
  $("timeline-scrubber").setAttribute("aria-valuetext", `Frame ${index + 1} of ${data.included}`);
  displayFrame(frame, image.src);
}
async function playbackStep() {
  if (!playing) return;
  try {
    const started = performance.now();
    await showVideoFrame(frameIndex);
    if (!playing) return;
    if (frameIndex >= timeline.included - 1) { stopPlayback(); return; }
    playTimer = setTimeout(() => { frameIndex++; playbackStep(); }, Math.max(0, 1000 / project().settings.export_fps - (performance.now() - started)));
  } catch (error) { stopPlayback(); notify(`Preview could not load a frame: ${error.message}`, true); }
}
async function editFrames(ids, excluded) {
  if (!ids.length || selectionBusy) return;
  stopPlayback();
  const id = currentId, version = routeVersion;
  selectionBusy = true;
  updateVideoButtons();
  try {
    await api(endpoint(id, "frames/selection"), "PATCH", { frame_ids: ids, excluded });
    if (version !== routeVersion) return;
    await loadTimeline();
    notify(`${ids.length} frame${ids.length === 1 ? "" : "s"} ${excluded ? "removed from the video. Original photos are kept." : "restored to the video."}`);
  } catch (error) { notify(error.message, true); }
  finally { if (version === routeVersion) { selectionBusy = false; updateVideoButtons(); } }
}
function timelineAction(callback) { Promise.resolve().then(callback).catch((error) => notify(error.message, true)); }
$("refresh-timeline").onclick = () => timelineAction(() => loadTimeline(true));
$("timeline-previous").onclick = () => { timelinePage--; timelineAction(() => loadTimeline()); };
$("timeline-next").onclick = () => { timelinePage++; timelineAction(() => loadTimeline()); };
$("select-page").onchange = (event) => {
  for (const check of $("timeline-grid").querySelectorAll('input[type="checkbox"]')) {
    check.checked = event.target.checked;
    if (check.checked) selectedFrames.add(check.value); else selectedFrames.delete(check.value);
    check.closest("article").classList.toggle("selected", check.checked);
  }
  updateVideoButtons();
};
$("remove-selected").onclick = () => editFrames([...selectedFrames], true);
$("restore-selected").onclick = () => editFrames([...selectedFrames], false);
$("remove-current").onclick = () => currentFrame && editFrames([currentFrame.id], !currentFrame.excluded);
$("play-timeline").onclick = () => {
  if (playing) { stopPlayback(); return; }
  if (frameIndex >= timeline.included - 1) frameIndex = 0;
  playing = true;
  $("play-timeline").textContent = "Pause preview";
  playbackStep();
};
$("timeline-scrubber").oninput = (event) => { stopPlayback(); timelineAction(() => showVideoFrame(Number(event.target.value))); };
$("frame-back").onclick = () => { stopPlayback(); timelineAction(() => showVideoFrame(frameIndex - 1)); };
$("frame-forward").onclick = () => { stopPlayback(); timelineAction(() => showVideoFrame(frameIndex + 1)); };
document.addEventListener("visibilitychange", () => { if (document.hidden) stopPlayback(); });

// Interpolate from recorder time, so a different browser clock does not skew timers.
function projectNow(p) {
  return p.server_time + Math.max(0, performance.now() - p.receivedAt) / 1000;
}
function countdown(seconds) {
  seconds = Math.max(0, Math.ceil(seconds));
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds / 3600) % 24;
  const minutes = Math.floor(seconds / 60) % 60, secs = seconds % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return days ? `${days}d ${hours}h` : seconds >= 3600
    ? `${pad(hours)}:${pad(minutes)}:${pad(secs)}` : `${pad(minutes)}:${pad(secs)}`;
}
function progressRing(p, kind) {
  const node = el("div", undefined, `project-ring ${kind}`);
  node.dataset.project = p.id;
  node.dataset.ring = kind;
  const graphic = el("div", undefined, "ring-graphic");
  graphic.setAttribute("role", "progressbar");
  graphic.setAttribute("aria-valuemin", "0");
  graphic.setAttribute("aria-valuemax", "100");
  const value = el("span", "—", "ring-value");
  value.setAttribute("aria-hidden", "true");
  graphic.append(value);
  const copy = el("div", undefined, "ring-copy");
  copy.append(el("span", kind === "capture" ? "Next photo" : "Completion", "ring-title"), el("span", "", "ring-detail"));
  node.append(graphic, copy);
  updateRing(node, p);
  return node;
}
function updateRing(node, p) {
  const r = p.runtime, now = projectNow(p), next = r.next_capture_at;
  const finished = r.ends_at != null && now >= r.ends_at;
  const stale = performance.now() - p.receivedAt > 15000;
  let fraction = 0, value = "—", detail, description;
  if (node.dataset.ring === "completion") {
    const duration = r.ends_at - r.started_at;
    fraction = r.started_at != null && duration > 0 ? Math.max(0, Math.min(1, (now - r.started_at) / duration)) : 0;
    const percent = Math.floor(fraction * 1000) / 10;
    value = `${percent}%`;
    detail = finished ? "Complete" : r.started_at == null ? "Not started"
      : `Day ${Math.min(p.settings.duration_days, Math.max(1, Math.floor((now - r.started_at) / 86400) + 1))} of ${p.settings.duration_days}`;
    description = r.started_at == null ? "Project has not started" : `${percent}% of project duration elapsed. ${finished ? "Completed" : "Ends"} ${date(r.ends_at, p)}. Pauses count toward the end date.`;
  } else if (finished || !r.running) {
    detail = finished ? "Complete" : r.started_at == null ? "Not started" : "Paused";
    description = `Next photo: ${detail.toLowerCase()}. No capture scheduled.`;
  } else if (next != null && r.ends_at != null && next >= r.ends_at) {
    detail = "No more scheduled";
    description = "No more photos are scheduled before this project's end date.";
  } else if (next == null || next <= now) {
    fraction = 1;
    value = "00:00";
    detail = r.last_error ? "Retry due" : "Due now";
    description = "Photo due now. Waiting for the recorder; camera warm-up or other projects may delay the photo.";
  } else {
    const remaining = next - now;
    // Include overnight waits; retries fill over their one-minute retry period.
    const interval = r.last_error ? 60 : p.settings.interval_minutes * 60;
    const previous = p.frames.last_at ?? r.started_at ?? (next - interval);
    const cycleStart = r.last_error ? next - interval : Math.min(previous, next - interval);
    fraction = Math.max(0, Math.min(1, (now - cycleStart) / Math.max(1, next - cycleStart)));
    value = countdown(remaining);
    detail = r.last_error ? "Until retry" : remaining > interval && p.settings.daylight_only ? "Until capture window" : "Until next photo";
    description = `${Math.ceil(remaining)} seconds until ${r.last_error ? "capture retry" : "next scheduled photo"}, at ${date(next, p, {dateStyle: "medium", timeStyle: "medium"})}. Camera warm-up may add a few seconds.`;
  }
  if (stale) {
    value = "—";
    detail = "Reconnecting…";
    description = "Waiting for an updated schedule from the recorder.";
  }
  const graphic = node.querySelector(".ring-graphic");
  graphic.style.setProperty("--ring-progress", `${fraction * 100}%`);
  graphic.setAttribute("aria-label", `${p.settings.name}: ${node.dataset.ring === "capture" ? "Next photo" : "Project completion"}`);
  graphic.setAttribute("aria-valuetext", description);
  if (stale) graphic.removeAttribute("aria-valuenow");
  else graphic.setAttribute("aria-valuenow", String(Math.round(fraction * 1000) / 10));
  node.classList.toggle("muted", stale || (node.dataset.ring === "capture" && (!r.running || finished)));
  node.classList.toggle("retrying", node.dataset.ring === "capture" && r.running && Boolean(r.last_error));
  graphic.title = description;
  node.querySelector(".ring-value").textContent = value;
  node.querySelector(".ring-value").classList.toggle("long-countdown", value.length > 5);
  node.querySelector(".ring-detail").textContent = detail;
}
function updateProgressRings() {
  const byId = new Map(projects.map((p) => [p.id, p]));
  for (const node of document.querySelectorAll("[data-ring]")) {
    const p = byId.get(node.dataset.project);
    if (p) updateRing(node, p);
  }
}
setInterval(() => { if (!document.hidden) updateProgressRings(); }, 1000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    updateProgressRings();
    refresh().catch(() => {});
  }
});

route();
setInterval(() => {
  if (!document.hidden) refresh().catch(() => {});
}, 5000);
