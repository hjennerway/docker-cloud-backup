const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function loginPage() {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "", innerHTML: "Sign in", disabled: false,
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener() {},
      querySelector: () => element("submit"),
    });
    return elements.get(id);
  };
  let resolveLogin;
  let reloads = 0;
  const requests = [];
  const context = vm.createContext({
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    document: { getElementById: element, addEventListener() {} },
    window: { addEventListener() {}, location: { reload() { reloads++; } } },
    setInterval() {},
    fetch(url) {
      requests.push(url);
      if (url === "/api/session") return Promise.resolve({
        ok: true, json: async () => ({ authenticated: false }),
      });
      return new Promise((resolve) => { resolveLogin = resolve; });
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../app/static/app.js"), "utf8"), context);
  return {
    element, requests, reloads: () => reloads,
    respond(ok, detail) { resolveLogin({ ok, status: ok ? 200 : 401, json: async () => ({ detail }) }); },
    submit: () => element("login-form").onsubmit({ preventDefault() {} }),
  };
}

test("successful login reloads with the new cookie without waiting for dashboard requests", async () => {
  const page = loginPage();
  page.element("password").value = "correct password";
  const pending = page.submit();
  assert.equal(page.element("submit").textContent, "Signing in…");
  await page.submit();
  assert.equal(page.requests.filter((url) => url === "/api/login").length, 1);
  page.respond(true);
  await pending;
  assert.equal(page.reloads(), 1);
  assert.equal(page.element("password").value, "");
  assert.equal(page.requests.includes("/api/dashboard"), false);
});

test("rejected login shows the error and allows another attempt", async () => {
  const page = loginPage();
  page.element("password").value = "wrong password";
  const pending = page.submit();
  page.respond(false, "Incorrect password.");
  await pending;
  assert.equal(page.reloads(), 0);
  assert.equal(page.element("login-error").textContent, "Incorrect password.");
  assert.equal(page.element("submit").disabled, false);
  assert.equal(page.element("submit").innerHTML, "Sign in");
  assert.equal(page.element("password").value, "wrong password");
});
