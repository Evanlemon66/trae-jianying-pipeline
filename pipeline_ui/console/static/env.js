/* env.js —— 环境初始化页：只读清单 + 白名单动作 + presets.json 编辑器。
   自包含模块，挂载 window.Env；接口统一走 API。 */
(function () {
  "use strict";

  const state = {
    inv: null, deps: null, full: null, job: null,
    presets: null, form: null, form0: "",
    checking: false,
    activeTab: "presets",
    activeDepTab: "deps",
    autostart: null,
    autostartBusy: false,
    collapsed: new Set(),
    userSet: new Set(),
    menusBound: false,
    jobLogOpen: false,
    timer: null, inited: false
  };

  // 已关闭的安装记录 id（持久化，刷新后不再占位置）
  const JOB_DISMISS_KEY = "jy.console.envJobDismissed";
  function jobDismissedId() {
    try { return localStorage.getItem(JOB_DISMISS_KEY) || ""; } catch (e) { return ""; }
  }
  function setJobDismissed(id) {
    try {
      if (id) localStorage.setItem(JOB_DISMISS_KEY, id);
      else localStorage.removeItem(JOB_DISMISS_KEY);
    } catch (e) { /* 隐私模式下忽略 */ }
  }

  // 「环境检查」下的 7 个固定二级页签；健康树与之一一对应
  const TAB_DEFS = [
    { key: "deps",       label: "运行环境和状态",     groups: ["deps", "runtime"] },
    { key: "dirs",       label: "配置路径",           groups: ["dirs"] },
    { key: "config",     label: "预设配置检查",       groups: ["config"] },
    { key: "jianying",   label: "剪映前置",           groups: ["jianying"] },
    { key: "host",       label: "watcher 托管服务",   groups: ["host"] },
    { key: "depsDetail", label: "依赖明细",           groups: [] },
    { key: "service",    label: "Console 自启动服务", groups: [] }
  ];

  const $ = (s, r) => (r || document).querySelector(s);
  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  let toastTimer = null;
  function toast(msg, type) {
    const el = $("#toast"), text = $("#toast-msg");
    if (!el || !text) return;
    text.textContent = msg;
    el.className = "toast show " + (type || "info");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = "toast"; }, 4600);
  }
  function tag(st) {
    const map = { ok: ["ok", "通过"], warn: ["warn", "警告"], fail: ["fail", "失败"] };
    const m = map[st] || ["quiet", st || "未检查"];
    return `<span class="tag ${m[0]}">${m[1]}</span>`;
  }

  // ── 页面骨架 ────────────────────────────────────────────────────
  function mount() {
    $("#page-env").innerHTML = `
    <div class="page-head">
      <div>
        <h1>环境初始化</h1>
        <p>环境预设、依赖与运行状态集中管理；完整检查只读，写操作都要二次确认</p>
      </div>
      <div class="head-actions">
        <button class="btn btn-primary" data-env-check
                title="重新检查全部环境、依赖和运行状态；含 CUDA、Chromium、ffmpeg、OCR 与模型实测">完整检查</button>
      </div>
    </div>
    <div class="env-tabs" role="tablist" aria-label="环境初始化">
      <button class="env-tab active" id="env-tab-presets" data-env-tab="presets"
              role="tab" aria-selected="true" aria-controls="env-panel-presets">环境预设</button>
      <button class="env-tab" id="env-tab-deps" data-env-tab="deps"
              role="tab" aria-selected="false" aria-controls="env-panel-deps">环境检查</button>
    </div>
    <div class="env-tab-panel" id="env-panel-presets" role="tabpanel" aria-labelledby="env-tab-presets">
      <div id="env-editor"></div>
    </div>
    <div class="env-tab-panel" id="env-panel-deps" role="tabpanel" aria-labelledby="env-tab-deps" hidden>
      <div class="env-section-head">
        <div>
          <h2>环境检查</h2>
          <p>运行环境、路径、配置、托管与运行状态统一在这里检查；配置项在「环境预设」页编辑</p>
        </div>
      </div>
      <div class="env-tab-health" id="env-tree"></div>
      <div id="env-job"></div>
      <div id="env-deps"></div>
      <div id="env-service"></div>
      <div id="env-dep-groups"></div>
    </div>`;

    // 卡片标题栏点击 = 展开/收起（再次点击恢复）
    $("#page-env").addEventListener("click", (e) => {
      const head = e.target.closest(".card-head[data-collapse]");
      if (head) toggleCollapse(head);
    });
    $("#page-env").addEventListener("keydown", (e) => {
      const head = e.target.closest(".card-head[data-collapse]");
      if (head && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault();
        toggleCollapse(head);
      }
    });

    // 事件委托：健康条 / 页签 / 二级页签 / 行内菜单 / 动作按钮
    $("#page-env").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      if (btn.dataset.envCheck !== undefined) {
        runFullCheck();
      } else if (btn.dataset.treeTab) {
        gotoGroup(btn.dataset.treeTab);
      } else if (btn.dataset.envTab) {
        switchTab(btn.dataset.envTab);
      } else if (btn.dataset.rowMenu !== undefined) {
        toggleRowMenu(btn);
      } else if (btn.dataset.action) {
        closeRowMenus();
        runAction(btn.dataset.action, btn.dataset.label || btn.textContent, btn.dataset.item || null);
      } else if (btn.id === "env-install-stop") {
        confirmStopInstall();
      } else if (btn.id === "env-job-toggle") {
        state.jobLogOpen = !state.jobLogOpen;
        renderJob();
      } else if (btn.id === "env-job-close") {
        if (state.job) setJobDismissed(state.job.id);
        state.job = null;
        state.jobLogOpen = false;
        renderJob();
      }
    });
    $("#page-env").addEventListener("change", (e) => {
      if (e.target.id === "env-autostart") setAutostart(e.target.checked);
    });
    bindGlobalMenus();
    // 配置编辑：未保存标记 + Ctrl/Cmd+S 保存 + 关页前提示
    $("#env-editor").addEventListener("input", syncDirty);
    document.addEventListener("keydown", (e) => {
      if (!(e.ctrlKey || e.metaKey) || String(e.key).toLowerCase() !== "s") return;
      if (!$("#page-env").classList.contains("active") || !state.form) return;
      e.preventDefault();
      saveEditor();
    });
    window.addEventListener("beforeunload", (e) => {
      if (!editorDirty()) return;
      e.preventDefault();
      e.returnValue = "";
    });
    renderDeps();
  }

  function switchTab(name) {
    state.activeTab = name === "deps" ? "deps" : "presets";
    $("#page-env").querySelectorAll("[data-env-tab]").forEach((btn) => {
      const active = btn.dataset.envTab === state.activeTab;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    ["presets", "deps"].forEach((t) => {
      $("#env-panel-" + t).hidden = state.activeTab !== t;
    });
    renderTree();
  }

  // 状态栏「服务」→ 直接落到 Console 自启动服务
  function openService() {
    if (!state.inited) { mount(); state.inited = true; }
    state.activeTab = "deps";
    state.activeDepTab = "service";
    switchTab("deps");
    renderGroups();
    loadAutostart();
    const el = $("#env-service");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // 健康树 → 环境检查对应二级页签
  function gotoGroup(key) {
    const map = { runtime: "deps", depsDetail: "depsDetail" };
    const tab = map[key] || key;
    state.activeTab = "deps";
    if (TAB_DEFS.some((t) => t.key === tab)) state.activeDepTab = tab;
    switchTab("deps");
    renderGroups();
  }

  // ── 写操作确认（统一 Dialog） ───────────────────────────────────
  const ACTIONS = {
    "deps.install": {
      title: "确认执行：全量安装依赖",
      subtitle: "先普通权限探测；仅存在管理员项（长路径 / VC++ / 缺失 ffmpeg）时才弹一次 UAC",
      text: "全量 10 步，可能下载数 GB、耗时较长；检测到管理员前置项才会弹 UAC，请点「是」。安装期间不能提交剪辑任务。",
      details: [
        "① 普通权限 -DetectOnly 探测（普通项 / 管理员项）",
        "② 仅缺失长路径、VC++ Redistributable、ffmpeg 时提权补齐",
        "③ 普通权限安装 Python / pip / torch / requirements",
        "UI OCR（rapidocr_onnxruntime）、Playwright Chromium、Ollama 与模型"
      ],
      confirmText: "开始安装"
    },
    "config.migrate": {
      title: "确认迁移配置",
      text: "将把 watch.roots 里唯一的素材根迁移到 presets.json 的 series_root（先备份 presets.json.bak-时间戳）。多根或缺少 export_dir/deps_root 时拒绝写入。",
      details: [
        "series_root 成为唯一素材根，watch.roots 降级为额外扫描根",
        "写回前自动备份，写回后立即校验",
        "迁移完成后请再点一次「完整检查」"
      ],
      confirmText: "执行迁移"
    },
    "dirs.ensure": {
      title: "确认创建目录",
      text: "将创建配置里尚不存在的目录（素材根/导出根/归档根/依赖根），不删除任何文件。",
      confirmText: "创建目录"
    },
    "presets.init": {
      title: "确认创建 presets.json",
      text: "将复制 presets.example.json 为 pipeline/presets.json（仅当不存在，不覆盖）。",
      confirmText: "创建配置"
    },
    "task.register": {
      title: "确认注册计划任务",
      text: "将注册 Windows 计划任务 TraeDramaWatcher（每 5 分钟拉起 watcher 守护）。",
      checkbox: { label: "注册后立即启用（默认开启）", checked: true },
      confirmText: "注册任务"
    },
    "task.pause": {
      title: "确认暂停 watcher 自动拉起",
      text: "将写入 pipeline/_work/watcher.pause：计划任务与 delayed_start 不再自动拉起 watcher。正在跑的任务不会被杀。",
      details: ["测试/调参时用，避免 watcher 自动抢任务", "恢复时删除哨兵即可"],
      confirmText: "暂停自动拉起"
    },
    "task.resume": {
      title: "确认恢复 watcher 自动拉起",
      text: "将删除暂停哨兵 pipeline/_work/watcher.pause，计划任务下次触发即重新拉起 watcher。",
      confirmText: "恢复自动拉起"
    },
    "task.stop": {
      title: "确认优雅停止 watcher",
      text: "将写入 pipeline/_work/watcher.stop：watcher 跑完当前这一轮任务后自动退出，不会砍断正在进行的编辑/导出。",
      details: ["停止后不再自动接新任务", "已暂停自动拉起时，停止后也不会被重启"],
      confirmText: "优雅停止"
    },
    "task.stop-cancel": {
      title: "确认撤销停止请求",
      text: "将删除 pipeline/_work/watcher.stop，watcher 继续正常调度。",
      confirmText: "撤销停止"
    },
    "ollama.start": {
      title: "一键拉起 Ollama",
      text: "将在本机后台运行 ollama serve，并等待本地翻译服务恢复；不会重新下载或删除模型。",
      details: ["已运行时不重复启动", "缺少配置模型时仍需另行补齐依赖"],
      confirmText: "拉起服务"
    }
  };

  async function runAction(action, label, itemId) {
    if (action === "presets.edit") { switchTab("presets"); openEditor(); return; }
    if (action === "install.stop") { confirmStopInstall(); return; }
    const meta = ACTIONS[action] || {};
    const r = await window.Dialog.confirm({
      title: meta.title || ("确认执行：" + (label || action)),
      subtitle: meta.subtitle || "此操作会修改本机环境，请确认后继续。",
      text: meta.text || "",
      details: meta.details || [],
      confirmText: meta.confirmText || "确定执行",
      checkbox: meta.checkbox
    });
    if (!r.ok) return;
    try {
      if (action === "deps.install") {
        toast("安装已启动：正在普通权限探测，仅在需要时弹一次 UAC…", "info");
        const job = await API.envAction({ action: "deps.install" });
        setJobDismissed("");
        state.jobLogOpen = true;
        state.job = job; renderJob(); pollJob();
        toast("安装已启动，实时输出见下方", "ok");
      } else if (action === "config.migrate") {
        const res = await API.envAction({ action: "config.migrate", write: true });
        toast(res.changes && res.changes.length
          ? ("迁移完成：" + res.changes.join("；"))
          : (res.detail || "无需迁移"), "ok");
        runFullCheck();
      } else if (action === "task.pause") {
        const res = await API.envAction({ action: "task.pause", reason: "Console 手动暂停" });
        toast(res.detail || "已暂停自动拉起", "ok");
        runFullCheck();
      } else if (action === "task.resume") {
        const res = await API.envAction({ action: "task.resume" });
        toast(res.detail || "已恢复自动拉起", "ok");
        runFullCheck();
      } else if (action === "task.stop") {
        const res = await API.envAction({ action: "task.stop", reason: "Console 手动停止" });
        toast(res.detail || "已请求停止", res.requested ? "ok" : "info");
        runFullCheck();
      } else if (action === "task.stop-cancel") {
        const res = await API.envAction({ action: "task.stop-cancel" });
        toast(res.detail || "已撤销停止请求", "ok");
        runFullCheck();
      } else if (action === "ollama.start") {
        toast("正在拉起 Ollama 并等待服务恢复…", "info");
        const res = await API.envAction({ action: "ollama.start" });
        toast(res.detail || (res.ok ? "Ollama 已恢复" : "Ollama 拉起失败"),
          res.ok ? "ok" : "err");
        runFullCheck();
      } else if (action === "dirs.ensure") {
        const res = await API.envAction({ action: "dirs.ensure" });
        toast(`目录创建完成：新建 ${res.created.length} 个，跳过 ${res.skipped.length} 个`, "ok");
        runFullCheck();
      } else if (action === "presets.init") {
        await API.envAction({ action: "presets.init" });
        toast("已从模板创建 presets.json，请在下方表单里改成本机路径", "ok");
        runFullCheck(); openEditor();
      } else if (action === "task.register") {
        const res = await API.envAction({ action: "task.register", enable: r.checked });
        toast("计划任务：" + (res.detail || "已注册"), "ok");
        runFullCheck();
      }
    } catch (err) {
      toast(err.message || String(err), "err");
      if (action === "deps.install") {
        state.job = await API.envJob().catch(() => null);
        renderJob();
      }
    }
  }

  async function confirmStopInstall() {
    const r = await window.Dialog.confirm({
      title: "停止依赖安装",
      subtitle: "正在运行的 install_deps.ps1 会被中止",
      text: "已下载的内容会保留，已安装的包不会回滚；之后可重新全量安装补齐。",
      tone: "danger",
      confirmText: "停止安装"
    });
    if (!r.ok) return;
    try {
      await API.envAction({ action: "install.stop" });
      toast("已请求停止安装", "warn");
      pollJob();
    } catch (err) { toast(err.message || String(err), "err"); }
  }

  // ── 统一完整检查 ─────────────────────────────────────────────────
  function setCheckBusy(busy) {
    document.querySelectorAll("[data-env-check]").forEach((btn) => {
      btn.disabled = busy;
      btn.textContent = busy ? "检查中…" : (state.inv ? "重新检查" : "完整检查");
    });
  }

  async function runFullCheck() {
    if (state.checking) return;
    state.checking = true;
    setCheckBusy(true);
    $("#env-deps").innerHTML = `<section class="card"><div class="card-body">
      <p class="hint">正在完整检查：环境、配置、运行状态、逐包版本，以及 CUDA / Chromium / ffmpeg / OCR / 模型实测…</p>
    </div></section>`;
    try {
      const report = await API.envCheck();
      state.full = report;
      state.inv = { generated_at: report.generated_at, groups: report.groups,
                    summary: report.summary };
      state.deps = report.dependencies;
      renderGroups();
      renderDeps();
      const s = report.summary;
      toast(`完整检查完成：通过 ${s.ok}，警告 ${s.warn}，失败 ${s.fail}（${report.elapsed_s}s）`,
            s.fail ? "err" : s.warn ? "warn" : "ok");
      // 事件联动：任务页引导条、其他页签（runtime tab 首次出现）刷新
      document.dispatchEvent(new CustomEvent("env:fullcheck", {
        detail: { summary: s, deps_summary: report.dependencies && report.dependencies.summary || null }
      }));
    } catch (err) {
      if (state.deps) renderDeps();
      else $("#env-deps").innerHTML = `<div class="notice fail">${esc(err.message || err)}</div>`;
      toast(err.message || String(err), "err");
    } finally {
      state.checking = false;
      setCheckBusy(false);
    }
  }

  // ── 健康树：7 项与「环境检查」二级页签一一对应，是唯一的状态导航 ──
  function tabCounts(def) {
    const inv = state.inv;
    if (def.key === "depsDetail") {
      const s = state.deps && state.deps.summary;
      return s ? { ok: s.ok, warn: s.warn, fail: s.fail, total: state.deps.rows.length } : null;
    }
    if (def.key === "service") return null;      // 开关型，用文字状态
    if (!inv) return null;
    const keys = def.groups;
    const gs = inv.groups.filter((g) => keys.includes(g.key));
    if (!gs.length) return null;
    const c = { ok: 0, warn: 0, fail: 0, total: 0 };
    gs.forEach((g) => {
      const s = groupCounts(g);
      c.ok += s.ok; c.warn += s.warn; c.fail += s.fail;
      c.total += g.items.length;
    });
    return c;
  }

  function renderTree() {
    const box = $("#env-tree");
    if (!box) return;
    const inv = state.inv;
    let sumOk = 0, sumWarn = 0, sumFail = 0, sumTotal = 0;
    const pills = TAB_DEFS.map((def) => {
      const c = tabCounts(def);
      let dot = "pending", counts = '<span class="env-pill-counts">未检查</span>', tip = "尚未检查";
      if (def.key === "service") {
        const a = state.autostart;
        dot = a ? (a.enabled ? "ok" : "warn") : "pending";
        counts = `<span class="env-pill-counts">${a ? (a.enabled ? "已开启" : "已关闭") : "读取中"}</span>`;
        tip = a ? (a.enabled ? "已开启登录自启" : "未开启登录自启") : "读取中";
      } else if (c) {
        dot = worstOf(c);
        counts = `<span class="env-pill-counts"><b class="ok">${c.ok}</b><b class="warn">${c.warn}</b><b class="fail">${c.fail}</b></span>`;
        tip = `通过 ${c.ok} · 警告 ${c.warn} · 失败 ${c.fail} · 共 ${c.total} 项`;
        sumOk += c.ok; sumWarn += c.warn; sumFail += c.fail; sumTotal += c.total;
      }
      const active = state.activeTab === "deps" && state.activeDepTab === def.key;
      return `<button class="env-pill${active ? " active" : ""}" data-tree-tab="${esc(def.key)}" title="${esc(tip)}">
        <i class="dot ${dot}"></i><span>${esc(def.label)}</span>${counts}</button>`;
    }).join("");
    let sum = "";
    if (sumTotal) {
      const tone = sumFail ? "fail" : (sumWarn ? "warn" : "ok");
      sum = `<span class="env-sum ${tone}" title="通过 ${sumOk} · 警告 ${sumWarn} · 失败 ${sumFail} · 共 ${sumTotal} 项">
        <i class="dot ${tone}"></i>通过 <b>${sumOk}</b>/${sumTotal}</span>`;
      if (sumWarn) sum += `<span class="env-sum warn"><b>${sumWarn}</b> 警告</span>`;
      if (sumFail) sum += `<span class="env-sum fail"><b>${sumFail}</b> 失败</span>`;
    }
    box.innerHTML = `<span class="env-tab-health-title">检查概览</span>
      <div class="env-pill-list">${pills}</div>
      <span class="env-health-meta">${sum}
        <span class="hint">${inv ? "更新于 " + esc(inv.generated_at) : "尚未检查"}</span></span>`;
  }

  // ── 分组计数 ───────────────────────────────────────────────────
  function groupCounts(g) {
    const s = { ok: 0, warn: 0, fail: 0 };
    (g ? g.items : []).forEach((it) => {
      if (Object.prototype.hasOwnProperty.call(s, it.status)) s[it.status] += 1;
    });
    return s;
  }

  function worstOf(s) {
    if (s.fail) return "fail";
    if (s.warn) return "warn";
    return "ok";
  }

  function renderGroups() {
    const inv = state.inv;
    if (!TAB_DEFS.some((t) => t.key === state.activeDepTab)) state.activeDepTab = "deps";
    renderTree();
    renderService();
    renderDeps();
    const def = TAB_DEFS.find((t) => t.key === state.activeDepTab);
    const want = def ? def.groups : [];
    const groups = inv ? inv.groups.filter((g) => want.includes(g.key)) : [];
    renderGroupSet("#env-dep-groups", groups);
  }

  // 「Console 自启动服务」：登录自启开关（写 Startup 快捷方式）
  function renderService() {
    const box = $("#env-service");
    if (!box) return;
    if (state.activeDepTab !== "service") { box.innerHTML = ""; return; }
    const a = state.autostart;
    const on = !!(a && a.enabled);
    const busy = state.autostartBusy;
    box.innerHTML = `<section class="card env-service-card">
      <div class="card-head">
        <div><h2>Console 自启动服务</h2>
        <span class="card-sub">开机后自动静默启动 Console 服务（只起服务，不弹浏览器）</span></div>
      </div>
      <div class="card-body">
        <label class="switch-row">
          <input type="checkbox" id="env-autostart"${on ? " checked" : ""}${busy || !a ? " disabled" : ""}>
          <span>开机时自动启动 Console</span>
          <span class="tag ${on ? "ok" : "quiet"}">${busy ? "切换中…" : on ? "已开启" : "已关闭"}</span>
        </label>
        <p class="hint">快捷方式：${a ? esc(a.shortcut) : "读取中…"}</p>
      </div></section>`;
  }

  async function loadAutostart() {
    try {
      state.autostart = await API.consoleAutostart();
    } catch (err) {
      state.autostart = { enabled: false, shortcut: "",
                          detail: err.message || String(err) };
    }
    renderService();
    renderTree();
  }

  async function setAutostart(enabled) {
    if (state.autostartBusy) return;
    state.autostartBusy = true;
    renderService();
    try {
      const res = await API.consoleSetAutostart(enabled);
      state.autostart = res;
      toast(res.detail || (res.enabled ? "已开启登录自启" : "已关闭登录自启"), "ok");
    } catch (err) {
      toast(err.message || String(err), "err");
    } finally {
      state.autostartBusy = false;
      renderService();
    }
  }

  function isOpen(key, dflt) {
    // 所有卡片默认展开；只有用户手动折叠过才尊重，刷新后又恢复默认展开
    if (state.userSet.has(key)) return !state.collapsed.has(key);
    return true;
  }

  function toggleCollapse(head) {
    const card = head.closest(".card");
    const body = card && card.querySelector(":scope > .card-body");
    if (!body || !head.dataset.collapse) return;
    const key = head.dataset.collapse;
    const willOpen = body.hidden;
    body.hidden = !willOpen;
    card.classList.toggle("collapsed", !willOpen);
    head.setAttribute("aria-expanded", willOpen ? "true" : "false");
    state.userSet.add(key);
    if (willOpen) state.collapsed.delete(key);
    else state.collapsed.add(key);
  }

  function renderGroupSet(selector, groups) {
    const box = $(selector);
    if (!box) return;
    const cards = groups.map((g) => {
      const s = groupCounts(g);
      const rows = g.items.map((it) => {
        const acts = it.actions || [];
        const items = acts.map(([a, label]) =>
          `<button class="row-menu-item" data-action="${esc(a)}" data-label="${esc(label)}" data-item="${esc(it.id)}">${esc(label)}</button>`
        ).join("");
        const menu = acts.length ? `<div class="row-menu">
            <button class="btn btn-secondary btn-sm row-menu-btn" data-row-menu
                    aria-haspopup="true" aria-expanded="false" aria-label="更多操作">⋯</button>
            <div class="row-menu-list" hidden>${items}</div></div>` : "";
        return `<tr>
          <td class="env-item-name">${esc(it.name)}</td>
          <td>${tag(it.status)}</td>
          <td class="env-detail">${esc(it.detail)}${it.impact ? `<div class="env-impact">${esc(it.impact)}</div>` : ""}</td>
          <td class="env-actions">${menu}</td>
        </tr>`;
      }).join("");
      const key = "group:" + g.key;
      // 全通过默认收起，有警告/失败自动展开；用户手动切换过则尊重用户选择
      const open = isOpen(key, s.warn + s.fail > 0);
      return `<section class="card${open ? "" : " collapsed"}">
        <div class="card-head" data-collapse="${esc(key)}" role="button" tabindex="0"
             aria-expanded="${open ? "true" : "false"}">
          <h2>${esc(g.title)}</h2>
          <span class="card-sub">${g.items.length} 项 · ${s.ok} 通过${s.warn ? ` · ${s.warn} 警告` : ""}${s.fail ? ` · ${s.fail} 失败` : ""}</span>
          <span class="caret" aria-hidden="true"></span>
        </div>
        <div class="card-body table-wrap"${open ? "" : " hidden"}><table class="env-table">
          <thead><tr><th>检查项</th><th>状态</th><th>判定依据 / 影响</th><th>操作</th></tr></thead>
          <tbody>${rows}</tbody></table></div>
      </section>`;
    }).join("");
    box.innerHTML = cards;
  }

  // ── 行尾「⋯」菜单（fixed 定位，避免被表格滚动区裁剪） ─────────
  function bindGlobalMenus() {
    if (state.menusBound) return;
    state.menusBound = true;
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".row-menu")) closeRowMenus();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeRowMenus();
    });
    window.addEventListener("scroll", closeRowMenus, true);
    window.addEventListener("resize", closeRowMenus);
  }

  function closeRowMenus() {
    document.querySelectorAll(".row-menu-list").forEach((list) => { list.hidden = true; });
    document.querySelectorAll(".row-menu-btn[aria-expanded='true']").forEach((btn) => {
      btn.setAttribute("aria-expanded", "false");
    });
  }

  function toggleRowMenu(btn) {
    const menu = btn.closest(".row-menu");
    const list = menu && menu.querySelector(".row-menu-list");
    if (!list) return;
    const willOpen = list.hidden;
    closeRowMenus();
    if (!willOpen) return;
    list.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    const r = btn.getBoundingClientRect();
    const w = list.offsetWidth, h = list.offsetHeight;
    const left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
    let top = r.bottom + 4;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
    list.style.left = left + "px";
    list.style.top = top + "px";
  }

  // ── 依赖安装明细（独立二级页签）───────────────────────────────────
  function renderDeps() {
    const d = state.deps;
    const box = $("#env-deps");
    // 依赖明细独立成 tab，与「运行环境和状态」解耦
    if (state.activeDepTab !== "depsDetail") { box.innerHTML = ""; return; }
    const installBtn = `<button class="btn btn-secondary btn-sm" data-action="deps.install"
      data-label="全量安装依赖">全量安装依赖</button>`;
    if (!d) {
      box.innerHTML = `<section class="card">
        <div class="card-head"><div><h2>依赖明细</h2>
          <span class="card-sub">torch CUDA / Chromium / ffmpeg / OCR / 模型实测</span></div>
          ${installBtn}</div>
        <div class="card-body"><p class="hint">尚未检查。点上方「完整检查」刷新。</p></div>
      </section>`;
      return;
    }
    const s = d.summary;
    const rows = d.rows.map((r) => `<tr>
      <td class="mono">${esc(r.name)}</td><td>${tag(r.status)}</td>
      <td class="mono">${esc(r.required)}</td>
      <td class="mono">${esc(r.installed)}</td>
      <td class="env-detail">${esc(r.detail)}</td></tr>`).join("");
    box.innerHTML = `<section class="card">
      <div class="card-head"><div><h2>依赖明细</h2>
        <span class="card-sub">正常 ${s.ok} · 警告 ${s.warn} · 缺失/版本不符 ${s.fail} · ${esc(d.python)} · ${esc(d.generated_at)}</span></div>
        <div class="head-actions">
          <span class="tag ${s.fail ? "fail" : s.warn ? "warn" : "ok"}">共 ${d.rows.length} 项</span>
          ${installBtn}
        </div>
      </div>
      <div class="card-body">
        <div class="table-wrap"><table class="env-table">
          <thead><tr><th>组件</th><th>状态</th><th>要求</th><th>实际</th><th>说明</th></tr></thead>
          <tbody>${rows}</tbody></table></div>
      </div></section>`;
  }

  // ── 安装 job ────────────────────────────────────────────────────
  function renderJob() {
    const box = $("#env-job");
    const j = state.job;
    if (!j || j.status === "idle") { box.innerHTML = ""; syncInstallButton(); return; }
    const running = j.status === "running";
    // 已关闭的历史记录不再占位；运行中始终展示
    if (!running && jobDismissedId() === j.id) {
      box.innerHTML = "";
      syncInstallButton();
      return;
    }
    if (running) state.jobLogOpen = true;
    const stText = { running: "安装中", done: "执行成功", stopped: "已停止", error: "执行失败" };
    const stTag = { running: "warn", done: "ok", stopped: "warn", error: "fail" };
    const actions = running
      ? '<button class="btn btn-danger btn-sm" id="env-install-stop">停止安装</button>'
      : `<button class="btn btn-secondary btn-sm" id="env-job-toggle">${state.jobLogOpen ? "收起日志" : "查看日志"}</button>
         <button class="btn btn-secondary btn-sm" id="env-job-close">关闭</button>`;
    const logHtml = (running || state.jobLogOpen)
      ? `<pre class="env-log" id="env-log">${esc(j.log || "（等待输出…）")}</pre>`
      : `<p class="hint">日志已收起，需要排查时点「查看日志」。</p>`;
    box.innerHTML = `<section class="card">
      <div class="card-head"><div><h2>依赖安装 · ${esc(stText[j.status] || j.status)}</h2>
        <span class="card-sub">${esc(j.id || "")} · ${esc(j.started_at || "")}</span></div>
        <div class="head-actions">${actions}</div></div>
      <div class="card-body" role="status" aria-live="polite">
        <div class="env-summary">
          <span class="tag ${stTag[j.status] || "quiet"}">${esc(stText[j.status] || j.status)}</span>
          ${j.exit != null && String(j.exit) !== "0"
            ? `<span class="chip">退出码 ${esc(j.exit)}</span>` : ""}
        </div>
        ${running ? '<div class="progress" aria-label="安装进行中"><i></i></div>' : ""}
        ${logHtml}
      </div></section>`;
    const pre = $("#env-log");
    if (pre) pre.scrollTop = pre.scrollHeight;
    syncInstallButton();
  }

  function syncInstallButton() {
    const btn = $("#env-install-all");
    if (!btn) return;
    const running = !!(state.job && state.job.status === "running");
    btn.disabled = running;
    btn.textContent = running ? "安装中…" : "全量安装依赖";
  }

  async function pollJob() {
    clearTimeout(state.timer);
    const tick = async () => {
      try {
        state.job = await API.envJob();
        renderJob();
        if (state.job && state.job.status === "running") {
          state.timer = setTimeout(tick, 2000);
        } else if (state.job && state.job.status !== "idle") {
          const jt = state.job.status === "done" ? "依赖安装执行成功"
            : state.job.status === "stopped" ? "依赖安装已停止"
            : "依赖安装执行失败（退出码 " + (state.job.exit == null ? "?" : state.job.exit) + "）";
          toast(jt, state.job.status === "done" ? "ok" : "err");
          runFullCheck();
        }
      } catch (err) {
        state.timer = setTimeout(tick, 4000);
      }
    };
    tick();
  }

  // ── presets.json 编辑器 ─────────────────────────────────────────
  // 顺序与 pipeline/presets.json 顶部保持一致：先配四件套，再看其他路径
  const FIELDS_MAIN = [
    ["series_root", "短剧素材根 series_root（唯一素材根）", true],
    ["export_dir", "成品导出根目录 export_dir", true],
    ["archive_root", "归档根 archive_root（留空=不归档）", false],
    ["deps_root", "统一依赖根 deps_root", true]
  ];
  const FIELDS_MORE = [
    ["jianying_dir", "剪映安装目录 jianying_dir（留空自动探测）", false],
    ["projects_root", "剪映草稿根 projects_root（留空自动探测）", false],
    ["presets_root", "剪映组合预设库 presets_root（是目录，不是预设组）", false],
    ["jianying_log_dir", "剪映日志目录 jianying_log_dir（留空自动探测）", false]
  ];

  async function openEditor(scroll) {
    try {
      state.presets = await API.envPresets();
      state.form = JSON.parse(JSON.stringify(state.presets.config || {}));
      state.form0 = JSON.stringify(state.form);
      renderEditor();
      syncDirty();
      const el = $("#env-editor");
      if (el && scroll !== false) el.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) { toast(err.message || String(err), "err"); }
  }

  function groupKeys() {
    return Object.keys(state.form || {}).filter((k) =>
      /^presets/i.test(k) && Array.isArray(state.form[k]));
  }

  // ── 未保存保护 ─────────────────────────────────────────────────
  function editorDirty() {
    return !!(state.form && state.form0 && JSON.stringify(state.form) !== state.form0);
  }

  function syncDirty() {
    const chip = $("#env-dirty");
    if (chip) chip.hidden = !editorDirty();
  }

  function renderEditor() {
    const box = $("#env-editor");
    if (!state.form) { box.innerHTML = ""; return; }
    const f = state.form;
    const watch = f.watch || {};
    const admission = f.admission || {};
    const zhipu = ((f.translate || {}).providers || []).find((p) => p.name === "zhipu") || {};

    const fieldHtml = ([key, label, req]) => `
      <div class="field"><label>${esc(label)}${req ? ' <em class="req">*</em>' : ""}</label>
        <input type="text" data-path="${key}" value="${esc(f[key] || "")}" spellcheck="false">
      </div>`;
    const baseFields = FIELDS_MAIN.map(fieldHtml).join("");
    const moreFields = FIELDS_MORE.map(fieldHtml).join("");

    const groups = groupKeys().map((g) => {
      const rows = (f[g] || []).map((entry, i) => `
        <div class="pg-row" data-group="${esc(g)}" data-idx="${i}">
          <input type="text" class="pg-name" data-k="name" placeholder="预设名（与剪映组合预设逐字一致）"
                 value="${esc(entry.name || "")}">
          <input type="number" class="pg-alpha" data-k="alpha" placeholder="alpha" min="0" max="1" step="0.01"
                 value="${entry.alpha == null ? "" : esc(entry.alpha)}" title="滤镜透明度 0~1，可留空">
          <button class="btn btn-secondary btn-sm" data-del-entry>删除</button>
        </div>`).join("");
      return `<div class="preset-group" data-group-card="${esc(g)}">
        <div class="pg-head"><b class="mono">${esc(g)}</b><span class="hint">${(f[g] || []).length} 条</span>
          <button class="btn btn-secondary btn-sm" data-add-entry="${esc(g)}">+ 条目</button>
          <button class="btn btn-danger-ghost btn-sm" data-del-group="${esc(g)}">删除组</button></div>
        ${rows || '<p class="hint">空组</p>'}
      </div>`;
    }).join("");

    const openEd = isOpen("editor", true);
    box.innerHTML = `<section class="card${openEd ? "" : " collapsed"}" id="env-editor-card">
      <div class="card-head" data-collapse="editor" role="button" tabindex="0"
           aria-expanded="${openEd ? "true" : "false"}">
        <h2>presets.json 配置</h2>
        <span class="card-sub">保存前自动校验 + 自动备份（保留 10 份）</span>
        <span class="caret" aria-hidden="true"></span></div>
      <div class="card-body"${openEd ? "" : " hidden"}>
        <h3 class="sec-title">基础四件套（与 presets.json 顶部顺序一致）</h3>
        <div class="env-editor-grid">${baseFields}</div>
        <h3 class="sec-title">其他路径（留空则自动探测）</h3>
        <div class="env-editor-grid">${moreFields}</div>
        <h3 class="sec-title">运行参数</h3>
        <div class="env-editor-grid">
          <div class="field"><label>admission.draft_name（UI 自动剪辑默认草稿名）</label>
            <input type="text" data-path="admission.draft_name" value="${esc(admission.draft_name || "")}"></div>
          <div class="field"><label>default_preset_profile（默认预设组）</label>
            <input type="text" data-path="default_preset_profile" value="${esc(f.default_preset_profile || "")}"></div>
          <div class="field"><label>watch.archive_root（watcher 归档根）</label>
            <input type="text" data-path="watch.archive_root" value="${esc(watch.archive_root || "")}"></div>
          <div class="field"><label>智谱 api_key（英文字幕；掩码输入）</label>
            <input type="password" data-special="zhipu_key" value="${esc(zhipu.api_key || "")}" autocomplete="off"></div>
        </div>
        <div class="field"><label>watch.roots（额外扫描根，每行一个；留空只用 series_root）</label>
          <textarea data-path="watch.roots" rows="3" spellcheck="false">${esc((watch.roots || []).join("\n"))}</textarea></div>
        <div class="pg-toolbar">
          <h3 class="sec-title">预设组（presets_a / presets_b …）</h3>
          <button class="btn btn-secondary btn-sm" id="env-detect-jy">自动探测剪映目录</button>
          <button class="btn btn-secondary btn-sm" id="env-add-group">+ 新增预设组</button>
        </div>
        <div id="env-preset-groups">${groups}</div>
        <div id="env-validate-result"></div>
        <div class="action-dock">
          <span class="dirty-chip" id="env-dirty" hidden>未保存</span>
          <button class="btn btn-secondary" id="env-editor-reload">放弃修改</button>
          <button class="btn btn-primary" id="env-editor-save"
                  title="校验并保存 presets.json（Ctrl+S）">校验并保存</button>
        </div>
      </div></section>`;

    // 输入绑定
    box.querySelectorAll("[data-path]").forEach((el) => {
      el.addEventListener("input", () => {
        setPath(el.dataset.path, el.value);
      });
    });
    box.querySelector("[data-special='zhipu_key']").addEventListener("input", (e) => {
      const tr = state.form.translate = state.form.translate || {};
      const providers = tr.providers = tr.providers || [];
      let z = providers.find((p) => p.name === "zhipu");
      if (!z) { z = { name: "zhipu" }; providers.push(z); }
      z.api_key = e.target.value;
    });
    // 预设条目绑定
    box.querySelectorAll(".pg-row").forEach((row) => {
      const g = row.dataset.group, i = Number(row.dataset.idx);
      row.querySelectorAll("[data-k]").forEach((el) => {
        el.addEventListener("input", () => {
          const k = el.dataset.k;
          if (k === "alpha") {
            if (el.value === "") delete state.form[g][i].alpha;
            else state.form[g][i].alpha = Number(el.value);
          } else {
            state.form[g][i][k] = el.value;
          }
        });
      });
    });

    box.querySelector("#env-editor-reload").addEventListener("click", async () => {
      if (editorDirty()) {
        const r = await window.Dialog.confirm({
          title: "放弃未保存的修改？",
          subtitle: "当前编辑器有未保存的改动",
          text: "放弃后将重新读取已保存的 presets.json，本次改动不会写入磁盘。",
          tone: "danger",
          confirmText: "放弃修改"
        });
        if (!r.ok) return;
      }
      openEditor();
    });
    box.querySelector("#env-editor-save").addEventListener("click", saveEditor);
    box.querySelector("#env-detect-jy").addEventListener("click", detectJianying);
    box.querySelector("#env-add-group").addEventListener("click", () => {
      const name = ($("#env-new-group-name") || {}).value || "";
      addGroup(name);
    });
    box.querySelectorAll("[data-add-entry]").forEach((b) => {
      b.addEventListener("click", () => {
        const g = b.dataset.addEntry;
        state.form[g] = state.form[g] || [];
        state.form[g].push({ name: "" });
        renderEditor();
      });
    });
    box.querySelectorAll("[data-del-group]").forEach((b) => {
      b.addEventListener("click", () => {
        delete state.form[b.dataset.delGroup];
        renderEditor();
      });
    });
    box.querySelectorAll("[data-del-entry]").forEach((b) => {
      b.addEventListener("click", () => {
        const row = b.closest(".pg-row");
        state.form[row.dataset.group].splice(Number(row.dataset.idx), 1);
        renderEditor();
      });
    });
  }

  function addGroup(raw) {
    const name = String(raw || "").trim().replace(/^presets_/i, "");
    if (!/^[a-z0-9_]{1,16}$/i.test(name)) {
      // 没有输入时自动给下一个可用组名
      let n = String.fromCharCode(97 + groupKeys().length);
      const key = "presets_" + n;
      state.form[key] = state.form[key] || [{ name: "" }];
      renderEditor();
      return;
    }
    const key = "presets_" + name.toLowerCase();
    if (state.form[key]) { toast("组已存在: " + key, "warn"); return; }
    state.form[key] = [{ name: "" }];
    renderEditor();
  }

  function setPath(path, value) {
    const parts = path.split(".");
    let obj = state.form;
    for (let i = 0; i < parts.length - 1; i++) {
      obj[parts[i]] = obj[parts[i]] || {};
      obj = obj[parts[i]];
    }
    const last = parts[parts.length - 1];
    if (path === "watch.roots") {
      obj[last] = value.split("\n").map((s) => s.trim()).filter(Boolean);
    } else {
      obj[last] = value;
    }
  }

  async function detectJianying() {
    try {
      const r = await API.envJianying();
      if (!r.candidates || !r.candidates.length) {
        toast("未探测到剪映安装，请手工填写 jianying_dir", "warn");
        return;
      }
      const dir = r.candidates[0].dir;
      const input = document.querySelector("#env-editor [data-path='jianying_dir']");
      if (input) { input.value = dir; setPath("jianying_dir", dir); }
      toast("已填入探测结果（未写盘）: " + dir, "ok");
    } catch (err) { toast(err.message || String(err), "err"); }
  }

  function diffKeys() {
    const old = state.presets.config || {}, now = state.form;
    const keys = new Set([...Object.keys(old), ...Object.keys(now)]);
    const out = [];
    keys.forEach((k) => {
      const a = JSON.stringify(old[k]), b = JSON.stringify(now[k]);
      if (a !== b) out.push(k);
    });
    return out;
  }

  async function saveEditor() {
    const box = $("#env-validate-result");
    box.innerHTML = '<p class="hint">正在校验…</p>';
    try {
      const rep = await API.envValidate({ config: state.form });
      if (rep.errors && rep.errors.length) {
        box.innerHTML = `<div class="notice fail"><b>校验未通过，未写入任何内容：</b><ul>${
          rep.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div>`;
        return;
      }
      box.innerHTML = "";
      const changed = diffKeys();
      const details = [
        "变更的顶层键：" + (changed.length ? changed.join("、") : "（无变化）")
      ].concat((rep.warnings || []).map((w) => "警告（不阻断）：" + w));
      const r = await window.Dialog.confirm({
        title: "确认写入 presets.json",
        subtitle: "将写入真实配置文件 pipeline/presets.json",
        text: "写入前会先备份当前文件（保留最近 10 份），再用临时文件原子替换。",
        details: details,
        confirmText: "确认写入"
      });
      if (!r.ok) return;
      try {
        const res = await API.envSave({ config: state.form });
        toast("已保存 presets.json" + (res.backup ? "，备份: " + res.backup : ""), "ok");
        state.presets = await API.envPresets();
        state.form = JSON.parse(JSON.stringify(state.presets.config || {}));
        state.form0 = JSON.stringify(state.form);
        renderEditor();
        syncDirty();
        runFullCheck();
      } catch (err) {
        box.innerHTML = `<div class="notice fail">${esc(err.message || err)}</div>`;
      }
    } catch (err) {
      box.innerHTML = `<div class="notice fail">${esc(err.message || err)}</div>`;
    }
  }

  // ── 入口 ────────────────────────────────────────────────────────
  // 首屏快速盘点：不等「完整检查」也能看到环境预设/剪映前置/托管三组
  async function loadInventory() {
    if (state.inv || state.checking) return;
    state.checking = true;
    try {
      const inv = await API.envInventory();
      state.inv = { generated_at: inv.generated_at, groups: inv.groups,
                    summary: inv.summary };
      renderGroups();
      const s = inv.summary;
      toast(`环境盘点：通过 ${s.ok}，警告 ${s.warn}，失败 ${s.fail}`,
            s.fail ? "err" : s.warn ? "warn" : "ok");
    } catch (err) {
      // 盘点失败不阻塞页面，用户仍可点「完整检查」
    } finally {
      state.checking = false;
    }
  }

  function show() {
    if (!state.inited) { mount(); state.inited = true; }
    switchTab(state.activeTab);
    renderGroups();
    renderDeps();
    if (!state.form) openEditor(false);
    loadInventory();
    loadAutostart();
    API.envJob().then((j) => {
      state.job = j; renderJob();
      if (j && j.status === "running") pollJob();
    }).catch(() => {});
  }

  window.Env = { show, openService, _state: state, _toast: toast, _esc: esc, _tag: tag, _runFullCheck: runFullCheck };
})();
