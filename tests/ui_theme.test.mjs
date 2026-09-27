import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const script = readFileSync(new URL("../genomic_annotator/static/theme.js", import.meta.url), "utf8");
const storageFailure = name => Object.assign(new Error("Synthetic storage failure"), { name });

function workspace({ stored = null, dark = false, readError, writeError, getterError } = {}) {
  const values = new Map(stored === null ? [] : [["helix-theme", stored]]);
  const writes = [];
  const listeners = {};
  const elements = new Map(["theme-select", "theme-icon", "theme-notice"].map(id => [id, {
    value: "system", hidden: true, textContent: "", attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(event, handler) { this[event] = handler; },
  }]));
  const root = { dataset: {} };
  let ready = false;
  const storage = {
    getItem(key) {
      if (readError) throw readError;
      return values.get(key) ?? null;
    },
    setItem(key, value) {
      if (writeError) throw writeError;
      writes.push([key, value]);
      values.set(key, value);
    },
    removeItem(key) {
      if (writeError) throw writeError;
      writes.push([key, null]);
      values.delete(key);
    },
  };
  const media = {
    matches: dark,
    addEventListener(event, handler) { this[event] = handler; },
  };
  const window = {
    matchMedia(query) {
      assert.equal(query, "(prefers-color-scheme: dark)");
      return media;
    },
    addEventListener(event, handler) { listeners[event] = handler; },
  };
  Object.defineProperty(window, "localStorage", {
    get() {
      if (getterError) throw getterError;
      return storage;
    },
  });
  vm.runInNewContext(script, {
    window,
    document: {
      documentElement: root,
      getElementById: id => ready ? elements.get(id) : null,
      addEventListener(event, handler) { listeners[event] = handler; },
    },
    fetch() { throw new Error("The theme must never send a network request."); },
  });
  return {
    root, values, writes, elements,
    mount() { ready = true; listeners.DOMContentLoaded(); },
    select(value) {
      const select = elements.get("theme-select");
      select.value = value;
      select.change();
    },
    system(dark) { media.matches = dark; media.change(); },
    stored(value, key = "helix-theme", storageArea = storage) {
      listeners.storage({ key, newValue: value, storageArea });
    },
  };
}

test("system appearance is applied before DOM content without creating stored data", () => {
  for (const dark of [false, true]) {
    const page = workspace({ dark });
    assert.equal(page.root.dataset.theme, dark ? "dark" : "light");
    assert.deepEqual(page.writes, []);
    page.mount();
    assert.equal(page.elements.get("theme-select").value, "system");
    assert.equal(page.elements.get("theme-icon").attributes.href, dark ? "#i-moon" : "#i-sun");
    assert.equal(page.elements.get("theme-notice").hidden, true);
    page.system(!dark);
    assert.equal(page.root.dataset.theme, dark ? "light" : "dark");
  }
});

test("an explicit saved preference wins over system changes and survives a new page", () => {
  const page = workspace({ stored: "dark", dark: false });
  assert.equal(page.root.dataset.theme, "dark");
  page.mount();
  page.system(false);
  assert.equal(page.root.dataset.theme, "dark");
  page.select("light");
  assert.equal(page.root.dataset.theme, "light");
  page.system(true);
  assert.equal(page.root.dataset.theme, "light");
  assert.deepEqual(page.writes, [["helix-theme", "light"]]);
  const reloaded = workspace({ stored: page.values.get("helix-theme"), dark: true });
  assert.equal(reloaded.root.dataset.theme, "light");
});

test("only a light/dark preference is persisted and System removes that preference", () => {
  const page = workspace({ dark: true });
  page.mount();
  page.select("light");
  page.select("dark");
  page.select("system");
  assert.deepEqual(page.writes, [
    ["helix-theme", "light"], ["helix-theme", "dark"], ["helix-theme", null],
  ]);
  assert.equal(page.values.size, 0);
  assert.equal(page.root.dataset.theme, "dark");
  page.system(false);
  assert.equal(page.root.dataset.theme, "light");
});

test("theme changes synchronize between windows without accepting unrelated storage", () => {
  const page = workspace();
  page.mount();
  page.stored("dark");
  assert.equal(page.root.dataset.theme, "dark");
  assert.equal(page.elements.get("theme-select").value, "dark");
  page.stored("light", "unrelated");
  page.stored("light", "helix-theme", {});
  assert.equal(page.root.dataset.theme, "dark");
  page.stored(null);
  assert.equal(page.elements.get("theme-select").value, "system");
  page.system(true);
  assert.equal(page.root.dataset.theme, "dark");
  page.stored(null, null);
  assert.equal(page.elements.get("theme-select").value, "system");
  assert.deepEqual(page.writes, []);
});

test("invalid stored preferences are reported and never interpolated into markup", () => {
  const page = workspace({ stored: "<script>untrusted</script>", dark: true });
  page.mount();
  assert.equal(page.root.dataset.theme, "dark");
  assert.equal(page.elements.get("theme-select").value, "system");
  assert.equal(page.elements.get("theme-notice").hidden, false);
  assert.match(page.elements.get("theme-notice").textContent, /invalid/);
  page.select("light");
  assert.equal(page.elements.get("theme-notice").hidden, true);
  assert.equal(page.values.get("helix-theme"), "light");
});

test("blocked storage reads leave theme switching usable with an explicit notice", () => {
  for (const option of ["readError", "getterError"]) {
    const page = workspace({ [option]: storageFailure("SecurityError"), dark: true });
    page.mount();
    assert.equal(page.root.dataset.theme, "dark");
    assert.equal(page.elements.get("theme-notice").hidden, false);
    assert.match(page.elements.get("theme-notice").textContent, /blocked/);
    page.select("light");
    assert.equal(page.root.dataset.theme, "light");
  }
});

test("write and remove failures apply the theme in memory without pretending it was saved", () => {
  for (const name of ["SecurityError", "QuotaExceededError"]) {
    const page = workspace({ writeError: storageFailure(name) });
    page.mount();
    page.select("dark");
    assert.equal(page.root.dataset.theme, "dark");
    assert.equal(page.elements.get("theme-notice").hidden, false);
    assert.match(page.elements.get("theme-notice").textContent, /blocked saving/);
    assert.equal(page.values.size, 0);
    page.select("system");
    assert.equal(page.root.dataset.theme, "light");
    assert.match(page.elements.get("theme-notice").textContent, /blocked saving/);
  }
});

test("unexpected programming errors are not swallowed as storage failures", () => {
  assert.throws(() => workspace({ readError: new TypeError("Synthetic bug") }), /Synthetic bug/);
  const page = workspace({ writeError: new TypeError("Synthetic bug") });
  page.mount();
  assert.throws(() => page.select("dark"), /Synthetic bug/);
});
