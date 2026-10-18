"use strict";

const $ = (id) => document.getElementById(id);
const VIDEO_EXTS = new Set(["mp4", "mov", "avi", "mkv", "wmv", "webm", "flv", "m4v", "mpg", "mpeg", "3gp", "mts"]);

let keepers = [];      // 每组保留者下标（自动结果，可被点击覆盖）
let lastState = null;
let lastBrowse = null; // 浏览弹窗当前目录信息

/* ---------- 工具函数 ---------- */

function fmtSize(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${n} B` : `${n.toFixed(1)} ${units[i]}`;
}

function fmtDate(ts) {
  const d = new Date(ts * 1000), p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function ext(path) {
  const m = /\.([^.]+)$/.exec(path);
  return m ? m[1].toLowerCase() : "";
}

function showSections(...ids) {
  ["card-config", "card-progress", "card-results", "card-done"].forEach((id) =>
    $(id).classList.toggle("hidden", !ids.includes(id)));
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let msg = `请求失败（${res.status}）`;
    try { const j = await res.json(); if (j.detail) msg = j.detail; } catch (_) { /* 忽略 */ }
    throw new Error(msg);
  }
  return res.json();
}

/* ---------- 状态轮询与视图切换 ---------- */

async function poll() {
  let st;
  try { st = await api("/api/status"); } catch (_) { return; }
  if (st.state !== lastState) {
    lastState = st.state;
    onStateChange(st);
  }
  if (st.state === "scanning" || st.state === "executing") renderProgress(st);
}

function onStateChange(st) {
  if (st.root) $("in-root").value = st.root;
  if (st.keep) $("in-keep").value = st.keep;
  if (st.dup) $("in-dup").value = st.dup;
  $("config-error").textContent = "";
  switch (st.state) {
    case "idle":
      showSections("card-config");
      break;
    case "scanning":
      $("progress-title").textContent = "正在扫描…";
      showSections("card-progress");
      break;
    case "scanned":
      loadResults();
      break;
    case "executing":
      $("progress-title").textContent = "正在复制…";
      showSections("card-progress");
      break;
    case "done":
    case "cancelled":
      if (st.summary) renderDone(st);
      else { $("config-error").textContent = "已取消"; showSections("card-config"); }
      break;
    case "error":
      $("config-error").textContent = `出错了：${st.error || "未知错误"}`;
      showSections("card-config");
      break;
  }
}

function renderProgress(st) {
  const p = st.progress || {};
  const bar = document.querySelector(".bar"), fill = $("bar-fill");
  const base = p.current ? p.current.split(/[\\/]/).pop() : "";
  let text = "";
  if (p.phase === "walk") {
    bar.classList.add("indeterminate");
    text = `正在清点文件夹… 已发现 ${p.files_seen} 个图片/视频${base ? " · " + base : ""}`;
  } else if (p.phase === "hash") {
    bar.classList.remove("indeterminate");
    const total = p.total_files || 0, done = p.done_files || 0;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 100;
    fill.style.width = pct + "%";
    text = `(${done}/${total}) 内容比对中 ${pct}%${base ? " · " + base : ""}`;
  } else if (p.phase === "copy") {
    bar.classList.remove("indeterminate");
    const pct = p.total ? Math.min(100, Math.round((p.done / p.total) * 100)) : 100;
    fill.style.width = pct + "%";
    const where = p.kind === "keep" ? "保留" : "重复";
    text = `(${p.done}/${p.total}) 正在复制到${where}文件夹 ${pct}%${base ? " · " + base : ""}`;
  }
  $("progress-text").textContent = text;
}

/* ---------- 结果渲染 ---------- */

async function loadResults() {
  let data;
  try { data = await api("/api/results"); } catch (e) {
    $("config-error").textContent = e.message;
    showSections("card-config");
    return;
  }
  keepers = data.groups.map((g) => g.keeper);
  const st = await api("/api/status");
  const s = st.stats || {};
  $("stats").innerHTML =
    `共 <b>${s.files}</b> 个图片/视频 · ${s.unique_content} 个唯一内容 · ` +
    `<b>${s.groups}</b> 组重复 · 冗余 ${s.redundant} 个副本（可释放 ${fmtSize(s.redundant_bytes || 0)}）` +
    (s.non_media ? ` · 已忽略 ${s.non_media} 个非图片/视频文件` : "") +
    (s.skipped ? ` · 跳过 ${s.skipped} 个（空文件/无法读取）` : "");
  $("no-dup-hint").classList.toggle("hidden", s.groups > 0);
  renderGroups(data);
  $("uniques-count").textContent = data.uniques.length;
  const list = $("uniques-list");
  list.innerHTML = data.uniques.map((f) => `<li><span class="mono">${esc(f.rel)}</span> · ${fmtSize(f.size)}</li>`).join("");
  showSections("card-results");
}

function renderGroups(data) {
  const wrap = $("groups");
  wrap.innerHTML = "";
  data.groups.forEach((g, gi) => {
    const div = document.createElement("div");
    div.className = "group";
    div.innerHTML =
      `<div class="group-head">组 ${gi + 1} · ${g.count} 个副本 · 每个文件 ${fmtSize(g.size)} · 冗余 ${fmtSize(g.size * (g.count - 1))}</div>` +
      `<div class="file-grid">${g.files.map((f, i) => fileCard(f, i === keepers[gi])).join("")}</div>`;
    div.querySelectorAll(".file").forEach((el, i) => {
      el.onclick = () => { keepers[gi] = i; renderGroups(data); };
    });
    wrap.append(div);
  });
  wrap.querySelectorAll(".thumb img").forEach((img) => {
    img.onerror = () => { img.replaceWith(Object.assign(document.createElement("div"), { className: "icon", textContent: "🖼️" })); };
  });
}

function fileCard(f, isKeeper) {
  const cls = isKeeper ? "file keeper" : "file";
  const badge = isKeeper ? "✓ 保留" : "重复";
  let thumb;
  if (f.is_image) {
    thumb = `<img loading="lazy" src="/api/thumb?path=${encodeURIComponent(f.path)}" alt="">`;
  } else {
    thumb = `<div class="icon">${VIDEO_EXTS.has(ext(f.name)) ? "🎬" : "🖼️"}</div>`;
  }
  return `<div class="${cls}" title="${esc(f.path)}">
    <div class="thumb">${thumb}</div>
    <div class="fname">${esc(f.name)}</div>
    <div class="fmeta">${esc(f.rel)}<br>${fmtSize(f.size)} · ${fmtDate(f.mtime)}</div>
    <span class="badge">${badge}</span>
  </div>`;
}

/* ---------- 执行与完成 ---------- */

$("btn-execute").onclick = async () => {
  try {
    await api("/api/execute", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keepers }),
    });
  } catch (e) { alert(e.message); }
};

$("btn-cancel").onclick = () => { fetch("/api/cancel", { method: "POST" }); };

function renderDone(st) {
  const s = st.summary || {};
  const failHtml = s.failures && s.failures.length
    ? `<div class="error">复制失败 ${s.failures.length} 个：<ul>${s.failures.map((f) => `<li><span class="mono">${esc(f.src)}</span>（${esc(f.error)}）</li>`).join("")}</ul></div>`
    : "";
  $("done-summary").innerHTML =
    `<div class="stats">复制完成${s.cancelled ? "（已被取消，部分文件未复制）" : ""}：保留文件夹 <b>${s.copied_keep}</b> 个文件 · ` +
    `重复文件夹 <b>${s.copied_dup}</b> 个文件` +
    (s.skipped_existing ? ` · 已存在相同内容跳过 ${s.skipped_existing} 个` : "") +
    (s.renamed ? ` · 重名自动编号 ${s.renamed} 个` : "") +
    ` · 共 ${fmtSize(s.bytes_copied || 0)}${s.failures && s.failures.length ? ` · 失败 ${s.failures.length} 个` : ""}</div>` +
    `<div>报告已写入：<span class="mono">${esc(s.report || "")}</span></div>${failHtml}` +
    `<div class="hint">文件已平铺存放（不保留原目录层级）。源文件夹未被修改，确认无误后可自行删除。</div>`;
  showSections("card-done");
}

$("btn-open-keep").onclick = () => openFolder($("in-keep").value);
$("btn-open-dup").onclick = () => openFolder($("in-dup").value);
$("btn-again").onclick = () => { $("config-error").textContent = ""; showSections("card-config"); };
$("btn-back-config").onclick = () => showSections("card-config");

async function openFolder(path) {
  if (!path) return;
  try { await api("/api/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) }); }
  catch (e) { alert(e.message); }
}

/* ---------- 扫描启动 ---------- */

$("btn-scan").onclick = async () => {
  const payload = {
    root: $("in-root").value.trim(),
    keep_dir: $("in-keep").value.trim(),
    dup_dir: $("in-dup").value.trim(),
  };
  $("config-error").textContent = "";
  if (!payload.root || !payload.keep_dir || !payload.dup_dir) {
    $("config-error").textContent = "请填写全部三个文件夹路径";
    return;
  }
  try {
    await api("/api/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  } catch (e) { $("config-error").textContent = e.message; }
};

/* ---------- 无重复文件列表折叠 ---------- */

$("uniques-toggle").onclick = () => {
  $("uniques-box").classList.toggle("open");
  $("uniques-list").classList.toggle("hidden");
  $("uniques-toggle").firstChild.textContent = $("uniques-list").classList.contains("hidden") ? "▸ 另有 " : "▾ 另有 ";
};

/* ---------- 文件夹浏览弹窗 ---------- */

let browseTarget = null;

document.querySelectorAll("[data-browse]").forEach((btn) => {
  btn.onclick = () => openBrowse(btn.dataset.browse);
});

async function openBrowse(targetInputId) {
  browseTarget = targetInputId;
  $("browse-modal").classList.remove("hidden");
  await gotoBrowse($(targetInputId).value.trim() || "");
}

async function gotoBrowse(path) {
  $("browse-list").innerHTML = "<div class='browse-item'>加载中…</div>";
  try {
    lastBrowse = await api(`/api/browse${path ? "?path=" + encodeURIComponent(path) : ""}`);
  } catch (e) {
    lastBrowse = null;
    $("browse-list").innerHTML = "";
    $("browse-current").textContent = `⚠ ${e.message}`;
    return;
  }
  $("browse-input").value = lastBrowse.path;
  $("browse-current").textContent = lastBrowse.path;
  const list = $("browse-list");
  list.innerHTML = "";
  if (!lastBrowse.dirs.length) {
    list.innerHTML = "<div class='browse-item'>（此文件夹下没有子文件夹）</div>";
  }
  lastBrowse.dirs.forEach((name) => {
    const b = document.createElement("button");
    b.className = "browse-item";
    b.textContent = "📁 " + name;
    b.onclick = () => {
      const sep = lastBrowse.path.endsWith("\\") || lastBrowse.path.endsWith("/") ? "" : "\\";
      gotoBrowse(lastBrowse.path + sep + name);
    };
    list.append(b);
  });
}

$("browse-goto").onclick = () => gotoBrowse($("browse-input").value.trim());
$("browse-input").onkeydown = (e) => { if (e.key === "Enter") gotoBrowse($("browse-input").value.trim()); };
$("browse-up").onclick = () => { if (lastBrowse && lastBrowse.parent) gotoBrowse(lastBrowse.parent); };
$("browse-pick").onclick = () => {
  if (lastBrowse) $(browseTarget).value = lastBrowse.path;
  $("browse-modal").classList.add("hidden");
};
$("browse-cancel").onclick = () => $("browse-modal").classList.add("hidden");

/* ---------- 启动 ---------- */

setInterval(poll, 700);
poll();
