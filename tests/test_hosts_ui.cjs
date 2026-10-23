const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function workspace(host = "nas", savedView = "selected", navigation = null) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "", innerHTML: "", disabled: false, open: false,
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener() {},
    });
    return elements.get(id);
  };
  const stored = new Map([["docker-host", host], ["stack-host-view", savedView], ["stack-host-navigation", navigation]]);
  let reloads = 0;
  const context = vm.createContext({
    document: { getElementById: element, addEventListener() {} },
    window: { addEventListener() {}, location: { reload() { reloads++; } } },
    sessionStorage: {
      getItem(key) { return stored.get(key); },
      setItem(key, value) { stored.set(key, value); },
      removeItem(key) { stored.delete(key); },
    },
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    fetch: async () => ({ ok: true, json: async () => ({ authenticated: false }) }),
  });
  vm.runInContext(fs.readFileSync(require("node:path").join(__dirname, "../app/static/app.js"), "utf8"), context);
  return { context, element, stored, reloads: () => reloads, run: (code) => vm.runInContext(code, context) };
}

test("host selection routes workspace APIs but keeps portal authentication local", () => {
  const ui = workspace();
  for (const path of ["/dashboard", "/stacks/same-name/backup", "/restore/upload", "/restore/preview", "/restore", "/jobs/id/download", "/settings"]) {
    assert.equal(ui.run(`apiPath(${JSON.stringify(path)})`), "/api/hosts/nas" + path);
  }
  for (const path of ["/login", "/logout", "/session", "/password", "/hosts", "/stack-overview", "/host-config", "/host-config/test", "/host-config/host-id", "/host-config/host-id/test"]) {
    assert.equal(ui.run(`apiPath(${JSON.stringify(path)})`), "/api" + path);
  }
  assert.equal(workspace("local").run('apiPath("/restore")'), "/api/restore");
});

test("all-host stacks share one list with host badges, duplicate names and offline hosts", () => {
  const ui = workspace();
  const stack = { name: "same-stack", containers: [{ name: "app", state: "running" }], issues: [], volumes: [], backup_selection: { mode: "auto", volumes: [] }, schedule: { enabled: false } };
  const overview = { hosts: [
    { id: "local", name: "Local Docker host", stacks: [stack], error: null },
    { id: "nas", name: "NAS", stacks: [stack], error: null },
    { id: "offline", name: "Offline host", stacks: [], error: "Cannot reach the remote host." },
  ] };
  ui.run(`state = {}; stackOverview = ${JSON.stringify(overview)}; renderStacks();`);
  const html = ui.element("stacks-list").innerHTML;
  assert.equal((html.match(/<strong>same-stack<\/strong>/g) || []).length, 2);
  assert.match(html, /<span class="badge neutral">local<\/span>/);
  assert.doesNotMatch(html, /host-stack-group/);
  for (const column of ["STACK", "BACKUP CONTENTS", "VOLUMES", "BACKUP SCHEDULE", "MANUAL BACKUP"]) assert.ok(html.includes(`<span>${column}</span>`));
  assert.match(html, /class="stack-row/);
  assert.match(html, /NAS/);
  assert.match(html, /Cannot reach the remote host/);
  assert.match(html, /data-id="local"/);
  assert.match(html, /data-id="nas"/);
  assert.equal(ui.element("stack-total").textContent, 2);
  assert.equal(ui.element("backup-selected").disabled, true);
  ui.run("state = null; renderStacks();");
  assert.match(ui.element("stacks-list").innerHTML, /NAS/);
  ui.element("stack-search").value = "nas";
  ui.run("renderStackOverview()");
  assert.equal((ui.element("stacks-list").innerHTML.match(/<strong>same-stack<\/strong>/g) || []).length, 1);
});

test("unified stacks sort across hosts and retain the host for duplicate names", () => {
  const ui = workspace();
  const stack = (name) => ({ name, containers: [], issues: [], volumes: [], backup_selection: { mode: "auto", volumes: [] }, schedule: { enabled: false } });
  const overview = { hosts: [
    { id: "nas", name: "NAS", stacks: [stack("Zulu"), stack("alpha"), stack("Same")] },
    { id: "local", name: "Local Docker host", stacks: [stack("beta"), stack("Same")] },
  ] };
  ui.run(`stackOverview = ${JSON.stringify(overview)}; renderStackOverview();`);
  const html = ui.element("stacks-list").innerHTML;
  assert.deepEqual([...html.matchAll(/<strong>(.*?)<\/strong>/g)].map((match) => match[1]),
    ["alpha", "beta", "Same", "Same", "Zulu"]);
  assert.deepEqual([...html.matchAll(/data-id="([^"]+)" data-name="Same"/g)].map((match) => match[1]),
    ["local", "local", "local", "nas", "nas", "nas"]);
  ui.element("stack-search").value = "local";
  ui.run("renderStackOverview()");
  assert.doesNotMatch(ui.element("stacks-list").innerHTML, /data-id="nas"/);
  assert.match(ui.element("stacks-list").innerHTML, /<strong>beta<\/strong>/);
});

test("downloads remain bound to their host", () => {
  const ui = workspace();
  ui.run('state = {jobs: [{id: "job-id", stack: "same-name", status: "success", archive: "backup.tar.gz"}]}; renderJobs();');
  assert.match(ui.element("jobs-list").innerHTML, /href="\/api\/hosts\/nas\/jobs\/job-id\/download"/);
});

test("switching reloads the workspace and cannot interrupt an action or dialog", () => {
  const ui = workspace();
  const selector = ui.element("docker-host");
  for (const guard of ["pendingHostActions = 1", "restoreBusy = true", "manualBackupBusy = true", '$("modal").open = true']) {
    ui.run(guard);
    selector.value = "local";
    selector.onchange({ target: selector });
    assert.equal(selector.value, "nas");
    assert.equal(ui.reloads(), 0);
    ui.run('pendingHostActions = 0; restoreBusy = false; manualBackupBusy = false; $("modal").open = false;');
  }
  selector.value = "local";
  selector.onchange({ target: selector });
  assert.equal(ui.stored.get("docker-host"), "local");
  assert.equal(ui.reloads(), 1);
});

test("all hosts is the default even after a previous selected-host view", () => {
  assert.equal(workspace().run("stackHostView"), "all");
  const navigation = workspace("nas", "selected", "selected");
  assert.equal(navigation.run("stackHostView"), "selected");
  assert.equal(navigation.stored.has("stack-host-navigation"), false);
});
