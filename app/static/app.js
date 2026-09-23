"use strict";
// Uno Notetaker — single-page UI, no build step.

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PIECE = 8 * 1024 * 1024;

let STATE = null;
let current = null;      // meeting id on screen
let pollTimer = null;
let recording = null;    // active recording session

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
  return isNaN(d) ? "" : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

// Minimal, safe Markdown: headings, lists, checkboxes, bold/italic/code, http links.
function renderMd(src) {
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
    .replace(/\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g, '<a href="#" class="ts" data-ts="$1">[$1]</a>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of String(src || "").split("\n")) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { close(); const n = Math.min(m[1].length + 1, 4); out.push(`<h${n}>${inline(m[2])}</h${n}>`); }
    else if ((m = line.match(/^\s*[-*]\s+\[( |x|X)\]\s+(.*)$/))) { if (list !== "ul") { close(); out.push('<ul class="tasks">'); list = "ul"; } out.push(`<li><span class="box">${m[1].trim() ? "☑" : "☐"}</span> ${inline(m[2])}</li>`); }
    else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { if (list !== "ul") { close(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { if (list !== "ol") { close(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(m[1])}</li>`); }
    else if (!line.trim()) { close(); }
    else { close(); out.push(`<p>${inline(line)}</p>`); }
  }
  close();
  return out.join("\n");
}
const tsToSec = (t) => t.split(":").map(Number).reduce((a, b) => a * 60 + b, 0);

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

async function loadList() {
  const q = $("#search").value.trim();
  const { meetings } = await api(`/api/meetings${q ? `?q=${encodeURIComponent(q)}` : ""}`);
  const list = $("#list");
  if (!meetings.length) {
    list.innerHTML = `<p class="empty-list">${q ? "Nothing found." : "No meetings yet."}</p>`;
    return meetings;
  }
  list.innerHTML = meetings.map((m) => `
    <a href="#/m/${m.id}" class="item ${m.id === current ? "active" : ""}" data-id="${m.id}">
      <span class="t">${esc(m.title || "Untitled meeting")}</span>
      <span class="s">${fmtDate(m.created_at)}${m.duration ? " · " + fmtTs(m.duration) : ""}${statusBadge(m.status)}</span>
      ${m.snippet ? `<span class="snip">…${esc(m.snippet)}…</span>` : ""}
    </a>`).join("");
  return meetings;
}
function statusBadge(s) {
  const map = { recording: "● recording", uploading: "uploading", queued: "in line", transcribing: "transcribing", summarizing: "writing notes", error: "⚠ error" };
  return map[s] ? ` · <em class="st st-${s}">${map[s]}</em>` : "";
}

// ---- home ------------------------------------------------------------------

function showHome() {
  current = null;
  clearInterval(pollTimer);
  const p = STATE.provider;
  $("#main").innerHTML = `
    <section class="home">
      <h1>Meeting notes, done for you</h1>
      <p class="lead">Record a call or upload a file. You get a transcript with timestamps, a summary, decisions and action items — saved in <code>~/Meetings</code> on this computer.</p>
      <div class="cards">
        <button class="card" id="home-record"><span class="ic">●</span><b>Record a meeting</b><span>A call in another tab (Meet, Zoom, Teams) or a meeting in the room.</span></button>
        <button class="card" id="home-upload"><span class="ic">⬆</span><b>Upload a recording</b><span>Audio or video: m4a, mp3, wav, mp4, webm…</span></button>
      </div>
      ${p.ready ? "" : `<div class="warn">The AI is not set up yet. <button class="link" id="home-settings">Open Settings</button></div>`}
      <p class="fine">Recording other people? Tell them first — in many places it's required by law.</p>
    </section>`;
  $("#home-record").onclick = openRecordDialog;
  $("#home-upload").onclick = () => $("#file-input").click();
  if (!p.ready) $("#home-settings").onclick = openSettings;
  loadList();
}

// ---- recording -------------------------------------------------------------

function openRecordDialog() {
  if (recording) { toast("A recording is already running"); return; }
  const dlg = $("#dlg-record");
  dlg.querySelector("form").reset();
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

class TrackUploader {
  constructor(mid, track) { this.mid = mid; this.track = track; this.offset = 0; this.chain = Promise.resolve(); this.failed = 0; }
  push(blob) {
    this.chain = this.chain.then(() => this.send(blob));
    return this.chain;
  }
  async send(blob) {
    const buf = await blob.arrayBuffer();
    for (let attempt = 0; ; attempt++) {
      try {
        const r = await api(`/api/meetings/${this.mid}/tracks/${this.track}?ext=webm&offset=${this.offset}`, { method: "PUT", body: buf });
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
  if (!window.MediaRecorder || !navigator.mediaDevices) throw new Error("This browser can't record. Use Chrome, Edge or Firefox.");
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
  const meeting = await api("/api/meetings", { method: "POST", body: JSON.stringify({ title, source: "browser", template, language }) });
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/webm";
  const ctx = new AudioContext();
  ctx.resume().catch(() => {});
  const tracks = [];
  const addTrack = (name, stream) => {
    const up = new TrackUploader(meeting.id, name);
    const rec = new MediaRecorder(stream, { mimeType: mime, audioBitsPerSecond: 32000 });
    rec.ondataavailable = (ev) => { if (ev.data && ev.data.size) up.push(ev.data); };
    const an = ctx.createAnalyser(); an.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(an);
    rec.start(5000);
    tracks.push({ name, rec, up, an, stream });
  };
  if (micStream) addTrack("mic", micStream);
  if (tabStream) addTrack("tab", tabStream);

  recording = {
    id: meeting.id, started: Date.now(), tracks, ctx, display,
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
      <p class="muted">Keep this tab open. Audio is saved to your computer every few seconds.</p>
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
$("#btn-upload").onclick = () => $("#file-input").click();
$("#file-input").onchange = (e) => {
  pendingFile = e.target.files[0];
  e.target.value = "";
  if (!pendingFile) return;
  const dlg = $("#dlg-upload");
  dlg.querySelector("form").reset();
  $("#upload-name").textContent = `${pendingFile.name} · ${(pendingFile.size / 1048576).toFixed(1)} MB`;
  dlg.querySelector('[name="title"]').value = pendingFile.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ");
  $("#up-template").value = STATE.settings.template;
  dlg.querySelector('[name="language"]').value = STATE.settings.language;
  dlg.showModal();
};
$("#dlg-upload").addEventListener("close", async () => {
  const dlg = $("#dlg-upload");
  if (dlg.returnValue !== "ok" || !pendingFile) return;
  const f = new FormData(dlg.querySelector("form"));
  const file = pendingFile;
  pendingFile = null;
  try {
    const m = await api("/api/meetings", { method: "POST", body: JSON.stringify({ title: f.get("title"), source: "upload", template: f.get("template"), language: f.get("language") }) });
    location.hash = `#/m/${m.id}`;
    const ext = ((file.name.match(/\.([a-z0-9]{1,5})$/i) || [])[1] || "bin").toLowerCase();
    let offset = 0;
    while (offset < file.size) {
      const piece = file.slice(offset, offset + PIECE);
      const r = await api(`/api/meetings/${m.id}/tracks/upload?ext=${ext}&offset=${offset}`, { method: "PUT", body: piece });
      offset = r.bytes;
      const bar = $("#up-progress");
      if (bar) bar.style.width = `${Math.round((offset / file.size) * 100)}%`;
    }
    await api(`/api/meetings/${m.id}/finish`, { method: "POST" });
    openMeeting(m.id);
  } catch (e) { toast(`Upload failed: ${e.message}`, "bad"); }
});

// ---- meeting view ----------------------------------------------------------

let tab = "notes";

async function openMeeting(id) {
  current = id;
  clearInterval(pollTimer);
  await renderMeeting(true);
  loadList();
}

async function renderMeeting(first = false) {
  const id = current;
  let m;
  try { m = await api(`/api/meetings/${id}`); }
  catch (e) { $("#main").innerHTML = `<section class="home"><h1>Not found</h1><p>${esc(e.message)}</p></section>`; return; }
  if (id !== current) return;
  const busy = ["queued", "transcribing", "summarizing", "uploading"].includes(m.status);
  const live = recording && recording.id === id;
  if (!first && busy && document.querySelector(".meeting")) {
    const p = $("#progress-text"); if (p) p.textContent = m.progress || m.status;
    return;
  }
  const tpl = Object.entries(STATE.templates).map(([k, v]) => `<option value="${k}" ${k === m.template ? "selected" : ""}>${esc(v)}</option>`).join("");
  $("#main").innerHTML = `
    <article class="meeting">
      <header class="m-head">
        <input class="m-title" id="m-title" value="${esc(m.title)}" placeholder="Untitled meeting">
        <div class="m-meta">${fmtDate(m.created_at)}${m.duration ? ` · ${fmtTs(m.duration)}` : ""} · ${m.source === "browser" ? "recorded in the browser" : "uploaded"} · <span title="Folder on this computer">📁 ${esc(m.path)}</span></div>
      </header>
      ${live ? recordingPanel() : ""}
      ${m.status === "uploading" && !live ? `<section class="progress"><p>Uploading…</p><div class="bar"><i id="up-progress"></i></div></section>` : ""}
      ${busy && m.status !== "uploading" ? `<section class="progress"><div class="spinner"></div><p id="progress-text">${esc(m.progress || "Working…")}</p></section>` : ""}
      ${m.status === "error" ? `<section class="error-box"><b>Something went wrong.</b> ${esc(m.error)} <button class="btn" id="btn-retry">Retry</button></section>` : ""}
      ${m.has_audio ? `<audio controls preload="metadata" id="player" src="/api/meetings/${id}/audio"></audio>` : ""}
      ${m.segments.length ? `
      <nav class="tabs">
        <button data-tab="notes" class="${tab === "notes" ? "on" : ""}">Notes</button>
        <button data-tab="transcript" class="${tab === "transcript" ? "on" : ""}">Transcript</button>
        <button data-tab="ask" class="${tab === "ask" ? "on" : ""}">Ask about this meeting</button>
        <span class="grow"></span>
        <button class="link" id="btn-share">🔗 Share</button>
        <button class="link danger" id="btn-delete">Delete</button>
      </nav>
      <div class="tab-body" id="tab-body"></div>` : ""}
      ${!m.segments.length && !busy && !live && m.status !== "error" ? `<p class="muted">Nothing here yet.</p>` : ""}
    </article>`;
  $("#m-title").onchange = async (e) => { await api(`/api/meetings/${id}`, { method: "PUT", body: JSON.stringify({ title: e.target.value }) }); loadList(); };
  if ($("#btn-retry")) $("#btn-retry").onclick = async () => { await api(`/api/meetings/${id}/retry`, { method: "POST" }); openMeeting(id); };
  if (live) { $("#rec-stop").onclick = stopRecording; requestAnimationFrame(tickRecording); }
  if (m.segments.length) {
    document.querySelectorAll(".tabs [data-tab]").forEach((b) => b.onclick = () => { tab = b.dataset.tab; renderMeeting(true); });
    $("#btn-share").onclick = () => shareDialog(m);
    $("#btn-delete").onclick = async () => {
      if (!confirm("Delete this meeting and its recording from the computer?")) return;
      await api(`/api/meetings/${id}`, { method: "DELETE" });
      location.hash = "#/";
    };
    renderTab(m, tpl);
  }
  if (busy) {
    pollTimer = setInterval(async () => {
      const s = await api(`/api/meetings/${id}`).catch(() => null);
      if (!s || id !== current) return;
      const p = $("#progress-text"); if (p) p.textContent = s.progress || s.status;
      if (!["queued", "transcribing", "summarizing", "uploading"].includes(s.status)) { clearInterval(pollTimer); renderMeeting(true); loadList(); }
    }, 2000);
  }
}

function seek(sec) {
  const p = $("#player");
  if (p) { p.currentTime = sec; p.play(); }
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a.ts");
  if (a) { e.preventDefault(); seek(tsToSec(a.dataset.ts)); }
});

function renderTab(m, tpl) {
  const body = $("#tab-body");
  if (tab === "notes") {
    body.innerHTML = `
      <div class="notes-tools">
        <label>Kind <select id="n-template">${tpl}</select></label>
        <button class="btn" id="n-regen">↻ Rewrite notes</button>
        <span class="grow"></span>
        <button class="link" id="n-copy">Copy</button>
        <button class="link" id="n-edit">Edit</button>
      </div>
      <div class="notes md" id="notes">${m.notes ? renderMd(m.notes) : `<p class="muted">Notes are not written yet.</p>`}</div>
      ${m.usage && m.usage.model ? `<p class="fine">Notes by ${esc(m.usage.model)} · transcript: ${esc(m.usage.stt === "local" ? "Whisper on this computer" : m.usage.stt === "local-fallback" ? "Whisper on this computer (Uno AI speech-to-text is not available for this computer's key yet)" : "Whisper via " + (m.usage.stt === "uno" ? "Uno AI" : "your provider"))}</p>` : ""}`;
    $("#n-regen").onclick = async () => {
      await api(`/api/meetings/${m.id}/regenerate`, { method: "POST", body: JSON.stringify({ template: $("#n-template").value }) });
      openMeeting(m.id);
    };
    $("#n-copy").onclick = () => { navigator.clipboard.writeText(m.notes); toast("Notes copied as Markdown"); };
    $("#n-edit").onclick = () => {
      $("#notes").outerHTML = `<textarea class="notes-edit" id="notes-edit">${esc(m.notes)}</textarea><div class="dlg-actions"><button class="btn primary" id="n-save">Save</button></div>`;
      $("#n-save").onclick = async () => {
        await api(`/api/meetings/${m.id}`, { method: "PUT", body: JSON.stringify({ notes: $("#notes-edit").value }) });
        toast("Saved to notes.md"); renderMeeting(true);
      };
    };
  } else if (tab === "transcript") {
    const guessed = m.segments.some((s) => s.guessed);
    body.innerHTML = `${guessed ? `<p class="fine">Speakers were told apart by AI from what was said — names may be off.</p>` : ""}<div class="transcript">${m.segments.map((s) => `
      <p><a href="#" class="ts" data-ts="${fmtTs(s.start)}">${fmtTs(s.start)}</a>${s.speaker ? ` <b class="spk" style="color:${spkColor(s.speaker)}">${esc(s.speaker === "Me" ? "You" : s.speaker)}</b>` : ""} ${esc(s.text)}</p>`).join("")}</div>`;
  } else {
    const chat = m.chat || [];
    body.innerHTML = `
      <div class="chat" id="chat">${chat.length ? chat.map(chatMsg).join("") : `<p class="muted">Ask anything: “What did we promise the client?”, “Who owns the login bug?”, “Write a follow-up email”.</p>`}</div>
      <form class="ask" id="ask-form"><input id="ask-q" placeholder="Ask about this meeting…" autocomplete="off"><button class="btn primary">Ask</button></form>`;
    $("#ask-form").onsubmit = async (e) => {
      e.preventDefault();
      const q = $("#ask-q").value.trim();
      if (!q) return;
      $("#ask-q").value = "";
      const c = $("#chat");
      if (!chat.length) c.innerHTML = "";
      c.insertAdjacentHTML("beforeend", chatMsg({ role: "user", content: q }) + `<div class="msg ai pending" id="pending">Thinking…</div>`);
      try {
        const r = await api(`/api/meetings/${m.id}/ask`, { method: "POST", body: JSON.stringify({ question: q }) });
        $("#pending").outerHTML = chatMsg({ role: "assistant", content: r.answer });
        m.chat = r.chat;
      } catch (err) { $("#pending").outerHTML = `<div class="msg ai err">${esc(err.message)}</div>`; }
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

async function shareDialog(m) {
  if (m.share && m.share.token) {
    const url = `${STATE.app_url || location.origin}/s/${m.share.token}`;
    const choice = prompt("Anyone with this link can read the notes. Copy it, or type STOP to stop sharing.", url);
    if (choice && choice.trim().toUpperCase() === "STOP") {
      await api(`/api/meetings/${m.id}/share`, { method: "DELETE" });
      toast("Link turned off");
      renderMeeting(true);
    }
    return;
  }
  const withTranscript = confirm("Share a read-only link to the notes.\n\nOK — include the transcript too\nCancel — notes only");
  const r = await api(`/api/meetings/${m.id}/share`, { method: "POST", body: JSON.stringify({ transcript: withTranscript }) });
  const url = r.url.startsWith("http") ? r.url : location.origin + r.url;
  try { await navigator.clipboard.writeText(url); toast("Link copied"); } catch { /* clipboard blocked */ }
  prompt("Link to the notes (anyone with it can read):", url);
  renderMeeting(true);
}

// ---- settings --------------------------------------------------------------

function openSettings() {
  const f = $("#settings-form");
  const s = STATE.settings;
  for (const [k, v] of Object.entries(s)) {
    const els = f.querySelectorAll(`[name="${k}"]`);
    els.forEach((el) => { if (el.type === "radio") el.checked = el.value === v; else el.value = v ?? ""; });
  }
  const p = STATE.provider;
  $("#uno-provider-note").textContent = p.name === "uno" && p.ready
    ? (p.route === "app"
      ? "Works out of the box — uses the AI of this computer. How much this app may spend is set in Uno Work → Settings → Apps."
      : `Works out of the box — using ${p.source}. Usage is billed to your Uno AI balance.`)
    : "Works out of the box on a Uno computer — no keys. Usage is billed to your Uno AI balance.";
  $("#ai-route").textContent = p.ready ? `Now in use: ${p.route_label} · notes model ${p.model}` : "Now in use: nothing — the AI is not set up";
  $("#settings-form").querySelector('[name="model"]').placeholder =
    p.route === "app" ? "default — this computer's choice" : "deepseek/deepseek-v3.2";
  $("#test-out").innerHTML = "";
  syncCustom();
  $("#dlg-settings").showModal();
}
function syncCustom() {
  const f = $("#settings-form");
  f.querySelector(".custom-fields").hidden = f.querySelector('[name="provider"]:checked')?.value !== "custom";
}
$("#settings-form").addEventListener("change", syncCustom);
$("#btn-settings").onclick = openSettings;
async function saveSettings() {
  const f = new FormData($("#settings-form"));
  const body = Object.fromEntries(f.entries());
  STATE = await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
  await loadState();
}
$("#dlg-settings").addEventListener("close", async () => {
  if ($("#dlg-settings").returnValue === "ok") {
    try { await saveSettings(); toast("Settings saved"); } catch (e) { toast(e.message, "bad"); }
  }
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
let searchTimer;
$("#search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadList, 250); };

function route() {
  const m = location.hash.match(/^#\/m\/([a-z0-9]{10})$/);
  if (m) openMeeting(m[1]); else showHome();
}
window.addEventListener("hashchange", route);

(async () => {
  await loadState();
  route();
  setInterval(() => { if (!recording) loadList(); }, 15000);
})();
