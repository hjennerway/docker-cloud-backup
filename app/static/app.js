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
  refreshPromise = null,
  manualBackupBusy = false,
  restoreBusy = false,
  restoreContainers = [],
  restoreUpload = null,
  restorePreview = null,
  restoreJobId = null,
  restoreMaxBytes = 0,
  toastTimer,
  folderTrail = [];
const manualSelectionsSaving = new Map();
let selectedHost = sessionStorage.getItem("docker-host") || "local";
let hostName = "Local Docker host", pendingHostActions = 0;
let remoteHostConfigs = [];
let stackHostView = sessionStorage.getItem("stack-host-navigation") === "selected" ? "selected" : "all";
sessionStorage.removeItem("stack-host-navigation");
let stackOverview = null, overviewPromise = null;
const localEndpoints = new Set(["/login", "/logout", "/session", "/password", "/hosts", "/stack-overview"]);
function apiPath(url) {
  return "/api" + (selectedHost !== "local" && !localEndpoints.has(url) && !url.startsWith("/host-config")
    ? "/hosts/" + encodeURIComponent(selectedHost) : "") + url;
}
let stackFilter = "all";
let scheduledDays = 7,
  scheduledData = null,
  scheduledStart = null,
  scheduledPage = 0,
  scheduledCursors = [null],
  scheduledLoading = false,
  scheduledRequest = 0,
  scheduledHtml = "";
