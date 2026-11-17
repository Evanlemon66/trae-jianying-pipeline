/* dialog.js —— 全局确认弹窗：所有写操作共用同一套 modal。
   用法：Dialog.confirm({title, subtitle, text, details, tone, confirmText,
                        cancelText, checkbox: {label, checked}})
        .then(({ok, checked}) => ...)
   复用 style.css 的 .env-modal 样式；Esc / 点遮罩取消，Enter 确认。 */
(function () {
  "use strict";

  let root = null;
  let active = null;

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function ensureRoot() {
    if (root && document.body.contains(root)) return root;
    root = document.createElement("div");
    root.id = "app-dialog-root";
    root.className = "env-modal app-dialog";
    root.hidden = true;
    document.body.appendChild(root);
    return root;
  }

  function checkedNow() {
    const box = ensureRoot().querySelector("[data-dialog-check]");
    return box ? box.checked : true;
  }

  function finish(result) {
    const cur = active;
    if (!cur) return;
    active = null;
    const el = ensureRoot();
    el.hidden = true;
    el.innerHTML = "";
    el.onclick = null;
    document.body.classList.remove("dialog-open");
    document.removeEventListener("keydown", onKey, true);
    if (cur.lastFocus && typeof cur.lastFocus.focus === "function") {
      try { cur.lastFocus.focus(); } catch (e) { /* 元素可能已被重渲染 */ }
    }
    cur.resolve(result);
  }

  function onKey(e) {
    if (!active) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      finish({ ok: false, checked: checkedNow() });
    } else if (e.key === "Enter") {
      const tag = ((e.target && e.target.tagName) || "").toLowerCase();
      if (tag === "textarea") return;
      e.preventDefault();
      e.stopPropagation();
      finish({ ok: true, checked: checkedNow() });
    }
  }

  function markup(opts) {
    const danger = opts.tone === "danger";
    const details = (opts.details || []).filter(Boolean)
      .map((d) => `<li>${esc(d)}</li>`).join("");
    const checkbox = opts.checkbox
      ? `<label class="env-enable"><input type="checkbox" data-dialog-check${
          opts.checkbox.checked === false ? "" : " checked"}> ${
          esc(opts.checkbox.label || "")}</label>`
      : "";
    return `<div class="env-modal-dialog${danger ? " danger" : ""}" role="alertdialog"
                 aria-modal="true" aria-labelledby="app-dialog-title"
                 ${opts.text ? 'aria-describedby="app-dialog-text"' : ""}>
      <div class="env-modal-heading">
        <span class="env-modal-icon${danger ? " danger" : ""}" aria-hidden="true">${danger ? "!" : "?"}</span>
        <div><h2 id="app-dialog-title">${esc(opts.title || "请确认")}</h2>
          ${opts.subtitle ? `<p>${esc(opts.subtitle)}</p>` : ""}</div>
      </div>
      ${opts.text ? `<p class="env-modal-text" id="app-dialog-text">${esc(opts.text)}</p>` : ""}
      ${details ? `<ul class="dialog-details">${details}</ul>` : ""}
      ${checkbox}
      <div class="env-modal-actions">
        ${opts.cancelText === null ? "" :
          `<button class="btn btn-secondary" data-dialog-cancel>${esc(opts.cancelText || "取消")}</button>`}
        <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-dialog-ok>${
          esc(opts.confirmText || "确定执行")}</button>
      </div>
    </div>`;
  }

  function open(opts) {
    const o = opts || {};
    return new Promise((resolve) => {
      if (active) finish({ ok: false, checked: false });
      const el = ensureRoot();
      el.innerHTML = markup(o);
      el.hidden = false;
      document.body.classList.add("dialog-open");
      const lastFocus = document.activeElement;
      active = { resolve, lastFocus };
      el.onclick = (e) => {
        if (e.target === el || e.target.closest("[data-dialog-cancel]")) {
          finish({ ok: false, checked: checkedNow() });
        } else if (e.target.closest("[data-dialog-ok]")) {
          finish({ ok: true, checked: checkedNow() });
        }
      };
      document.addEventListener("keydown", onKey, true);
      const ok = el.querySelector("[data-dialog-ok]");
      if (ok) ok.focus();
    });
  }

  window.Dialog = {
    confirm: open,
    alert: (opts) => open(Object.assign({}, opts, { cancelText: null }))
      .then(() => undefined)
  };
})();
