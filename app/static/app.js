"use strict";
// Uno Notetaker — single-page UI, no build step.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PIECE = 8 * 1024 * 1024;
const BUSY = ["queued", "transcribing", "summarizing", "uploading"];
const phone = () => window.matchMedia("(max-width: 760px)").matches;

let STATE = null;
let current = null;      // meeting id on screen
let view = "meetings";   // meetings | actions
let pollTimer = null;
let recording = null;    // active recording session
let lastMeeting = null;  // the meeting on screen (full JSON)

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body && !(opts.body instanceof Blob) && !(opts.body instanceof ArrayBuffer)
      ? { "Content-Type": "application/json", ...(opts.headers || {}) } : (opts.headers || {}),
  });
  if (res.status === 401) { location.reload(); throw new Error("login required"); }
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && (body.detail || body.error)) || `HTTP ${res.status}`);
  return body;
}

function fmtTs(sec) {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
function fmtDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  const yest = new Date(today - 864e5).toDateString() === d.toDateString();
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (same) return `Today, ${time}`;
  if (yest) return `Yesterday, ${time}`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(d.getFullYear() !== today.getFullYear() ? { year: "numeric" } : {}) }) + `, ${time}`;
}
function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4200);
}
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

// Minimal, safe Markdown: headings, lists, checkboxes, bold/italic/code, http links.
function inlineMd(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
    .replace(/\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g, '<a href="#" class="ts" data-ts="$1">$1</a>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function renderMd(src, { skipTitle = "", skipSection = null } = {}) {
  const out = [];
  let list = null, skipping = false;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of String(src || "").split("\n")) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      skipping = !!(skipSection && skipSection.test(m[2]));
      if (skipping) { close(); continue; }
      if (m[1].length === 1 && skipTitle && m[2].trim().toLowerCase() === skipTitle.trim().toLowerCase()) continue;
      close(); const n = Math.min(m[1].length + 1, 4); out.push(`<h${n}>${inlineMd(m[2])}</h${n}>`);
      continue;
    }
    if (skipping) continue;
    if ((m = line.match(/^\s*[-*]\s+\[( |x|X)\]\s+(.*)$/))) { if (list !== "ul") { close(); out.push('<ul class="tasks">'); list = "ul"; } out.push(`<li><span class="box">${m[1].trim() ? "☑" : "☐"}</span> ${inlineMd(m[2])}</li>`); }
    else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { if (list !== "ul") { close(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inlineMd(m[1])}</li>`); }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { if (list !== "ol") { close(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inlineMd(m[1])}</li>`); }
    else if (!line.trim()) { close(); }
    else { close(); out.push(`<p>${inlineMd(line)}</p>`); }
  }
  close();
  return out.join("\n");
}
const tsToSec = (t) => t.split(":").map(Number).reduce((a, b) => a * 60 + b, 0);
const ACTION_SECTION = /action items|next steps|задачи|действия|поручения|следующие шаги/i;

// ---- state & sidebar -------------------------------------------------------

async function loadState() {
  STATE = await api("/api/state");
  const p = STATE.provider;
  const el = $("#ai-status");
  const pill = { app: "Computer AI ✓", gateway: "Uno AI ✓", custom: "Custom AI ✓" };
  el.textContent = p.ready ? (pill[p.route] || "AI ✓") : "AI not set up";
  el.title = p.ready ? `AI: ${p.route_label} — ${p.source}` : "";
  el.className = `ai-status ${p.ready ? "ok" : "bad"}`;
  for (const id of ["#rec-template", "#up-template", "#set-template"]) {
    $(id).innerHTML = Object.entries(STATE.templates).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    $(id).value = STATE.settings.template;
  }
  $("#models").innerHTML = STATE.models.map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join("");
}

function statusBadge(s) {
  const map = { recording: "● recording", uploading: "uploading", queued: "in line", transcribing: "transcribing", summarizing: "writing notes", error: "⚠ error" };
  return map[s] ? `<em class="st st-${s}">${map[s]}</em>` : "";
}

