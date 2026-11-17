/* api.js —— 只负责调接口，不做任何渲染、不持有业务状态 */
(function () {
  "use strict";

  async function get(path) {
    const res = await fetch(path, { headers: { "Accept": "application/json" } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  async function post(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body || {})
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  async function uploadCover(file) {
    const res = await fetch("/api/upload/cover", {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Filename": encodeURIComponent(file.name),
        "Accept": "application/json"
      },
      body: file
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  window.API = {
    config: () => get("/api/config"),
    series: () => get("/api/series"),
    drafts: () => get("/api/drafts"),
    lock: () => get("/api/lock"),
    external: () => get("/api/external"),
    current: () => get("/api/run/current"),
    preview: (payload) => post("/api/preview", payload),
    start: (payload) => post("/api/run/start", payload),
    stop: () => post("/api/run/stop", {}),
    runs: () => get("/api/runs"),
    runDetail: (id) => get("/api/runs/" + encodeURIComponent(id)),
    rerun: (id) => post("/api/runs/" + encodeURIComponent(id) + "/rerun", {}),
    runExportOnly: (payload) => post("/api/run/export-only", payload),
    manifests: () => get("/api/manifests"),
    plan: (payload) => post("/api/plan", payload),
    execute: (payload) => post("/api/execute", payload),
    uploadCover: uploadCover,
    events: () => new EventSource("/api/events"),
    // ── 环境初始化 ──
    envCheck: () => get("/api/env/check"),
    envInventory: () => get("/api/env/inventory"),
    envDeps: () => get("/api/env/deps"),
    envJob: () => get("/api/env/job"),
    envPresets: () => get("/api/env/presets"),
    envJianying: () => get("/api/env/jianying"),
    envAction: (payload) => post("/api/env/action", payload),
    envValidate: (payload) => post("/api/env/presets/validate", payload),
    envSave: (payload) => post("/api/env/presets/save", payload),
    consoleAutostart: () => get("/api/console/autostart"),
    consoleSetAutostart: (enabled) => post("/api/console/autostart", { enabled: !!enabled })
  };
})();
