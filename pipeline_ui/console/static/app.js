/* app.js —— 状态与路由；不发裸请求（统一走 API），不拼业务 HTML（统一走 View） */
(function () {
  "use strict";

  function escHtml(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  const state = {
    page: "run",
    scroll: {},          // 每个页面的滚动位置，切页后恢复
    guide: null,         // 首屏环境盘点摘要（失败/警告数）
    historyQuery: "",
    historyStatus: "all",
    archQuery: "",
    config: null,
    series: [],
    stages: [],
    uiStages: [],        // UI 自动剪辑链路的阶段表（与串流不同）
    run: null,
    queue: 0,
    lock: null,
    external: null,
    manifests: [],
    archiveDraft: "",
    lastPlan: null,
    history: [],
    externalHistory: [],
    detail: null,
    coverInfo: null,
    runTab: "edit",      // 任务页：edit=剪辑任务 / export=导出任务
    exMode: "single",    // single / batch
    exDrafts: null,      // null=未加载
    exSelected: [],
    exMultiPreset: false,
    exProfiles: [],      // 多预设导出：勾选的预设组（顺序即导出顺序）
    exCoverFallback: false,  // 组封面缺失时复用源草稿封面（关闭=抽帧/中止）
    form: {
      mode: "batch",
      series_dir: "",
      series_manual: "",
      sel_mode: "all",
      from: 1,
      to: "",
      n: 10,
      cut: 0,            // 默认 0 = 不裁剪（命令行不会出现 --cut）
      cover: "",
      profiles: [],      // 有序：勾选顺序即账号顺序，空 = 跟随 presets.json 默认
      coverFallback: false,  // 多预设组时：组封面缺失则复用源草稿封面（关闭=抽帧/中止）
      export: true,
      retries: 2,
      timeout: 60,
      prefix: "",
      delay: "",
      resolution: "",
      vocal: "none",
      normalize: false
    }
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // ── toast / busy ───────────────────────────────────────────────
  let toastTimer = null;
  function toast(msg, type) {
    const el = $("#toast");
    const text = $("#toast-msg");
    if (!el || !text) return;
    text.textContent = msg;
    el.className = "toast show " + (type || "info");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = "toast"; }, 4200);
  }

  function setBusy(btn, on) {
    if (!btn) return;
    btn.classList.toggle("busy", !!on);
    btn.disabled = !!on;
  }

  // ── 主题 ───────────────────────────────────────────────────────
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("jy-console-theme", t); } catch (e) {}
    const label = $("#theme-label");
    if (label) label.textContent = t === "dark" ? "深色" : "浅色";
  }

  function initTheme() {
    let t = document.documentElement.dataset.theme;
    if (t !== "dark" && t !== "light") t = "light";
    applyTheme(t);
    const btn = $("#theme-toggle");
    if (btn) {
      btn.addEventListener("click", () => {
        applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
      });
    }
  }

  // ── 路由 ───────────────────────────────────────────────────────
  function showPage(name) {
    if (state.page && state.page !== name) state.scroll[state.page] = window.scrollY || 0;
    state.page = name;
    $$(".nav-item").forEach((t) => t.classList.toggle("active", t.dataset.page === name));
    $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + name));
    if (name === "doctor") { location.hash = "#/env"; return; }
    if (name === "env" && window.Env) window.Env.show();
    if (name === "archive") loadManifests();
    if (name === "history") loadHistory();
    // 切页默认回到顶部；返回时恢复上次位置
    const top = state.scroll[name] || 0;
    requestAnimationFrame(() => window.scrollTo(0, top));
  }

  function goPage(name, after) {
    const h = "#/" + name;
    if (location.hash === h) {
      showPage(name);
      if (after) after();
    } else {
      location.hash = h;
      if (after) setTimeout(after, 0);
    }
  }

  function bindNav() {
    $$(".nav-item").forEach((t) => {
      t.addEventListener("click", () => { location.hash = "#/" + t.dataset.page; });
    });
    // 状态栏胶囊：服务 → Console 服务页签；任务 / 导出锁 → 任务页
    $$(".sb-link").forEach((el) => {
      el.addEventListener("click", () => {
        const target = el.dataset.goto;
        if (target === "env-service") {
          goPage("env", () => { if (window.Env) window.Env.openService(); });
        } else {
          goPage("run");
        }
      });
    });
    // 空态 / 引导条里的「去某页」按钮
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-goto-page]");
      if (btn) goPage(btn.dataset.gotoPage);
    });
    window.addEventListener("hashchange", () => {
      const n = (location.hash || "#/run").replace("#/", "") || "run";
      showPage(n);
    });
  }

  // ── 顶部状态栏 ─────────────────────────────────────────────────
  function setService(on) {
    const el = $("#sb-service");
    if (!el) return;
    el.innerHTML = on
      ? '<i class="dot ok"></i>已连接'
      : '<i class="dot err"></i>连接断开';
  }

  function updateStatusbar() {
    const run = state.run;
    const taskEl = $("#sb-task");
    if (taskEl) {
      if (!run) {
        taskEl.textContent = "空闲";
      } else {
        const label = { queued: "排队中", running: "运行中",
                        succeeded: "成功", failed: "失败", stopped: "已停止" }[run.status] || run.status;
        const stage = run.stage ? run.stage.label : "等待阶段";
        taskEl.textContent = label + " · " + stage;
      }
    }
    const lockEl = $("#sb-lock");
    if (lockEl) {
      const L = state.lock;
      if (!L) {
        lockEl.innerHTML = '<i class="dot pending"></i>读取中';
      } else if (L.state === "free") {
        lockEl.innerHTML = '<i class="dot ok"></i>空闲';
      } else if (L.state === "busy") {
        lockEl.innerHTML = '<i class="dot busy"></i>占用 pid=' + L.pid;
      } else if (L.state === "stale") {
        lockEl.innerHTML = '<i class="dot warn"></i>残留锁 pid=' + L.pid;
      } else {
        lockEl.innerHTML = '<i class="dot warn"></i>状态未知';
      }
    }
    const timeEl = $("#sb-time");
    if (timeEl) {
      const d = new Date();
      const p = (n) => String(n).padStart(2, "0");
      timeEl.textContent = p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
    }
  }

  async function refreshLock() {
    try {
      const res = await API.lock();
      state.lock = res.lock || null;
    } catch (err) {
      state.lock = null;
    }
    updateStatusbar();
  }

  async function refreshExternal() {
    try {
      state.external = await API.external();
    } catch (err) {
      state.external = null;
    }
    renderExternal();
  }

  function renderExternal() {
    const area = $("#external-area");
    if (area) area.innerHTML = View.externalCard(state.external);
  }

  // ── 任务页 ─────────────────────────────────────────────────────
  function renderRun() {
    $("#page-run").innerHTML = View.runForm(state);
    updateStatus();
    renderExternal();
    syncRun();
    applyRunTab();
  }

  function applyRunTab() {
    const page = $("#page-run");
    if (!page) return;
    const isExport = state.runTab === "export";
    const edit = $("#run-pane-edit", page);
    const exp = $("#run-pane-export", page);
    if (edit) edit.hidden = isExport;
    if (exp) exp.hidden = !isExport;
    page.querySelectorAll("[data-run-tab]").forEach((b) => {
      const on = b.dataset.runTab === state.runTab;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (isExport) {
      updateExHint();
      initExportPane();
    }
  }

  async function initExportPane() {
    const page = $("#page-run");
    if (!page) return;
    if (!state.config) {
      try { state.config = await API.config(); } catch (e) { /* 不阻塞导出 */ }
    }
    if (state.exDrafts === null) {
      const list = $("#ex-draft-list", page);
      if (list) list.innerHTML = '<p class="hint">加载中…</p>';
      try {
        const res = await API.drafts();
        state.exDrafts = res.drafts || [];
      } catch (e) {
        state.exDrafts = [];
        toast("草稿列表读取失败：" + e.message, "err");
      }
    }
    renderDraftList();
    renderProfileList();
  }

  function renderProfileList() {
    const page = $("#page-run");
    const box = page && $("#ex-profile-list", page);
    if (!box) return;
    const cfg = state.config && state.config.presets ? state.config.presets : {};
    const ps = cfg.profiles || [];
    const dflt = cfg.default || "";
    if (dflt) state.exProfiles = state.exProfiles.filter((p) => p !== dflt);
    if (!ps.length) {
      box.innerHTML = '<p class="hint">presets.json 未配置预设组（presets_a / presets_b）。</p>';
      return;
    }
    box.innerHTML = ps.map((p) => {
      const isDefault = !!dflt && p === dflt;
      const idx = state.exProfiles.indexOf(p);
      const on = idx >= 0 && !isDefault;
      const locked = isDefault;
      return `<label class="pick${on ? " on" : ""}${locked ? " locked" : ""}"
        title="${locked ? "草稿默认就用这组预设，换预设会生成完全相同的副本，已禁用" : ""}">
        <input type="checkbox" data-ex-profile="${escHtml(p)}"${on ? " checked" : ""}${locked ? " disabled" : ""}>
        <span class="pick-name">${escHtml(p)}${locked ? '<em class="pick-tag">默认</em>' : ""}</span>
        <span class="pick-order">${on ? idx + 1 : ""}</span>
      </label>`;
    }).join("");
  }

  function renderDraftList() {
    const page = $("#page-run");
    const list = page && $("#ex-draft-list", page);
    if (!list) return;
    const input = $("#ex-search", page);
    const q = (input && input.value || "").trim().toLowerCase();
    const items = (state.exDrafts || []).filter((d) => !q || d.name.toLowerCase().includes(q));
    if (!items.length) {
      list.innerHTML = '<p class="hint">没有匹配的草稿；确认草稿根是否正确，或点「刷新草稿」。</p>';
      return;
    }
    // 单剧导出：一次只允许勾一个草稿，已勾选后其余行禁用，取消后才能改选
    const single = state.exMode !== "batch";
    const taken = single && state.exSelected.length >= 1;
    list.innerHTML = items.slice(0, 300).map((d) => {
      const on = state.exSelected.indexOf(d.name) >= 0;
      const locked = taken && !on;
      return `<label class="pick${on ? " on" : ""}${locked ? " locked" : ""}"${locked
        ? ' title="单剧导出一次只导一个草稿，取消已勾选的草稿后才能改选"' : ""}>
        <input type="checkbox" data-ex-draft="${escHtml(d.name)}"${on ? " checked" : ""}${locked ? " disabled" : ""}>
        <span class="pick-name mono">${escHtml(d.name)}</span>
        <span class="pick-order">${on ? state.exSelected.indexOf(d.name) + 1 : ""}</span>
      </label>`;
    }).join("");
  }

  function exCounts() {
    let picked = state.exSelected.slice();
    if (state.exMode !== "batch") picked = picked.slice(0, 1);
    const profiles = state.exMultiPreset ? state.exProfiles : [];
    // 成品份数 = 草稿数 ×（默认预设 1 份 + 勾选的预设各 1 份）
    const perDraft = profiles.length ? profiles.length + 1 : 1;
    return { drafts: picked.length, profiles: profiles.length, perDraft: perDraft,
             total: picked.length * perDraft };
  }

  function updateExHint() {
    const el = $("#ex-pick-hint");
    if (!el) return;
    const c = exCounts();
    const bits = [];
    if (state.exMode === "batch") bits.push("可勾选多个草稿，一次任务顺序导出。");
    if (c.profiles) {
      bits.push(`当前：${c.drafts} 个草稿 ×（默认预设 1 + 已选 ${c.profiles} 个预设）= 共 ${c.total} 份成品。`);
    }
    el.textContent = bits.join(" ");
    el.hidden = !bits.length;
  }

  function exportPayload() {
    const page = $("#page-run");
    const dirEl = page && $("#ex-dir");
    const toEl = page && $("#ex-timeout");
    let picked = state.exSelected.slice();
    if (state.exMode !== "batch") picked = picked.slice(0, 1);
    const profiles = state.exMultiPreset ? state.exProfiles.slice() : [];
    const items = [];
    if (profiles.length) {
      picked.forEach((draft) => {
        // 默认预设版（草稿原样）先导一份，再按勾选的预设各克隆一份
        items.push({ draft: draft });
        profiles.forEach((profile) => items.push({ draft: draft, profile: profile }));
      });
    } else {
      picked.forEach((draft) => items.push({ draft: draft }));
    }
    return {
      items: items,
      // 多预设导出会克隆新草稿，导出根固定取 presets.json
      export_dir: (state.exMultiPreset || !dirEl) ? "" : dirEl.value.trim(),
      cover_fallback: (state.exMultiPreset && state.exCoverFallback) ? "source" : "",
      export_timeout: toEl ? Number(toEl.value) || 60 : 60
    };
  }

  function renderGuide() {
    const box = $("#run-guide");
    if (box) box.innerHTML = View.guideBanner(state.guide);
  }

  function collectForm() {
    const page = $("#page-run");
    if (!page) return;
    const f = state.form;
    const modeEl = $('input[name="mode"]:checked', page);
    f.mode = modeEl ? modeEl.value : "batch";
    const sel = $('input[name="sel-mode"]:checked', page);
    f.sel_mode = sel ? sel.value : "all";
    const manual = $('[data-field="series_manual"]', page);
    const pick = $('[data-field="series_dir"]', page);
    f.series_manual = manual ? manual.value.trim() : "";
    f.series_dir = f.series_manual || (pick ? pick.value : "");
    const get = (k) => { const el = $(`[data-field="${k}"]`, page); return el ? el.value : ""; };
    f.from = get("from");
    f.to = get("to");
    f.n = get("n");
    f.cut = get("cut");
    f.retries = get("retries");
    f.timeout = get("timeout");
    f.prefix = get("prefix");
    f.delay = get("delay");
    f.resolution = get("resolution");
    f.vocal = get("vocal");
    const exp = $('[data-field="export"]', page);
    f.export = exp ? exp.checked : true;
    const nz = $('[data-field="normalize"]', page);
    f.normalize = nz ? nz.checked : false;
    const cf = $('[data-field="coverFallback"]', page);
    f.coverFallback = cf ? cf.checked : false;
  }

  function syncRun() {
    const page = $("#page-run");
    if (!page) return;
    // UI 自动剪辑底层只能生成一个草稿：多选回落为单选（保留最先勾选的那组）
    if (state.form.mode === "ui" && state.form.profiles.length > 1) {
      state.form.profiles.length = 1;
    }
    const pickBox = $("[data-profile-picks]", page);
    const pickList = pickBox && pickBox.querySelector(".pick-list");
    const uiFlag = state.form.mode === "ui" ? "1" : "0";
    if (pickList && pickList.dataset.ui !== uiFlag) renderProfilePicks();
    const hint = $("#mode-hint", page);
    if (hint) {
      hint.textContent = state.form.mode === "ui"
        ? "当前走 UI 自动化链路：分辨率 / 人声模式 / 转码仅此模式有效。"
        : "当前走标准串流链路（pipeline/batch_runner.py），与「快速上手」一致。";
    }
    const uiBlock = $("[data-ui-only]", page);
    if (uiBlock) uiBlock.classList.toggle("hidden", state.form.mode !== "ui");
    $$("[data-batch-only]", page).forEach((el) => {
      el.classList.toggle("hidden", state.form.mode === "ui");
    });
    $$("[data-sel]", page).forEach((el) => {
      el.classList.toggle("hidden", el.dataset.sel !== state.form.sel_mode);
    });

    // 裁剪秒数：0 = 不裁剪；越界给出即时提示
    const cutEl = $('[data-field="cut"]', page);
    const cutNote = $("#cut-note", page);
    if (cutEl && cutNote) {
      const raw = cutEl.value.trim();
      const n = raw === "" ? 0 : Number(raw);
      if (!isFinite(n) || n < 0 || n > 3600) {
        cutNote.textContent = "需在 0–3600 秒之间";
        cutNote.className = "chip chip-warn";
      } else if (n === 0) {
        cutNote.textContent = "0 = 不裁剪（不传 --cut）";
        cutNote.className = "chip";
      } else {
        cutNote.textContent = "将传 --cut " + n;
        cutNote.className = "chip chip-brand";
      }
    }

    const running = !!(state.run && state.run.status === "running");
    const stop = $("#btn-stop", page);
    if (stop) {
      stop.disabled = !running;
      stop.title = running ? "停止当前任务" : "当前没有运行中的任务";
    }
    const start = $("#btn-start", page);
    if (start) {
      start.disabled = running;
      start.title = running ? "任务运行中，请先停止或等待完成" : "提交任务";
    }
  }

  function numberOrNull(v) {
    return v === "" || v == null ? null : Number(v);
  }

  function payload() {
    const f = state.form;
    const p = {
      mode: f.mode,
      series_dir: f.series_dir,
      selection: { mode: f.sel_mode },
      cut: numberOrNull(f.cut),
      cover_image: f.cover,
      profiles: f.mode === "ui" ? f.profiles.slice(0, 1) : f.profiles.slice(),
      cover_fallback: (f.mode !== "ui" && f.profiles.length > 1 && f.coverFallback) ? "source" : "",
      export: f.export,
      draft_prefix: f.prefix,
      retries: numberOrNull(f.retries),
      export_timeout: numberOrNull(f.timeout),
      delay: numberOrNull(f.delay)
    };
    if (f.sel_mode === "range") {
      p.selection.from = numberOrNull(f.from);
      p.selection.to = numberOrNull(f.to);
    } else if (f.sel_mode === "first") {
      p.selection.n = numberOrNull(f.n);
    }
    if (f.mode === "ui") {
      p.export_resolution = f.resolution || null;
      p.vocal_mode = f.vocal;
      p.normalize = f.normalize;
    }
    return p;
  }

  // 阶段表按链路区分：UI 自动剪辑（ui_pipeline）与串流（batch_runner）步骤不同；
  // 仅导出没有剪辑阶段，只保留一个「导出」阶段。
  const EXPORT_ONLY_STAGES = [{ key: "export", label: "导出", percent: 0 }];
  function stageList(mode) {
    if (mode === "export_only") return EXPORT_ONLY_STAGES;
    return (mode === "ui" ? state.uiStages : state.stages) || [];
  }

  function updateStatus() {
    const area = $("#status-area");
    if (area) area.innerHTML = View.statusCard(state.run, state.queue,
      stageList(state.run && state.run.mode));
    const exArea = $("#status-area-export");
    if (exArea) exArea.innerHTML = View.statusCard(state.run, state.queue,
      stageList(state.run && state.run.mode));
    const stop = $("#btn-stop");
    if (stop) stop.disabled = !(state.run && state.run.status === "running");
    const exStop = $("#btn-export-stop");
    if (exStop) exStop.disabled = !(state.run && state.run.status === "running");
    const exStart = $("#btn-export-start");
    if (exStart) exStart.disabled = !!(state.run && state.run.status === "running");
    updateStatusbar();
  }

  // ── 预设组多选（勾选顺序 = 账号顺序）───────────────────────────
  function renderProfilePicks() {
    const page = $("#page-run");
    if (!page) return;
    const box = $("[data-profile-picks]", page);
    if (box) box.innerHTML = View.profilePicks(state);
    const hint = $("#prof-hint", page);
    if (hint) hint.textContent = View.profileHint(state);
    const cfField = $("#f-cover-fallback-field", page);
    if (cfField) cfField.hidden = !(state.form.mode !== "ui" && state.form.profiles.length > 1);
  }

  function toggleProfile(name, on) {
    const list = state.form.profiles;
    const i = list.indexOf(name);
    if (on && i < 0) list.push(name);
    else if (!on && i >= 0) list.splice(i, 1);
    if (state.form.mode === "ui" && list.length > 1) list.length = 1;
    renderProfilePicks();
    syncRun();
  }

  // ── 封面上传 ───────────────────────────────────────────────────
  function renderCover() {
    const drop = $("#cover-drop");
    if (drop) drop.innerHTML = View.coverBox(state.coverInfo);
  }

  async function uploadCoverFile(file) {
    if (!file) return;
    if (!/^image\/(png|jpeg|webp)$/.test(file.type) &&
        !/\.(png|jpe?g|webp)$/i.test(file.name)) {
      toast("只支持 PNG / JPG / WEBP", "err");
      return;
    }
    if (file.size > 20 * 1024 * 1024) {
      toast("图片不能超过 20 MB", "err");
      return;
    }
    try {
      const res = await API.uploadCover(file);
      if (state.coverInfo && state.coverInfo.url) URL.revokeObjectURL(state.coverInfo.url);
      state.coverInfo = {
        path: res.path, name: res.name, width: res.width, height: res.height,
        size: res.size, warnings: res.warnings || [], url: URL.createObjectURL(file)
      };
      state.form.cover = res.path;
      renderCover();
      const n = (res.warnings || []).length;
      toast(n ? "已上传，但尺寸不合规：" + res.warnings.join("；")
              : "封面上传成功：" + res.width + "×" + res.height,
            n ? "warn" : "ok");
    } catch (err) {
      toast(err.message, "err");
    }
  }

  function clearCover() {
    if (state.coverInfo && state.coverInfo.url) URL.revokeObjectURL(state.coverInfo.url);
    state.coverInfo = null;
    state.form.cover = "";
    renderCover();
    toast("已清除上传封面：将使用剧目录中的封面", "info");
  }

  function bindRunPage() {
    const page = $("#page-run");
    const sync = () => { collectForm(); syncRun(); };
    page.addEventListener("input", (e) => {
      if (e.target.id === "ex-search") {
        renderDraftList();
        return;
      }
      if (e.target.name === "mode" || e.target.name === "sel-mode" || e.target.dataset.field) {
        sync();
      }
    });
    page.addEventListener("change", (e) => {
      if (e.target.dataset && e.target.dataset.exDraft != null) {
        const name = e.target.dataset.exDraft;
        const i = state.exSelected.indexOf(name);
        if (e.target.checked && i < 0) state.exSelected.push(name);
        if (!e.target.checked && i >= 0) state.exSelected.splice(i, 1);
        renderDraftList();
        updateExHint();
        return;
      }
      if (e.target.dataset && e.target.dataset.exProfile != null) {
        const name = e.target.dataset.exProfile;
        const i = state.exProfiles.indexOf(name);
        if (e.target.checked && i < 0) state.exProfiles.push(name);
        if (!e.target.checked && i >= 0) state.exProfiles.splice(i, 1);
        renderProfileList();
        updateExHint();
        return;
      }
      if (e.target.id === "ex-cover-fallback") {
        state.exCoverFallback = e.target.checked;
        return;
      }
      if (e.target.name === "ex-mode") {
        state.exMode = e.target.value;
        // 切回单剧导出时只保留第一个勾选，避免遗留多选
        if (state.exMode !== "batch" && state.exSelected.length > 1) {
          state.exSelected = state.exSelected.slice(0, 1);
        }
        renderDraftList();
        updateExHint();
        return;
      }
      if (e.target.id === "ex-multi-preset") {
        state.exMultiPreset = e.target.checked;
        const sec = $("#ex-profile-sec");
        if (sec) sec.hidden = !state.exMultiPreset;
        const dir = $("#ex-dir");
        if (dir) dir.disabled = state.exMultiPreset;
        const dirHint = $("#ex-dir-hint");
        if (dirHint) {
          dirHint.textContent = state.exMultiPreset
            ? "多预设导出会克隆新草稿，固定使用 presets.json 的导出根，此输入已禁用。"
            : "";
        }
        updateExHint();
        return;
      }
      if (e.target.dataset && e.target.dataset.preset != null) {
        toggleProfile(e.target.dataset.preset, e.target.checked);
        return;
      }
      if (e.target.id === "cover-file") {
        const f = e.target.files && e.target.files[0];
        if (f) uploadCoverFile(f);
        e.target.value = "";
        return;
      }
      if (e.target.name === "mode" || e.target.name === "sel-mode" || e.target.dataset.field) {
        sync();
      }
    });
    page.addEventListener("keydown", (e) => {
      if (e.target.id === "cover-drop" && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault();
        const fi = $("#cover-file");
        if (fi) fi.click();
      }
    });
    ["dragenter", "dragover"].forEach((ev) => {
      page.addEventListener(ev, (e) => {
        const drop = e.target.closest && e.target.closest("#cover-drop");
        if (!drop) return;
        e.preventDefault();
        drop.classList.add("drag");
      });
    });
    page.addEventListener("dragleave", (e) => {
      const drop = e.target.closest && e.target.closest("#cover-drop");
      if (drop) drop.classList.remove("drag");
    });
    page.addEventListener("drop", (e) => {
      const drop = e.target.closest && e.target.closest("#cover-drop");
      if (!drop) return;
      e.preventDefault();
      drop.classList.remove("drag");
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) uploadCoverFile(f);
    });

    // 重新扫描素材根（/api/series 每次实时读盘），只重建下拉，不动其它已填字段。
    async function refreshSeriesList() {
      const btn = $("#btn-series-refresh", page);
      const sel = $('[data-field="series_dir"]', page);
      const prev = sel ? sel.value : state.form.series_dir;
      if (btn) { btn.disabled = true; btn.textContent = "刷新中…"; }
      try {
        const r = await API.series();
        const list = r.series || [];
        state.series = list;
        const keep = list.some((s) => s.path === prev) ? prev : (list.length ? list[0].path : "");
        state.form.series_dir = keep;
        if (sel) {
          sel.innerHTML = '<option value="">（选择素材根下的剧）</option>' + list.map((s) =>
            `<option value="${escHtml(s.path)}"${s.path === keep ? " selected" : ""}>${
              escHtml(s.name + "（" + s.episodes + " 集，第" + s.first_ep + "–" + s.last_ep + "集）")
            }</option>`).join("");
          sel.value = keep;
        }
        toast(list.length ? ("剧集目录已刷新：" + list.length + " 个")
                          : "素材根下未扫描到剧集目录",
              list.length ? "ok" : "warn");
      } catch (err) {
        toast("刷新失败：" + err.message, "err");
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = "刷新剧集"; }
      }
    }

    page.addEventListener("click", async (e) => {
      if (e.target.closest("#cover-clear")) {
        clearCover();
        return;
      }
      const drop = e.target.closest("#cover-drop");
      if (drop) {
        if (!state.coverInfo) {
          const fi = $("#cover-file");
          if (fi) fi.click();
        }
        return;
      }
      const btn = e.target.closest("button");
      if (!btn || !page.contains(btn)) return;
      const id = btn.id;
      try {
        if (btn.dataset.runTab) {
          state.runTab = btn.dataset.runTab;
          applyRunTab();
          return;
        }
        if (id === "btn-series-refresh") {
          await refreshSeriesList();
          return;
        }
        if (id === "btn-export-reload") {
          state.exDrafts = null;
          await initExportPane();
          toast("草稿列表已刷新", "ok");
          return;
        }
        if (id === "btn-export-stop") {
          const conf = await window.Dialog.confirm({
            title: "确认停止导出",
            subtitle: "将终止当前正在执行的导出任务",
            text: "正在导出的草稿若中断，半成品文件按现有清理规则处理。",
            tone: "danger",
            confirmText: "停止导出"
          });
          if (!conf.ok) return;
          const res = await API.stop();
          toast(res.stopped ? "已请求停止导出" : (res.reason || "未停止"),
                res.stopped ? "warn" : "info");
          return;
        }
        if (id === "btn-export-start") {
          if (state.exMultiPreset && !state.exProfiles.length) {
            toast("已开启多预设导出，请至少勾选一个预设组", "err");
            return;
          }
          const p = exportPayload();
          if (!p.items.length) {
            toast("请先勾选要导出的草稿", "err");
            return;
          }
          const rebaseAll = !!state.exMultiPreset && state.exProfiles.length > 0;
          const draftSet = Array.from(new Set(p.items.map((it) => it.draft)));
          const conf = await window.Dialog.confirm({
            title: "确认导出",
            subtitle: rebaseAll ? "克隆草稿、换预设后导出" : "直接导出已存在的草稿",
            text: state.exMode === "batch"
              ? "批量导出会在同一次任务中顺序处理这些草稿。"
              : "",
            details: [
              "草稿数：" + draftSet.length,
              draftSet.slice(0, 6).join("、") + (draftSet.length > 6 ? " 等" : ""),
              "换预设：" + (rebaseAll
                ? state.exProfiles.join("、") + "（另含默认预设版 1 份/草稿）"
                : "否"),
              "成品份数：" + p.items.length,
              "封面兜底：" + (rebaseAll
                ? (state.exCoverFallback
                    ? "缺组封面时复用源草稿封面"
                    : "缺组封面时抽帧，失败则中止")
                : "不涉及"),
              "导出目录：" + (p.export_dir || "（presets.json 默认）"),
              "单条超时：" + p.export_timeout + " 分钟"
            ],
            confirmText: "开始导出"
          });
          if (!conf.ok) return;
          setBusy(btn, true);
          try {
            const res = await API.runExportOnly(p);
            state.run = res.run;
            updateStatus();
            if (res.run) toast("导出任务已提交：" + res.run.id, "ok");
            refreshLock();
          } finally {
            setBusy(btn, false);
          }
          return;
        }
        if (id === "btn-preview") {
          const area = $("#preview-area");
          if (area.dataset.open === "1") {
            area.innerHTML = "";
            area.dataset.open = "0";
            return;
          }
          collectForm();
          setBusy(btn, true);
          try {
            area.innerHTML = View.previewBlock(await API.preview(payload()));
            area.dataset.open = "1";
          } finally {
            setBusy(btn, false);
          }
        } else if (id === "btn-start") {
          collectForm();
          const p = payload();
          const conf = await window.Dialog.confirm({
            title: "确认执行任务",
            subtitle: "提交后会拉起剪辑流程，期间剪映窗口会被自动操作",
            text: "请确认剧集目录、预设组与导出设置无误。",
            details: [
              "模式：" + (p.mode === "ui" ? "UI 自动剪辑" : "串流批量"),
              "剧集目录：" + (p.series_dir || "（未选择）"),
              "草稿前缀：" + (p.draft_prefix || "（默认）"),
              "预设组：" + ((p.profiles || []).join("、") || "（默认）"),
              "完成后导出：" + (p.export ? "是" : "否")
            ],
            confirmText: "执行任务"
          });
          if (!conf.ok) return;
          setBusy(btn, true);
          try {
            const res = await API.start(p);
            state.run = res.run;
            updateStatus();
            if (res.run) toast("任务已提交：" + res.run.id, "ok");
            refreshLock();
          } finally {
            setBusy(btn, false);
            syncRun();
          }
        } else if (id === "btn-stop") {
          const multi = state.run && (state.run.account_total || 0) > 1;
          const conf = await window.Dialog.confirm({
            title: "确认停止任务",
            subtitle: "将终止当前正在执行的剪辑任务",
            text: multi
              ? "当前账号与后续账号会一并终止，半成品副本将被清理。"
              : "任务会立即终止，已生成的中间结果按现有清理规则处理。",
            tone: "danger",
            confirmText: "停止任务"
          });
          if (!conf.ok) return;
          const res = await API.stop();
          toast(res.stopped
            ? (multi
                ? "已请求停止：当前账号与后续账号一并终止，半成品副本会清理"
                : "已请求停止（pid " + res.pid + "）")
            : (res.reason || "未停止"), res.stopped ? "warn" : "info");
        }
      } catch (err) {
        toast(err.message, "err");
      }
    });
  }

  // ── 归档清理页 ─────────────────────────────────────────────────
  async function loadManifests() {
    const page = $("#page-archive");
    page.innerHTML = View.skeleton(5);
    try {
      const res = await API.manifests();
      state.manifests = res.manifests || [];
      if (!state.archiveDraft && state.manifests.length) {
        state.archiveDraft = state.manifests[0].draft;
      }
      renderArchive();
    } catch (err) {
      toast(err.message, "err");
      state.manifests = [];
      renderArchive();
    }
  }

  function renderArchive() {
    $("#page-archive").innerHTML = View.archive(state);
  }

  function currentArchiveAction() {
    const el = $('input[name="arc-action"]:checked');
    return el ? el.value : "archive";
  }

  function bindArchivePage() {
    const page = $("#page-archive");
    page.addEventListener("change", (e) => {
      if (e.target.id === "f-archive-draft") {
        state.archiveDraft = e.target.value;
      }
    });
    // 清单筛选（委托，重渲染后仍有效）
    page.addEventListener("input", (e) => {
      if (e.target.id !== "f-arc-q") return;
      state.archQuery = e.target.value;
      const pos = e.target.selectionStart;
      renderArchive();
      const q2 = $("#f-arc-q");
      if (q2) { q2.focus(); q2.setSelectionRange(pos, pos); }
    });
    page.addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn || !page.contains(btn)) return;
      const id = btn.id;
      try {
        if (id === "btn-reload-manifests") {
          await loadManifests();
        } else if (id === "btn-plan") {
          const sel = $("#f-archive-draft");
          state.archiveDraft = sel ? sel.value : "";
          setBusy(btn, true);
          const action = currentArchiveAction();
          const inter = $("#f-arc-inter");
          try {
            const res = await API.plan({
              action: action,
              draft: state.archiveDraft,
              intermediates_only: action === "clean" && !!(inter && inter.checked)
            });
            state.lastPlan = res;
            $("#plan-area").innerHTML = View.planBlock(res, false);
            toast("计划已生成（dry-run，磁盘未变化）", "ok");
          } finally {
            setBusy(btn, false);
          }
        } else if (id === "btn-execute") {
          const token = btn.dataset.token;
          const action = currentArchiveAction();
          const conf = await window.Dialog.confirm({
            title: "确认真实执行",
            subtitle: "将按上一步生成的计划改动磁盘",
            text: action === "clean"
              ? "clean 会删除计划中的文件/草稿，操作不可撤销。"
              : "将按计划归档/移动文件，执行前请再核对列表。",
            details: [
              "草稿：" + (state.archiveDraft || "-"),
              "动作：" + action
            ],
            tone: "danger",
            confirmText: "真实执行"
          });
          if (!conf.ok) return;
          setBusy(btn, true);
          try {
            const res = await API.execute({ token: token });
            $("#plan-area").innerHTML = View.executeBlock(res);
            toast(res.exit_code === 0 ? "执行成功"
              : "执行失败（退出码 " + res.exit_code + "）",
              res.exit_code === 0 ? "ok" : "err");
          } finally {
            btn.classList.remove("busy");
            btn.disabled = false;
          }
        }
      } catch (err) {
        toast(err.message, "err");
        if (id === "btn-plan") await loadManifests();
      }
    });
  }

  // ── 历史页 ─────────────────────────────────────────────────────
  async function loadHistory() {
    const page = $("#page-history");
    page.innerHTML = View.skeleton(6);
    try {
      const res = await API.runs();
      state.history = res.runs || [];
      state.externalHistory = res.external || [];
      state.detail = null;
      renderHistoryPage();
    } catch (err) {
      toast(err.message, "err");
      state.history = [];
      state.externalHistory = [];
      renderHistoryPage();
    }
  }

  function renderHistoryPage() {
    const page = $("#page-history");
    page.innerHTML = View.history(state);
    bindHistoryRows();
  }

  function bindHistoryRows() {
    // 事件委托：仅导出按钮（比行点击优先级高，stopPropagation 避免同时打开详情）
    const page = $("#page-history");
    if (page && !page.dataset.exportBound) {
      page.dataset.exportBound = "1";
      page.addEventListener("click", async (e) => {
        const btn = e.target.closest("[data-export-only]");
        if (!btn) return;
        e.stopPropagation();
        const draft = btn.dataset.draft;
        const rid = btn.dataset.exportOnly;
        const res = await window.Dialog.confirm({
          title: "确认仅导出",
          subtitle: "草稿已就绪，直接串行导出（不重建草稿）",
          text: `草稿：${draft}\n导出根：默认 export_dir\n超时 60min/份`,
          confirmText: "开始仅导出"
        });
        if (!res.ok) return;
        try {
          btn.disabled = true; btn.textContent = "提交中…";
          const out = await API.runExportOnly({ draft_name: draft });
          toast("仅导出任务已提交：" + (out.run ? out.run.id : ""), "ok");
          location.hash = "#/run";
          state.run = out.run || state.run;
          refreshLock();
        } catch (err) {
          toast(err.message, "err");
          btn.disabled = false; btn.textContent = "仅导出";
        }
      });
    }
    $$("#page-history tr[data-run]").forEach((tr) => {
      tr.addEventListener("click", async () => {
        try {
          state.detail = await API.runDetail(tr.dataset.run);
          const box = $("#history-detail");
          if (box) box.innerHTML = View.historyDetail(state.detail);
          const btn = $("#btn-rerun");
          if (btn) {
            btn.onclick = async () => {
              const conf = await window.Dialog.confirm({
                title: "确认重跑",
                subtitle: "将按该次运行的历史参数重新提交任务",
                details: ["运行记录：" + btn.dataset.run],
                confirmText: "重跑"
              });
              if (!conf.ok) return;
              setBusy(btn, true);
              try {
                const res = await API.rerun(btn.dataset.run);
                state.run = res.run;
                location.hash = "#/run";
                toast("已按上次参数重跑：" + (res.run ? res.run.id : ""), "ok");
                refreshLock();
              } catch (err) {
                toast(err.message, "err");
              } finally {
                setBusy(btn, false);
              }
            };
          }
        } catch (err) {
          toast(err.message, "err");
        }
      });
    });
    const reload = $("#btn-reload-history");
    if (reload) reload.addEventListener("click", loadHistory);
    // 筛选：搜索 + 状态；重渲染后把焦点还给输入框
    const q = $("#f-history-q");
    if (q) {
      q.addEventListener("input", () => {
        state.historyQuery = q.value;
        const pos = q.selectionStart;
        renderHistoryPage();
        const q2 = $("#f-history-q");
        if (q2) { q2.focus(); q2.setSelectionRange(pos, pos); }
      });
    }
    const st = $("#f-history-status");
    if (st) st.addEventListener("change", () => {
      state.historyStatus = st.value;
      renderHistoryPage();
    });
  }

  // ── 实时状态（SSE） ─────────────────────────────────────────────
  function bindEvents() {
    // 失败/停止后的「重新执行」：状态卡每秒整块重渲染，只能走事件委托
    document.addEventListener("click", async (e) => {
      const btn = e.target.closest("#btn-rerun-current");
      if (!btn) return;
      const rid = btn.dataset.run;
      const conf = await window.Dialog.confirm({
        title: "确认重新执行",
        subtitle: "按本次失败任务的参数重新提交",
        details: ["运行记录：" + rid],
        confirmText: "重新执行"
      });
      if (!conf.ok) return;
      try {
        const res = await API.rerun(rid);
        if (res.run) state.run = res.run;
        toast("已重新提交：" + (res.run ? res.run.id : ""), "ok");
        refreshLock();
      } catch (err) {
        toast(err.message, "err");
      }
    });
    const es = API.events();
    es.onopen = () => setService(true);
    es.onerror = () => setService(false);
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type !== "state") return;
        const prev = state.run ? state.run.status : null;
        state.run = data.run;
        state.queue = data.queue || 0;
        updateStatus();
        if (state.run && state.run.status !== prev
            && (state.run.status === "succeeded" || state.run.status === "failed"
                || state.run.status === "stopped")) {
          refreshLock();
        }
      } catch (err) { /* 忽略坏帧 */ }
    };
  }

  // ── 启动 ───────────────────────────────────────────────────────
  async function boot() {
    initTheme();
    bindNav();
    bindRunPage();
    bindArchivePage();
    // 环境页点了「完整检查」后，把任务页引导条也同步更新（用最新完整检查 summary 替换 boot 时的快速盘点）
    document.addEventListener("env:fullcheck", (e) => {
      const s = e.detail && e.detail.summary;
      if (!s) return;
      state.guide = state.guide || {};
      state.guide.fail = s.fail || 0;
      state.guide.warn = s.warn || 0;
      renderGuide();
    });
    try {
      const [cfg, series] = await Promise.all([API.config(), API.series()]);
      state.config = cfg;
      state.stages = cfg.stages || [];
      state.uiStages = cfg.ui_stages || [];
      state.series = series.series || [];
      state.guide = {
        fail: 0, warn: 0,
        noPresets: !(cfg.presets && (cfg.presets.profiles || []).length)
      };
      if (!state.form.series_dir && state.series.length) {
        state.form.series_dir = state.series[0].path;
      }
      const cur = await API.current();
      state.run = cur.run;
      state.queue = cur.queue || 0;
    } catch (err) {
      toast("初始化失败：" + err.message, "err");
    }
    renderRun();
    // 首屏快速盘点：有失败/警告时在任务页给一条引导，而不是让用户自己找
    API.envInventory().then((inv) => {
      const s = inv.summary || {};
      state.guide = {
        fail: s.fail || 0, warn: s.warn || 0,
        noPresets: !!(state.guide && state.guide.noPresets)
      };
      renderGuide();
    }).catch(() => {});
    bindEvents();
    refreshLock();
    refreshExternal();
    setInterval(refreshLock, 12000);
    setInterval(refreshExternal, 12000);
    setInterval(updateStatusbar, 1000);
    showPage((location.hash || "#/run").replace("#/", "") || "run");
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