async function loadList() {
  const q = $("#search").value.trim();
  const { meetings } = await api(`/api/meetings${q ? `?q=${encodeURIComponent(q)}` : ""}`);
  const total = meetings.reduce((n, m) => n + (m.open_actions || 0), 0);
  if (!q) $("#actions-count").textContent = total ? String(total) : "";
  $$(".view-tab").forEach((a) => a.classList.toggle("on", a.dataset.view === view && !q));
  const list = $("#list");
  if (q) {
    list.innerHTML = meetings.length ? `<p class="list-note">${plural(meetings.length, "meeting")} with “${esc(q)}”</p>` + meetings.map((m) => `
      <div class="item hit ${m.id === current ? "active" : ""}">
        <a href="#/m/${m.id}" class="item-main"><span class="t">${esc(m.title || "Untitled meeting")}</span>
        <span class="s">${fmtDate(m.created_at)}</span></a>
        ${(m.hits || []).map((h) => `<a class="snip" href="#/m/${m.id}${h.where === "transcript" ? `/t/${Math.floor(h.ts)}` : ""}">
          ${h.where === "transcript" ? `<span class="snip-ts">${fmtTs(h.ts)}</span>` : `<span class="snip-ts">notes</span>`}<span class="snip-text">${mark(h.text, q)}</span></a>`).join("")}
      </div>`).join("") : `<p class="empty-list">Nothing found for “${esc(q)}”.</p>`;
    return meetings;
  }
  if (!meetings.length) {
    list.innerHTML = `<p class="empty-list">No meetings yet. Record one, upload a recording${STATE.telegram.paired ? ` or send a voice message to @${esc(STATE.telegram.bot)}` : ""}.</p>`;
    return meetings;
  }
  let lastDay = "";
  list.innerHTML = meetings.map((m) => {
    const day = new Date(m.created_at).toDateString();
    const head = day !== lastDay ? `<div class="day">${dayLabel(m.created_at)}</div>` : "";
    lastDay = day;
    return `${head}<a href="#/m/${m.id}" class="item ${m.id === current ? "active" : ""}" data-id="${m.id}">
      <span class="t">${esc(m.title || (m.status === "done" ? "Untitled meeting" : "New meeting"))}</span>
      <span class="s">${new Date(m.created_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}${m.duration ? " · " + fmtTs(m.duration) : ""}
        ${m.open_actions ? `<span class="pill">${plural(m.open_actions, "to-do")}</span>` : ""}${statusBadge(m.status)}</span>
    </a>`;
  }).join("");
  return meetings;
}
function dayLabel(iso) {
  const d = new Date(iso), t = new Date();
  if (d.toDateString() === t.toDateString()) return "Today";
  if (new Date(t - 864e5).toDateString() === d.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}
function mark(text, q) {
  let html = esc(text);
  for (const w of q.split(/\s+/).filter(Boolean)) {
    const re = new RegExp(`(${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
    html = html.replace(re, "<mark>$1</mark>");
  }
  return html;
}

function setView(v) {
  document.body.classList.toggle("view-list", v === "list");
  document.body.classList.toggle("view-detail", v === "detail");
}

// ---- home ------------------------------------------------------------------

function showHome() {
  current = null; view = "meetings";
  clearInterval(pollTimer);
  setView("list");
  const p = STATE.provider;
  $("#main").innerHTML = `
    <section class="home">
      <h1>Meeting notes, done for you</h1>
      <p class="lead">Record a call or upload a file. You get a transcript with speakers, a summary and action items — sent to your Inbox${STATE.telegram.paired ? " and Telegram" : ""}, saved in <code>~/Meetings</code>.</p>
      <div class="cards">
        <button class="card" id="home-record"><span class="ic rec"></span><b>Record</b><span>A call in another tab (Meet, Zoom, Teams) or a meeting in the room.</span></button>
        <button class="card" id="home-upload"><span class="ic up">⬆</span><b>Upload a recording</b><span>Audio or video: m4a, mp3, wav, mp4, webm…</span></button>
      </div>
      ${p.ready ? "" : `<div class="warn">The AI is not set up yet. <button class="link" id="home-settings">Open Settings</button></div>`}
      <div class="tips">
        <div><b>From your phone</b><span>${STATE.telegram.paired ? `Send a voice message or a recording to <b>@${esc(STATE.telegram.bot)}</b> in Telegram.` : `Connect a Telegram bot in <button class="link inline" id="tip-tg">Settings</button> and send recordings to it.`} Or open this page on the phone and add it to the Home Screen.</span></div>
        <div><b>A bot that joins the call?</b><span>Not yet: Meet and Teams make the host let a bot in, Zoom needs a Marketplace app. Record the call's tab instead — same notes.</span></div>
      </div>
      <p class="fine">Recording other people? Tell them first — in many places it's required by law.</p>
    </section>`;
  $("#home-record").onclick = openRecordDialog;
  $("#home-upload").onclick = () => $("#file-input").click();
  if (!p.ready) $("#home-settings").onclick = openSettings;
  if ($("#tip-tg")) $("#tip-tg").onclick = openSettings;
  loadList();
}

// ---- action items across meetings --------------------------------------------

async function showActions() {
  current = null; view = "actions";
  clearInterval(pollTimer);
  setView("detail");
  const showDone = sessionStorage.getItem("nt-done") === "1";
  const { actions } = await api(`/api/actions${showDone ? "?done=1" : ""}`);
  let last = null;
  const rows = actions.map((a) => {
    const head = a.meeting_id !== last ? `<h3 class="group"><a href="#/m/${a.meeting_id}">${esc(a.meeting_title)}</a> <span class="muted">${fmtDate(a.meeting_at)}</span></h3>` : "";
    last = a.meeting_id;
    return head + actionRow(a, a.meeting_id);
  }).join("");
  $("#main").innerHTML = `
    <section class="page">
      <header class="page-head">
        <a href="#/" class="back only-phone">‹ Meetings</a>
        <h1>Action items</h1>
        <label class="check small"><input type="checkbox" id="show-done" ${showDone ? "checked" : ""}> Show done</label>
      </header>
      <p class="muted">From the notes of every meeting. Tick one off here or in the meeting — it's the same checkbox in <code>notes.md</code>.</p>
      <div class="actions-list">${rows || `<p class="empty">Nothing open. 🎉</p>`}</div>
    </section>`;
  $("#show-done").onchange = (e) => { sessionStorage.setItem("nt-done", e.target.checked ? "1" : "0"); showActions(); };
  bindActionRows($("#main"), showActions);
  loadList();
}

function actionRow(a, mid, handed = {}) {
  const h = handed[a.id];
  const aiBtn = STATE.uno_work && !a.done
    ? (h ? `<span class="handed" title="A chat in Uno Work is working on it">In Uno Work ✓</span>`
      : `<button class="link small ai-btn" data-ai="${a.id}" data-mid="${mid}" title="Start a chat in Uno Work that prepares this (drafts, documents). It asks you before changing anything.">Ask Uno to do it</button>`)
    : "";
  return `<div class="action ${a.done ? "done" : ""}">
    <label><input type="checkbox" data-toggle="${a.id}" data-mid="${mid}" ${a.done ? "checked" : ""}>
    <span class="a-text">${a.owner ? `<b class="owner">${esc(a.owner === "Me" ? "You" : a.owner)}</b> ` : ""}${esc(a.text)}</span></label>
    <span class="a-meta">${a.due ? `<span class="due">${esc(a.due)}</span>` : ""}${a.ts ? `<a class="ts" href="#/m/${mid}/t/${a.ts_seconds}" data-ts="${a.ts}">${a.ts}</a>` : ""}${aiBtn}</span>
  </div>`;
}
function bindActionRows(root, refresh) {
  $$("[data-toggle]", root).forEach((cb) => cb.onchange = async () => {
    cb.closest(".action").classList.toggle("done", cb.checked);
    try { await api(`/api/meetings/${cb.dataset.mid}/actions/${cb.dataset.toggle}`, { method: "PATCH", body: JSON.stringify({ done: cb.checked }) }); loadList(); }
    catch (e) { toast(e.message, "bad"); refresh(); }
  });
  $$("[data-ai]", root).forEach((b) => b.onclick = async (e) => {
    e.preventDefault();
    b.disabled = true; b.textContent = "Starting…";
    try {
      await api(`/api/meetings/${b.dataset.mid}/actions/${b.dataset.ai}/ai`, { method: "POST" });
      b.outerHTML = `<span class="handed">In Uno Work ✓</span>`;
      toast("A chat started in Uno Work — it asks you before it changes anything.");
    } catch (err) { toast(err.message, "bad"); b.disabled = false; b.textContent = "Ask Uno to do it"; }
  });
}

// ---- recording -------------------------------------------------------------

const canShareTab = () => !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) && !phone();

function openRecordDialog() {
  if (recording) { location.hash = `#/m/${recording.id}`; return; }
  const dlg = $("#dlg-record");
  dlg.querySelector("form").reset();
  const tab = canShareTab();
  $(".mode-call", dlg).hidden = !tab;
  dlg.querySelector(`[name="mode"][value="${tab ? "call" : "mic"}"]`).checked = true;
  $("#rec-template").value = STATE.settings.template;
  dlg.querySelector('[name="language"]').value = STATE.settings.language;
  dlg.showModal();
}
$("#copy-consent").onclick = () => { navigator.clipboard.writeText($("#consent-text").textContent); toast("Copied"); };

$("#dlg-record").addEventListener("close", async () => {
  const dlg = $("#dlg-record");
  if (dlg.returnValue !== "ok") return;
  const f = new FormData(dlg.querySelector("form"));
  try { await startRecording(f.get("mode"), f.get("title"), f.get("template"), f.get("language")); }
  catch (e) { toast(e.message || String(e), "bad"); }
});

// Chrome/Firefox record webm/opus; Safari (iPhone, Mac) only mp4/aac.
function pickMime() {
  const opts = [["audio/webm;codecs=opus", "webm"], ["audio/webm", "webm"], ["audio/mp4;codecs=mp4a.40.2", "mp4"], ["audio/mp4", "mp4"], ["audio/ogg;codecs=opus", "ogg"]];
  for (const [mime, ext] of opts) if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(mime)) return { mime, ext };
  return { mime: "", ext: "mp4" };
}

class TrackUploader {
  constructor(mid, track, ext) { this.mid = mid; this.track = track; this.ext = ext; this.offset = 0; this.chain = Promise.resolve(); this.failed = 0; }
  push(blob) {
    this.chain = this.chain.then(() => this.send(blob));
    return this.chain;
  }
  async send(blob) {
    const buf = await blob.arrayBuffer();
    for (let attempt = 0; ; attempt++) {
      try {
        const r = await api(`/api/meetings/${this.mid}/tracks/${this.track}?ext=${this.ext}&offset=${this.offset}`, { method: "PUT", body: buf });
        this.offset = r.bytes;
        this.failed = 0;
        return;
      } catch (e) {
        this.failed++;
        if (recording) recording.setNetwork(false);
        await new Promise((r) => setTimeout(r, Math.min(15000, 1000 * 2 ** attempt)));
      } finally { if (recording && this.failed === 0) recording.setNetwork(true); }
    }
  }
}

async function startRecording(mode, title, template, language) {
  if (!window.MediaRecorder || !navigator.mediaDevices) throw new Error("This browser can't record. Use Chrome, Safari, Edge or Firefox.");
  let tabStream = null, display = null;
  if (mode === "call") {
    toast("Pick the tab with your call and turn on “Share tab audio”.");
    display = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: { suppressLocalAudioPlayback: false }, systemAudio: "include", selfBrowserSurface: "exclude", surfaceSwitching: "include" });
    const at = display.getAudioTracks();
    if (!at.length) {
      display.getTracks().forEach((t) => t.stop());
      throw new Error("No sound was shared. Share the tab with the call and switch on “Share tab audio”.");
    }
    tabStream = new MediaStream(at);
  }
  let micStream = null;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) {
    if (!tabStream) throw new Error("Microphone access was blocked. Allow the microphone for this site and try again.");
    toast("No microphone — recording the call's sound only.", "bad");
  }
  const meeting = await api("/api/meetings", { method: "POST", body: JSON.stringify({ title, source: "browser", template, language, tz_offset: -new Date().getTimezoneOffset() }) });
  const { mime, ext } = pickMime();
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  ctx.resume().catch(() => {});
  const tracks = [];
  const addTrack = (name, stream) => {
    const up = new TrackUploader(meeting.id, name, ext);
    const rec = new MediaRecorder(stream, { ...(mime ? { mimeType: mime } : {}), audioBitsPerSecond: 32000 });
    rec.ondataavailable = (ev) => { if (ev.data && ev.data.size) up.push(ev.data); };
    const an = ctx.createAnalyser(); an.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(an);
    rec.start(5000);
    tracks.push({ name, rec, up, an, stream });
  };
  if (micStream) addTrack("mic", micStream);
  if (tabStream) addTrack("tab", tabStream);

  let wake = null;
  try { wake = await navigator.wakeLock?.request("screen"); } catch { /* not allowed: fine */ }
  recording = {
    id: meeting.id, started: Date.now(), tracks, ctx, display, wake,
    online: true,
    setNetwork(ok) { this.online = ok; const n = $("#rec-net"); if (n) n.hidden = ok; },
  };
  if (tabStream) tabStream.getAudioTracks()[0].addEventListener("ended", () => toast("Tab sharing stopped — still recording your microphone.", "bad"));
  window.onbeforeunload = () => "Recording is in progress";
  location.hash = `#/m/${meeting.id}`;
}

