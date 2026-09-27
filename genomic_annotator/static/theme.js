"use strict";

// Run before stylesheets so the saved appearance is applied before first paint.
(() => {
  const key = "helix-theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";
  let storage = null;
  let notice = "";

  function readPreference(value) {
    if (value === null || ["light", "dark", "system"].includes(value)) {
      notice = "";
      return value || "system";
    }
    notice = "The saved appearance setting is invalid. Using your system theme.";
    return "system";
  }

  function storageError(error, message) {
    if (!["SecurityError", "QuotaExceededError"].includes(error.name)) throw error;
    notice = message;
  }

  try {
    storage = window.localStorage;
    preference = readPreference(storage.getItem(key));
  } catch (error) {
    storageError(error, "The browser blocked appearance storage. Using your system theme; changes can still apply to this tab.");
  }

  function applyTheme() {
    const theme = preference === "system" ? (system.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = theme;
    const select = document.getElementById("theme-select");
    if (select) {
      select.value = preference;
      document.getElementById("theme-icon").setAttribute("href", `#i-${theme === "dark" ? "moon" : "sun"}`);
      const message = document.getElementById("theme-notice");
      message.textContent = notice;
      message.hidden = !notice;
    }
  }

  applyTheme();
  system.addEventListener("change", () => {
    if (preference === "system") applyTheme();
  });
  window.addEventListener("storage", event => {
    if (event.storageArea !== storage || (event.key !== key && event.key !== null)) return;
    preference = readPreference(event.key === null ? null : event.newValue);
    applyTheme();
  });
  document.addEventListener("DOMContentLoaded", () => {
    const select = document.getElementById("theme-select");
    applyTheme();
    select.addEventListener("change", () => {
      preference = readPreference(select.value);
      try {
        storage = window.localStorage;
        if (preference === "system") storage.removeItem(key);
        else storage.setItem(key, preference);
      } catch (error) {
        storageError(error, "Theme changed for this tab, but the browser blocked saving it. It may reset when you reload.");
      }
      applyTheme();
    });
  }, { once: true });
})();
