"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let state = null,
  page = "stacks",
  refreshBusy = false,
  toastTimer,
  folderTrail = [];
const pages = {
  stacks: "Stacks",
  destinations: "Destinations",
  mappings: "Image mappings",
  activity: "Activity",
  notifications: "Notifications",
};
function toast(message, failure = false) {
  $("toast").textContent = message;
  $("toast").className = "visible" + (failure ? " failure" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").className = ""), 7000);
}
async function api(url, method = "GET", data) {
  const response = await fetch("/api" + url, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "cloud-backup",
    },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("The app did not respond. Check its container status.");
  }
  if (!response.ok) {
    if (response.status === 401 && url !== "/login") showLogin();
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "Check the values and try again.",
    );
  }
  return result;
}
function date(value, timezone) {
  if (!value) return "Not scheduled";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    ...(timezone ? { timeZone: timezone, timeZoneName: "short" } : {}),
  }).format(new Date(value));
}
function size(bytes) {
  if (bytes == null) return "Size unavailable";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const n = Math.min(3, Math.floor(Math.log(bytes) / Math.log(1000)));
  return (bytes / 1000 ** n).toFixed(n ? 1 : 0) + " " + units[n];
}
function selectionSummary(stack) {
  const composeOnly = stack.backup_selection.mode === "auto" && stack.containers.length > 0
    && stack.containers.every((c) => c.rule && !c.rule.paths.length);
  const label = stack.backup_selection.mode === "volumes" ? "Selected volumes"
    : composeOnly ? "Compose files" : "Config files";
  return `${label} — ${stack.backup_size_bytes == null ? "Size unavailable" : "≈" + size(stack.backup_size_bytes)}`;
}
function badge(status) {
  const colors = {
    success: "good",
    failed: "bad",
    partial: "warn",
    interrupted: "warn",
    queued: "progress",
    running: "progress",
    uploading: "progress",
  };
  return `<span class="badge ${colors[status] || "neutral"}">${esc(status.charAt(0).toUpperCase() + status.slice(1))}</span>`;
}
function showLogin() {
  $("shell").classList.add("hidden");
  $("login").classList.remove("hidden");
  state = null;
}
function showApp() {
  $("login").classList.add("hidden");
  $("shell").classList.remove("hidden");
  route();
}
function route() {
  page = location.hash.slice(1);
  if (!pages[page]) page = "stacks";
  document
    .querySelectorAll(".page")
    .forEach((el) => el.classList.toggle("hidden", el.id !== "page-" + page));
  document
    .querySelectorAll("nav a")
    .forEach((el) => el.classList.toggle("active", el.dataset.page === page));
  $("page-label").textContent = pages[page];
  if (state) {
    render();
    if (page === "destinations" || page === "notifications") fillSettings();
  }
}
function getStack(name) {
  return state.stacks.find((s) => s.name === name);
}
async function refresh() {
  if (refreshBusy) return;
  refreshBusy = true;
  try {
    state = await api("/dashboard");
    render();
  } finally {
    refreshBusy = false;
  }
}
function render() {
  if (!state) return;
  $("stack-count").textContent = state.stacks.length;
  $("stack-total").textContent = state.stacks.length;
  $("host-state").textContent = state.docker_error
    ? "Connection unavailable"
    : "Connected · auto-discovery";
  let alerts = [];
  if (state.docker_error) alerts.push(state.docker_error);
  if (state.recovery.length)
    alerts.push(
      "Restart recovery needs attention: " +
        state.recovery.map((r) => r.name).join(", ") +
        ". The app retries every ten seconds; new backups are paused.",
    );
  $("global-alert").innerHTML = alerts
    .map((a) => `<div class="alert">${esc(a)}</div>`)
    .join("");
  renderStacks();
  renderJobs();
  renderRules();
}
function renderStacks() {
  const scheduled = state.stacks.filter((s) => s.schedule.enabled).length,
    latest = state.jobs[0],
    destinations =
      Number(state.settings.smb_enabled) + Number(state.settings.drive_enabled);
  const metric = (title, value, note, icon) =>
    `<article class="metric"><div class="metric-top"><span>${title}</span><span>${icon}</span></div><strong>${value}</strong><small>${note}</small></article>`;
  $("metrics").innerHTML =
    metric(
      "Discovered stacks",
      state.stacks.length,
      "On this Docker host",
      "▦",
    ) +
    metric(
      "Scheduled backups",
      scheduled,
      scheduled
        ? "<em>Schedules enabled</em>"
        : "Choose a schedule to get started",
      "◷",
    ) +
    metric(
      "Remote destinations",
      destinations,
      destinations
        ? "Local copy always included"
        : "Local backups are available",
      "↗",
    ) +
    metric(
      "Latest backup",
      latest
        ? esc(latest.status.charAt(0).toUpperCase() + latest.status.slice(1))
        : "No runs yet",
      latest ? esc(date(latest.started)) : "Ready when you are",
      "✓",
    );
  const query = $("stack-search").value.toLowerCase();
  const stacks = state.stacks.filter((s) =>
    s.name.toLowerCase().includes(query),
  );
  if (!stacks.length) {
    $("stacks-list").innerHTML =
      `<div class="empty"><h3>${query ? "No matching stacks" : "No stacks discovered yet"}</h3><p>${query ? "Try a different search." : "Compose labels and saved Dockge stacks will appear here. Check Docker and the read-only host mount."}</p></div>`;
    return;
  }
  $("stacks-list").innerHTML =
    `<div class="table-head"><span>STACK</span><span>BACKUP CONTENTS</span><span>VOLUMES</span><span>BACKUP SCHEDULE</span><span></span></div>` +
    stacks
      .map((s) => {
        const ready = !s.issues.length;
        const running = s.containers.filter(
          (c) => c.state === "running",
        ).length;
        const coverage = s.excluded
          ? '<span class="badge neutral">Excluded</span>'
          : ready
            ? '<span class="badge good">✓ Ready to back up</span>'
            : '<span class="badge warn">Needs review</span>';
        const mode = selectionSummary(s);
        const volumes = s.volumes.map((v) => `<li><strong>${esc(v.name)}</strong><small>${v.type === "bind" ? "Bind mount" : "Named volume"} · ${size(v.size_bytes)}${s.backup_selection.mode === "volumes" && s.backup_selection.volumes.includes(v.id) ? " · Included" : ""}</small></li>`).join("");
        return `<article class="stack-row"><div class="stack-name"><span class="stack-icon">▦</span><div><strong>${esc(s.name)}</strong><small>${s.containers.length} container${s.containers.length === 1 ? "" : "s"} · ${running} running</small></div></div><div class="state-cell">${coverage}<small title="Approximate uncompressed total, including Compose files. Refreshed about once a minute.">${esc(mode)}</small><button class="text-button" data-action="selection" data-name="${esc(s.name)}" ${s.excluded ? "disabled" : ""}>Choose contents</button></div><div class="volumes-cell"><span class="mobile-label">Volumes</span>${volumes ? `<ul class="volume-list">${volumes}</ul>` : `<small>${s.containers.length ? "No volumes mounted · Config can still be backed up" : "Create containers to discover volumes"}</small>`}</div><div class="schedule-cell">${s.schedule.enabled ? `<code>${esc(s.schedule.cron)}</code><small>Next: ${esc(date(s.schedule.next_run, s.schedule.timezone))}</small>` : '<span class="badge neutral">Paused</span><small>Manual backup available</small>'}</div><div class="row-actions"><button class="secondary" data-action="schedule" data-name="${esc(s.name)}" ${s.excluded ? "disabled" : ""}>Schedule</button><button class="${ready ? "primary" : "secondary"}" data-action="review" data-name="${esc(s.name)}">${ready ? "Back up" : "Review"}</button></div></article>`;
      })
      .join("");
}
function renderJobs() {
  if (!state.jobs.length) {
    $("jobs-list").innerHTML =
      '<div class="empty"><h3>A fresh start.</h3><p>Your first backup will appear here with its result, archive and checksum.</p></div>';
    return;
  }
  $("jobs-list").innerHTML = state.jobs
    .map(
      (j) =>
        `<article class="job-card"><div class="job-title"><h3>${esc(j.stack)}</h3>${badge(j.status)}<time>${esc(date(j.started))}</time></div><p>${esc(j.detail || "Waiting for the backup worker…")}</p>${j.notification_error ? `<p class="error">Notification: ${esc(j.notification_error)}</p>` : ""}<div class="job-meta"><span>${size(j.size)}</span>${j.finished ? `<span>Finished ${esc(date(j.finished))}</span>` : ""}${j.archive ? `<a href="/api/jobs/${esc(j.id)}/download">↓ Download archive</a>` : ""}${j.archive && ["partial", "success"].includes(j.status) ? `<button class="text-button" data-action="retry" data-id="${esc(j.id)}" ${state.busy ? "disabled" : ""}>Retry upload</button>` : ""}</div>${j.sha256 ? `<details><summary>SHA-256 checksum</summary><p class="checksum"><code>${esc(j.sha256)}</code></p></details>` : ""}</article>`,
    )
    .join("");
}
function renderRules() {
  const query = $("mapping-search").value.toLowerCase();
  $("rules-list").innerHTML =
    state.rules
      .filter((r) => r.pattern.toLowerCase().includes(query))
      .map(
        (r) =>
          `<article class="rule-row"><div><strong>${esc(r.pattern)}</strong><p>${r.paths.length ? r.paths.map((p) => `<code>${esc(p)}</code>`).join(" &nbsp; ") : "Compose only · explicitly stateless"}${r.excludes.length ? `<br>Exclude: ${esc(r.excludes.join(", "))}` : ""}</p></div><button class="secondary" data-action="edit-rule" data-pattern="${esc(r.pattern)}">Edit mapping</button></article>`,
      )
      .join("") ||
    '<div class="empty"><p>No matching image mappings.</p></div>';
}
function fillSettings() {
  const s = state.settings;
  for (const [key, value] of Object.entries(s)) {
    const el = $(key);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = value;
    else el.value = value;
  }
  $("smb-badge").textContent = s.smb_enabled ? "Enabled" : "Off";
  $("smb-badge").className = "badge " + (s.smb_enabled ? "good" : "neutral");
  $("drive-badge").textContent = s.drive_token_set
    ? "Connected"
    : "Not connected";
  $("drive-badge").className =
    "badge " + (s.drive_token_set ? "good" : "neutral");
  $("smb_password").placeholder = s.smb_password_set
    ? "Saved · leave blank to keep"
    : "Enter share password";
  $("drive-setup").open = !s.drive_token_set;
  notificationFields();
}
function notificationFields() {
  const kind = $("notification_type").value;
  $("ntfy-fields").classList.toggle("hidden", kind !== "ntfy");
  $("telegram-fields").classList.toggle("hidden", kind !== "telegram");
  $("token-field").classList.toggle("hidden", kind === "none");
}
function values(keys) {
  const result = {};
  for (const key of keys) {
    const el = $(key);
    result[key] =
      el.type === "checkbox"
        ? el.checked
        : el.type === "number"
          ? Number(el.value)
          : el.value.trim();
  }
  return result;
}
async function saveSettings(data) {
  state.settings = await api("/settings", "PUT", data);
  toast("Settings saved.");
  await refresh();
}
function openModal(eyebrow, html) {
  $("modal-eyebrow").textContent = eyebrow;
  $("modal-content").innerHTML = html;
  $("modal").showModal();
}
function closeModal() {
  $("modal").close();
}
function scheduleDialog(name) {
  const s = getStack(name),
    parts = s.schedule.cron.split(" ");
  openModal(
    "BACKUP SCHEDULE",
    `<h2>${esc(name)}</h2><p>Choose when this stack pauses briefly for a consistent config backup.</p><form id="schedule-form"><label>Frequency<select id="preset"><option value="custom">Custom schedule</option><option value="0 * * * *">Every hour</option><option value="0 3 * * *">Every day at 03:00</option><option value="0 3 * * 0">Every Sunday at 03:00</option><option value="0 3 1 * *">First day of the month at 03:00</option></select></label><div class="cron-grid">${["Minute", "Hour", "Day", "Month", "Weekday"].map((label, i) => `<label>${label}<input id="cron-${i}" value="${esc(parts[i])}" required aria-label="Cron ${label.toLowerCase()}"></label>`).join("")}</div><div id="cron-expression" class="cron-expression"></div><label>Timezone<input id="schedule-timezone" value="${esc(s.schedule.timezone)}" placeholder="Europe/London" required></label><label class="check"><input id="schedule-enabled" type="checkbox" ${s.schedule.enabled ? "checked" : ""}>Enable scheduled backups</label><p class="hint">Five-field cron. Sunday is 0 or 7. Day-of-month and weekday use standard cron OR semantics. Preview uses this timezone, including daylight saving.</p><div class="preview-label">NEXT FIVE RUNS</div><div id="cron-preview"></div><div class="actions"><button class="primary">Save schedule</button><button type="button" class="secondary" data-action="close">Cancel</button></div></form>`,
  );
  if ([...$("preset").options].some((o) => o.value === s.schedule.cron))
    $("preset").value = s.schedule.cron;
  let timer;
  const preview = () => {
    const cron = [0, 1, 2, 3, 4].map((i) => $("cron-" + i).value).join(" ");
    $("cron-expression").textContent = cron;
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        const tz = $("schedule-timezone").value;
        const result = await api("/cron/preview", "POST", {
          cron,
          timezone: tz,
          enabled: false,
        });
        if ($("cron-preview"))
          $("cron-preview").innerHTML =
            '<ol class="preview-list">' +
            result.dates.map((d) => `<li>${esc(date(d, tz))}</li>`).join("") +
            "</ol>";
      } catch (e) {
        if ($("cron-preview"))
          $("cron-preview").innerHTML =
            `<p class="error">${esc(e.message)}</p>`;
      }
    }, 250);
  };
  $("preset").onchange = () => {
    if ($("preset").value !== "custom")
      $("preset")
        .value.split(" ")
        .forEach((v, i) => ($("cron-" + i).value = v));
    preview();
  };
  for (let i = 0; i < 5; i++)
    $("cron-" + i).oninput = () => {
      $("preset").value = "custom";
      preview();
    };
  $("schedule-timezone").oninput = preview;
  preview();
  $("schedule-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api(`/stacks/${encodeURIComponent(name)}/schedule`, "PUT", {
        cron: [0, 1, 2, 3, 4].map((i) => $("cron-" + i).value).join(" "),
        timezone: $("schedule-timezone").value,
        enabled: $("schedule-enabled").checked,
      });
      closeModal();
      toast("Schedule saved.");
      await refresh();
    } catch (e) {
      toast(e.message, true);
    }
  };
}
function reviewDialog(name) {
  const s = getStack(name);
  openModal(
    "BACKUP CONTENTS",
    `<h2>${esc(name)}</h2><p>${esc(selectionSummary(s))}<br><span class="hint">Approximate uncompressed total, including Compose files.</span></p><p>Running containers in this stack will stop together, then restart before the archive is uploaded. Already stopped containers stay stopped.</p>${s.issues.length ? `<ul class="issue-list">${s.issues.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}<button class="secondary" data-action="selection" data-name="${esc(name)}" ${s.excluded ? "disabled" : ""}>Choose backup contents</button><p><strong>Compose files</strong><br>${s.compose_files.map(esc).join("<br>") || "Not found"}</p><ul class="backup-items">${s.containers.map((c) => `<li><strong>${esc(c.name)}</strong> <span class="badge neutral">${esc(c.state)}</span><small>${esc(c.image)}</small><small>${c.backup_paths.length ? c.backup_paths.map((p) => `<code>${esc(p.path)}</code>${p.kind === "volume" ? " (volume)" : ""}${p.excludes.length ? ` · Excluded: ${esc(p.excludes.join(", "))}` : ""}`).join("<br>") : s.backup_selection.mode === "auto" && !c.rule ? `<button class="text-button" data-action="map-image" data-image="${esc(c.image)}">+ Add config mapping</button>` : "No additional paths selected"}</small><small>Mounted paths: ${esc(c.mounts.map((m) => m.destination).join(", ") || "None")}</small></li>`).join("")}</ul><div class="actions"><button class="primary" data-action="backup" data-name="${esc(name)}" ${s.issues.length || state.busy ? "disabled" : ""}>Stop, back up & restart</button><button class="secondary" data-action="close">Close</button></div>`,
  );
}
function selectionDialog(name) {
  const s = getStack(name), selection = s.backup_selection;
  const services = [...new Set(s.containers.map((c) => c.service))];
  closeModal();
  openModal(
    "CHOOSE BACKUP CONTENTS",
    `<h2>${esc(name)}</h2><p>Current selection: ${esc(selectionSummary(s))}</p><p>Compose files are always included. These choices apply to manual and scheduled backups.</p><form id="selection-form"><label>Back up<select id="selection-mode"><option value="auto">Automatically matched config paths</option><option value="volumes">One or more volumes</option><option value="manual">Manually specified config files or folders</option></select></label><div id="selection-auto"><p class="hint">Use each image's config mapping. Every mapped path is checked in its container.</p><ul class="backup-items">${s.containers.map((c) => `<li><strong>${esc(c.name)}</strong><small>${c.rule ? esc(c.rule.paths.join(", ") || "Compose only · stateless") : "No matching image mapping"}</small>${c.rule ? `<button type="button" class="text-button" data-action="edit-rule" data-pattern="${esc(c.rule.pattern)}">Edit image mapping</button>` : `<button type="button" class="text-button" data-action="map-image" data-image="${esc(c.image)}">Add image mapping</button>`}</li>`).join("")}</ul></div><div id="selection-volumes"><p class="hint">Include the complete contents of each selected volume or bind mount. Image mapping exclusions do not apply. Shared volumes are copied once; nested mounts are included only when selected separately.</p>${s.volumes.map((v) => `<label class="check volume-choice"><input type="checkbox" name="volume" value="${esc(v.id)}" ${selection.volumes.includes(v.id) ? "checked" : ""}><span><strong>${esc(v.name)}</strong><small>${v.type === "bind" ? "Bind mount" : "Named volume"} · ${size(v.size_bytes)}</small><small>${v.mounts.map((m) => `${esc(m.service)}: ${esc(m.destination)}`).join("<br>")}</small></span></label>`).join("") || '<p class="hint">No mounted volumes available. For existing containers, use config mappings or manual config paths instead.</p>'}<p class="hint">Sizes are uncompressed file totals, refreshed about once a minute. Unreadable or very large folders show “Size unavailable”; they can still be selected.</p></div><div id="selection-manual"><p class="hint">Enter absolute paths inside each service’s container, one per line. Leave a service blank to skip its config. Paths are checked before saving.</p>${services.map((service, i) => `<label>${esc(service)}<textarea id="selection-paths-${i}" rows="3" placeholder="/config/settings.yaml" spellcheck="false">${esc((selection.paths[service] || []).join("\n"))}</textarea></label>`).join("")}</div><p id="selection-error" class="error" role="alert"></p><div class="actions"><button class="primary" ${!s.containers.length || s.excluded ? "disabled" : ""}>Save backup contents</button><button type="button" class="secondary" data-action="close">Cancel</button></div></form>`,
  );
  $("selection-mode").value = selection.mode;
  const updateMode = () => {
    for (const mode of ["auto", "volumes", "manual"])
      $("selection-" + mode).classList.toggle("hidden", $("selection-mode").value !== mode);
    $("selection-error").textContent = "";
  };
  $("selection-mode").onchange = updateMode;
  updateMode();
  $("selection-form").onsubmit = async (event) => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const mode = $("selection-mode").value;
      const paths = Object.fromEntries(services.map((service, i) => [service, $("selection-paths-" + i).value.split("\n").map((p) => p.trim()).filter(Boolean)]));
      await api(`/stacks/${encodeURIComponent(name)}/selection`, "PUT", {
        mode,
        volumes: mode === "volumes" ? [...document.querySelectorAll('input[name="volume"]:checked')].map((el) => el.value) : [],
        paths: mode === "manual" ? paths : {},
      });
      closeModal();
      toast("Backup contents saved and paths checked.");
      await refresh();
    } catch (error) {
      if ($("selection-error")) $("selection-error").textContent = error.message;
      button.disabled = false;
    }
  };
}
function ruleDialog(pattern = "", image = "") {
  let rule = state.rules.find((r) => r.pattern === pattern) || {
    pattern: pattern || image.split("@")[0].replace(/:[^/]*$/, ""),
    paths: [],
    excludes: [],
  };
  if (rule.pattern && !rule.pattern.includes("/"))
    rule.pattern = "library/" + rule.pattern;
  openModal(
    "IMAGE CONFIG MAPPING",
    `<h2>${pattern ? "Edit mapping" : "Add a mapping"}</h2><p>Include only config and required application state. Do not include media, downloads or system directories.</p><form id="rule-form"><label>Image repository / pattern<input id="rule-pattern" value="${esc(rule.pattern)}" placeholder="lscr.io/linuxserver/sonarr" ${pattern ? "readonly" : ""} required></label><label>Config paths · one per line<textarea id="rule-paths" rows="4" placeholder="/config" spellcheck="false">${esc(rule.paths.join("\n"))}</textarea></label><label>Exclude patterns · one per line<textarea id="rule-excludes" rows="3" placeholder="logs/**&#10;cache/**" spellcheck="false">${esc(rule.excludes.join("\n"))}</textarea></label><p class="hint">Exclusions are relative to each selected config folder. Use an empty path list only for an intentionally stateless image. More specific image patterns win.</p><label class="check"><input id="stateless-confirm" type="checkbox" ${!rule.paths.length && pattern ? "checked" : ""}>This image has no config files (Compose only)</label><p id="rule-error" class="error" role="alert"></p><div class="actions"><button class="primary">Save mapping</button><button type="button" class="secondary" data-action="close">Cancel</button></div></form>`,
  );
  $("rule-form").onsubmit = async (e) => {
    e.preventDefault();
    const button = e.submitter;
    button.disabled = true;
    $("rule-error").textContent = "";
    try {
      const lines = (id) =>
        $(id)
          .value.split("\n")
          .map((v) => v.trim())
          .filter(Boolean);
      const paths = lines("rule-paths");
      if (!paths.length && !$("stateless-confirm").checked)
        throw new Error(
          "Add a config path or explicitly mark the image as stateless.",
        );
      const result = await api("/rules", "PUT", {
        pattern: $("rule-pattern").value.trim(),
        paths,
        excludes: lines("rule-excludes"),
      });
      closeModal();
      toast(result.checked_containers ? "Image mapping saved and paths checked." : "Mapping saved. No matching containers exist yet; paths will be checked when discovered.");
      await refresh();
    } catch (e) {
      if ($("rule-error") && $("modal").open) $("rule-error").textContent = e.message;
      else toast(e.message, true);
    } finally {
      button.disabled = false;
    }
  };
}
async function browseFolders(id = "root", name = "My Drive", up = false) {
  if (!up) folderTrail.push({ id, name });
  if (!$("modal").open)
    openModal(
      "GOOGLE DRIVE",
      "<h2>Choose a backup folder</h2><p>Loading folders…</p>",
    );
  else
    $("modal-content").innerHTML =
      "<h2>Choose a backup folder</h2><p>Loading folders…</p>";
  try {
    const result = await api("/drive/folders?parent=" + encodeURIComponent(id));
    $("modal-content").innerHTML =
      `<h2>Choose a backup folder</h2><div class="folder-crumbs">${folderTrail.map((f) => esc(f.name)).join(" / ")}</div>${folderTrail.length > 1 ? '<button class="secondary" data-action="folder-up">← Parent folder</button>' : ""}<div class="folder-list">${result.folders.map((f) => `<button data-action="folder" data-id="${esc(f.id)}" data-name="${esc(f.name)}"><span>▱ &nbsp; ${esc(f.name)}</span><span>›</span></button>`).join("") || '<p class="muted">No subfolders here.</p>'}</div><p class="hint">A cloud-backup subfolder will hold this app’s archives. Other files are left untouched.</p><div class="actions"><button class="primary" data-action="select-folder">Use this folder</button><button class="secondary" data-action="close">Cancel</button></div>`;
  } catch (e) {
    $("modal-content").innerHTML =
      `<h2>Could not load folders</h2><p class="error">${esc(e.message)}</p><button class="secondary" data-action="close">Close</button>`;
  }
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-action]");
  if (!b || b.disabled) return;
  try {
    switch (b.dataset.action) {
      case "close":
        closeModal();
        break;
      case "schedule":
        scheduleDialog(b.dataset.name);
        break;
      case "review":
        reviewDialog(b.dataset.name);
        break;
      case "selection":
        selectionDialog(b.dataset.name);
        break;
      case "edit-rule":
        closeModal();
        ruleDialog(b.dataset.pattern);
        break;
      case "map-image":
        closeModal();
        ruleDialog("", b.dataset.image);
        break;
      case "backup":
        b.disabled = true;
        await api(
          `/stacks/${encodeURIComponent(b.dataset.name)}/backup`,
          "POST",
        );
        closeModal();
        toast("Backup started. Follow progress in Activity.");
        location.hash = "activity";
        await refresh();
        break;
      case "retry":
        await api(`/jobs/${b.dataset.id}/retry`, "POST");
        toast("Upload retry started.");
        await refresh();
        break;
      case "folder":
        await browseFolders(b.dataset.id, b.dataset.name);
        break;
      case "folder-up":
        folderTrail.pop();
        const parent = folderTrail.at(-1);
        await browseFolders(parent.id, parent.name, true);
        break;
      case "select-folder":
        const selected = folderTrail.at(-1);
        $("drive_folder_id").value = selected.id;
        $("drive_folder_name").value = folderTrail
          .map((f) => f.name)
          .join(" / ");
        closeModal();
        toast("Folder selected. Save mirror settings to apply.");
        break;
    }
  } catch (e) {
    toast(e.message, true);
    b.disabled = false;
  }
});
$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    await api("/login", "POST", { password: $("password").value });
    $("password").value = "";
    await refresh();
    showApp();
  } catch (e) {
    $("login-error").textContent = e.message;
  }
};
$("logout").onclick = async () => {
  try {
    await api("/logout", "POST");
    showLogin();
  } catch (e) {
    toast(e.message, true);
  }
};
$("refresh").onclick = () =>
  refresh()
    .then(() => toast("Stack discovery refreshed."))
    .catch((e) => toast(e.message, true));