async function stopRecording() {
  const r = recording;
  if (!r) return;
  const btn = $("#rec-stop");
  if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
  await Promise.all(r.tracks.map((t) => new Promise((res) => {
    if (t.rec.state === "inactive") return res();
    t.rec.addEventListener("stop", res, { once: true });
    t.rec.stop();
  })));
  r.tracks.forEach((t) => t.stream.getTracks().forEach((x) => x.stop()));
  if (r.display) r.display.getTracks().forEach((x) => x.stop());
  await new Promise((res) => setTimeout(res, 50)); // let the last dataavailable queue
  await Promise.all(r.tracks.map((t) => t.up.chain));
  r.ctx.close();
  try { await r.wake?.release(); } catch { /* ignore */ }
  recording = null;
  window.onbeforeunload = null;
  await api(`/api/meetings/${r.id}/finish`, { method: "POST" });
  openMeeting(r.id);
}

function recordingPanel() {
  const r = recording;
  return `
    <section class="rec">
      <div class="rec-dot"></div>
      <div class="rec-time" id="rec-time">00:00</div>
      <div class="meters">${r.tracks.map((t) => `
        <div class="meter"><span>${t.name === "mic" ? "Your microphone" : "Call audio (tab)"}</span><div class="bar"><i id="lvl-${t.name}"></i></div></div>`).join("")}
      </div>
      <p class="muted">${phone() ? "Keep this screen open — a locked phone stops recording." : "Keep this tab open."} Audio is saved to your computer every few seconds.</p>
      <p class="warn" id="rec-net" hidden>Connection lost — the recording continues and will catch up.</p>
      <button class="btn danger big" id="rec-stop">■ Stop and make notes</button>
    </section>`;
}
function tickRecording() {
  const r = recording;
  if (!r || current !== r.id) return;
  const t = $("#rec-time");
  if (t) t.textContent = fmtTs((Date.now() - r.started) / 1000);
  const buf = new Uint8Array(256);
  for (const tr of r.tracks) {
    tr.an.getByteTimeDomainData(buf);
    let peak = 0;
    for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
    const el = $(`#lvl-${tr.name}`);
    if (el) el.style.width = `${Math.min(100, (peak / 128) * 160)}%`;
  }
  requestAnimationFrame(tickRecording);
}

