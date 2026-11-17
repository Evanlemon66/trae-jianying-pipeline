/* view.js —— 只负责把数据渲染成 HTML；不发请求、不持有业务状态 */
(function () {
  "use strict";

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* 内联 SVG：图标与空态插画，不引 CDN / 图标字体 */
  const ICON = {
    play: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.6v12.8a1 1 0 0 0 1.53.85l9.8-6.4a1 1 0 0 0 0-1.7l-9.8-6.4A1 1 0 0 0 8 5.6z"/></svg>',
    stop: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>',
    terminal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4.5" width="18" height="15" rx="3"/><path d="M7.5 10l2.5 2.5-2.5 2.5M13 15h3.5"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11.5A8 8 0 1 0 17.7 17"/><path d="M20 5v6h-6"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6.5L9.2 17.3 4 12.1"/></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.5l8.5 15.5h-17L12 3.5z"/><path d="M12 9.5v4M12 16.5h.01"/></svg>',
    x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg>',
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l7 3v5.6c0 4.4-2.9 7.9-7 9.4-4.1-1.5-7-5-7-9.4V6l7-3z"/><path d="M9 12l2 2 4-4"/></svg>',
    archive: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8h16v11.5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V8z"/><path d="M3 4h18v4H3z"/><path d="M10 12h4"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>'
  };

  const ART = {
    inbox: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 34l6-18h28l6 18v14a4 4 0 0 1-4 4H16a4 4 0 0 1-4-4V34z"/><path d="M12 34h12l3 6h10l3-6h12"/><path d="M26 24h12"/></svg>',
    shield: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M32 8l18 7v14c0 11-7.4 20-18 24-10.6-4-18-13-18-24V15l18-7z"/><path d="M24 32l6 6 10-12"/></svg>',
    archive: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 21h42v29H11z"/><path d="M9 12h46v9H9z"/><path d="M27 31h10"/></svg>',
    clock: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="32" cy="32" r="20"/><path d="M32 20v13l9 6"/></svg>'
  };

  const STATUS_TEXT = {
    queued: "排队中", running: "运行中", succeeded: "成功",
    failed: "失败", stopped: "已停止", pending: "待执行"
  };
  const MODE_TEXT = { batch: "标准串流", ui: "UI 自动剪辑", export_only: "仅导出" };

  function runTypeTag(r) {
    if (r.mode === "export_only") return `<span class="chip-brand">仅导出</span>`;
    const p = r.params || {};
    const hasArt = !!r.artifact;
    if (!p.export && !hasArt) return `<span class="chip-quiet">仅剪辑</span>`;
    return `<span class="chip-ok">剪辑+导出</span>`;
  }

  function exportTargets(r) {
    // 可「仅导出」的草稿：按账号状态判断——没成功（失败/停止/只克隆完）就给一次补导出的机会，
    // 不能因为同一次运行里 A 号成功，就把 B 号的按钮一起隐藏。
    if (r.mode === "export_only") return [];
    const accts = r.accounts || [];
    if (accts.length > 1) {
      return accts.filter(function (a) {
        return a.draft && a.status !== "exporting" && a.status !== "ok";
      }).map(function (a) { return a.draft; });
    }
    if (!r.draft_name || r.status === "succeeded") return [];
    return [r.draft_name];
  }
  const ACCT_TEXT = {
    pending: "待处理", building: "建稿中", cloning: "克隆中", cloned: "草稿就绪",
    exporting: "导出中", ok: "成功", failed: "失败", stopped: "已停止", cleaned: "已清理"
  };
  const OUTCOME_TEXT = {
    all_ok: "全部成功", partial: "部分成功", all_failed: "全失败", stopped: "已停止"
  };
  const OUTCOME_TONE = { all_ok: "ok", partial: "warn", all_failed: "fail", stopped: "warn" };

  function fmtSec(s) {
    if (s == null) return "";
    const n = Math.round(Number(s) || 0);
    if (n < 60) return n + "s";
    return Math.floor(n / 60) + "m " + (n % 60) + "s";
  }

  function opt(value, label, selected) {
    return `<option value="${esc(value)}"${String(value) === String(selected) ? " selected" : ""}>${esc(label)}</option>`;
  }

  function badge(st) {
    return `<span class="badge ${esc(st)}"><i></i>${esc(STATUS_TEXT[st] || st)}</span>`;
  }

  function empty(art, title, desc, cta) {
    return `<div class="empty"><span class="empty-art">${ART[art] || ART.inbox}</span>
      <b>${esc(title)}</b><span>${esc(desc)}</span>
      ${cta ? `<button class="btn btn-secondary btn-sm" data-goto-page="${esc(cta.page)}">${esc(cta.label)}</button>` : ""}</div>`;
  }

  // 首次使用引导：环境未就绪时在任务页顶部给一条可跳转的提示
  function guideBanner(guide) {
    if (!guide) return "";
    const bits = [];
    if (guide.noPresets) bits.push("presets.json 里还没有预设组（presets_a / presets_b）");
    if (guide.fail) bits.push(guide.fail + " 项环境检查未通过");
    else if (guide.warn) bits.push(guide.warn + " 项环境检查有警告");
    if (!bits.length) return "";
    const tone = (guide.fail || guide.noPresets) ? "fail" : "warn";
    return `<div class="notice ${tone} guide-banner">
      <span>环境提示：${esc(bits.join("；"))}，可能影响任务执行。</span>
      <button class="btn btn-secondary btn-sm notice-action" data-goto-page="env">去环境初始化</button>
    </div>`;
  }

  function skeleton(lines) {
    const widths = [92, 76, 84, 62, 88, 70];
    let out = "";
    for (let i = 0; i < (lines || 5); i++) {
      out += `<div class="sk-line" style="width:${widths[i % widths.length]}%"></div>`;
    }
    return `<section class="card"><div class="card-body"><div class="sk">${out}</div></div></section>`;
  }

  // ── 封面上传 ───────────────────────────────────────────────────
  function coverBox(cov) {
    if (!cov) {
      return `<div class="up-empty">
        <svg viewBox="0 0 48 48" width="34" height="34" fill="none" aria-hidden="true">
          <rect x="5" y="9" width="38" height="30" rx="5" stroke="currentColor" stroke-width="2.2"/>
          <circle cx="17" cy="20" r="3.4" stroke="currentColor" stroke-width="2.2"/>
          <path d="M7.5 34l9.5-9 7 6.5 6-5 10 9" stroke="currentColor" stroke-width="2.2"
                stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <div class="up-title"><b>点击选择</b> 或把图片拖到这里</div>
        <p class="hint">PNG / JPG / WEBP · 单张 ≤20MB · 建议 9:16 竖版、短边 ≥720px</p>
      </div>`;
    }
    const kb = Math.max(1, Math.round((cov.size || 0) / 1024));
    const warn = (cov.warnings || []).length
      ? `<div class="up-warn">${esc(cov.warnings.join("；"))}</div>`
      : `<div class="up-ok">尺寸符合 9:16 · 短边 ≥720px</div>`;
    return `<div class="up-preview">
      <img id="cover-thumb" src="${esc(cov.url || "")}" alt="封面预览">
      <div class="up-meta">
        <div class="up-name">${esc(cov.name || "")}</div>
        <div class="up-dim">${cov.width} × ${cov.height} · ${kb} KB</div>
        ${warn}
        <div class="up-actions">
          <button type="button" class="btn btn-secondary btn-sm" id="cover-clear">清除</button>
        </div>
      </div>
    </div>`;
  }

  // ── 预设组多选（勾选顺序 = 账号顺序）───────────────────────────
  function profilePicks(state) {
    const cfg = state.config || {};
    const presets = (cfg.presets && cfg.presets.profiles) || [];
    const picked = state.form.profiles || [];
    const ui = state.form.mode === "ui";
    if (!presets.length) {
      return `<div class="pick-list" data-ui="${ui ? "1" : "0"}">
        <p class="hint" style="margin:0">presets.json 未配置预设组（presets_a / presets_b）。</p>
      </div>`;
    }
    const rows = presets.map(function (p) {
      const idx = picked.indexOf(p);
      const on = idx >= 0;
      const locked = ui && !on && picked.length >= 1;
      return `<label class="pick${on ? " on" : ""}${locked ? " locked" : ""}"${locked
        ? ' title="UI 自动剪辑一次只能用一个预设组"' : ""}>
        <input type="checkbox" data-preset="${esc(p)}"${on ? " checked" : ""}${locked ? " disabled" : ""}>
        <span class="pick-name">${esc(p)}</span>
        <span class="pick-order">${on ? idx + 1 : ""}</span>
      </label>`;
    }).join("");
    return `<div class="pick-list" data-ui="${ui ? "1" : "0"}">${rows}</div>`;
  }

  function profileHint(state) {
    const list = state.form.profiles || [];
    if (state.form.mode === "ui") {
      return "UI 自动剪辑链路底层只能生成一个草稿，预设组固定单选；多账号请切回「标准串流」。";
    }
    if (list.length > 1) {
      return `已选 ${list.length} 组（顺序即账号顺序）：第一组正常建稿，其余复制草稿只换预设层，串行导出 ${list.length} 份成品。`;
    }
    if (list.length === 1) return "单预设：只生成 1 份草稿与成品，与改动前完全一致。";
    return "未选择 = 跟随 presets.json 的 default_preset_profile。";
  }

  // ── 任务页 ─────────────────────────────────────────────────────
  function runForm(state) {
    const f = state.form;
    const seriesOpts = (state.series || []).map(function (s) {
      return opt(s.path, `${s.name}（${s.episodes} 集，第${s.first_ep}–${s.last_ep}集）`, f.series_dir);
    }).join("");

    return `
    <div class="page-head">
      <div>
        <h1>提交任务</h1>
        <p>单队列串行执行 · 同一时间只跑一个任务，Console 不接管 watcher</p>
      </div>
      <div class="head-actions">
        <span class="chip chip-quiet">P0</span>
      </div>
    </div>

    <div id="run-guide">${guideBanner(state.guide)}</div>

    <div class="env-tabs" id="run-tabs" role="tablist" aria-label="任务类型">
      <button class="env-tab${state.runTab === "export" ? "" : " active"}" data-run-tab="edit"
              role="tab" aria-selected="${state.runTab === "export" ? "false" : "true"}">剪辑任务</button>
      <button class="env-tab${state.runTab === "export" ? " active" : ""}" data-run-tab="export"
              role="tab" aria-selected="${state.runTab === "export" ? "true" : "false"}">导出任务</button>
    </div>

    <div id="run-pane-edit"${state.runTab === "export" ? " hidden" : ""}>
    <div class="run-grid">
      <div class="stack">
        <section class="card">
          <div class="card-head"><h2>任务来源</h2>
            <span class="card-sub">执行链路 / 剧目录 / 选集</span></div>
          <div class="card-body">
            <div class="form-section">
              <h3 class="sec-title">执行链路</h3>
              <div class="choice-grid">
                <label class="choice">
                  <input type="radio" name="mode" value="batch" ${f.mode !== "ui" ? "checked" : ""}>
                  <span class="choice-text"><b>标准串流</b>
                    <span>pipeline/batch_runner.py · 与「快速上手」一致</span></span>
                </label>
                <label class="choice">
                  <input type="radio" name="mode" value="ui" ${f.mode === "ui" ? "checked" : ""}>
                  <span class="choice-text"><b>UI 自动剪辑</b>
                    <span>pipeline_ui/ui_pipeline.py · 走剪映界面自动化</span></span>
                </label>
              </div>
              <p class="hint" id="mode-hint">${f.mode === "ui"
                ? "当前走 UI 自动化链路：分辨率 / 人声模式 / 转码仅此模式有效。"
                : "当前走标准串流链路（pipeline/batch_runner.py），与「快速上手」一致。"}</p>
            </div>

            <div class="form-section">
              <h3 class="sec-title">剧集与选集</h3>
              <div class="field">
                <div class="field-head">
                  <label>剧集目录 <em class="req">*</em></label>
                  <button type="button" class="btn btn-secondary btn-sm" id="btn-series-refresh"
                          title="重新扫描素材根目录，获取当前目录下的最新剧集">刷新剧集</button>
                </div>
                <select data-field="series_dir">
                  <option value="">（选择素材根下的剧）</option>
                  ${seriesOpts}
                </select>
                <input type="text" data-field="series_manual" style="margin-top:8px"
                       placeholder="或直接填绝对路径（须在白名单内）"
                       value="${esc(f.series_manual || "")}">
                <div class="notice info">手动填写会覆盖下拉选择；路径必须位于白名单根目录内。</div>
              </div>

              <div class="field">
                <label>选集范围</label>
                <div class="seg">
                  <label><input type="radio" name="sel-mode" value="range" ${f.sel_mode === "range" ? "checked" : ""}>集数区间</label>
                  <label><input type="radio" name="sel-mode" value="first" ${f.sel_mode === "first" ? "checked" : ""}>只要前 N 集</label>
                  <label><input type="radio" name="sel-mode" value="all" ${f.sel_mode === "all" ? "checked" : ""}>全集</label>
                </div>
                <div class="row ${f.sel_mode === "range" ? "" : "hidden"}" data-sel="range">
                  <input type="number" min="1" data-field="from" value="${esc(f.from)}" aria-label="起始集">
                  <span class="sep">—</span>
                  <input type="number" min="1" data-field="to" value="${esc(f.to)}" aria-label="结束集">
                  <span class="hint" style="margin:0">集</span>
                </div>
                <div class="row ${f.sel_mode === "first" ? "" : "hidden"}" data-sel="first">
                  <input type="number" min="1" data-field="n" value="${esc(f.n)}" aria-label="前 N 集">
                  <span class="hint" style="margin:0">集</span>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section class="card">
          <div class="card-head"><h2>剪辑参数</h2>
            <span class="card-sub">裁剪 / 封面 / 预设</span></div>
          <div class="card-body">
            <div class="field">
              <label for="f-cut">裁剪开头 <span class="unit">--cut</span></label>
              <div class="num-row">
                <input id="f-cut" type="number" min="0" max="3600" step="0.1"
                       inputmode="decimal" data-field="cut" value="${esc(f.cut)}">
                <span class="unit">秒</span>
                <span class="chip" id="cut-note">0 = 不裁剪（不传 --cut）</span>
              </div>
              <div class="notice info">整条串流时间线开头裁掉 N 秒，<b>不是每集各裁</b>；
                支持 0.1 / 2.5 这类小数，范围 0–3600。</div>
            </div>

            <div class="field">
              <label>封面图 <span class="unit">--cover-image（可选）</span></label>
              <div class="uploader" id="cover-drop" tabindex="0" role="button"
                   aria-label="上传封面图">
                ${coverBox(state.coverInfo)}
              </div>
              <input type="file" id="cover-file"
                     accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp" hidden>
              <p class="hint">上传后原图直接写入草稿封面，<b>不裁剪、不缩放</b>；
                未上传将使用剧目录中的封面（封面.png / 04_covers）。</p>
            </div>

            <div class="field">
              <label>预设组 <span class="unit">--profile · 可多选，按勾选顺序</span></label>
              <div data-profile-picks>${profilePicks(state)}</div>
              <p class="hint" id="prof-hint">${esc(profileHint(state))}</p>
              <div class="field" id="f-cover-fallback-field"${(f.mode === "ui" || f.profiles.length < 2) ? " hidden" : ""}>
                <label class="switch-row">
                  <input type="checkbox" data-field="coverFallback" ${f.coverFallback ? "checked" : ""}>
                  <span>缺少预设组封面时，复用源草稿封面</span>
                </label>
                <p class="hint">仅在该预设组没有配置竖版封面时生效；有封面时始终优先组封面。
                  关闭 = 从源草稿主轨视频抽帧兜底，抽不到则中止；打开 = 直接用源草稿（默认版）的封面。</p>
              </div>
            </div>

            <div class="field" data-batch-only>
              <label class="switch-row">
                <input type="checkbox" data-field="export" ${f.export ? "checked" : ""}>
                <span>剪辑后自动导出（UI 模式固定导出）</span>
              </label>
            </div>

            <div class="form-section ${f.mode === "ui" ? "" : "hidden"}" data-ui-only>
              <h3 class="sec-title">UI 模式专属</h3>
              <div class="inline-2">
                <div class="field">
                  <label>导出分辨率</label>
                  <select data-field="resolution">
                    ${opt("", "沿用剪映当前值", f.resolution)}
                    ${opt("480P", "480P", f.resolution)}
                    ${opt("720P", "720P", f.resolution)}
                    ${opt("1080P", "1080P", f.resolution)}
                    ${opt("2K", "2K", f.resolution)}
                    ${opt("4K", "4K", f.resolution)}
                  </select>
                </div>
                <div class="field">
                  <label>人声模式</label>
                  <select data-field="vocal">
                    ${opt("none", "不处理（none）", f.vocal)}
                    ${opt("vocal", "仅保留人声（vocal）", f.vocal)}
                    ${opt("background", "仅保留背景声（background）", f.vocal)}
                  </select>
                </div>
              </div>
              <div class="field">
                <label class="switch-row">
                  <input type="checkbox" data-field="normalize" ${f.normalize ? "checked" : ""}>
                  <span>转码 HEVC → H.264（--normalize）</span>
                </label>
                <div class="notice warn">副作用：首次建稿从秒级变成约 85 分钟。</div>
              </div>
            </div>
          </div>
        </section>

        <section class="card">
          <details class="acc">
            <summary>
              <h2>高级参数</h2>
              <span class="acc-hint">默认不改 · 涉及台账与兼容性</span>
            </summary>
            <div class="card-body">
              <div class="inline-2">
                <div class="field">
                  <label>单集重试 <span class="unit">--retries</span></label>
                  <input type="number" min="0" max="5" data-field="retries" value="${esc(f.retries)}">
                </div>
                <div class="field">
                  <label>导出超时（分钟） <span class="unit">--export-timeout</span></label>
                  <input type="number" min="1" max="600" data-field="timeout" value="${esc(f.timeout)}">
                </div>
              </div>
              <div class="field">
                <label>草稿名前缀 <span class="unit">--draft-prefix</span></label>
                <input type="text" data-field="prefix" value="${esc(f.prefix)}" placeholder="默认取剧目录名">
                <div class="notice warn">改了等于换清单键，历史台账会对不上。</div>
              </div>
              <div class="field">
                <label>每集间隔秒 <span class="unit">--delay</span></label>
                <input type="number" min="0" step="0.5" data-field="delay" value="${esc(f.delay)}" placeholder="默认 5">
              </div>
            </div>
          </details>
        </section>

        <div id="preview-area"></div>

        <div class="action-dock">
          <button class="btn btn-primary btn-lg" id="btn-start">${ICON.play}<span>开始任务</span></button>
          <button class="btn btn-secondary" id="btn-preview">${ICON.terminal}<span>查看命令行</span></button>
          <button class="btn btn-danger-ghost" id="btn-stop"
            ${state.run && state.run.status === "running" ? "" : "disabled"}>${ICON.stop}<span>停止</span></button>
          <span class="dock-note">固定追加 --no-watch · 不抢占全局导出锁</span>
        </div>
      </div>

      <div class="stack run-aside">
        <div class="stack" id="status-area" aria-live="polite"></div>
        <div id="external-area"></div>
      </div>
    </div>
    </div>

    <div id="run-pane-export"${state.runTab === "export" ? "" : " hidden"}>
      <div class="run-grid">
        <div class="stack">
          <section class="card">
            <div class="card-head"><h2>导出任务</h2>
              <span class="card-sub">单剧或批量导出已有草稿；开启多预设后，每个草稿 × 每个预设各生成一份副本再导出</span></div>
            <div class="card-body">
              <div class="form-section">
                <h3 class="sec-title">导出模式</h3>
                <div class="choice-grid">
                  <label class="choice"><input type="radio" name="ex-mode" value="single"
                    ${state.exMode === "batch" ? "" : "checked"}>
                    <span><b>单剧导出</b><small>一次只导出 1 个草稿</small></span></label>
                  <label class="choice"><input type="radio" name="ex-mode" value="batch"
                    ${state.exMode === "batch" ? "checked" : ""}>
                    <span><b>批量导出</b><small>一次任务顺序导出多个草稿</small></span></label>
                </div>
              </div>

              <div class="form-section">
                <h3 class="sec-title">选择草稿</h3>
                <div class="field">
                  <label>搜索</label>
                  <input id="ex-search" placeholder="输入草稿名关键词过滤">
                </div>
                <div id="ex-draft-list" class="pick-list"><p class="hint">加载中…</p></div>
                <p class="hint" id="ex-pick-hint" hidden></p>
              </div>

              <div class="form-section">
                <h3 class="sec-title">换预设</h3>
                <label class="switch-row">
                  <input type="checkbox" id="ex-multi-preset" ${state.exMultiPreset ? "checked" : ""}>
                  <span>多预设导出（默认预设 1 份 + 勾选的预设各 1 份）</span>
                </label>
                <div id="ex-profile-sec"${state.exMultiPreset ? "" : " hidden"}>
                  <div id="ex-profile-list" class="pick-list"><p class="hint">加载中…</p></div>
                  <p class="hint">每个草稿先按原样（默认预设）导 1 份，再把勾选的预设各克隆成「原名_预设名」导 1 份；固定使用 presets.json 的导出根。标记「默认」的预设组不可选——草稿本身就是该预设，重复换只会得到相同副本。</p>
                  <label class="switch-row">
                    <input type="checkbox" id="ex-cover-fallback" ${state.exCoverFallback ? "checked" : ""}>
                    <span>缺少预设组封面时，复用源草稿封面</span>
                  </label>
                  <p class="hint">仅在该预设组没有配置竖版封面时生效；有组封面时始终优先组封面。关闭 = 从源草稿主轨视频抽帧兜底，抽不到则中止；打开 = 直接用源草稿（默认版）的封面。</p>
                </div>
              </div>

              <div class="form-section">
                <h3 class="sec-title">输出</h3>
                <div class="field">
                  <label>导出目录</label>
                  <input id="ex-dir" placeholder="留空 = presets.json 的 export_dir"
                    ${state.exMultiPreset ? "disabled" : ""}>
                  <p class="hint" id="ex-dir-hint">${state.exMultiPreset
                    ? "多预设导出会克隆新草稿，固定使用 presets.json 的导出根，此输入已禁用。"
                    : ""}</p>
                </div>
                <div class="field">
                  <label>单条超时（分钟）</label>
                  <input id="ex-timeout" type="number" min="5" value="60">
                </div>
              </div>
            </div>
          </section>

          <div class="action-dock">
            <button class="btn btn-primary btn-lg" id="btn-export-start">开始导出</button>
            <button class="btn btn-secondary" id="btn-export-reload">刷新草稿</button>
            <button class="btn btn-danger-ghost" id="btn-export-stop"
              ${state.run && state.run.status === "running" ? "" : "disabled"}>停止</button>
            <span class="dock-note">与剪辑任务共用单队列</span>
          </div>
        </div>

        <div class="stack run-aside">
          <div class="stack" id="status-area-export" aria-live="polite"></div>
        </div>
      </div>
    </div>`;
  }

  function timeline(run, stages) {
    if (!stages || !stages.length) return "";
    const finished = run && run.status === "succeeded";
    const cur = run && run.stage;
    return `<ol class="timeline">` + stages.map(function (s) {
      let st = "pending";
      if (finished) {
        st = "done";
      } else if (cur) {
        if (s.key === cur.key) st = "current";
        else if (run.percent != null && s.percent < run.percent) st = "done";
      }
      const hint = st === "done" ? "已完成" : st === "current" ? "进行中" : "未开始";
      const dot = st === "done" ? `<span class="tl-dot">${ICON.check}</span>`
                                : `<span class="tl-dot"></span>`;
      return `<li class="tl ${st}">${dot}<span class="tl-label">${esc(s.label)}</span>
        <span class="tl-hint">${hint}</span></li>`;
    }).join("") + `</ol>`;
  }

  // ── 多账号成品清单 ─────────────────────────────────────────────
  function accountList(run) {
    const accs = run.accounts || [];
    if (accs.length < 2) return "";
    const done = accs.filter(function (a) {
      return a.status === "ok" || a.status === "cloned";
    }).length;
    const rows = accs.map(function (a, i) {
      const st = a.status || "pending";
      const cur = run.status === "running" && run.account_index === i + 1;
      const tone = st === "ok" || st === "cloned" ? "ok"
        : st === "failed" ? "fail"
        : st === "stopped" ? "warn" : "quiet";
      return `<div class="acct${cur ? " cur" : ""}">
        <span class="acct-no">${i + 1}</span>
        <div class="acct-body">
          <div class="acct-top">
            <b>${esc(a.profile)}</b>
            <span class="tag ${tone}">${esc(ACCT_TEXT[st] || st)}</span>
            ${a.export_s != null ? `<span class="acct-time">${esc(fmtSec(a.export_s))}</span>` : ""}
          </div>
          <span class="acct-draft mono">${esc(a.draft || "—")}</span>
          ${a.artifact ? `<span class="acct-art mono" title="${esc(a.artifact)}">${esc(a.artifact)}</span>` : ""}
          ${a.reason ? `<span class="acct-reason">${esc(a.reason)}</span>` : ""}
        </div>
      </div>`;
    }).join("");
    const out = run.outcome
      ? `<span class="tag ${OUTCOME_TONE[run.outcome] || "quiet"}">${esc(OUTCOME_TEXT[run.outcome] || run.outcome)}</span>`
      : "";
    return `<div class="acct-wrap">
      <div class="acct-head"><b>账号成品</b>${out}
        <span class="acct-sum">${done}/${accs.length} 完成</span></div>
      <div class="acct-list">${rows}</div>
      ${(run.cleaned_drafts || []).length
        ? `<p class="hint">已清理半成品草稿：<span class="mono">${esc(run.cleaned_drafts.join("、"))}</span></p>`
        : ""}
    </div>`;
  }

  // ── 仅导出：逐草稿串行进度（没有剪辑阶段） ─────────────────────
  const DRAFT_STATUS_TEXT = {
    pending: "待导出", exporting: "导出中", ok: "完成",
    failed: "失败", stopped: "已停止"
  };
  const DRAFT_TONE = { pending: "quiet", exporting: "brand", ok: "ok",
                       failed: "fail", stopped: "warn" };

  // 导出体积：>=1GB 用 GB，其余 MB；null 返回空串
  function sizeText(mb) {
    if (mb == null) return "";
    return mb >= 1024 ? (mb / 1024).toFixed(2) + " GB" : Math.round(mb) + " MB";
  }
  // 已用时长：run.started_at / ended_at 都是 "YYYY-MM-DDTHH:MM:SS"（本地时间）
  function elapsedText(start, end) {
    if (!start) return "";
    const t0 = Date.parse(start);
    const t1 = end ? Date.parse(end) : Date.now();
    if (!isFinite(t0) || !isFinite(t1) || t1 <= t0) return "";
    return fmtSec((t1 - t0) / 1000);
  }

  // 时间戳统一成 "YYYY-MM-DD HH:MM:SS"（后端用 ISO 的 T 分隔）
  function fmtTs(s) {
    return String(s == null ? "" : s).replace("T", " ");
  }

  function exportOnlyCard(run, queue) {
    const st = run.status || "queued";
    let drafts = run.drafts && run.drafts.length ? run.drafts : null;
    if (!drafts) {
      // 旧记录没有 drafts 字段：降级成单行，状态取 run 状态
      drafts = [{ draft: run.draft_name || "—",
                  status: st === "succeeded" ? "ok"
                    : st === "running" ? "exporting"
                    : st === "failed" ? "failed" : st === "stopped" ? "stopped" : "pending",
                  artifact: run.artifact }];
    }
    const total = drafts.length;
    const done = drafts.filter(function (d) { return d.status === "ok"; }).length;
    const failed = drafts.filter(function (d) { return d.status === "failed"; }).length;
    const cur = drafts.filter(function (d) { return d.status === "exporting"; })[0];
    const curPct = cur && cur.percent_value != null ? Math.round(cur.percent_value) : null;
    // 没有预期总体积（导出侧「无估算」）时 pct 保持 null，前端走不定长动画，
    // 只用体积/速率体现实时推进，绝不写死 0%。
    let pct = null;
    if (st === "succeeded") pct = 100;
    else if (total === 1) pct = curPct;
    else if (done > 0 || curPct != null) pct = Math.round(run.percent || 0);
    pct = pct == null ? null : Math.max(0, Math.min(100, pct));

    const e = run.export || {};
    const exported = e.exported_mb != null ? e.exported_mb
      : (cur && cur.exported_mb != null ? cur.exported_mb : null);
    const totalMB = e.total_mb != null ? e.total_mb : (cur ? cur.total_mb : null);
    const rate = e.rate_mbps != null ? e.rate_mbps : (cur ? cur.rate_mbps : null);
    const used = elapsedText(run.started_at, run.ended_at);
    const alive = st === "running" || st === "queued";

    const title = (cur && cur.draft)
      || (drafts.filter(function (d) { return d.artifact; })[0] || drafts[0] || {}).draft
      || run.draft_name || "导出任务";
    const descBits = [];
    if (st === "queued") descBits.push("等待开始");
    else if (st === "running") descBits.push(cur ? "正在导出" : "准备中");
    if (exported != null && st !== "queued") {
      descBits.push("已导出 " + sizeText(exported)
        + (totalMB != null ? " / " + sizeText(totalMB) : ""));
    }
    if (rate) descBits.push(rate + " MB/s");
    if (used) descBits.push("已用 " + used);
    if (st === "running" && run.eta_s != null) {
      descBits.push("预计剩余 " + (run.eta_s < 60 ? "不到 1 分钟"
        : "约 " + Math.round(run.eta_s / 60) + " 分钟"));
    }

    const ring = pct != null
      ? `<div class="ring" style="--pct:${pct}%">
           <div class="ring-label"><b>${pct}%</b><span>导出进度</span></div>
         </div>`
      : `<div class="ring indet">
           <div class="ring-label"><b class="sm">${st === "queued" ? "待开始"
             : (sizeText(exported) || "编码中")}</b><span>${st === "queued" || !exported
             ? "" : "已导出"}</span></div>
         </div>`;

    // 单稿时进度条放主区，避免下面再重复一行草稿
    const heroBar = total === 1 && alive
      ? (pct != null
          ? `<div class="hero-bar"><i style="width:${pct}%"></i></div>`
          : `<div class="hero-bar indet"><i></i></div>`)
      : "";

    const chips = [];
    if (total > 1) {
      chips.push((done + "/" + total + " 完成") + (failed ? " · 失败 " + failed : ""));
    }
    if (alive && queue > 0) chips.push("队列 " + queue);
    const chipRow = chips.length
      ? `<div class="hero-meta">` + chips.map(function (c) {
          return `<span class="chip">${esc(c)}</span>`;
        }).join("") + `</div>`
      : "";

    const rows = total > 1 ? drafts.map(function (d, i) {
      const on = d.status === "exporting";
      const p = on && d.percent_value != null ? Math.round(d.percent_value) : null;
      const meta = [];
      if (on && d.exported_mb != null) meta.push("已导出 " + sizeText(d.exported_mb));
      if (on && d.rate_mbps) meta.push(d.rate_mbps + " MB/s");
      if (on && d.eta_s != null) meta.push("约剩 " + fmtSec(d.eta_s));
      if (d.seconds != null && d.status === "ok") meta.push("耗时 " + fmtSec(d.seconds));
      return `<div class="draft-row${on ? " cur" : ""}">
        <span class="acct-no">${i + 1}</span>
        <div class="draft-body">
          <div class="draft-top">
            <span class="tag ${DRAFT_TONE[d.status] || "quiet"}">${esc(DRAFT_STATUS_TEXT[d.status] || d.status)}</span>
            <span class="draft-name">${esc(d.draft)}</span>
            ${p != null ? `<span class="draft-pct">${p}%</span>` : ""}
          </div>
          ${on ? (p != null
            ? `<div class="draft-bar"><i style="width:${p}%"></i></div>`
            : `<div class="draft-bar indet"><i></i></div>`) : ""}
          ${meta.length ? `<div class="draft-meta">${esc(meta.join(" · "))}</div>` : ""}
          ${d.artifact
            ? `<div class="acct-art mono" title="${esc(d.artifact)}">${esc(d.artifact)}</div>`
            : ""}
        </div>
      </div>`;
    }).join("") : "";

    function kv(k, v, mono, span2) {
      return `<div class="kv-item${span2 ? " span-2" : ""}">
        <span class="k">${esc(k)}</span>
        <span class="v${mono ? " mono" : ""}">${v}</span></div>`;
    }
    const kvRows = [];
    const exportDir = (run.params && run.params.export_dir) || "";
    if (exportDir) kvRows.push(kv("导出目录", esc(exportDir), true, true));
    const artifact = (cur && cur.artifact)
      || (drafts.filter(function (d) { return d.artifact; })[0] || {}).artifact;
    if (artifact) kvRows.push(kv("成品", esc(artifact), true, true));
    if (run.error_class) kvRows.push(kv("错误分类", `<b class="err">${esc(run.error_class)}</b>`));
    if (run.error_line) kvRows.push(kv("错误输出", esc(run.error_line), true, true));
    if (run.started_at) kvRows.push(kv("开始", esc(fmtTs(run.started_at))));
    if (run.ended_at) kvRows.push(kv("结束", esc(fmtTs(run.ended_at))));

    // 实时日志：后端给的是导出流程最后几条节点（log_lines），旧记录降级到单行 last_line
    const logLines = run.log_lines && run.log_lines.length
      ? run.log_lines : (run.last_line ? [run.last_line] : []);
    const logText = logLines.length ? logLines.join("\n") : "（暂无输出）";

    return `<section class="card">
      <div class="card-head">
        <div><h2>仅导出</h2><p class="card-sub">串行导出 · 进度实时同步</p></div>
        ${badge(st)}
      </div>
      <div class="card-body">
        <div class="hero">
          ${ring}
          <div class="hero-info">
            <div class="hero-title">${esc(title)}</div>
            <div class="hero-desc">${esc(descBits.join(" · "))}</div>
            ${heroBar}
            ${chipRow}
          </div>
        </div>
        ${rows ? `<div class="draft-list">${rows}</div>` : ""}
        <div class="kv" style="margin-top:16px">${kvRows.join("")}</div>
        <details class="log-fold">
          <summary>实时日志 <span class="hint">点开查看最新输出</span></summary>
          <pre class="light">${esc(logText)}</pre>
        </details>
        ${(st === "failed" || st === "stopped") && run.id
          ? `<div class="action-dock" style="margin-top:12px">
              <button class="btn btn-primary btn-sm" id="btn-rerun-current"
                data-run="${esc(run.id)}">${ICON.refresh}<span>重新执行</span></button>
              <span class="dock-note">按本次参数重新提交</span>
            </div>`
          : ""}
      </div>
    </section>`;
  }

  function statusCard(run, queue, stages) {
    const head = `<div class="card-head">
      <div><h2>实时状态</h2><p class="card-sub">SSE 推送 · 1s 采样 · 15s 心跳</p></div>
      ${run ? badge(run.status || "queued") : ""}
    </div>`;

    if (!run) {
      return `<section class="card">${head}<div class="card-body">
        ${empty("inbox", "暂无运行任务", "提交后会在这里显示当前剧、阶段进度、导出指标与错误分类。")}
      </div></section>`;
    }
    if (run.mode === "export_only") return exportOnlyCard(run, queue);

    const st = run.status || "queued";
    const hasPct = run.percent != null;
    const pct = hasPct ? Math.max(0, Math.min(100, run.percent)) : 0;
    const stageLabel = run.stage_label || (run.stage ? run.stage.label : "等待阶段输出");
    const etaLine = st === "running"
      ? `<div class="hero-sub">预计剩余 ${run.eta_s == null ? "估算中"
          : (run.eta_s < 60 ? "&lt;1 分钟" : "约 " + Math.round(run.eta_s / 60) + " 分钟")}</div>`
      : "";

    function kv(k, v, mono, span2) {
      return `<div class="kv-item${span2 ? " span-2" : ""}">
        <span class="k">${esc(k)}</span>
        <span class="v${mono ? " mono" : ""}">${v}</span></div>`;
    }

    const rows = [];
    rows.push(kv("剧目录", esc(run.series_dir), true, true));
    rows.push(kv("草稿", run.draft_name ? esc(run.draft_name) : "—", true));
    rows.push(kv("链路", esc(MODE_TEXT[run.mode] || run.mode)));
    rows.push(kv("队列", String(queue || 0)));
    if (run.error_class) rows.push(kv("错误分类", `<b class="err">${esc(run.error_class)}</b>`));
    if (run.error_line) rows.push(kv("错误输出", esc(run.error_line), true, true));
    if (run.artifact) rows.push(kv("产物", esc(run.artifact), true, true));
    if (run.exit_code != null) {
      rows.push(kv("执行结果", run.exit_code === 0 ? "成功"
        : `<b class="err">失败（退出码 ${esc(run.exit_code)}）</b>`));
    }
    if (run.started_at) rows.push(kv("开始", esc(fmtTs(run.started_at))));
    if (run.ended_at) rows.push(kv("结束", esc(fmtTs(run.ended_at))));

    let chips = "";
    if (run.export) {
      const e = run.export, bits = [];
      if (e.status) bits.push("导出 " + e.status);
      if (e.percent_value != null) bits.push("导出 " + e.percent_value + "%");
      if (e.rate_mbps) bits.push(e.rate_mbps + " MB/s");
      if (e.eta_s != null) bits.push("约剩 " + Math.max(1, Math.round(e.eta_s / 60)) + " 分钟");
      if (e.exported_mb != null && e.total_mb != null) bits.push(e.exported_mb + "/" + e.total_mb + " MB");
      if (bits.length) {
        chips = `<div class="chip-row">` +
          bits.map(function (b) { return `<span class="chip">${esc(b)}</span>`; }).join("") +
          `</div>`;
      }
    }

    let jobLine = "";
    if (run.job) {
      const j = run.job;
      jobLine = `<p class="hint">台账 job.json：stage=${esc(j.stage || "-")} ·
        revision=${esc(j.revision || "-")} · video.status=${esc(j.video_status || "-")} ·
        export_state=${esc(j.export_state || "-")}</p>`;
    }

    return `<section class="card">${head}
      <div class="card-body">
        <div class="hero">
          <div class="ring" style="--pct:${pct}%">
            <div class="ring-label">
              <b>${pct}%</b>
              <span>${hasPct ? "阶段权重" : "初始化中"}</span>
            </div>
          </div>
          <div class="hero-info">
            <div class="hero-title">${esc(stageLabel)}</div>
            <div class="hero-sub mono">${esc((run.series_dir || "").split(/[\\/]/).filter(Boolean).pop() || "")}</div>
            ${etaLine}
            <div class="hero-meta">
              <span class="chip chip-brand">${esc(MODE_TEXT[run.mode] || run.mode)}</span>
              <span class="chip">队列 ${queue || 0}</span>
              ${(run.accounts || []).length > 1
                ? `<span class="chip chip-warn">账号 ${run.account_index || 1}/${run.account_total}</span>`
                : ""}
              <span class="chip">${esc(run.progress_basis || "阶段权重")}</span>
            </div>
          </div>
        </div>
        ${timeline(run, stages)}
        ${accountList(run)}
        <div class="kv" style="margin-top:16px">${rows.join("")}</div>
        ${chips}
        ${jobLine}
        <div class="log-box">
          <div class="log-head">最新输出</div>
          <pre class="light">${esc(run.last_line || "（暂无输出）")}</pre>
        </div>
        ${(st === "failed" || st === "stopped") && run.id
          ? `<div class="action-dock" style="margin-top:12px">
              <button class="btn btn-primary btn-sm" id="btn-rerun-current"
                data-run="${esc(run.id)}">${ICON.refresh}<span>重新执行</span></button>
              <span class="dock-note">按本次参数重新提交（同一剧目录 / 选集 / 预设）</span>
            </div>`
          : ""}
      </div>
    </section>`;
  }

  function previewBlock(res) {
    const profs = res.profiles || [];
    return `<section class="card">
      <div class="card-head">
        <div><h2>命令行预览</h2><p class="card-sub">仅预览，不会执行</p></div>
        <span class="chip chip-brand">${esc(MODE_TEXT[res.mode] || res.mode)}</span>
      </div>
      <div class="card-body">
        <pre>${esc(res.cmd)}</pre>
        ${res.draft_name ? `<p class="hint">草稿名：<span class="mono">${esc(res.draft_name)}</span></p>` : ""}
        ${profs.length > 1
          ? `<p class="hint">多账号顺序：${esc(profs.join(" → "))}（第一组正常建稿，其余复制草稿只换预设层，串行导出 ${profs.length} 份成品）</p>`
          : ""}
      </div>
    </section>`;
  }

  // ── 归档清理页 ─────────────────────────────────────────────────
  function archive(state) {
    const all = state.manifests || [];
    const q = String(state.archQuery || "").trim().toLowerCase();
    const list = q
      ? all.filter(function (m) { return String(m.draft || "").toLowerCase().indexOf(q) >= 0; })
      : all;
    const opts = list.map(function (m) {
      const st = Object.keys(m.versions || {}).map(function (k) {
        return k + "=" + ((m.versions[k] || {}).state || "-");
      }).join(",");
      return `<option value="${esc(m.draft)}"${m.draft === state.archiveDraft ? " selected" : ""}>
        ${esc(m.draft)}${st ? "（" + esc(st) + "）" : ""}</option>`;
    }).join("");

    return `
    <div class="page-head">
      <div><h1>归档 / 清理</h1><p>先出计划，确认后才执行；源剧集目录始终原地保留</p></div>
      <div class="head-actions">
        <button class="btn btn-secondary btn-sm" id="btn-reload-manifests">${ICON.refresh}<span>刷新清单</span></button>
      </div>
    </div>
    <div class="grid-2">
      <section class="card">
        <div class="card-head"><h2>生成计划</h2><span class="card-sub">dry-run</span></div>
        <div class="card-body">
          ${all.length ? "" : `<div class="notice warn">清单目录为空：pipeline/_stream_manifests 下没有记录。
            <button class="btn btn-secondary btn-sm notice-action" data-goto-page="run">去提交任务</button></div>`}
          ${all.length ? `<div class="filter-bar">
            <input type="search" id="f-arc-q" placeholder="筛选草稿名"
                   value="${esc(state.archQuery || "")}" aria-label="筛选草稿">
            <span class="hint">共 ${all.length} 个 · 显示 ${list.length} 个</span>
          </div>` : ""}
          <div class="field">
            <label>选择草稿 <em class="req">*</em></label>
            <select id="f-archive-draft">${opts || `<option value="">（无记录）</option>`}</select>
          </div>
          <div class="field">
            <label>动作</label>
            <div class="seg">
              <label><input type="radio" name="arc-action" value="archive" checked>归档</label>
              <label><input type="radio" name="arc-action" value="clean">清理</label>
            </div>
            <p class="hint">归档＝把成品 bundle 搬到归档根；清理＝删除成品与中间产物。</p>
            <div class="notice info">先出 dry-run 计划，确认后才执行；保留项会明确标注「仍被草稿引用」。</div>
          </div>
          <div class="field">
            <label class="switch-row">
              <input type="checkbox" id="f-arc-inter">
              <span>只清中间产物（--intermediates-only，仅清理动作）</span>
            </label>
          </div>
          <div class="actions">
            <button class="btn btn-primary" id="btn-plan"${list.length ? "" : " disabled"}>${ICON.archive}<span>生成计划（dry-run）</span></button>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-head"><h2>执行计划</h2><span class="card-sub">删什么 / 搬什么 / 保留什么</span></div>
        <div class="card-body">
          <div id="plan-area">
            ${empty("archive", "还没有计划", "选择草稿与动作后点「生成计划」，这里会按待删除、待搬运、保留分组列出。")}
          </div>
        </div>
      </section>
    </div>`;
  }

  function planBlock(res, awaiting) {
    if (res.error) return `<div class="errbox">${esc(res.error)}</div>`;
    const p = res.plan;

    function group(title, arr, cls) {
      if (!arr || !arr.length) return "";
      return `<div class="plan-group"><h4>${esc(title)}（${arr.length}）</h4>` +
        arr.map(function (it) {
          return `<div class="plan-item ${cls || ""}">
            <span><b>${esc(it.label)}</b>：${esc(it.detail)}</span>
            ${it.size ? `<span class="sz">${esc(it.size)}</span>` : ""}
          </div>`;
        }).join("") + `</div>`;
    }

    return `
      <div class="notice info">这是 dry-run 计划，磁盘未发生任何变化；确认后才会执行。</div>
      <p class="hint mono" style="word-break:break-all">${esc(res.cmd)}</p>
      ${group(res.action === "archive" ? "待搬运" : "待删除", p.delete.concat(p.move),
              res.action === "archive" ? "move" : "delete")}
      ${group("保留（仍被草稿引用 / 受保护）", p.keep, "keep")}
      ${group("阻断 / 冲突 / 错误", p.blocked, "delete")}
      ${group("跳过 / 告警", p.skip)}
      ${p.notes && p.notes.length ? `<p class="hint">${esc(p.notes.join("；"))}</p>` : ""}
      ${p.total ? `<p class="hint"><b>${esc(p.total)}</b></p>` : ""}
      ${awaiting ? `<div class="notice danger">
        <b>此操作会真实移动 / 删除文件，且不可撤销。</b>再次点击下方按钮立即执行。</div>` : ""}
      <div class="actions">
        <button class="btn ${awaiting ? "btn-danger" : "btn-danger-ghost"}" id="btn-execute"
          data-token="${esc(res.token)}" data-awaited="${awaiting ? "1" : "0"}">
          ${ICON.check}<span>${awaiting ? "确认执行（立即生效）" : "确认执行"}</span></button>
      </div>`;
  }

  function executeBlock(res) {
    const ok = res.exit_code === 0;
    return `<div class="${ok ? "okbox" : "errbox"}">
      ${ok ? "执行成功" : `执行失败（退出码 ${esc(res.exit_code)}，详情见日志）`}</div>
      <pre class="light" style="margin-top:12px">${esc(res.log || "")}</pre>`;
  }

  // ── 历史页 ─────────────────────────────────────────────────────
  function outcomeCell(r) {
    const accs = r.accounts || [];
    if (!r.outcome) return `<span class="hint">—</span>`;
    const ok = accs.filter(function (a) {
      return a.status === "ok" || a.status === "cloned";
    }).length;
    return `<span class="tag ${OUTCOME_TONE[r.outcome] || "quiet"}">${esc(OUTCOME_TEXT[r.outcome] || r.outcome)}</span>`
      + (accs.length ? `<span class="hint" style="margin-left:6px">${ok}/${accs.length}</span>` : "");
  }

  function accountTable(r) {
    const accs = r.accounts || [];
    if (accs.length < 2) return "";
    const body = accs.map(function (a, i) {
      const st = a.status || "pending";
      const tone = st === "ok" || st === "cloned" ? "ok"
        : st === "failed" ? "fail"
        : st === "stopped" ? "warn" : "quiet";
      return `<tr>
        <td class="num">${i + 1}</td>
        <td>${esc(a.profile)}</td>
        <td class="mono">${esc(a.draft || "-")}</td>
        <td><span class="tag ${tone}">${esc(ACCT_TEXT[st] || st)}</span></td>
        <td class="num">${esc(fmtSec(a.export_s) || "-")}</td>
        <td class="mono" style="word-break:break-all">${esc(a.artifact || "-")}</td>
        <td>${esc(a.reason || "-")}</td>
      </tr>`;
    }).join("");
    return `<div class="acct-wrap">
      <div class="acct-head"><b>账号成品</b>
        <span class="tag ${OUTCOME_TONE[r.outcome] || "quiet"}">${esc(OUTCOME_TEXT[r.outcome] || "—")}</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>#</th><th>账号</th><th>草稿名</th><th>状态</th><th>导出耗时</th>
          <th>成品路径</th><th>失败原因</th></tr></thead>
        <tbody>${body}</tbody></table></div>
      ${(r.cleaned_drafts || []).length
        ? `<p class="hint">已清理半成品草稿：<span class="mono">${esc(r.cleaned_drafts.join("、"))}</span></p>`
        : ""}
    </div>`;
  }

  const SOURCE_TEXT = { watcher: "watcher", manual: "手工串流" };

  // 外部任务记录：只读聚合 watcher 的 job.json 与手工串流的 _batch_state.json，
  // 不写 runs.jsonl，因此不可点击、不支持重跑。
  function externalHistory(rows) {
    const list = rows || [];
    if (!list.length) {
      return `<section class="card" style="margin-top:16px">
        <div class="card-head"><div><h2>外部任务记录 · 只读</h2>
          <p class="card-sub">watcher 自动执行 / 手工命令行串流 · 不写 runs.jsonl，不支持重跑</p></div></div>
        <div class="card-body"><p class="hint">暂无外部任务记录：watcher 尚未执行过，或素材根下没有 cover_job/job.json。</p></div>
      </section>`;
    }
    const body = list.map(function (r) {
      const note = r.last_error ? ("失败原因：" + r.last_error) : (r.detail || "-");
      return `<tr>
        <td><span class="chip chip-quiet">${esc(SOURCE_TEXT[r.source] || r.source || "")}</span></td>
        <td>${esc(r.series || "-")}${r.draft
          ? `<div class="hint mono" style="word-break:break-all">${esc(r.draft)}</div>` : ""}</td>
        <td class="mono">${esc(fmtTs(r.ended_at) || "-")}</td>
        <td>${badge(r.status || "")}</td>
        <td class="num">${r.percent == null ? "-" : esc(r.percent) + "%"}</td>
        <td>${esc(r.stage || "-")}</td>
        <td style="word-break:break-all" title="${esc(note)}">${esc(note)}</td>
        <td class="mono" style="word-break:break-all">${esc(r.artifact || "-")}</td>
      </tr>`;
    }).join("");
    return `<section class="card" style="margin-top:16px">
      <div class="card-head"><div><h2>外部任务记录 · 只读</h2>
        <p class="card-sub">watcher 自动执行 / 手工命令行串流 · 不写 runs.jsonl，不支持重跑</p></div>
        <span class="chip chip-quiet">${list.length} 条</span></div>
      <div class="card-body"><div class="table-wrap"><table>
        <thead><tr><th>来源</th><th>剧 / 草稿</th><th>结束时间</th><th>状态</th>
          <th>进度</th><th>阶段</th><th>说明</th><th>成品路径</th></tr></thead>
        <tbody>${body}</tbody></table></div></div>
    </section>`;
  }

  function history(state) {
    const all = state.history || [];
    const q = String(state.historyQuery || "").trim().toLowerCase();
    const st = state.historyStatus || "all";
    const list = all.filter(function (r) {
      if (st !== "all" && r.status !== st) return false;
      if (!q) return true;
      return [r.id, r.series_dir, r.mode, r.status, r.stage && r.stage.label]
        .filter(Boolean).join(" ").toLowerCase().indexOf(q) >= 0;
    });
    const rows = list.map(function (r) {
      const draft = r.draft_name || (r.params && r.params.draft_name) || "";
      const exportBtn = exportTargets(r).map(function (d) {
        return `<button class="btn btn-primary btn-sm" data-export-only="${esc(r.id)}"
            data-draft="${esc(d)}" title="对草稿 ${d} 执行仅导出">仅导出</button>`;
      }).join(" ");
      return `<tr class="clickable" data-run="${esc(r.id)}">
        <td class="mono">${esc(r.id)}</td>
        <td class="mono">${esc(fmtTs(r.started_at || r.queued_at) || "")}</td>
        <td>${esc(MODE_TEXT[r.mode] || r.mode)}</td>
        <td>${runTypeTag(r)}</td>
        <td>${esc((r.series_dir || "").split(/[\\/]/).filter(Boolean).pop() || "")}${draft && r.mode !== "export_only" ? `<div class="hint mono" title="草稿名">${esc(draft)}</div>` : ""}</td>
        <td>${badge(r.status || "")}</td>
        <td>${outcomeCell(r)}</td>
        <td>${esc(r.stage ? r.stage.label : "-")}</td>
        <td class="num">${r.exit_code == null ? "-"
          : r.exit_code === 0 ? '<span class="tag ok">成功</span>'
          : '<span class="tag fail">失败</span>'}</td>
        <td data-stop>${exportBtn}</td>
      </tr>`;
    }).join("");

    const statusOpts = [["all", "全部状态"], ["running", "运行中"], ["queued", "排队中"],
      ["succeeded", "成功"], ["failed", "失败"], ["stopped", "已停止"]]
      .map(function (p) {
        return `<option value="${p[0]}"${st === p[0] ? " selected" : ""}>${p[1]}</option>`;
      }).join("");

    return `
    <div class="page-head">
      <div><h1>历史记录</h1>
        <p>落盘：${esc((state.config && state.config.runs_file) || "pipeline_ui/_console/runs.jsonl")}</p></div>
      <div class="head-actions">
        <button class="btn btn-secondary btn-sm" id="btn-reload-history">${ICON.refresh}<span>刷新</span></button>
      </div>
    </div>
    <section class="card">
      <div class="card-body">
        ${all.length ? `<div class="filter-bar">
          <input type="search" id="f-history-q" placeholder="搜索运行 ID / 剧目录 / 链路"
                 value="${esc(state.historyQuery || "")}" aria-label="搜索历史记录">
          <select id="f-history-status" aria-label="按状态筛选">${statusOpts}</select>
          <span class="hint">共 ${all.length} 条 · 显示 ${list.length} 条</span>
        </div>` : ""}
        ${rows ? `<div class="table-wrap"><table>
          <thead><tr><th>运行 ID</th><th>开始</th><th>链路</th><th>类型</th>
          <th>剧 / 草稿</th><th>状态</th><th>结论</th><th>阶段</th>
          <th>执行结果</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table></div>`
        : (all.length
          ? `<div class="empty"><b>没有符合条件的记录</b><span>换个关键词或把状态切回「全部状态」。</span></div>`
          : empty("clock", "暂无 Console 运行记录",
            "只有从 Console 提交的任务才写入 runs.jsonl；watcher 自动执行与手工命令行串流见下方「外部任务记录」。",
            { label: "去提交任务", page: "run" }))}
      </div>
    </section>
    ${externalHistory(state.externalHistory)}
    <div id="history-detail"></div>`;
  }

  function historyDetail(d) {
    if (!d) return "";
    const r = d.run || {};
    function kv(k, v, mono) {
      return `<div class="kv-item"><span class="k">${esc(k)}</span>
        <span class="v${mono ? " mono" : ""}">${v}</span></div>`;
    }
    return `<section class="card" style="margin-top:16px">
      <div class="card-head">
        <div><h2>运行详情</h2><p class="card-sub mono">${esc(r.id)}</p></div>
        <button class="btn btn-primary btn-sm" id="btn-rerun" data-run="${esc(r.id)}">
          ${ICON.refresh}<span>按上次参数重跑</span></button>
      </div>
      <div class="card-body">
        <div class="kv">
          ${kv("剧目录", esc(r.series_dir), true)}
          ${kv("封面来源", r.cover_source === "upload"
                ? "上传图片 · <span class=\"mono\">" + esc(r.cover_image || "") + "</span>"
                : r.cover_source === "path"
                  ? "指定路径 · <span class=\"mono\">" + esc(r.cover_image || "") + "</span>"
                  : "剧目录自动探测（封面.png / 04_covers）")}
          ${kv("状态", badge(r.status || ""))}
          ${kv("开始 / 结束", esc(fmtTs(r.started_at) || "-") + " → " + esc(fmtTs(r.ended_at) || "-"))}
          ${kv("执行结果", r.exit_code == null ? "-" : (r.exit_code === 0 ? "成功"
                : `<b class="err">失败（退出码 ${esc(r.exit_code)}）</b>`))}
          ${kv("失败阶段", esc(r.stage ? r.stage.label : "-") + (r.error_class ? " · " + esc(r.error_class) : ""))}
          ${kv("产物", esc(r.artifact || "-"), true)}
          <div class="kv-item span-2"><span class="k">参数</span>
            <span class="v mono">${esc(JSON.stringify(r.params || {}))}</span></div>
          <div class="kv-item span-2"><span class="k">日志</span>
            <span class="v mono">${esc(r.log_path || "-")}</span></div>
        </div>
        ${accountTable(r)}
        <div class="log-box">
          <div class="log-head">日志尾部（最多 300 行）</div>
          <pre>${esc(d.log_tail || "（无日志）")}</pre>
        </div>
      </div>
    </section>`;
  }

  // ── 外部任务 / watcher（只读卡片）───────────────────────────────
  const WATCHER_STATE_TEXT = {
    running: "运行中", lock_only_dead: "残留锁", stale: "心跳过期", stopped: "未运行"
  };

  function externalCard(d) {
    const head = `<div class="card-head">
      <div><h2>外部任务 · 只读</h2>
        <p class="card-sub">watcher / 命令行串流 · 只展示，不提供停止</p></div>
      <span class="chip chip-quiet">${d && d.tasks ? d.tasks.length + " 个在跑" : "—"}</span>
    </div>`;
    if (!d) {
      return `<section class="card">${head}<div class="card-body">
        <p class="hint">暂时读不到外部任务状态（接口不可用或正在启动）。</p>
      </div></section>`;
    }
    const w = d.watcher || {};
    const hb = w.heartbeat || {};
    const pids = ((w.process || {}).pids || []).join(", ") || "—";
    const tasks = d.tasks || [];
    const rows = tasks.map(function (t) {
      const beat = t.heartbeat || {};
      return `<tr>
        <td><span class="chip chip-quiet">${esc(t.source || "")}</span></td>
        <td>${esc(t.series || "—")}</td>
        <td>${esc(t.stage || "—")}
          <div class="hint" style="margin:2px 0 0">${esc(t.detail || "")}</div></td>
        <td>${t.percent == null ? "—" : esc(t.percent) + "%"}</td>
        <td>${esc(beat.age_text || "—")}</td>
        <td class="mono" style="word-break:break-all">${esc(t.task_dir || "—")}</td>
      </tr>`;
    }).join("");
    const body = rows
      ? `<div class="table-wrap"><table class="env-table">
          <thead><tr><th>来源</th><th>剧集</th><th>阶段</th><th>进度</th>
            <th>心跳</th><th>任务目录</th></tr></thead>
          <tbody>${rows}</tbody></table></div>`
      : `<div class="empty">当前没有检测到 watcher 或命令行任务</div>`;
    return `<section class="card">${head}
      <div class="card-body">
        <div class="kv">
          <div class="kv-item"><span class="k">watcher</span>
            <span class="v">${esc(WATCHER_STATE_TEXT[w.state] || w.state || "未知")}</span></div>
          <div class="kv-item"><span class="k">心跳</span>
            <span class="v">${esc(hb.age_text || "无心跳")}
              <span class="hint"> ${esc(hb.updated_at || "")}</span></span></div>
          <div class="kv-item"><span class="k">进程</span>
            <span class="v mono">${esc(pids)}</span></div>
          <div class="kv-item"><span class="k">扫描时间</span>
            <span class="v">${esc(d.scanned_at || "")}</span></div>
        </div>
        ${body}
      </div>
    </section>`;
  }

  window.View = {
    runForm, coverBox, statusCard, previewBlock, skeleton, archive, planBlock,
    executeBlock, history, historyDetail, profilePicks, profileHint, externalCard,
    guideBanner
  };
})();