$("stack-search").oninput = renderStacks;
$("mapping-search").oninput = renderRules;
$("new-rule").onclick = () => ruleDialog();
$("close-modal").onclick = closeModal;
$("smb-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await saveSettings(
      values([
        "smb_enabled",
        "smb_path",
        "smb_user",
        "smb_password",
        "smb_domain",
      ]),
    );
    fillSettings();
  } catch (e) {
    toast(e.message, true);
  }
};
$("drive-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await saveSettings(
      values(["drive_enabled", "drive_folder_id", "drive_folder_name"]),
    );
    fillSettings();
  } catch (e) {
    toast(e.message, true);
  }
};
$("save-drive-token").onclick = async () => {
  try {
    const data = values([
      "drive_token",
      "drive_client_id",
      "drive_client_secret",
    ]);
    if (!data.drive_token) throw new Error("Paste the connection token first.");
    await saveSettings({
      ...data,
      drive_enabled: false,
      drive_folder_id: "",
      drive_folder_name: "",
    });
    fillSettings();
    toast("Google Drive connected. Choose a folder to mirror backups.");
  } catch (e) {
    toast(e.message, true);
  }
};
$("google-connect").onclick = async () => {
  try {
    await saveSettings(
      values(["drive_client_id", "drive_client_secret", "oauth_redirect_uri"]),
    );
    const result = await api("/drive/connect", "POST");
    location.href = result.url;
  } catch (e) {
    toast(e.message, true);
  }
};
$("disconnect-drive").onclick = async () => {
  try {
    await api("/drive/disconnect", "POST");
    await refresh();
    fillSettings();
    toast("Google Drive disconnected. Existing backups were kept.");
  } catch (e) {
    toast(e.message, true);
  }
};
$("browse-drive").onclick = () => {
  folderTrail = [];
  browseFolders();
};
for (const provider of ["smb", "drive"]) {
  $("test-" + provider).onclick = async (e) => {
    e.target.disabled = true;
    const feedback = $(provider + "-test-result");
    feedback.className = "hint";
    feedback.textContent = "Checking the saved connection…";
    try {
      const result = await api(`/destinations/${provider}/test`, "POST");
      feedback.textContent = result.message;
      toast(result.message);
    } catch (e) {
      feedback.className = "hint error";
      feedback.textContent = e.message;
      toast(e.message, true);
    } finally {
      e.target.disabled = false;
    }
  };
}
$("retention-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await saveSettings(values(["keep_days"]));
  } catch (e) {
    toast(e.message, true);
  }
};
$("notification_type").onchange = notificationFields;
$("notification-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await saveSettings(
      values([
        "notification_type",
        "notification_url",
        "notification_topic",
        "notification_token",
        "telegram_chat_id",
        "notify_success",
      ]),
    );
    fillSettings();
  } catch (e) {
    toast(e.message, true);
  }
};
$("test-notification").onclick = async () => {
  try {
    const result = await api("/notifications/test", "POST");
    toast(result.message);
  } catch (e) {
    toast(e.message, true);
  }
};
window.addEventListener("hashchange", route);
setInterval(() => {
  if (state && !document.hidden) refresh().catch(() => {});
}, 10000);
(async () => {
  try {
    const result = await api("/session");
    if (result.authenticated) {
      await refresh();
      showApp();
    } else showLogin();
  } catch (e) {
    showLogin();
    $("login-error").textContent = e.message;
  }
})();