// ---- upload ----------------------------------------------------------------

let pendingFile = null;
const pickFile = () => $("#file-input").click();
$("#btn-upload").onclick = pickFile;
$("#btn-upload-phone").onclick = pickFile;
$("#file-input").onchange = (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (f) openUploadDialog(f);
};
function openUploadDialog(file, title = "") {
  pendingFile = file;
  const dlg = $("#dlg-upload");
  dlg.querySelector("form").reset();
  $("#upload-name").textContent = `${file.name} · ${(file.size / 1048576).toFixed(1)} MB`;
  dlg.querySelector('[name="title"]').value = title || file.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ");
  $("#up-template").value = STATE.settings.template;
  dlg.querySelector('[name="language"]').value = STATE.settings.language;
  dlg.showModal();
}
$("#dlg-upload").addEventListener("close", async () => {
  const dlg = $("#dlg-upload");
  if (dlg.returnValue !== "ok" || !pendingFile) return;
  const f = new FormData(dlg.querySelector("form"));
  const file = pendingFile;
  pendingFile = null;
  try {
    const m = await api("/api/meetings", { method: "POST", body: JSON.stringify({ title: f.get("title"), source: "upload", template: f.get("template"), language: f.get("language"), tz_offset: -new Date().getTimezoneOffset() }) });
    location.hash = `#/m/${m.id}`;
    const ext = ((file.name.match(/\.([a-z0-9]{1,5})$/i) || [])[1] || "bin").toLowerCase();
    let offset = 0;
    while (offset < file.size) {
      const piece = file.slice(offset, offset + PIECE);
      const r = await api(`/api/meetings/${m.id}/tracks/upload?ext=${ext}&offset=${offset}`, { method: "PUT", body: piece });
      offset = r.bytes;
      const bar = $("#up-progress");
      if (bar) bar.style.width = `${Math.round((offset / file.size) * 100)}%`;
      const pct = $("#up-pct");
      if (pct) pct.textContent = `Uploading… ${Math.round((offset / file.size) * 100)}%`;
    }
    await api(`/api/meetings/${m.id}/finish`, { method: "POST" });
    openMeeting(m.id);
  } catch (e) { toast(`Upload failed: ${e.message}`, "bad"); }
});

// Android share sheet → the service worker put the file in a cache.
async function takeSharedFile() {
  try {
    const cache = await caches.open("nt-share");
    const res = await cache.match("/shared/file");
    if (!res) { showHome(); return; }
    await cache.delete("/shared/file");
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-File-Name") || "recording");
    const file = new File([blob], name, { type: blob.type });
    history.replaceState(null, "", "#/");
    showHome();
    openUploadDialog(file, decodeURIComponent(res.headers.get("X-Title") || ""));
  } catch (e) { showHome(); toast("Could not read the shared file.", "bad"); }
}

// ---- meeting view ----------------------------------------------------------

let tab = "notes";
let seekOnOpen = null;

async function openMeeting(id, at = null) {
  if (current !== id) tab = "notes";
  current = id; view = "meetings";
  clearInterval(pollTimer);
  setView("detail");
  if (at !== null) { seekOnOpen = at; tab = "transcript"; }
  await renderMeeting(true);
  loadList();
}

function progressSteps(m) {
  const steps = [["transcribing", "Transcript"], ["summarizing", "Notes"]];
  const idx = m.status === "summarizing" ? 1 : 0;
  return `<ol class="steps">${steps.map(([, label], i) => `<li class="${i < idx ? "done" : i === idx ? "now" : ""}">${label}</li>`).join("")}</ol>`;
}

