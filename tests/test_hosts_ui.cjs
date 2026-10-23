const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function workspace(host = "nas") {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "", innerHTML: "", disabled: false, open: false,
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener() {},
    });
    return elements.get(id);
  };
  const stored = new Map([["docker-host", host]]);
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

test("all-host stacks are grouped by machine, including duplicate names and offline hosts", () => {
  const ui = workspace();
  const stack = { name: "same-stack", containers: [{ name: "app", state: "running" }], issues: [], schedule: { enabled: false } };
  const overview = { hosts: [
    { id: "local", name: "Local Docker host", stacks: [stack], error: null },
    { id: "nas", name: "NAS", stacks: [stack], error: null },
    { id: "offline", name: "Offline host", stacks: [], error: "Cannot reach the remote host." },
  ] };
  ui.run(`state = {}; stackOverview = ${JSON.stringify(overview)}; renderStacks();`);
  const html = ui.element("stacks-list").innerHTML;
  assert.equal((html.match(/<h3>same-stack<\/h3>/g) || []).length, 2);
  assert.match(html, /Local Docker host/);
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
  assert.equal((ui.element("stacks-list").innerHTML.match(/<h3>same-stack<\/h3>/g) || []).length, 1);
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