const pages = {
  stacks: "Stacks",
  scheduled: "Scheduled",
  restore: "Restore",
  destinations: "Destinations",
  mappings: "Image mappings",
  activity: "Activity",
  notifications: "Notifications",
  hosts: "Remote hosts",
};
function toast(message, failure = false) {
  $("toast").textContent = message;
  $("toast").className = "visible" + (failure ? " failure" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").className = ""), 7000);
}
async function api(url, method = "GET", data) {
  if (method !== "GET") pendingHostActions++;
  try {
    const response = await fetch(apiPath(url), {
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
  } finally {
    if (method !== "GET") pendingHostActions--;
  }
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
  if ($("modal").open) closeModal();
  $("shell").classList.add("hidden");
  $("login").classList.remove("hidden");
  state = null;
  restoreContainers = [];
  restoreUpload = restorePreview = null;
  restoreJobId = null;
  resetRestorePreview();
  $("restore-source-choice").classList.add("hidden");
  $("restore-activity").classList.add("hidden");
  $("restore-status").textContent = "";
  $("restore-file").value = "";
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
  if (page === "hosts") loadHostConfiguration().catch((e) => {
    $("remote-hosts-list").textContent = e.message;
  });
  if (page === "stacks" && stackHostView === "all" && !stackOverview) {
    loadStackOverview().then(() => { if (page === "stacks") renderStacks(); }).catch((e) => toast(e.message, true));
  }
  if (page === "stacks" && !state && stackOverview) renderStacks();
  if (state) {
    render();
    if (page === "destinations" || page === "notifications") fillSettings();
    if (page === "restore") loadRestoreContainers().catch((e) => restoreError(e));
    if (page === "scheduled") loadScheduled(true);
  }
}
function getStack(name) {
  return state.stacks.find((s) => s.name === name);
}
async function refresh(fresh = false) {
  if (fresh && refreshPromise) await refreshPromise;
  if (refreshPromise) return refreshPromise;
  refreshPromise = api("/dashboard")
    .then(async (dashboard) => {
      state = dashboard;
      if (page === "stacks" && stackHostView === "all") await loadStackOverview();
      render();
      if (page === "restore" && !restoreBusy) await loadRestoreContainers();
      if (page === "scheduled" && !scheduledLoading) await loadScheduled();
    })
    .finally(() => { refreshPromise = null; });
  return refreshPromise;
}
function render() {
  if (!state) return;
  $("stack-count").textContent = state.stacks.length;
  $("stack-total").textContent = state.stacks.length;
  $("host-state").textContent = state.docker_error
    ? "Connection unavailable"
    : "Connected · auto-discovery";
  $("host-name").textContent = hostName;
  let alerts = [];
  if (state.docker_error) alerts.push(state.docker_error);
  if (state.recovery.length && !state.busy)
    alerts.push(
      "Restart recovery needs attention: " +
        state.recovery.map((r) => r.name).join(", ") +
        ". The app retries every ten seconds; backups and restores are paused.",
    );
  $("global-alert").innerHTML = alerts
    .map((a) => `<div class="alert">${esc(a)}</div>`)
    .join("");
  renderStacks();
  renderJobs();
  renderRules();
  renderRestore();
  renderScheduled();
}

async function loadScheduled(reset = false) {
  if (reset) {
    scheduledPage = 0;
    scheduledCursors = [null];
    scheduledStart = null;
    scheduledData = null;
  }
  const request = ++scheduledRequest;
  const params = new URLSearchParams({ days: scheduledDays });
  if (scheduledStart && scheduledPage > 0) params.set("start", scheduledStart);
  const cursor = scheduledCursors[scheduledPage];
  if (cursor) {
    params.set("after", cursor.after);
    params.set("after_stack", cursor.after_stack);
  }
  scheduledLoading = true;
  $("scheduled-error").textContent = "";
  renderScheduled();
  try {
    const result = await api("/scheduled?" + params);
    if (request !== scheduledRequest || !state) return;
    scheduledData = result;
    scheduledStart = result.start;
    scheduledCursors[scheduledPage + 1] = result.next;
  } catch (e) {
    if (request === scheduledRequest && state) {
      $("scheduled-error").textContent = e.message;
      scheduledData = null;
    }
  } finally {
    if (request === scheduledRequest) {
      scheduledLoading = false;
      if (state) renderScheduled();
    }
  }
}
function setScheduledList(html) {
  if (scheduledHtml === html) return;
  const previous = $("scheduled-list").querySelector(".scheduled-table-wrap");
  const top = previous?.scrollTop || 0, left = previous?.scrollLeft || 0;
  $("scheduled-list").innerHTML = html;
  scheduledHtml = html;
  const current = $("scheduled-list").querySelector(".scheduled-table-wrap");
  if (current) {
    current.scrollTop = top;
    current.scrollLeft = left;
  }
}
function renderScheduled() {
  $("scheduled-window").textContent = `Next ${scheduledDays} days`;
  document.querySelectorAll('[data-action="scheduled-window"]').forEach((button) => {
    button.setAttribute("aria-pressed", String(Number(button.dataset.days) === scheduledDays));
  });
  $("scheduled-list").setAttribute("aria-busy", String(scheduledLoading));
  $("refresh-scheduled").disabled = scheduledLoading;
  $("scheduled-previous").disabled = scheduledLoading || scheduledPage === 0;
  $("scheduled-next").disabled = scheduledLoading || !scheduledData?.next;
  $("scheduled-pagination").classList.toggle("hidden", !scheduledData?.jobs.length && scheduledPage === 0);
  $("scheduled-range").textContent = scheduledData
    ? `${date(scheduledData.start, state.timezone)} – ${date(scheduledData.end, state.timezone)}`
    : "";
  if (!scheduledData) {
    setScheduledList(`<div class="empty"><p>${scheduledLoading ? "Loading scheduled jobs…" : "Use Refresh jobs to load the schedule."}</p></div>`);
    return;
  }
  const jobs = scheduledData.jobs;
  $("scheduled-count").textContent = jobs.length
    ? `Showing jobs ${scheduledPage * 100 + 1}–${scheduledPage * 100 + jobs.length}${scheduledData.next ? " · More jobs on the next page" : ""}`
    : "No more jobs in this window.";
  if (!jobs.length) {
    setScheduledList(`<div class="empty"><h3>No jobs scheduled in the next ${scheduledDays} days</h3><p>Enable a stack's schedule on the <a href="#stacks">Stacks page</a>${scheduledDays === 7 ? ", or choose the 30d window" : ""}.</p></div>`);
    return;
  }
  setScheduledList(`<div class="scheduled-table-wrap"><table class="scheduled-table"><caption class="sr-only">Backup jobs scheduled in the next ${scheduledDays} days</caption><thead><tr><th scope="col">#</th><th scope="col">Stack</th><th scope="col">Scheduled for</th><th scope="col">Schedule</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>${jobs.map((job, index) => {
    const stack = getStack(job.stack);
    return `<tr><td class="scheduled-number">${scheduledPage * 100 + index + 1}</td><th scope="row">${esc(job.stack)}</th><td><time datetime="${esc(job.run_at)}">${esc(date(job.run_at, job.timezone))}</time><small>${esc(job.timezone)}</small>${job.overdue ? '<span class="badge warn">Overdue · waiting for worker</span>' : ""}</td><td><code>${esc(job.cron)}</code></td><td>${stack && !stack.excluded ? `<button class="text-button" data-action="schedule" data-name="${esc(job.stack)}" aria-label="Edit schedule for ${esc(job.stack)}">Edit schedule</button>` : '<span class="muted">Stack unavailable</span>'}</td></tr>`;
  }).join("")}</tbody></table></div>`);
}

function restoreError(error) {
  $("restore-error").textContent = error.message;
  $("restore-status").textContent = "";
}
function resetRestorePreview() {
  restorePreview = null;
  $("restore-confirm").checked = false;
  $("restore-preview").classList.add("hidden");
  $("restore-confirmation").classList.add("hidden");
  $("restore-error").textContent = "";
  $("restore-status").className = "hint";
}
async function loadRestoreContainers() {
  const result = await api("/restore/containers");
  const selected = $("restore-container").value;
  restoreContainers = result.containers;
  restoreMaxBytes = result.max_upload_bytes;
  $("restore-container").innerHTML = '<option value="">Select a container…</option>' +
    restoreContainers.map((c) => `<option value="${esc(c.id)}">${esc(c.name)} · ${esc(c.state)}</option>`).join("");
  $("restore-container").value = selected;
  if (selected && !$("restore-container").value) resetRestorePreview();
  renderRestore();
}
function renderRestore() {
  const container = restoreContainers.find((c) => c.id === $("restore-container").value);
  const blocked = restoreBusy || !state || state.busy || state.recovery.length > 0;
  $("restore-container").disabled = restoreBusy;
  $("restore-file").disabled = restoreBusy;
  $("restore-source").disabled = restoreBusy;
  $("refresh-restore").disabled = restoreBusy;
  $("restore-upload").disabled = blocked || !container || !$("restore-file").files.length;
  $("restore-start").disabled = blocked || !restorePreview || !$("restore-confirm").checked;
  $("restore-container-info").textContent = container ? container.image :
    restoreMaxBytes && !restoreContainers.length ? "No available containers. Create or start a container on the selected host first. Excluded stacks are omitted." : "";
  if (restoreJobId && state) {
    const job = state.jobs.find((j) => j.id === restoreJobId);
    if (job) {
      $("restore-status").textContent = job.detail;
      $("restore-status").className = "hint" + (job.status === "failed" ? " error" : "");
    }
  }
}
function uploadRestoreFile(file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", apiPath("/restore/upload") + "?filename=" + encodeURIComponent(file.name));
    request.setRequestHeader("X-Requested-With", "cloud-backup");
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.onprogress = (event) => {
      $("restore-status").textContent = event.lengthComputable && event.loaded < event.total
        ? `Uploading backup… ${Math.round(event.loaded / event.total * 100)}%`
        : "Upload complete. Checking backup…";
    };
    request.onload = () => {
      let result;
      try { result = JSON.parse(request.responseText); }
      catch { reject(new Error("The app did not respond. Check its container status.")); return; }
      if (request.status === 401) showLogin();
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(typeof result.detail === "string" ? result.detail : "Could not upload this backup."));
      } else resolve(result);
    };
    request.onerror = () => reject(new Error("Backup upload failed. Check the connection and try again."));
    request.onabort = () => reject(new Error("Backup upload was cancelled."));
    request.send(file);
  });
}
async function previewRestore() {
  resetRestorePreview();
  if (!restoreUpload || !$("restore-container").value || $("restore-source").value === "") return;
  restoreBusy = true;
  renderRestore();
  $("restore-status").textContent = "Preparing files and checking restore destinations…";
  try {
    restorePreview = await api("/restore/preview", "POST", {
      token: restoreUpload.token,
      container_id: $("restore-container").value,
      source_index: Number($("restore-source").value),
    });
    $("restore-preview").innerHTML = `<h2>Review restore</h2><p>From <strong>${esc(restorePreview.backup_container)}</strong> to <strong>${esc(restorePreview.container)}</strong></p><ul class="backup-items">${restorePreview.paths.map((p) => `<li><code>${esc(p.source)}</code><small>${p.files} entries · ${size(p.size)}</small></li>`).join("")}</ul>`;
    $("restore-preview").classList.remove("hidden");
    $("restore-confirmation").classList.remove("hidden");
    $("restore-start").textContent = restorePreview.state === "running" ? "Stop, restore & restart" : "Restore files · keep container stopped";
    $("restore-status").textContent = "Backup checked. Review the paths and confirm to restore.";
  } catch (e) { restoreError(e); }
  finally { restoreBusy = false; renderRestore(); }
}
$("restore-form").onsubmit = async (event) => {
  event.preventDefault();
  const file = $("restore-file").files[0];
  if (!file || !$("restore-container").value) return;
  resetRestorePreview();
  restoreUpload = null;
  restoreJobId = null;
  $("restore-activity").classList.add("hidden");
  $("restore-source-choice").classList.add("hidden");
  if (file.size > restoreMaxBytes) { restoreError(new Error(`Backup upload exceeds the ${size(restoreMaxBytes)} limit.`)); return; }
  restoreBusy = true;
  renderRestore();
  $("restore-status").textContent = "Uploading backup…";
  try {
    restoreUpload = await uploadRestoreFile(file);
    const target = restoreContainers.find((c) => c.id === $("restore-container").value);
    $("restore-source").innerHTML = '<option value="">Select a container from the backup…</option>' +
      restoreUpload.containers.map((c) => `<option value="${c.index}">${esc(c.name)} · ${esc(c.image)}</option>`).join("");
    const byName = restoreUpload.containers.find((c) => c.name === target.name);
    const byService = restoreUpload.containers.filter((c) => c.service === target.service);
    const selected = byName || (byService.length === 1 ? byService[0] : null) ||
      (restoreUpload.containers.length === 1 ? restoreUpload.containers[0] : null);
    $("restore-source").value = selected ? String(selected.index) : "";
    $("restore-source-choice").classList.remove("hidden");
    $("restore-status").textContent = "Backup uploaded. Select the container in the backup to review its files.";
  } catch (e) { restoreError(e); }
  finally { restoreBusy = false; renderRestore(); }
  if (restoreUpload && $("restore-source").value !== "") await previewRestore();
};
$("restore-container").onchange = () => { restoreJobId = null; previewRestore(); renderRestore(); };
$("restore-source").onchange = previewRestore;
$("restore-file").onchange = () => {
  restoreUpload = null;
  restoreJobId = null;
  resetRestorePreview();
  $("restore-status").textContent = "";
  $("restore-source-choice").classList.add("hidden");
  $("restore-activity").classList.add("hidden");
  renderRestore();
};
$("restore-confirm").onchange = renderRestore;
$("refresh-restore").onclick = () => loadRestoreContainers().catch(restoreError);
$("restore-start").onclick = async () => {
  if (!restorePreview || !$("restore-confirm").checked) return;
  restoreBusy = true;
  renderRestore();
  try {
    const result = await api("/restore", "POST", { token: restoreUpload.token, confirmed: true });
    restoreJobId = result.job_id;
    restoreUpload = null;
    resetRestorePreview();
    $("restore-source-choice").classList.add("hidden");
    $("restore-file").value = "";
    $("restore-activity").classList.remove("hidden");
    $("restore-status").textContent = "Restore queued. Activity will show progress and the result.";
    toast("Restore started. Follow its progress in Activity.");
    await refresh(true);
  } catch (e) { restoreError(e); }
  finally { restoreBusy = false; renderRestore(); }
};
function renderStacks() {
  $("stack-host-view").value = stackHostView;
  if (stackHostView === "all" && stackOverview) {
    renderStackOverview();
    return;
  }
  if (!state) return;
  const included = (stack) => !stack.excluded &&
    (manualSelectionsSaving.get(stack.name) ?? stack.include_in_manual_backup);
  const selected = state.stacks.filter(included);
  const needsReview = selected.some((stack) => stack.issues.length);
  $("backup-selected").hidden = false;
  $("backup-selected").textContent = `Backup ${selected.length} selected`;
  $("backup-selected").disabled = !selected.length || needsReview || state.busy ||
    !!state.docker_error || !!state.recovery.length || manualBackupBusy || !!manualSelectionsSaving.size;
  $("manual-backup-hint").textContent = needsReview
    ? "Review the selected stacks before backing up. You can also turn off their manual backup toggles."
    : "Include stacks in manual backup below. All selected stacks, including search results hidden from view, run one at a time, stopping and restarting their running containers.";
  const scheduled = state.stacks.filter((s) => s.schedule.enabled).length,
    latest = state.jobs[0],
    destinations =
      Number(state.settings.smb_enabled) + Number(state.settings.drive_enabled);
  const metric = (title, value, note, icon, target) => {
    const content = `<div class="metric-top"><span>${title}</span><span aria-hidden="true">${icon}</span></div><strong>${value}</strong><small>${note}</small>`;
    return target
      ? `<a class="metric metric-link" href="#${target}">${content}</a>`
      : `<article class="metric">${content}</article>`;
  };
  $("metrics").innerHTML =
    metric(
      "Discovered stacks",
      state.stacks.length,
      esc(hostName),
      "▦",
      "stacks",
    ) +
    metric(
      "Scheduled backups",
      scheduled,
      scheduled
        ? "<em>Schedules enabled</em>"
        : "Choose a schedule to get started",
      "◷",
      "scheduled",
    ) +
    metric(
      "Remote destinations",
      destinations,
      destinations
        ? "Local copy always included"
        : "Local backups are available",
      "↗",
      "destinations",
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
  const filters = {
    all: () => true,
    manual: included,
    scheduled: (s) => s.schedule.enabled,
    paused: (s) => !s.schedule.enabled,
  };
  for (const [filter, matches] of Object.entries(filters)) {
    $(`stack-filter-${filter}-count`).textContent = state.stacks.filter(matches).length;
  }
  const stacks = state.stacks.filter((s) =>
    s.name.toLowerCase().includes(query) && filters[stackFilter](s),
  );
  if (!stacks.length) {
    $("stacks-list").innerHTML =
      `<div class="empty"><h3>${query || stackFilter !== "all" ? "No matching stacks" : "No stacks discovered yet"}</h3><p>${query || stackFilter !== "all" ? "Try a different search or filter." : "Compose labels and saved Dockge stacks will appear here. Check Docker and the read-only host mount."}</p></div>`;
    return;
  }
  $("stacks-list").innerHTML =
    `<div class="table-head"><span>STACK</span><span>BACKUP CONTENTS</span><span>VOLUMES</span><span>BACKUP SCHEDULE</span><span>MANUAL BACKUP</span><span></span></div>` +
    stacks
      .map((s) => renderStackRow(s, included))
      .join("");
}
function renderStackRow(s, included, host = null) {
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
        const manualToggle = `<div class="manual-backup-cell"><label class="manual-backup-toggle"><input type="checkbox" role="switch" data-manual-backup data-name="${esc(s.name)}" ${included(s) ? "checked" : ""} ${s.excluded || manualSelectionsSaving.has(s.name) || manualBackupBusy ? "disabled" : ""}><span>Include in manual backup<span class="sr-only"> for ${esc(s.name)}</span></span></label></div>`;
        const html = `<article class="stack-row${included(s) || s.schedule.enabled ? " stack-row-highlighted" : ""}"><div class="stack-name"><span class="stack-icon">▦</span><div><strong>${esc(s.name)}</strong><small>${s.containers.length} container${s.containers.length === 1 ? "" : "s"} · ${running} running</small></div></div><div class="state-cell">${coverage}<small title="Approximate uncompressed total, including Compose files. Refreshed about once a minute.">${esc(mode)}</small><button class="text-button" data-action="selection" data-name="${esc(s.name)}" ${s.excluded ? "disabled" : ""}>Choose contents</button></div><div class="volumes-cell"><span class="mobile-label">Volumes</span>${volumes ? `<ul class="volume-list">${volumes}</ul>` : `<small>${s.containers.length ? "No volumes mounted · Config can still be backed up" : "Create containers to discover volumes"}</small>`}</div><div class="schedule-cell">${s.schedule.enabled ? `<code>${esc(s.schedule.cron)}</code><small>Next: ${esc(date(s.schedule.next_run, s.schedule.timezone))}</small>` : '<span class="badge neutral">Paused</span><small>Manual backup available</small>'}</div>${manualToggle}<div class="row-actions"><button class="secondary" data-action="schedule" data-name="${esc(s.name)}" ${s.excluded ? "disabled" : ""}>Schedule</button><button class="${ready ? "primary" : "secondary"}" data-action="review" data-name="${esc(s.name)}">${ready ? "Back up" : "Review"}</button></div></article>`;
        if (!host) return html;
        const label = host.id === "local" ? "local" : host.name;
        return html.replace(`<strong>${esc(s.name)}</strong>`, `<strong>${esc(s.name)}</strong><span class="badge neutral">${esc(label)}</span>`)
          .replace(/data-action="(selection|schedule|review)"/g, `data-action="host-stacks" data-stack-action="$1" data-id="${esc(host.id)}"`)
          .replace(manualToggle, `<div class="manual-backup-cell"><span class="badge neutral">${included(s) ? "Included" : "Not included"}</span><small>Saved manual backup selection</small><button class="text-button" data-action="host-stacks" data-id="${esc(host.id)}" aria-label="Manage manual backups for ${esc(s.name)} on ${esc(label)}">Open host</button></div>`);
}

async function loadStackOverview() {
  if (overviewPromise) return overviewPromise;
  overviewPromise = api("/stack-overview").then((result) => { stackOverview = result; })
    .finally(() => { overviewPromise = null; });
  return overviewPromise;
}
function renderStackOverview() {
  const hosts = stackOverview.hosts;
  const stacks = hosts.flatMap((host) => host.stacks);
  const offline = hosts.filter((host) => host.error).length;
  $("stack-count").textContent = stacks.length;
  $("stack-total").textContent = stacks.length;
  $("backup-selected").hidden = true;
  $("backup-selected").disabled = true;
  $("manual-backup-hint").textContent = "You’re viewing all hosts. Manual backup selections and batch backups are managed separately on each host. Choose Open host in a row to change its selection or back up selected stacks.";
  $("metrics").innerHTML = [["Docker hosts", hosts.length], ["Discovered stacks", stacks.length],
    ["Containers", stacks.reduce((total, stack) => total + stack.containers.length, 0)], ["Hosts needing attention", offline]]
    .map(([title, count]) => `<article class="metric"><div class="metric-top"><span>${title}</span></div><strong>${count}</strong><small>Across all configured hosts</small></article>`).join("");
  const filters = {
    all: () => true,
    manual: (stack) => !stack.excluded && stack.include_in_manual_backup,
    scheduled: (stack) => stack.schedule.enabled,
    paused: (stack) => !stack.schedule.enabled,
  };
  for (const [filter, matches] of Object.entries(filters)) {
    $(`stack-filter-${filter}-count`).textContent = stacks.filter(matches).length;
  }
  const query = $("stack-search").value.toLowerCase();
  const hostLabel = (host) => host.id === "local" ? "local" : host.name;
  const visible = hosts.flatMap((host) => host.stacks.map((stack) => ({ host, stack })))
    .filter(({ host, stack }) => filters[stackFilter](stack) &&
      `${hostLabel(host)} ${host.name} ${stack.name} ${stack.containers.map((c) => c.name).join(" ")}`.toLowerCase().includes(query))
    .sort((a, b) => a.stack.name.localeCompare(b.stack.name, undefined, { sensitivity: "base" }) ||
      hostLabel(a.host).localeCompare(hostLabel(b.host), undefined, { sensitivity: "base" }) ||
      a.host.id.localeCompare(b.host.id));
  const alerts = hosts.filter((host) => host.error).map((host) =>
    `<div class="alert" role="alert"><strong>${esc(hostLabel(host))}</strong>: ${esc(host.error)} <button class="text-button" data-action="host-stacks" data-id="${esc(host.id)}">Open host</button></div>`).join("");
  const heading = '<div class="table-head"><span>STACK</span><span>BACKUP CONTENTS</span><span>VOLUMES</span><span>BACKUP SCHEDULE</span><span>MANUAL BACKUP</span><span></span></div>';
  $("stacks-list").innerHTML = alerts + (visible.length
    ? heading + visible.map(({ host, stack }) => renderStackRow(stack, filters.manual, host)).join("")
    : `<div class="empty"><h3>${query || stackFilter !== "all" ? "No matching stacks" : "No stacks discovered yet"}</h3><p>${query || stackFilter !== "all" ? "Try a different search or filter." : "Stacks from all configured hosts will appear here."}</p></div>`);

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
        `<article class="job-card"><div class="job-title"><h3>${esc(j.stack)}</h3>${badge(j.status)}<time>${esc(date(j.started))}</time></div><p>${esc(j.detail || "Waiting for the backup worker…")}</p>${j.notification_error ? `<p class="error">Notification: ${esc(j.notification_error)}</p>` : ""}<div class="job-meta"><span>${size(j.size)}</span>${j.finished ? `<span>Finished ${esc(date(j.finished))}</span>` : ""}${j.archive ? `<a href="${apiPath(`/jobs/${encodeURIComponent(j.id)}/download`)}">↓ Download archive</a>` : ""}${j.archive && ["partial", "success"].includes(j.status) ? `<button class="text-button" data-action="retry" data-id="${esc(j.id)}" ${state.busy ? "disabled" : ""}>Retry upload</button>` : ""}</div>${j.sha256 ? `<details><summary>SHA-256 checksum</summary><p class="checksum"><code>${esc(j.sha256)}</code></p></details>` : ""}</article>`,
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
async function refreshHostSelector() {
  const { hosts } = await api("/hosts");
  $("docker-host").innerHTML = hosts.map((host) => `<option value="${esc(host.id)}">${esc(host.name)}</option>`).join("");
  $("docker-host").value = selectedHost;
  hostName = hosts.find((host) => host.id === selectedHost)?.name || "Local Docker host";
  $("host-name").textContent = hostName;
}
async function loadHostConfiguration() {
  const result = await api("/host-config");
  remoteHostConfigs = result.hosts;
  $("remote-hosts-list").innerHTML = remoteHostConfigs.length
    ? remoteHostConfigs.map((host) => `<article class="job-card"><div class="job-title"><h3>${esc(host.name)}</h3>${host.id === selectedHost ? '<span class="badge good">Selected host</span>' : ""}</div><p>${esc(host.url)}</p>${host.read_only ? '<p class="hint">Managed by the hosts file. Change its details in that file.</p>' : `<div class="actions"><button class="secondary" data-action="host-edit" data-id="${esc(host.id)}">Edit or test</button><button class="text-button" data-action="host-remove" data-id="${esc(host.id)}">Remove</button></div>`}</article>`).join("")
    : '<div class="empty"><h3>No remote hosts yet</h3><p>Add a host to view its containers and manage backups and restores here.</p></div>';
}
function hostDialog(hostId) {
  const host = remoteHostConfigs.find((item) => item.id === hostId);
  openModal("REMOTE HOST", `<h2>${host ? "Edit remote host" : "Add remote host"}</h2><form id="remote-host-form">
    <label>Host name<input id="remote-host-name" value="${esc(host?.name || "")}" maxlength="100" placeholder="NAS" required></label>
    <label>Cloud Backup address<input id="remote-host-url" type="url" value="${esc(host?.url || "")}" maxlength="2048" placeholder="http://192.168.1.20:8088" required></label>
    <p class="hint">Use the address you would open in a browser on the remote machine. Use HTTPS on untrusted networks.</p>
    <label>Remote admin password<input id="remote-host-password" type="password" maxlength="1024" autocomplete="new-password" ${host ? 'placeholder="Leave blank to keep the saved password"' : "required"}></label>
    <p class="hint">Use the password you currently use to sign in to Cloud Backup on the remote machine. Saved passwords are encrypted. Re-enter the password when changing the address.</p>
    <p id="remote-host-result" class="hint" role="status"></p>
    <div class="actions"><button class="primary">Save host</button><button id="test-remote-host" type="button" class="secondary">Test connection</button><button type="button" class="text-button" data-action="close">Cancel</button></div>
  </form>`);
  const form = $("remote-host-form"), result = $("remote-host-result");
  const data = () => ({ name: $("remote-host-name").value.trim(), url: $("remote-host-url").value.trim(), password: $("remote-host-password").value });
  const busy = (value) => form.querySelectorAll("button").forEach((button) => { button.disabled = value; });
  $("test-remote-host").onclick = async () => {
    if (!form.reportValidity()) return;
    busy(true);
    result.className = "hint";
    result.textContent = "Checking connection…";
    try {
      const response = await api("/host-config" + (host ? "/" + host.id : "") + "/test", "POST", data());
      result.textContent = response.message;
    } catch (e) {
      result.className = "hint error";
      result.textContent = e.message;
    } finally { busy(false); }
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    busy(true);
    try {
      await api("/host-config" + (host ? "/" + host.id : ""), host ? "PUT" : "POST", data());
      closeModal();
      if (host?.id === selectedHost) { window.location.reload(); return; }
      await Promise.all([loadHostConfiguration(), refreshHostSelector()]);
      toast("Remote host saved. Choose it in the Docker host selector.");
    } catch (e) {
      result.className = "hint error";
      result.textContent = e.message;
    } finally { busy(false); }
  };
}
function removeHostDialog(hostId) {
  const host = remoteHostConfigs.find((item) => item.id === hostId);
  if (!host || host.read_only) return;
  openModal("REMOTE HOST", `<h2>Remove ${esc(host.name)}?</h2><p>This removes the saved connection. Backups, schedules and containers on that host remain in place.</p><p id="remove-host-error" class="error" role="alert"></p><div class="actions"><button id="confirm-remove-host" class="primary">Remove connection</button><button class="secondary" data-action="close">Cancel</button></div>`);
  $("confirm-remove-host").onclick = async (event) => {
    event.target.disabled = true;
    try {
      await api("/host-config/" + host.id, "DELETE");
      closeModal();
      if (host.id === selectedHost) {
        sessionStorage.removeItem("docker-host");
        window.location.reload();
        return;
      }
      await Promise.all([loadHostConfiguration(), refreshHostSelector()]);
      toast("Remote host removed.");
    } catch (e) {
      $("remove-host-error").textContent = e.message;
      event.target.disabled = false;
    }
  };
}
function passwordDialog() {
  openModal(
    "ACCOUNT SECURITY",
    `<h2>Change password</h2><p>Choose a new admin password. You will stay signed in here; other browsers will need to sign in again.</p><form id="password-form"><label>Current password<input id="current-password" type="password" autocomplete="current-password" maxlength="1024" required></label><label>New password<input id="new-password" type="password" autocomplete="new-password" minlength="12" maxlength="1024" aria-describedby="password-hint" required></label><p id="password-hint" class="hint">Use at least 12 characters.</p><label>Confirm new password<input id="confirm-password" type="password" autocomplete="new-password" minlength="12" maxlength="1024" required></label><p id="password-error" class="error" role="alert"></p><div class="actions"><button class="primary">Save password</button><button type="button" class="secondary" data-action="close">Cancel</button></div></form>`,
  );
  const form = $("password-form"), error = $("password-error");
  form.onsubmit = async (e) => {
    e.preventDefault();
    error.textContent = "";
    const current = $("current-password").value,
      password = $("new-password").value,
      confirmation = $("confirm-password").value;
    if (password !== confirmation) {
      error.textContent = "New passwords do not match.";
      return;
    }
    if (current === password) {
      error.textContent = "Choose a different new password.";
      return;
    }
    const button = e.submitter;
    button.disabled = true;
    try {
      await api("/password", "POST", {
        current_password: current,
        new_password: password,
        confirm_password: confirmation,
      });
      form.reset();
      if (form.isConnected) closeModal();
      toast("Password changed. Use your new password next time you sign in.");
    } catch (e) {
      if (form.isConnected && $("modal").open) error.textContent = e.message;
      else toast(e.message, true);
    } finally {
      button.disabled = false;
    }
  };
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
document.addEventListener("change", async (e) => {
  if (e.target.matches('input[name="stack-filter"]')) {
    stackFilter = e.target.value;
    renderStacks();
    return;
  }
  const toggle = e.target.closest("[data-manual-backup]");
  if (!toggle || toggle.disabled) return;
  const name = toggle.dataset.name;
  manualSelectionsSaving.set(name, toggle.checked);
  renderStacks();
  try {
    const result = await api(`/stacks/${encodeURIComponent(name)}/manual-backup`, "PUT", {
      include_in_manual_backup: toggle.checked,
    });
    const stack = state && getStack(name);
    if (stack) stack.include_in_manual_backup = result.include_in_manual_backup;
    await refresh(true);
  } catch (e) {
    toast(e.message, true);
  } finally {
    manualSelectionsSaving.delete(name);
    if (state) renderStacks();
  }
});
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-action]");
  if (!b || b.disabled) return;
  try {
    switch (b.dataset.action) {
      case "host-stacks":
        if (pendingHostActions || restoreBusy || manualBackupBusy || manualSelectionsSaving.size) {
          toast("Finish the current action before switching hosts.", true);
          break;
        }
        sessionStorage.setItem("docker-host", b.dataset.id);
        sessionStorage.setItem("stack-host-navigation", "selected");
        if (b.dataset.name) sessionStorage.setItem("stack-focus", b.dataset.name);
        if (b.dataset.stackAction) sessionStorage.setItem("stack-open-action", b.dataset.stackAction);
        location.hash = "stacks";
        window.location.reload();
        break;
      case "host-edit":
        hostDialog(b.dataset.id);
        break;
      case "host-remove":
        removeHostDialog(b.dataset.id);
        break;
      case "scheduled-window":
        if (scheduledDays !== Number(b.dataset.days)) {
          scheduledDays = Number(b.dataset.days);
          await loadScheduled(true);
        }
        break;
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
  const button = $("login-form").querySelector("button");
  if (button.disabled) return;
  const label = button.innerHTML;
  button.disabled = true;
  button.textContent = "Signing in…";
  $("login-error").textContent = "";
  try {
    await api("/login", "POST", { password: $("password").value });
    $("password").value = "";
    // Start with the new session instead of reusing requests or UI state from
    // before authentication. Keep the current route when loading the dashboard.
    window.location.reload();
  } catch (e) {
    $("login-error").textContent = e.message;
    button.disabled = false;
    button.innerHTML = label;
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
$("change-password").onclick = passwordDialog;
$("modal").addEventListener("close", () => {
  $("password-form")?.reset();
});
$("refresh").onclick = () =>
  refresh()
    .then(() => toast("Stack discovery refreshed."))
    .catch((e) => toast(e.message, true));
$("refresh-scheduled").onclick = () => loadScheduled(true);
$("scheduled-previous").onclick = () => {
  if (scheduledLoading || scheduledPage === 0) return;
  scheduledPage--;
  scheduledData = null;
  return loadScheduled();
};
$("scheduled-next").onclick = () => {
  if (scheduledLoading || !scheduledData?.next) return;
  scheduledPage++;
  scheduledData = null;
  return loadScheduled();
};
$("backup-selected").onclick = async () => {
  if ($("backup-selected").disabled) return;
  manualBackupBusy = true;
  renderStacks();
  try {
    const result = await api("/backups/manual", "POST");
    state.busy = true;
    toast(`${result.count} selected stack${result.count === 1 ? "" : "s"} queued. Follow progress in Activity.`);
    location.hash = "activity";
    await refresh(true);
  } catch (e) {
    toast(e.message, true);
  } finally {
    manualBackupBusy = false;
    if (state) renderStacks();
  }
};
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
  const button = $("save-drive-token"), feedback = $("drive-connect-result");
  button.disabled = true;
  feedback.className = "hint";
  feedback.textContent = "Checking Google Drive access…";
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
    feedback.textContent = "Google Drive access checked. Choose a backup folder.";
    toast("Google Drive connected. Choose a folder to mirror backups.");
  } catch (e) {
    feedback.className = "hint error";
    feedback.textContent = e.message;
    toast(e.message, true);
  } finally {
    button.disabled = false;
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
    $("drive-connect-result").textContent = "";
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
$("stack-host-view").onchange = async (event) => {
  stackHostView = event.target.value;
  $("stack-search").value = "";
  try {
    if (stackHostView === "all") await loadStackOverview();
    if (state) renderStacks();
  } catch (e) { toast(e.message, true); }
};
$("add-remote-host").onclick = () => hostDialog();
$("docker-host").onchange = (event) => {
  if (pendingHostActions || restoreBusy || manualBackupBusy || manualSelectionsSaving.size || $("modal").open) {
    event.target.value = selectedHost;
    toast("Finish the current action or close its dialog before switching hosts.", true);
    return;
  }
  sessionStorage.setItem("docker-host", event.target.value);
  sessionStorage.setItem("stack-host-navigation", "selected");
  window.location.reload();
};
setInterval(() => {
  if (state && !document.hidden) refresh().catch(() => {});
}, 10000);
(async () => {
  try {
    const result = await api("/session");
    if (result.authenticated) {
      const { hosts } = await api("/hosts");
      if (!hosts.some((host) => host.id === selectedHost)) {
        selectedHost = "local";
        sessionStorage.removeItem("docker-host");
      }
      hostName = hosts.find((host) => host.id === selectedHost).name;
      $("docker-host").innerHTML = hosts.map((host) => `<option value="${esc(host.id)}">${esc(host.name)}</option>`).join("");
      $("docker-host").value = selectedHost;
      const focusedStack = sessionStorage.getItem("stack-focus");
      if (focusedStack) {
        $("stack-search").value = focusedStack;
        sessionStorage.removeItem("stack-focus");
      }
      await refresh();
      showApp();
      const stackAction = sessionStorage.getItem("stack-open-action");
      sessionStorage.removeItem("stack-open-action");
      if (focusedStack && getStack(focusedStack)) {
        if (stackAction === "selection") selectionDialog(focusedStack);
        if (stackAction === "schedule") scheduleDialog(focusedStack);
        if (stackAction === "review") reviewDialog(focusedStack);
      }
    } else showLogin();
  } catch (e) {
    if ($("docker-host").options.length > 1) {
      showApp();
      $("host-name").textContent = hostName;
      $("host-state").textContent = "Connection unavailable";
      $("global-alert").textContent = e.message + " Select another Docker host to continue.";
    } else {
      showLogin();
      $("login-error").textContent = e.message;
    }
  }
})();