async function renderMeeting(first = false) {
  const id = current;
  let m;
  try { m = await api(`/api/meetings/${id}`); }
  catch (e) { $("#main").innerHTML = `<section class="home"><a href="#/" class="back">‹ Meetings</a><h1>Not found</h1><p>${esc(e.message)}</p></section>`; return; }
  if (id !== current) return;
  lastMeeting = m;
  const busy = BUSY.includes(m.status);
  const live = recording && recording.id === id;
  if (!first && busy && document.querySelector(".meeting")) {
    const p = $("#progress-text"); if (p) p.textContent = m.progress || m.status;
    return;
  }
  const src = { browser: "recorded in the browser", upload: "uploaded", telegram: "from Telegram" }[m.source] || m.source;
  const open = (m.actions || []).filter((a) => !a.done).length;
  $("#main").innerHTML = `
    <article class="meeting">
      <a href="#/" class="back only-phone">‹ Meetings</a>
      <header class="m-head">
        <input class="m-title" id="m-title" value="${esc(m.title)}" placeholder="${busy || live ? "New meeting" : "Untitled meeting"}" aria-label="Title">
        <div class="m-meta">${fmtDate(m.created_at)}${m.duration ? ` · ${fmtTs(m.duration)}` : ""} · ${src}${m.segments.length && open ? ` · <b>${plural(open, "action item")}</b>` : ""}</div>
      </header>
      ${live ? recordingPanel() : ""}
      ${m.status === "uploading" && !live ? `<section class="progress"><p id="up-pct">Uploading…</p><div class="bar"><i id="up-progress"></i></div></section>` : ""}
      ${busy && m.status !== "uploading" ? `<section class="progress"><div class="spinner"></div><div><p id="progress-text">${esc(m.progress || "Working…")}</p>${progressSteps(m)}</div></section>` : ""}
      ${m.status === "error" ? `<section class="error-box"><b>Something went wrong.</b> <span>${esc(m.error)}</span> <button class="btn" id="btn-retry">Retry</button></section>` : ""}
      ${m.has_audio ? `<audio controls preload="metadata" id="player" src="/api/meetings/${id}/audio"></audio>` : ""}
      ${m.segments.length ? `
      <nav class="tabs">
        <button data-tab="notes" class="${tab === "notes" ? "on" : ""}">Notes</button>
        <button data-tab="transcript" class="${tab === "transcript" ? "on" : ""}">Transcript</button>
        <button data-tab="ask" class="${tab === "ask" ? "on" : ""}">Ask</button>
        <span class="grow"></span>
        <button class="link" id="btn-share">${m.share && m.share.token ? "Shared ✓" : "Share"}</button>
        <div class="menu-wrap">
          <button class="icon-btn" id="btn-more" aria-label="More" title="More">⋯</button>
          <div class="menu" id="menu" hidden>
            <a href="/api/meetings/${id}/export?what=notes" download>Download notes (.md)</a>
            <a href="/api/meetings/${id}/export?what=all" download>Download notes + transcript</a>
            <button data-act="copy-text">Copy notes as text</button>
            ${STATE.inbox ? `<button data-act="send-inbox">Send to Uno Work Inbox</button>` : ""}
            ${STATE.telegram.paired ? `<button data-act="send-telegram">Send to Telegram</button>` : ""}
            <hr>
            <button data-act="delete" class="danger">Delete meeting…</button>
          </div>
        </div>
      </nav>
      <div class="tab-body" id="tab-body"></div>` : ""}
      ${!m.segments.length && !busy && !live && m.status !== "error" ? `<p class="muted">Nothing here yet.</p>` : ""}
    </article>`;
  $("#m-title").onchange = async (e) => { await api(`/api/meetings/${id}`, { method: "PUT", body: JSON.stringify({ title: e.target.value }) }); loadList(); };
  if ($("#btn-retry")) $("#btn-retry").onclick = async () => { await api(`/api/meetings/${id}/retry`, { method: "POST" }); openMeeting(id); };
  if (live) { $("#rec-stop").onclick = stopRecording; requestAnimationFrame(tickRecording); }
  if (m.segments.length) {
    $$(".tabs [data-tab]").forEach((b) => b.onclick = () => { tab = b.dataset.tab; renderMeeting(true); });
    $("#btn-share").onclick = () => shareDialog(m);
    bindMenu(m);
    renderTab(m);
    bindPlayerFollow();
    if (seekOnOpen !== null) { const at = seekOnOpen; seekOnOpen = null; setTimeout(() => focusLine(at, true), 50); }
  }
  if (busy) {
    pollTimer = setInterval(async () => {
      const s = await api(`/api/meetings/${id}`).catch(() => null);
      if (!s || id !== current) return;
      const p = $("#progress-text"); if (p) p.textContent = s.progress || s.status;
      if (s.status !== m.status || !BUSY.includes(s.status)) { clearInterval(pollTimer); renderMeeting(true); loadList(); }
    }, 2000);
  }
}

function bindMenu(m) {
  const menu = $("#menu");
  $("#btn-more").onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; };
  document.addEventListener("click", () => { if (menu) menu.hidden = true; }, { once: true });
  $$("[data-act]", menu).forEach((b) => b.onclick = async () => {
    menu.hidden = true;
    const act = b.dataset.act;
    try {
      if (act === "delete") {
        if (!confirm("Delete this meeting, its recording and notes from the computer?")) return;
        await api(`/api/meetings/${m.id}`, { method: "DELETE" });
        location.hash = "#/";
      } else if (act === "copy-text") {
        await navigator.clipboard.writeText(plainNotes(m.notes)); toast("Notes copied — paste into an email or a chat");
      } else if (act === "send-inbox") {
        await api(`/api/meetings/${m.id}/send?to=inbox`, { method: "POST" }); toast("Sent to the Uno Work Inbox");
      } else if (act === "send-telegram") {
        await api(`/api/meetings/${m.id}/send?to=telegram`, { method: "POST" }); toast("Sent to Telegram");
      }
    } catch (e) { toast(e.message, "bad"); }
  });
}
function plainNotes(md) {
  return String(md || "").split("\n").map((l) => l
    .replace(/^#{1,4}\s+/, "").replace(/\*\*/g, "")
    .replace(/^\s*[-*]\s+\[ \]\s+/, "☐ ").replace(/^\s*[-*]\s+\[[xX]\]\s+/, "☑ ")
    .replace(/^\s*[-*]\s+/, "• ").replace(/\s*\[\d{1,2}:\d{2}(?::\d{2})?\]/g, "")).join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

function seek(sec, play = true) {
  const p = $("#player");
  if (p) { p.currentTime = sec; if (play) p.play().catch(() => {}); }
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a.ts");
  if (!a || !a.dataset.ts) return;
  if (a.getAttribute("href") !== "#" && !a.closest(".meeting")) return; // cross-meeting link: follow it
  e.preventDefault();
  const sec = tsToSec(a.dataset.ts);
  seek(sec);
  if (tab === "transcript") focusLine(sec);
});
function focusLine(sec, scroll = true) {
  const lines = $$(".transcript .line");
  let best = null;
  for (const l of lines) { if (Number(l.dataset.start) <= sec + 0.5) best = l; else break; }
  lines.forEach((l) => l.classList.toggle("now", l === best));
  if (best && scroll) best.scrollIntoView({ block: "center", behavior: "smooth" });
  if (seekOnOpen === null && scroll) seek(sec, false);
}
function bindPlayerFollow() {
  const p = $("#player");
  if (!p) return;
  let last = -1;
  p.ontimeupdate = () => {
    if (tab !== "transcript") return;
    const t = Math.floor(p.currentTime);
    if (t !== last) { last = t; focusLine(p.currentTime, false); }
  };
}

function renderTab(m) {
  const body = $("#tab-body");
  const tpl = Object.entries(STATE.templates).map(([k, v]) => `<option value="${k}" ${k === m.template ? "selected" : ""}>${esc(v)}</option>`).join("");
  if (tab === "notes") {
    const acts = m.actions || [];
    const d = m.delivery || {};
    const sent = [d.inbox === "sent" ? "Inbox" : "", d.telegram === "sent" ? "Telegram" : ""].filter(Boolean);
    body.innerHTML = `
      ${acts.length ? `<section class="card-box">
        <h2>Action items <span class="muted">${acts.filter((a) => !a.done).length} open</span></h2>
        ${acts.map((a) => actionRow(a, m.id, m.handed || {})).join("")}
      </section>` : ""}
      <div class="notes md" id="notes">${m.notes ? renderMd(m.notes, { skipTitle: m.title, skipSection: acts.length ? ACTION_SECTION : null }) : `<p class="muted">Notes are not written yet.</p>`}</div>
      <div class="notes-tools">
        <select id="n-template" aria-label="Kind of meeting">${tpl}</select>
        <button class="btn" id="n-regen">Rewrite notes</button>
        <span class="grow"></span>
        <button class="link" id="n-copy">Copy Markdown</button>
        <button class="link" id="n-edit">Edit</button>
      </div>
      <p class="fine">${sent.length ? `Sent to ${sent.join(" and ")} ✓ · ` : ""}${m.usage && m.usage.model ? `Notes by ${esc(m.usage.model)} · transcript: ${esc(m.usage.stt === "local" ? "Whisper on this computer" : m.usage.stt === "local-fallback" ? "Whisper on this computer (Uno AI speech-to-text was not available)" : "Whisper via " + (m.usage.stt === "uno" ? "Uno AI" : "your provider"))} · ` : ""}<span title="Folder on this computer">${esc(m.path)}</span></p>`;
    bindActionRows(body, () => renderMeeting(true));
    $("#n-regen").onclick = async () => {
      await api(`/api/meetings/${m.id}/regenerate`, { method: "POST", body: JSON.stringify({ template: $("#n-template").value }) });
      openMeeting(m.id);
    };
    $("#n-copy").onclick = () => { navigator.clipboard.writeText(m.notes); toast("Notes copied as Markdown"); };
    $("#n-edit").onclick = () => {
      $("#notes").outerHTML = `<textarea class="notes-edit" id="notes-edit">${esc(m.notes)}</textarea><div class="dlg-actions"><button class="btn" id="n-cancel">Cancel</button><button class="btn primary" id="n-save">Save</button></div>`;
      $("#n-cancel").onclick = () => renderMeeting(true);
      $("#n-save").onclick = async () => {
        await api(`/api/meetings/${m.id}`, { method: "PUT", body: JSON.stringify({ notes: $("#notes-edit").value }) });
        toast("Saved to notes.md"); renderMeeting(true); loadList();
      };
    };
  } else if (tab === "transcript") {
    const guessed = m.segments.some((s) => s.guessed);
    const tt = m.talk_time || [];
    let prev = null;
    body.innerHTML = `
      ${tt.length ? `<div class="speakers">${tt.map((x) => `<button class="spk-chip" data-spk="${esc(x.speaker)}" title="Rename">
        <i style="background:${spkColor(x.speaker)}"></i>${esc(x.speaker === "Me" ? "You" : x.speaker)} <span>${Math.round(x.share * 100)}%</span></button>`).join("")}
        <span class="fine">${guessed ? "Told apart by the AI from what was said — " : ""}tap a name to fix it</span></div>` : ""}
      <div class="transcript">${m.segments.map((s) => {
        const same = s.speaker === prev; prev = s.speaker;
        return `<p class="line ${same ? "cont" : ""}" data-start="${s.start}"><a href="#" class="ts" data-ts="${fmtTs(s.start)}">${fmtTs(s.start)}</a><span class="said">${s.speaker && !same ? `<b class="spk" style="color:${spkColor(s.speaker)}">${esc(s.speaker === "Me" ? "You" : s.speaker)}</b>` : ""}${esc(s.text)}</span></p>`;
      }).join("")}</div>`;
    $$(".spk-chip", body).forEach((b) => b.onclick = () => renameSpeaker(m, b.dataset.spk));
  } else {
    const chat = m.chat || [];
    body.innerHTML = `
      <div class="chat" id="chat">${chat.length ? chat.map(chatMsg).join("") : `<div class="suggest">${["What did we promise the client?", "Who owns what, by when?", "Write a follow-up email"].map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>`}</div>
      <form class="ask" id="ask-form"><input id="ask-q" placeholder="Ask about this meeting…" autocomplete="off" enterkeyhint="send"><button class="btn primary">Ask</button></form>`;
    const ask = async (q) => {
      const c = $("#chat");
      if (!chat.length) c.innerHTML = "";
      c.insertAdjacentHTML("beforeend", chatMsg({ role: "user", content: q }) + `<div class="msg ai pending" id="pending">Thinking…</div>`);
      try {
        const r = await api(`/api/meetings/${m.id}/ask`, { method: "POST", body: JSON.stringify({ question: q }) });
        $("#pending").outerHTML = chatMsg({ role: "assistant", content: r.answer });
        m.chat = r.chat; chat.push(1);
      } catch (err) { $("#pending").outerHTML = `<div class="msg ai err">${esc(err.message)}</div>`; }
    };
    $$(".chip[data-q]", body).forEach((b) => b.onclick = () => ask(b.dataset.q));
    $("#ask-form").onsubmit = (e) => {
      e.preventDefault();
      const q = $("#ask-q").value.trim();
      if (!q) return;
      $("#ask-q").value = "";
      ask(q);
    };
  }
}
const SPK_COLORS = ["#2f5bea", "#8a4bd6", "#1f8a4c", "#c2562f", "#a8327a", "#2a7f96"];
function spkColor(name) {
  if (name === "Me") return SPK_COLORS[0];
  if (name === "Others") return SPK_COLORS[1];
  let h = 0; for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return SPK_COLORS[h % SPK_COLORS.length];
}
const chatMsg = (x) => `<div class="msg ${x.role === "user" ? "me" : "ai md"}">${x.role === "user" ? esc(x.content) : renderMd(x.content)}</div>`;

let renaming = null;
function renameSpeaker(m, from) {
  renaming = { mid: m.id, from };
  $("#speaker-to").value = from === "Me" ? "" : from;
  $("#dlg-speaker").showModal();
}
$("#dlg-speaker").addEventListener("close", async () => {
  if ($("#dlg-speaker").returnValue !== "ok" || !renaming) return;
  const to = $("#speaker-to").value.trim();
  try { await api(`/api/meetings/${renaming.mid}/speakers`, { method: "POST", body: JSON.stringify({ from: renaming.from, to }) }); renderMeeting(true); }
  catch (e) { toast(e.message, "bad"); }
});

// ---- share -------------------------------------------------------------------

function shareDialog(m) {
  const dlg = $("#dlg-share");
  const shared = m.share && m.share.token;
  const url = shared ? `${STATE.app_url || location.origin}/s/${m.share.token}` : "";
  $("#share-url-row").hidden = !shared;
  $("#share-url").value = url;
  $("#share-transcript").checked = shared ? !!m.share.transcript : false;
  $("#share-transcript").disabled = !!shared;
  $("#share-stop").hidden = !shared;
  $("#share-create").hidden = !!shared;
  $("#share-create").onclick = async () => {
    const r = await api(`/api/meetings/${m.id}/share`, { method: "POST", body: JSON.stringify({ transcript: $("#share-transcript").checked }) });
    const u = r.url.startsWith("http") ? r.url : location.origin + r.url;
    m.share = { token: r.token, transcript: $("#share-transcript").checked };
    $("#share-url").value = u; $("#share-url-row").hidden = false; $("#share-create").hidden = true; $("#share-stop").hidden = false; $("#share-transcript").disabled = true;
    try { await navigator.clipboard.writeText(u); toast("Link copied"); } catch { /* clipboard blocked */ }
  };
  $("#share-copy").onclick = async () => {
    if (navigator.share && phone()) { navigator.share({ title: m.title || "Meeting notes", url: $("#share-url").value }).catch(() => {}); return; }
    await navigator.clipboard.writeText($("#share-url").value); toast("Link copied");
  };
  $("#share-stop").onclick = async () => {
    await api(`/api/meetings/${m.id}/share`, { method: "DELETE" });
    dlg.close(); toast("The link no longer works"); renderMeeting(true);
  };
  dlg.onclose = () => { if (m.id === current) renderMeeting(true); };
  dlg.showModal();
}

// ---- settings --------------------------------------------------------------

function openSettings() {
  const f = $("#settings-form");
  const s = STATE.settings;
  for (const [k, v] of Object.entries(s)) {
    const els = f.querySelectorAll(`[name="${k}"]`);
    els.forEach((el) => {
      if (el.type === "radio") el.checked = el.value === v;
      else if (el.type === "checkbox") el.checked = !!v;
      else el.value = v ?? "";
    });
  }
  const inbox = f.querySelector('[name="notify_inbox"]');
  inbox.disabled = !STATE.inbox;
  $("#inbox-note").textContent = STATE.inbox ? "— the bell in Uno Work: “Notes ready · 3 action items”." : "— needs Uno Work on this computer.";
  const p = STATE.provider;
  $("#uno-provider-note").textContent = p.name === "uno" && p.ready
    ? (p.route === "app"
      ? "Works out of the box — uses the AI of this computer. How much this app may spend is set in Uno Work → Settings → Apps."
      : `Works out of the box — using ${p.source}. Usage is billed to your Uno AI balance.`)
    : "Works out of the box on a Uno computer — no keys. Usage is billed to your Uno AI balance.";
  $("#ai-route").textContent = p.ready ? `Now in use: ${p.route_label} · notes model ${p.model}` : "Now in use: nothing — the AI is not set up";
  f.querySelector('[name="model"]').placeholder = p.route === "app" ? "default — this computer's choice" : "deepseek/deepseek-v3.2";
  $("#test-out").innerHTML = "";
  syncCustom();
  renderTelegram(STATE.telegram);
  $("#dlg-settings").showModal();
}
function syncCustom() {
  const f = $("#settings-form");
  f.querySelector(".custom-fields").hidden = f.querySelector('[name="provider"]:checked')?.value !== "custom";
}

let tgPoll = null;
function renderTelegram(t) {
  const box = $("#tg-box");
  clearInterval(tgPoll);
  if (t.paired) {
    box.innerHTML = `<p>Connected: <b>@${esc(t.bot)}</b> talks to <b>${esc(t.chat)}</b>. Send it voice messages and recordings; notes come back there. <span class="muted">/todo, /last, /find</span></p>
      <div class="row-btns"><button type="button" class="btn" id="tg-test">Send a test message</button><button type="button" class="link danger" id="tg-off">Disconnect</button></div>`;
    $("#tg-test").onclick = async () => { try { await api("/api/telegram/test", { method: "POST" }); toast("Sent — check Telegram"); } catch (e) { toast(e.message, "bad"); } };
  } else if (t.connected) {
    box.innerHTML = t.link ? `<p>Bot <b>@${esc(t.bot)}</b> is ready. <a class="btn primary" href="${esc(t.link)}" target="_blank" rel="noopener">Open in Telegram and press Start</a></p>
      <p class="fine">Or send it <code>/start ${esc(t.code)}</code>. Waiting for your message…</p>`
      : `<p>Bot <b>@${esc(t.bot)}</b> — the pairing link expired.</p><div class="row-btns"><button type="button" class="btn" id="tg-again">New link</button></div>`;
    box.insertAdjacentHTML("beforeend", `<div class="row-btns"><button type="button" class="link danger" id="tg-off">Disconnect</button></div>`);
    if ($("#tg-again")) $("#tg-again").onclick = () => connectTelegram("•");
    tgPoll = setInterval(async () => {
      if (!$("#dlg-settings").open) { clearInterval(tgPoll); return; }
      const s = await api("/api/telegram").catch(() => null);
      if (s && s.paired) { STATE.telegram = s; renderTelegram(s); toast("Telegram connected"); }
    }, 2500);
  } else {
    box.innerHTML = `<p class="fine">Get meeting notes in Telegram and send recordings from your phone. In Telegram, open <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a>, send <code>/newbot</code>, pick any name, and paste the token here. The bot is yours — notes don't pass through anyone else.</p>
      <div class="row-btns"><input id="tg-token" placeholder="123456789:AA…" autocomplete="off"><button type="button" class="btn" id="tg-connect">Connect</button></div>`;
    $("#tg-connect").onclick = () => connectTelegram($("#tg-token").value);
  }
  if ($("#tg-off")) $("#tg-off").onclick = async () => { STATE.telegram = await api("/api/telegram", { method: "DELETE" }); renderTelegram(STATE.telegram); };
}
async function connectTelegram(token) {
  try { STATE.telegram = await api("/api/telegram/connect", { method: "POST", body: JSON.stringify({ token }) }); renderTelegram(STATE.telegram); }
  catch (e) { toast(e.message, "bad"); }
}

$("#settings-form").addEventListener("change", syncCustom);
$("#btn-settings").onclick = openSettings;
$("#btn-settings-top").onclick = openSettings;
async function saveSettings() {
  const f = $("#settings-form");
  const body = Object.fromEntries(new FormData(f).entries());
  for (const cb of $$('input[type="checkbox"][name]', f)) body[cb.name] = cb.checked;
  delete body.consent;
  await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
  await loadState();
}
$("#dlg-settings").addEventListener("close", async () => {
  clearInterval(tgPoll);
  if ($("#dlg-settings").returnValue === "ok") {
    try { await saveSettings(); toast("Settings saved"); } catch (e) { toast(e.message, "bad"); }
  } else { loadState().catch(() => {}); }
});
$("#btn-test").onclick = async () => {
  const out = $("#test-out");
  out.innerHTML = `<p class="muted">Checking…</p>`;
  try {
    await saveSettings();
    const r = await api("/api/settings/test", { method: "POST" });
    const line = (label, x) => `<p class="${x.ok ? "ok" : "bad"}">${x.ok ? "✓" : "✗"} ${label}: ${esc(x.detail)}</p>`;
    out.innerHTML = line("Notes model", r.notes) + line("Speech-to-text", r.stt);
  } catch (e) { out.innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
};

// ---- routing ---------------------------------------------------------------

$("#btn-record").onclick = openRecordDialog;
$("#btn-record-phone").onclick = openRecordDialog;
let searchTimer;
$("#search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadList, 200); };

function route() {
  const h = location.hash;
  let m;
  if ((m = h.match(/^#\/m\/([a-z0-9]{10})(?:\/t\/(\d+))?$/))) openMeeting(m[1], m[2] !== undefined ? Number(m[2]) : null);
  else if (h === "#/actions") showActions();
  else if (h === "#/share") takeSharedFile();
  else if (h === "#/share-missed") { history.replaceState(null, "", "#/"); showHome(); toast("Open Notetaker once from the Home Screen, then share the recording again.", "bad"); }
  else showHome();
}
window.addEventListener("hashchange", route);

(async () => {
  await loadState();
  route();
  if ("serviceWorker" in navigator && isSecureContext) navigator.serviceWorker.register("/sw.js").catch(() => {});
  setInterval(() => { if (!recording && document.visibilityState === "visible") loadList(); }, 15000);
})();
