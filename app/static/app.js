"use strict";
// Uno Notetaker — single-page UI, no build step.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PIECE = 8 * 1024 * 1024;
const BUSY = ["queued", "transcribing", "summarizing", "uploading"];
const phone = () => window.matchMedia("(max-width: 760px)").matches;
const ic = (name, cls = "") => `<svg class="ic ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

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
  const pill = { app: "AI of this computer", gateway: "Uno AI", custom: "Your AI provider" };
  el.innerHTML = p.ready ? `<span class="dot-ok"></span> ${pill[p.route] || "AI"} is ready` : `<span class="dot-bad"></span> No AI for notes yet`;
  el.title = p.ready ? `AI: ${p.route_label} — ${p.source}` : "";
  for (const id of ["#rec-template", "#up-template", "#set-template"]) {
    const pickable = STATE.pickable_templates || Object.keys(STATE.templates);
    $(id).innerHTML = Object.entries(STATE.templates).filter(([k]) => pickable.includes(k)).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    $(id).value = STATE.settings.template;
  }
  liveCalls();
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
      <span class="s"><span class="src" title="${esc(SOURCE_LABEL[m.source] || "")}">${ic(SOURCE_ICON[m.source] || "notes")}</span>${fmtTime(m.created_at)}${m.duration ? " · " + fmtDur(m.duration) : ""}
        ${statusBadge(m.status)}${m.open_actions ? `<span class="todo" title="${plural(m.open_actions, "open action item")}">${ic("todo")}${m.open_actions}</span>` : ""}</span>
    </a>`;
  }).join("");
  return meetings;
}
const SOURCE_ICON = { bot: "bot", browser: "tab", upload: "upload", telegram: "send", screen: "screen" };
const SOURCE_LABEL = { bot: "The notetaker joined the call", browser: "Recorded in the browser", upload: "Uploaded", telegram: "From Telegram", screen: "Screen recording from Uno Work" };
function fmtDur(sec) {
  const m = Math.round((sec || 0) / 60);
  if (m < 1) return `${Math.max(1, Math.round(sec || 0))} s`;
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}`;
}
function dayLabel(iso) {
  const d = new Date(iso), t = new Date();
  if (d.toDateString() === t.toDateString()) return "Today";
  if (new Date(t - 864e5).toDateString() === d.toDateString()) return "Yesterday";
  if (new Date(+t + 864e5).toDateString() === d.toDateString()) return "Tomorrow";
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
  const today = new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  $("#main").innerHTML = `
    <section class="page home">
      <h1 class="home-title">${greeting()}</h1>
      <p class="home-date">${esc(today)}</p>
      ${p.ready ? "" : `<div class="notice">${ic("alert")}<span>Notes need an AI. Uno AI works by itself on a Uno computer, and this one can't reach it. <button class="link" id="home-settings">Choose an AI</button></span></div>`}
      <div class="sec" id="home-next"><div class="sec-head"><h2>Coming up</h2></div><div class="panel"><div class="row muted">Loading…</div></div></div>
      <div class="sec" id="home-recent"></div>
    </section>`;
  if (!p.ready) $("#home-settings").onclick = () => openSettings("set-adv");
  loadList().then(renderRecent).catch(() => {});
  renderComingUp();
}
function greeting() {
  const h = new Date().getHours();
  return h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

// Home: the next calls and whether the notetaker joins them.
async function renderComingUp() {
  const box = $("#home-next");
  if (!box) return;
  let d;
  try { d = await api("/api/calls"); } catch { box.hidden = true; return; }
  if (!$("#home-next")) return;
  const now = new Date();
  const calls = d.upcoming.filter((e) => e.url && new Date(e.end) > now).slice(0, 6);
  const anyCal = d.calendars.length > 0;
  let body;
  if (!anyCal) {
    body = connectPanel(d.uno);
  } else if (!calls.length) {
    body = `<div class="panel"><div class="row muted">${ic("calendar")} No calls with a link in the next 7 days.</div></div>`;
  } else {
    body = `<div class="panel">${eventRows(calls, { compact: true })}</div>`;
  }
  const off = anyCal && !d.worker.online ? `<p class="fine" style="margin-top:8px">${ic("alert")} The notetaker can't join calls right now: its call service isn't running on this computer.</p>` : "";
  box.innerHTML = `<div class="sec-head"><h2>Coming up</h2>${anyCal ? `<span class="aside">${esc(joinRule(d))} · <a href="#/calls">All calls</a></span>` : ""}</div>${body}${off}`;
  bindEventSwitches(box, renderComingUp);
  bindCalendarRows(box, () => renderComingUp());
  if ($("#home-cal-link", box)) $("#home-cal-link", box).onclick = openCalendarDialog;
  if ($("#home-send-bot", box)) $("#home-send-bot", box).onclick = openBotDialog;
}

// Not a single calendar yet: Google / Outlook from the Uno account in one click, or a link.
function connectPanel(u) {
  const rows = u && u.work && u.supported !== false ? u.calendars.filter((c) => c.state !== "unavailable") : [];
  const btns = rows.map((c) => c.state === "connected" ? "" :
    `<button type="button" class="btn ${rows[0] === c ? "primary" : ""}" data-uno-connect="${c.provider}" ${unoWatch && unoWatch.provider === c.provider ? "disabled" : ""}>${ic("calendar")} ${unoWatch && unoWatch.provider === c.provider ? "Waiting for Uno Work…" : `Connect ${esc(c.provider === "gmail" ? "Google Calendar" : "Outlook & Teams")}`}</button>`).join("");
  const hint = Object.values(unoHint)[0];
  return `<div class="panel connect">
    <h3>Let the notetaker join your calls</h3>
    <p>Connect your calendar and it joins calls with a link by itself: Google Meet, Zoom, Teams, Telemost, Zoho. You get the notes after each call.</p>
    <div class="btns">${btns}<button type="button" class="btn ${btns ? "ghost" : "primary"}" id="home-cal-link">${ic("link")} Add a calendar by link</button><button type="button" class="btn ghost" id="home-send-bot">${ic("bot")} Send to a call</button></div>
    ${hint ? `<p class="fine" style="margin:12px 0 0">${hint}</p>` : ""}
  </div>`;
}

function joinRule(d) {
  const asst = STATE.assistant && STATE.assistant.active;
  if (asst) return "Joins calls you're in with someone else";
  return { all: "Joins every call with a link", accepted: "Joins calls you accepted", off: "Joins only when you send it" }[STATE.settings.bot_auto_join] || "";
}

function eventRows(events, { compact = false } = {}) {
  let lastDay = "";
  const now = new Date();
  return events.map((e) => {
    const day = new Date(e.start).toDateString();
    const head = day !== lastDay ? `<div class="day-sep">${dayLabel(e.start)}</div>` : "";
    lastDay = day;
    const s = e.session;
    const live = new Date(e.start) <= now && new Date(e.end) > now;
    const state = s ? `<span class="st st-${s.status}">${esc(CALL_STATUS[s.status] || s.status)}</span>${s.meeting_id ? ` · <a href="#/m/${s.meeting_id}">notes</a>` : ""}`
      : (e.join ? "" : `<span>${esc(e.why || "Won't join")}</span>`);
    const past = new Date(e.end) < now;
    return `${head}<div class="ev ${e.join ? "" : "off"} ${past ? "past" : ""}">
      <span class="ev-time">${fmtTime(e.start)}<span class="ev-end">${fmtTime(e.end)}</span></span>
      <span class="ev-title"><span class="ev-name">${esc(e.title || "Busy")}</span>
        <span class="ev-sub">${platformTag(e.platform)}${e.attendees ? `<span>· ${plural(e.attendees, "guest")}</span>` : ""}${live && !s ? `<span class="now-chip">· now</span>` : ""}${state ? `<span>·</span>${state}` : ""}</span></span>
      <label class="switch" title="${e.join ? "The notetaker joins this call" : "The notetaker won't join"}"><span class="${compact ? "only-desktop" : ""}">${e.join ? "Joins" : "Off"}</span><input type="checkbox" data-ev="${e.key}" ${e.join ? "checked" : ""} ${past || s ? "disabled" : ""} aria-label="Notetaker joins ${esc(e.title || "this call")}"><i></i></label>
    </div>`;
  }).join("");
}
function bindEventSwitches(root, refresh) {
  $$("[data-ev]", root).forEach((cb) => cb.onchange = async () => {
    try { await api(`/api/calls/events/${cb.dataset.ev}`, { method: "PUT", body: JSON.stringify({ choice: cb.checked ? "join" : "skip" }) }); }
    catch (e) { toast(e.message, "bad"); }
    refresh();
  });
}

// Home: the latest notes, with the first line of each summary.
function renderRecent(meetings) {
  const box = $("#home-recent");
  if (!box || !meetings) return;
  if (!meetings.length) {
    box.innerHTML = `<div class="sec-head"><h2>Start your first notes</h2></div>
      <div class="starters">
        <button class="starter" data-new="call">${ic("tab")}<b>Record a call</b><span>Meet, Zoom or Teams in another tab of this browser.</span></button>
        <button class="starter" data-new="mic">${ic("mic")}<b>Record in the room</b><span>A meeting around a table or a voice memo.</span></button>
        <button class="starter" data-new="upload">${ic("upload")}<b>Upload a recording</b><span>Audio or video: m4a, mp3, wav, mp4…</span></button>
      </div>
      <p class="fine" style="margin-top:14px">Recording other people? Tell them first — in many places it's required by law.${STATE.telegram.paired ? ` You can also send a voice message to <b>@${esc(STATE.telegram.bot)}</b> in Telegram.` : ""}</p>`;
    bindNew(box);
    return;
  }
  const rows = meetings.slice(0, 8).map((m) => {
    const busy = BUSY.includes(m.status) || m.status === "recording";
    const prev = m.preview || (busy ? (m.progress || "Working on the notes…") : m.status === "error" ? "Something went wrong — open it to retry." : "");
    return `<a class="note-row" href="#/m/${m.id}">
      <div class="nr-top"><span class="nr-title">${esc(m.title || (m.status === "done" ? "Untitled meeting" : "New meeting"))}</span><span class="nr-when">${esc(fmtDate(m.created_at))}</span></div>
      ${prev ? `<div class="nr-prev">${esc(prev)}</div>` : ""}
      <div class="nr-meta"><span>${ic(SOURCE_ICON[m.source] || "notes")}${esc(SOURCE_LABEL[m.source] || "")}</span>${m.duration ? `<span>${ic("clock")}${fmtDur(m.duration)}</span>` : ""}${m.open_actions ? `<span>${ic("todo")}${plural(m.open_actions, "action item")}</span>` : ""}${busy || m.status === "error" ? statusBadge(m.status) : ""}</div>
    </a>`;
  }).join("");
  box.innerHTML = `<div class="sec-head"><h2>Recent notes</h2></div><div class="panel">${rows}</div>`;
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
      <a href="#/" class="back only-phone">${ic("back")} Meetings</a>
      <header class="page-head">
        <h1>Action items</h1>
        <label class="check small"><input type="checkbox" id="show-done" ${showDone ? "checked" : ""}> Show done</label>
      </header>
      <p class="page-lead">From the notes of every meeting. Tick one off here or in the meeting, it's the same item.</p>
      <div class="actions-list">${rows || `<p class="empty">Nothing open. Action items from your meetings show up here.</p>`}</div>
    </section>`;
  $("#show-done").onchange = (e) => { sessionStorage.setItem("nt-done", e.target.checked ? "1" : "0"); showActions(); };
  bindActionRows($("#main"), showActions);
  loadList();
}

function actionRow(a, mid, handed = {}) {
  const h = handed[a.id];
  const aiBtn = STATE.uno_work && !a.done
    ? (h ? `<span class="handed" title="A chat in Uno Work is working on it">${ic("check")} In Uno Work</span>`
      : `<button class="ai-btn" data-ai="${a.id}" data-mid="${mid}" title="Start a chat in Uno Work that prepares this (drafts, documents). It asks you before changing anything.">${ic("sparkle")} Ask Uno to do it</button>`)
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
      b.outerHTML = `<span class="handed">${ic("check")} In Uno Work</span>`;
      toast("A chat started in Uno Work — it asks you before it changes anything.");
    } catch (err) { toast(err.message, "bad"); b.disabled = false; b.innerHTML = `${ic("sparkle")} Ask Uno to do it`; }
  });
}

// ---- calls: calendars and the bot ------------------------------------------------

const PLATFORM = { meet: "Meet", zoom: "Zoom", teams: "Teams", telemost: "Telemost", zoho: "Zoho", jitsi: "Jitsi", other: "Call" };
const CALL_STATUS = { queued: "waiting for the bot", joining: "joining…", lobby: "waiting to be let in", recording: "● recording", leaving: "leaving…", done: "done", failed: "couldn't join", not_admitted: "not let in", cancelled: "cancelled" };
let callsTimer = null;
const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const platformTag = (p) => `<span class="pf pf-${esc(p || "other")}">${esc(PLATFORM[p] || "Call")}</span>`;

// ---- calendars: from the Uno account, and iCal links ---------------------------------

// Short, plain steps per service (checked 29.10.2026). The link is secret and read-only.
const ICAL_HOWTO = [
  ["Google Calendar", `Only if Google isn't connected above.`, [
    "On a computer, open Google Calendar → ⚙ → <b>Settings</b>.",
    "On the left, under <b>Settings for my calendars</b>, click your calendar.",
    "In <b>Integrate calendar</b>, copy <b>Secret address in iCal format</b>."]],
  ["Outlook.com / Microsoft 365", `Easier: connect Outlook &amp; Teams above. Some work accounts don't allow publishing.`, [
    "Outlook on the web → ⚙ <b>Settings</b> → <b>Calendar</b> → <b>Shared calendars</b>.",
    "Under <b>Publish a calendar</b>, pick your calendar and <b>Can view all details</b> → <b>Publish</b>.",
    "Copy the <b>ICS</b> link."]],
  ["Yandex Calendar", "", [
    "In Yandex Calendar, point at your calendar in the list on the left and click ⚙.",
    "Open <b>Export</b> (Экспорт), choose iCal and copy the link."]],
  ["Apple iCloud", "Anyone with this link can see the calendar.", [
    "On iCloud.com → Calendar, click the share icon next to your calendar (on iPhone: Calendars → ⓘ).",
    "Turn on <b>Public Calendar</b> and copy the link.",
    "Paste it as is — a <code>webcal://</code> link works too."]],
  ["Zoho Calendar", "", [
    "In Zoho Calendar, under <b>My Calendars</b>, click ⋮ next to your calendar → <b>Share</b>.",
    "Turn on <b>Enable private address for this calendar</b>.",
    "Copy the <b>ICAL</b> link."]],
];
const howtoHtml = () => ICAL_HOWTO.map(([name, note, steps]) => `<details class="howto"><summary>${name}</summary>
  <ol>${steps.map((x) => `<li>${x}</li>`).join("")}</ol>${note ? `<p class="fine">${note}</p>` : ""}</details>`).join("");
$("#cal-howto").innerHTML = howtoHtml();

const UNO_CONNECTOR = { gmail: "Gmail & Calendar", microsoft: "Outlook & Teams calendar" };
const framed = () => { try { return window.parent !== window; } catch { return true; } };
let unoWatch = null;   // after "Connect": re-check every few seconds for ~2 minutes
let unoHint = {};      // provider → the "open Uno Work" hint when not in Uno Work's frame

const CAL_IC = { gmail: "google", microsoft: "microsoft" };
function unoRows(u) {
  const off = !u.enabled && !u.assistant;
  return u.calendars.map((c) => {
    let state, btn = "";
    if (off) state = "Off";
    else if (c.state === "connected") state = `<span class="ok">Connected</span>${c.account ? ` · ${esc(c.account)}` : ""}${c.fetched_at ? ` · ${plural(c.events, "event")} this week` : ""}`;
    else if (c.state === "not_connected") { state = "Not connected"; btn = "Connect"; }
    else if (c.state === "reconnect") { state = `<span class="bad">Sign in again</span>`; btn = "Sign in again"; }
    else if (c.state === "unavailable") state = c.error ? esc(c.error) : "Not available yet";
    else state = `<span class="bad">${esc(c.error || "Couldn't read it")}</span>`;
    const waiting = unoWatch && unoWatch.provider === c.provider;
    return `<li class="ucal ${off ? "off" : ""}"><span class="cal-ic ${CAL_IC[c.provider] || ""}">${ic("calendar")}</span><span class="ucal-name"><b>${esc(c.name)}</b><span class="cal-sub">${state}</span>
        ${unoHint[c.provider] ? `<span class="fine">${unoHint[c.provider]}</span>` : ""}</span>
      ${btn ? `<button type="button" class="btn sm ${c.state === "not_connected" ? "primary" : ""}" data-uno-connect="${c.provider}" ${waiting ? "disabled" : ""}>${waiting ? "Waiting…" : btn}</button>` : ""}</li>`;
  }).join("");
}

// The "From your Uno account" block. withSwitch: the setting (inside the Settings form).
function unoBlock(u, withSwitch) {
  if (!u || !u.work) return "";
  const head = `<p class="sub">From your Uno account</p>`;
  if (u.supported === false) {
    return `${head}<p class="fine">Google Calendar and Outlook &amp; Teams from your Uno account need a newer Uno Work. Until then, add your calendar by its link below.</p>`;
  }
  const sw = withSwitch ? `<label class="check"><input type="checkbox" name="uno_calendars" ${STATE.settings.uno_calendars !== false ? "checked" : ""}> <span><b>Use calendars from my Uno account</b><br><span class="muted small">The ones you connected in Uno Work. Nothing to paste.</span></span></label>` : "";
  const asst = u.assistant ? `<p class="fine" style="margin-top:8px">Your assistant in Uno Work uses these calendars to join your calls. Turn that off in Uno Work → the assistant → Join my calls.</p>` : "";
  const off = !withSwitch && !u.enabled && !u.assistant ? `<p class="fine" style="margin-top:8px">Turned off in Settings → Calendars.</p>` : "";
  return `${head}${sw}<ul class="cals ucals">${unoRows(u)}</ul>${asst}${off}`;
}

function icalList(cals) {
  const own = cals.filter((c) => c.kind !== "uno");
  return own.length ? `<ul class="cals">${own.map((c) => `<li><span class="cal-ic">${ic("link")}</span><span class="cal-name"><b>${esc(c.name || "Calendar")}</b><span class="cal-sub">${esc(c.where)} · ${c.error ? `<span class="bad">${esc(c.error)}</span>` : `${plural(c.events, "event")} this week`}</span></span>
    <button type="button" class="icon-btn" data-cal-del="${c.id}" title="Remove" aria-label="Remove ${esc(c.name || "calendar")}">${ic("trash")}</button></li>`).join("")}</ul>` : "";
}

function connectUno(provider, rerender) {
  if (framed()) {
    window.parent.postMessage({ type: "uno-work:connect", provider }, "*");
    delete unoHint[provider];
  } else {
    unoHint[provider] = `Open Uno Work → Settings → Connectors and connect <b>${esc(UNO_CONNECTOR[provider] || provider)}</b>. This page notices it by itself.`;
  }
  clearInterval(unoWatch && unoWatch.timer);
  const until = Date.now() + 2 * 60 * 1000;
  unoWatch = { provider, timer: setInterval(async () => {
    const u = await api("/api/calendars/uno?fresh=1").catch(() => null);
    const c = u && u.calendars.find((x) => x.provider === provider);
    const done = c && c.state === "connected";
    if (done || Date.now() > until) {
      clearInterval(unoWatch.timer); unoWatch = null;
      if (done) { delete unoHint[provider]; toast(`${c.name} connected`); loadState().catch(() => {}); }
    }
    if (u) rerender(u);
  }, 4000) };
  rerender(null);
}

function bindCalendarRows(root, rerender) {
  $$("[data-uno-connect]", root).forEach((b) => b.onclick = () => connectUno(b.dataset.unoConnect, rerender));
  $$("[data-cal-del]", root).forEach((b) => b.onclick = async () => {
    if (!confirm("Remove this calendar? The bot stops joining its calls.")) return;
    try { await api(`/api/calendars/${b.dataset.calDel}`, { method: "DELETE" }); } catch (e) { toast(e.message, "bad"); }
    await loadState(); rerender(null);
  });
}

// Settings → Calendars
let calBoxData = null;
async function renderCalendarBox(uno) {
  const box = $("#cal-box");
  if (!uno || !calBoxData) {
    try { calBoxData = await api("/api/calendars"); } catch (e) { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; return; }
  }
  if (uno) calBoxData.uno = uno;
  const keep = box.querySelector('[name="uno_calendars"]');
  const checked = keep ? keep.checked : null;
  const typed = $("#cal-box-url") ? $("#cal-box-url").value : "";
  const openHowto = $$("details.howto[open] summary", box).map((x) => x.textContent);
  box.innerHTML = `${unoBlock(calBoxData.uno, true)}
    <p class="sub">Other calendars (iCal link)</p>
    <p class="fine">Yandex, iCloud, Zoho or any other calendar: paste its secret iCal address. Notetaker reads it every few minutes; it's read-only, and you can reset the address in your calendar any time.</p>
    ${icalList(calBoxData.calendars)}
    <div class="row-btns"><input id="cal-box-url" placeholder="https://… or webcal://…" autocomplete="off"><button type="button" class="btn" id="cal-box-add">Add</button></div>
    <div class="howto-list">${howtoHtml()}</div>`;
  if (checked !== null && box.querySelector('[name="uno_calendars"]')) box.querySelector('[name="uno_calendars"]').checked = checked;
  $("#cal-box-url").value = typed;
  $$("details.howto summary", box).forEach((x) => { if (openHowto.includes(x.textContent)) x.parentElement.open = true; });
  const add = async () => {
    const url = $("#cal-box-url").value.trim();
    if (!url) return;
    $("#cal-box-add").disabled = true;
    toast("Reading the calendar…");
    try {
      const r = await api("/api/calendars", { method: "POST", body: JSON.stringify({ url }) });
      toast(`Connected${r.name ? ` “${r.name}”` : ""} — ${plural(r.events, "event")} this week`);
      $("#cal-box-url").value = "";
      await loadState();
      calBoxData = null;
    } catch (e) { toast(e.message, "bad"); }
    renderCalendarBox();
  };
  $("#cal-box-add").onclick = add;
  $("#cal-box-url").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } };
  bindCalendarRows(box, (u) => { if ($("#dlg-settings").open) renderCalendarBox(u); });  // null: read it all again
}

async function showCalls() {
  current = null; view = "calls";
  clearInterval(pollTimer);
  setView("detail");
  await renderCalls();
  loadList();
  clearInterval(callsTimer);
  callsTimer = setInterval(() => {
    if (view !== "calls") { clearInterval(callsTimer); return; }
    if (document.visibilityState === "visible" && !document.querySelector("dialog[open]")) renderCalls();
  }, 5000);
}

async function renderCalls() {
  let d;
  try { d = await api("/api/calls"); }
  catch (e) { $("#main").innerHTML = `<section class="page"><h1>Calls</h1><p class="bad">${esc(e.message)}</p></section>`; return; }
  if (view !== "calls") return;
  const w = d.worker;
  const asst = STATE.assistant && STATE.assistant.active ? STATE.assistant : null;
  const botName = (asst && asst.bot_name) || STATE.settings.bot_name || "Uno Notetaker";
  const bot = w.online
    ? `<p class="bot-line"><span class="dot-ok"></span> Ready to join as “${esc(botName)}”${w.running ? ` · in ${plural(w.running, "call")} now` : ""}</p>`
    : `<p class="bot-line"><span class="dot-bad"></span> Can't join calls right now: the call service isn't running on this computer. Calls wait for it.</p>`;
  const active = d.active.map((x) => `
    <div class="call-card ${x.status}">
      <span class="rec-dot" style="${x.status === "recording" ? "" : "animation:none;background:var(--faint);box-shadow:none"}"></span>
      <div class="cc-main"><b>${esc(x.title || "Call")}</b> ${platformTag(x.platform)}
        <div class="ev-sub"><span class="st st-${x.status}">${esc(CALL_STATUS[x.status] || x.status)}</span>${x.detail ? `<span>· ${esc(x.detail)}</span>` : ""}</div></div>
      <div class="cc-btns">${x.meeting_id ? `<a class="btn sm" href="#/m/${x.meeting_id}">Open</a>` : ""}
        <button class="btn sm" data-stop="${x.id}">${x.status === "queued" ? "Cancel" : "Remove it"}</button></div>
    </div>`).join("");
  const upcoming = d.upcoming.filter((e) => e.url);
  const noLink = d.upcoming.filter((e) => !e.url && new Date(e.end) > new Date()).length;
  const uno = unoBlock(d.uno, false);
  const ical = icalList(d.calendars);
  const recent = d.recent.map((x) => `<div class="ev past">
      <span class="ev-time">${fmtTime(new Date(x.created_at * 1000).toISOString())}<span class="ev-end">${esc(dayLabel(new Date(x.created_at * 1000).toISOString()))}</span></span>
      <span class="ev-title"><span class="ev-name">${esc(x.title || "Call")}</span><span class="ev-sub">${platformTag(x.platform)}<span>·</span><span class="st st-${x.status}">${esc(CALL_STATUS[x.status] || x.status)}</span>${x.detail ? `<span>· ${esc(x.detail)}</span>` : ""}
        ${x.screenshot ? `<a href="/api/calls/sessions/${x.id}/screenshot" target="_blank" rel="noopener">What it saw</a>` : ""}</span></span>
      ${x.meeting_id ? `<a class="btn sm" href="#/m/${x.meeting_id}">Notes</a>` : "<span></span>"}
    </div>`).join("");
  $("#main").innerHTML = `
    <section class="page calls">
      <a href="#/" class="back only-phone">${ic("back")} Meetings</a>
      <header class="page-head">
        <h1>Calls</h1>
        <button class="btn primary" id="btn-send-bot">${ic("bot")} Send to a call</button>
      </header>
      ${bot}
      ${asst ? `<p class="fine">Your assistant <b>${esc(asst.assistant || "")}</b> in Uno Work sends this notetaker to your calls. The notes go to it, your Inbox and Telegram. Turn it off in Uno Work → the assistant → Join my calls.</p>` : ""}
      ${active ? `<div class="sec"><div class="sec-head"><h2>Now</h2></div><div class="panel">${active}</div></div>` : ""}
      <div class="sec">
        <div class="sec-head"><h2>Coming up</h2>${d.calendars.length ? `<span class="aside">${esc(joinRule(d))} · <button class="link small" id="calls-settings">Change</button></span>` : ""}</div>
        ${d.calendars.length ? (upcoming.length ? `<div class="panel">${eventRows(upcoming)}</div>` : `<div class="panel"><div class="row muted">${ic("calendar")} No calls with a link in the next 7 days.</div></div>`)
          + (noLink ? `<p class="fine" style="margin-top:8px">${plural(noLink, "event")} without a call link — nowhere to join.</p>` : "")
          : connectPanel(d.uno)}
      </div>
      ${d.calendars.length || uno ? `<div class="sec"><div class="sec-head"><h2>Calendars</h2><span class="aside"><button class="link small" id="btn-cal-refresh">${ic("refresh")} Check now</button></span></div>
        <div class="cal-box">${uno}${ical ? `<p class="sub">By link</p>${ical}` : ""}</div>
        <div class="row-btns"><button class="btn sm" id="btn-cal-add">${ic("plus")} Add a calendar by link</button></div></div>` : ""}
      ${recent ? `<div class="sec"><div class="sec-head"><h2>Recent</h2></div><div class="panel">${recent}</div></div>` : ""}
      <p class="fine" style="margin-top:28px">It joins as a guest, so the host may have to let it in. It leaves when the call ends, when everyone else has left or after a long silence. KakaoTalk group calls have no link to open in a browser, so it can't join those.</p>
    </section>`;
  $("#btn-send-bot").onclick = openBotDialog;
  for (const id of ["#btn-cal-add", "#home-cal-link"]) if ($(id)) $(id).onclick = openCalendarDialog;
  if ($("#home-send-bot")) $("#home-send-bot").onclick = openBotDialog;
  if ($("#calls-settings")) $("#calls-settings").onclick = () => openSettings("set-bot");
  if ($("#btn-cal-refresh")) $("#btn-cal-refresh").onclick = async (e) => { e.currentTarget.disabled = true; try { await api("/api/calendars/refresh", { method: "POST" }); } catch (err) { toast(err.message, "bad"); } renderCalls(); };
  $$("[data-stop]").forEach((b) => b.onclick = async () => { b.disabled = true; try { await api(`/api/calls/sessions/${b.dataset.stop}/stop`, { method: "POST" }); } catch (e) { toast(e.message, "bad"); } renderCalls(); });
  bindCalendarRows($("#main"), () => { if (view === "calls") renderCalls(); });
  bindEventSwitches($("#main"), renderCalls);
}

function openCalendarDialog() { $("#cal-url").value = ""; $("#dlg-calendar").showModal(); }

function openBotDialog() {
  $("#bot-name-hint").textContent = STATE.settings.bot_name || "Uno Notetaker";
  $("#dlg-bot form").reset();
  $("#dlg-bot").showModal();
}
$("#dlg-bot").addEventListener("close", async () => {
  if ($("#dlg-bot").returnValue !== "ok") return;
  const f = $("#dlg-bot form");
  try {
    const x = await api("/api/calls/join", { method: "POST", body: JSON.stringify({ url: f.url.value, title: f.title.value }) });
    toast(x.warning || "The bot is on its way", x.warning ? "bad" : "");
    location.hash === "#/calls" ? renderCalls() : (location.hash = "#/calls");
  } catch (e) { toast(e.message, "bad"); }
});
$("#dlg-calendar").addEventListener("close", async () => {
  if ($("#dlg-calendar").returnValue !== "ok") return;
  toast("Reading the calendar…");
  try {
    const r = await api("/api/calendars", { method: "POST", body: JSON.stringify({ url: $("#cal-url").value }) });
    toast(`Connected${r.name ? ` “${r.name}”` : ""} — ${plural(r.events, "event")} this week`);
    await loadState();
  } catch (e) { toast(e.message, "bad"); }
  if (view === "calls") renderCalls();
});

function liveCalls() {
  const n = STATE && STATE.bot ? STATE.bot.running : 0;
  $("#calls-live").textContent = n ? `● ${n}` : "";
}
async function refreshBot() {
  const s = await api("/api/state").catch(() => null);
  if (s) { STATE.bot = s.bot; STATE.calendars = s.calendars; liveCalls(); }
}

// ---- recording -------------------------------------------------------------

const canShareTab = () => !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) && !phone();

function openRecordDialog(mode) {
  if (recording) { location.hash = `#/m/${recording.id}`; return; }
  const dlg = $("#dlg-record");
  dlg.querySelector("form").reset();
  const tab = canShareTab();
  $(".mode-call", dlg).hidden = !tab;
  dlg.querySelector(`[name="mode"][value="${tab && mode !== "mic" ? "call" : "mic"}"]`).checked = true;
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
      <button class="btn danger big" id="rec-stop">Stop and make notes</button>
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
  catch (e) { $("#main").innerHTML = `<section class="page"><a href="#/" class="back">${ic("back")} Meetings</a><h1>This meeting isn't here</h1><p class="muted">${esc(e.message)}</p></section>`; return; }
  if (id !== current) return;
  lastMeeting = m;
  const busy = BUSY.includes(m.status);
  const live = recording && recording.id === id;
  if (!first && busy && document.querySelector(".meeting")) {
    const p = $("#progress-text"); if (p) p.textContent = m.progress || m.status;
    return;
  }
  const src = { browser: "Recorded in the browser", upload: "Uploaded", telegram: "From Telegram",
    screen: "Screen recording",
    bot: `Notetaker in ${PLATFORM[(m.call || {}).platform] || "the call"}` }[m.source] || m.source;
  const botLive = m.source === "bot" && m.status === "recording";
  const tt = (m.talk_time || []).filter((x) => x.speaker);
  spkOrder = tt.map((x) => x.speaker);
  const faces = tt.length ? `<span class="faces" title="${esc(tt.map((x) => x.speaker === "Me" ? "You" : x.speaker).join(", "))}">${tt.slice(0, 4).map((x) => face(x.speaker)).join("")}${tt.length > 4 ? `<span class="more-faces">+${tt.length - 4}</span>` : ""}</span>` : "";
  const ready = m.segments.length > 0;
  const tabBtn = (k, icon, label) => `<button data-tab="${k}" role="tab" aria-controls="tab-body" aria-selected="${tab === k}" class="${tab === k ? "on" : ""}">${ic(icon)} ${label}</button>`;
  $("#main").innerHTML = `
    <div class="topbar">
      <a href="#/" class="icon-btn only-phone" aria-label="Meetings">${ic("back")}</a>
      <span class="crumb">${esc(m.title || "Untitled meeting")}</span>
      <span class="grow"></span>
      ${ready ? `<button class="btn ghost sm" id="btn-share">${ic("link")} ${m.share && m.share.token ? "Shared" : "Share"}</button>
      <div class="menu-wrap">
        <button class="icon-btn" id="btn-more" aria-label="More" title="More" aria-expanded="false">${ic("more")}</button>
        <div class="menu" id="menu" role="menu" hidden>
          <button data-act="copy-text">${ic("copy")} Copy notes</button>
          <a href="/api/meetings/${id}/export?what=notes" download>${ic("download")} Download notes</a>
          <a href="/api/meetings/${id}/export?what=all" download>${ic("download")} Download notes and transcript</a>
          ${STATE.inbox ? `<button data-act="send-inbox">${ic("inbox")} Send to Uno Work Inbox</button>` : ""}
          ${STATE.telegram.paired ? `<button data-act="send-telegram">${ic("send")} Send to Telegram</button>` : ""}
          <hr>
          <button data-act="delete" class="danger">${ic("trash")} Delete meeting…</button>
        </div>
      </div>` : ""}
    </div>
    <article class="meeting">
      <header class="m-head">
        <input class="m-title" id="m-title" value="${esc(m.title)}" placeholder="${busy || live ? "New meeting" : "Untitled meeting"}" aria-label="Title">
        <div class="m-meta">
          <span>${ic("calendar")} ${esc(fmtDate(m.created_at))}</span>
          ${m.duration ? `<span>${ic("clock")} ${fmtDur(m.duration)}</span>` : ""}
          <span>${ic(SOURCE_ICON[m.source] || "notes")} ${esc(src)}</span>
          ${faces}
        </div>
      </header>
      ${live ? recordingPanel() : ""}
      ${botLive ? `<section class="rec-panel bot-panel"><span class="rec-dot"></span><div><b>Notetaker is in the call and recording</b><p class="muted">Notes arrive when the call ends. Remove it to end the recording now.</p></div>
        <button class="btn" id="bot-remove">Remove it</button></section>` : ""}
      ${m.status === "uploading" && !live ? `<section class="progress upl"><p id="up-pct">Uploading…</p><div class="bar"><i id="up-progress"></i></div></section>` : ""}
      ${busy && m.status !== "uploading" ? `<section class="progress"><div class="spinner"></div><div><p id="progress-text">${esc(m.progress || "Working…")}</p>${progressSteps(m)}</div></section>` : ""}
      ${m.status === "error" ? `<section class="error-box">${ic("alert")}<span><b>The notes couldn't be made.</b> ${esc(m.error)}</span><button class="btn sm" id="btn-retry">Try again</button></section>` : ""}
      ${m.source === "screen" ? screenPlayer(m) : ""}
      ${ready ? `
      <nav class="tabs" role="tablist">${tabBtn("notes", "notes", "Notes")}${tabBtn("transcript", "users", "Transcript")}${tabBtn("ask", "sparkle", "Ask")}</nav>
      <div class="tab-body" id="tab-body" role="tabpanel"></div>` : ""}
      ${!ready && !busy && !live && !botLive && m.status !== "error" ? `<p class="muted">Nothing here yet.</p>` : ""}
    </article>
    ${m.source !== "screen" && m.has_audio ? playerBar(id) : ""}`;
  $("#m-title").onchange = async (e) => { await api(`/api/meetings/${id}`, { method: "PUT", body: JSON.stringify({ title: e.target.value }) }); loadList(); };
  if ($("#btn-retry")) $("#btn-retry").onclick = async () => { await api(`/api/meetings/${id}/retry`, { method: "POST" }); openMeeting(id); };
  if (live) { $("#rec-stop").onclick = stopRecording; requestAnimationFrame(tickRecording); }
  if (botLive) {
    $("#bot-remove").onclick = async (e) => {
      e.target.disabled = true; e.target.textContent = "Leaving…";
      try { await api(`/api/calls/sessions/${m.call.session}/stop`, { method: "POST" }); } catch (err) { toast(err.message, "bad"); }
    };
  }
  if (m.segments.length) {
    $$(".tabs [data-tab]").forEach((b) => b.onclick = () => {
      tab = b.dataset.tab;
      $$(".tabs [data-tab]").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-selected", String(x === b)); });
      renderTab(m);
    });
    $("#btn-share").onclick = () => shareDialog(m);
    bindMenu(m);
    renderTab(m);
  }
  if ($("#player")) {
    bindPlayerBar();
    bindPlayerFollow();
  }
  if (ready && seekOnOpen !== null) { const at = seekOnOpen; seekOnOpen = null; setTimeout(() => focusLine(at, true), 50); }
  if (busy || botLive) {
    pollTimer = setInterval(async () => {
      const s = await api(`/api/meetings/${id}`).catch(() => null);
      if (!s || id !== current) return;
      const p = $("#progress-text"); if (p) p.textContent = s.progress || s.status;
      if (s.status !== m.status || (!BUSY.includes(s.status) && !botLive)) { clearInterval(pollTimer); renderMeeting(true); loadList(); }
    }, 2000);
  }
}

function bindMenu(m) {
  const menu = $("#menu");
  $("#btn-more").onclick = (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    closeMenus();
    menu.hidden = !open;
    e.currentTarget.setAttribute("aria-expanded", String(open));
  };
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
// A screen recording's video stays in Uno Drive; the meeting keeps its share
// link (7 days) — the WebM for browsers that play it, the MP4 copy for Safari.
function screenPlayer(m) {
  const sc = m.screen || {};
  const where = sc.drive_key ? `Uno Drive → ${esc(sc.drive_key)}` : "Uno Drive → Recordings";
  const expired = !sc.share_url || (sc.share_expires_at && new Date(sc.share_expires_at) < new Date());
  if (expired) return `<p class="screen-gone muted">The video is in ${where}${sc.share_url ? " (its link expired)" : ""}.</p>`;
  const u = sc.share_url;
  const join = u.includes("?") ? "&" : "?";
  return `<video controls preload="metadata" playsinline id="player" class="screen-video">
      <source src="${esc(u + join + "media=1")}" type="video/webm">
      <source src="${esc(u + join + "format=mp4")}" type="video/mp4">
    </video>
    <p class="fine screen-src">In ${where} · <a href="${esc(u)}" target="_blank" rel="noopener">Open the link</a></p>`;
}

function face(name) {
  const label = name === "Me" ? "You" : name;
  const ini = label === "Others" ? "…" : label.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  return `<span class="face" style="background:${spkColor(name)}" title="${esc(label)}">${esc(ini)}</span>`;
}
function playerBar(id) {
  return `<audio preload="metadata" id="player" src="/api/meetings/${id}/audio"></audio>
    <div class="player-bar" id="player-bar">
      <button class="play" id="pb-play" aria-label="Play">${ic("play")}</button>
      <button class="icon-btn skip" id="pb-back" aria-label="Back 15 seconds" title="Back 15 s">${ic("back15")}</button>
      <span class="t" id="pb-now">0:00</span>
      <input type="range" id="pb-seek" min="0" max="1000" value="0" step="1" aria-label="Position">
      <span class="t" id="pb-dur">–:––</span>
      <button class="icon-btn skip" id="pb-fwd" aria-label="Forward 15 seconds" title="Forward 15 s">${ic("fwd15")}</button>
      <button class="speed" id="pb-speed" title="Playback speed">1×</button>
    </div>`;
}
const SPEEDS = [1, 1.25, 1.5, 2];
function bindPlayerBar() {
  const p = $("#player"), bar = $("#player-bar");
  if (!p || !bar) return;
  const seekEl = $("#pb-seek");
  let dragging = false;
  const sync = () => {
    const d = p.duration && isFinite(p.duration) ? p.duration : (lastMeeting && lastMeeting.duration) || 0;
    $("#pb-now").textContent = fmtTs(p.currentTime || 0);
    $("#pb-dur").textContent = d ? fmtTs(d) : "–:––";
    if (!dragging) seekEl.value = d ? Math.round((p.currentTime / d) * 1000) : 0;
    seekEl.style.setProperty("--p", `${seekEl.value / 10}%`);
  };
  const icon = () => { $("#pb-play").innerHTML = ic(p.paused ? "play" : "pause"); $("#pb-play").setAttribute("aria-label", p.paused ? "Play" : "Pause"); };
  p.addEventListener("timeupdate", sync);
  p.addEventListener("loadedmetadata", sync);
  p.addEventListener("play", icon);
  p.addEventListener("pause", icon);
  $("#pb-play").onclick = () => (p.paused ? p.play().catch(() => {}) : p.pause());
  $("#pb-back").onclick = () => { p.currentTime = Math.max(0, p.currentTime - 15); };
  $("#pb-fwd").onclick = () => { p.currentTime = Math.min(p.duration || 1e9, p.currentTime + 15); };
  seekEl.oninput = () => {
    dragging = true;
    seekEl.style.setProperty("--p", `${seekEl.value / 10}%`);
    const d = p.duration && isFinite(p.duration) ? p.duration : 0;
    if (d) $("#pb-now").textContent = fmtTs((seekEl.value / 1000) * d);
  };
  seekEl.onchange = () => {
    const d = p.duration && isFinite(p.duration) ? p.duration : 0;
    if (d) p.currentTime = (seekEl.value / 1000) * d;
    dragging = false;
  };
  $("#pb-speed").onclick = (e) => {
    const next = SPEEDS[(SPEEDS.indexOf(p.playbackRate) + 1) % SPEEDS.length] || 1;
    p.playbackRate = next;
    e.currentTarget.textContent = `${next}×`;
  };
  sync();
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
    const stt = m.usage && m.usage.stt;
    const sttLabel = stt === "local" ? "Whisper on this computer" : stt === "local-fallback" ? "Whisper on this computer (Uno AI speech-to-text was busy)" : stt === "uno" ? "Whisper via Uno AI" : stt ? "Whisper via your provider" : "";
    body.innerHTML = `
      ${acts.length ? `<section class="todo-block">
        <h2>Action items <span class="muted">${acts.filter((a) => !a.done).length} open</span></h2>
        ${acts.map((a) => actionRow(a, m.id, m.handed || {})).join("")}
      </section>` : ""}
      <div class="notes md" id="notes">${m.notes ? renderMd(m.notes, { skipTitle: m.title, skipSection: acts.length ? ACTION_SECTION : null }) : `<p class="muted">The notes aren't written yet.</p>`}</div>
      <div class="notes-foot">
        <label class="rewrite">${ic("sparkle")} Rewrite as <select id="n-template" aria-label="Kind of meeting">${tpl}</select></label>
        <button class="btn sm" id="n-regen">Rewrite</button>
        <span class="grow"></span>
        <button class="btn ghost sm" id="n-edit">${ic("edit")} Edit</button>
        <button class="btn ghost sm" id="n-copy">${ic("copy")} Copy</button>
      </div>
      <p class="provenance">
        ${sent.length ? `<span>${ic("check")} Sent to ${sent.join(" and ")}</span>` : ""}
        <span title="${esc(["Folder on this computer", m.usage && m.usage.model ? `Notes by ${m.usage.model}` : "", sttLabel ? `Transcript: ${sttLabel}` : ""].filter(Boolean).join(" · "))}">${ic("folder")} ${esc(m.path)}</span>
      </p>`;
    bindActionRows(body, () => renderMeeting(true));
    $("#n-regen").onclick = async () => {
      await api(`/api/meetings/${m.id}/regenerate`, { method: "POST", body: JSON.stringify({ template: $("#n-template").value }) });
      openMeeting(m.id);
    };
    $("#n-copy").onclick = () => { navigator.clipboard.writeText(m.notes); toast("Notes copied"); };
    $("#n-edit").onclick = () => {
      $("#notes").outerHTML = `<textarea class="notes-edit" id="notes-edit">${esc(m.notes)}</textarea><div class="row-btns"><button class="btn" id="n-cancel">Cancel</button><button class="btn primary" id="n-save">Save</button></div>`;
      $(".notes-foot").hidden = true;
      $("#n-cancel").onclick = () => renderTab(m);
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
        ${face(x.speaker)}${esc(x.speaker === "Me" ? "You" : x.speaker)} <span>${Math.round(x.share * 100)}%</span></button>`).join("")}
        <span class="fine">${guessed ? "Told apart by the AI from what was said. " : ""}Tap a name to fix it.</span></div>` : ""}
      <div class="transcript">${m.segments.map((s) => {
        const same = s.speaker === prev; prev = s.speaker;
        return `<p class="line ${same ? "cont" : ""}" data-start="${s.start}"><a href="#" class="ts" data-ts="${fmtTs(s.start)}">${fmtTs(s.start)}</a><span class="said">${s.speaker && !same ? `<b class="spk" style="color:${spkColor(s.speaker)}">${esc(s.speaker === "Me" ? "You" : s.speaker)}</b>` : ""}${esc(s.text)}${s.screen ? `<span class="on-screen">On screen: ${esc(s.screen)}</span>` : ""}${s.did ? `<span class="on-screen did">Did: ${esc(s.did)}</span>` : ""}</span></p>`;
      }).join("")}</div>`;
    $$(".spk-chip", body).forEach((b) => b.onclick = () => renameSpeaker(m, b.dataset.spk));
  } else {
    const chat = m.chat || [];
    body.innerHTML = `
      <div class="chat" id="chat">${chat.length ? chat.map(chatMsg).join("") : `<div class="suggest">${(m.source === "screen" ? ["What exactly should be fixed?", "Write it up as a bug report", "List the steps shown"] : ["What did we promise the client?", "Who owns what, by when?", "Write a follow-up email"]).map((q) => `<button class="chip" data-q="${esc(q)}">${ic("sparkle")} ${esc(q)}</button>`).join("")}</div>`}</div>
      <form class="ask" id="ask-form"><input id="ask-q" placeholder="Ask about this meeting…" autocomplete="off" enterkeyhint="send"><button class="btn primary" aria-label="Ask">${ic("send")}</button></form>`;
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
const SPK_COLORS = [0, 1, 2, 3, 4, 5].map((i) => `var(--spk-${i})`);
let spkOrder = [];  // speakers of the meeting on screen, by talk time: distinct colors for up to six
function spkColor(name) {
  const i = spkOrder.indexOf(name);
  if (i >= 0 && spkOrder.length <= SPK_COLORS.length) return SPK_COLORS[i];
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

function openSettings(section) {
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
  $("#inbox-note").textContent = STATE.inbox ? "The bell in Uno Work: “Notes ready · 3 action items”." : "Needs Uno Work on this computer.";
  const p = STATE.provider;
  $("#uno-provider-note").textContent = p.name === "uno" && p.ready
    ? (p.route === "app"
      ? "Works out of the box — uses the AI of this computer. How much this app may spend is set in Uno Work → Settings → Apps."
      : `Works out of the box — using ${p.source}. Usage is billed to your Uno AI balance.`)
    : p.name === "uno" ? "Can't reach Uno AI from this computer. It works by itself on a Uno computer; anywhere else, choose another provider." : "Works by itself on a Uno computer, no keys. Usage is billed to your Uno AI balance.";
  $("#ai-route").textContent = p.ready ? `Now in use: ${p.route_label} · notes model ${p.model}` : "Now in use: nothing yet";
  f.querySelector('[name="model"]').placeholder = p.route === "app" ? "default — this computer's choice" : "deepseek/deepseek-v3.2";
  $("#test-out").innerHTML = "";
  syncCustom();
  renderTelegram(STATE.telegram);
  calBoxData = null;
  $("#cal-box").innerHTML = `<p class="muted">Loading…</p>`;
  renderCalendarBox();
  $("#adv-more").open = section === "set-adv";
  $("#dlg-settings").showModal();
  $("#set-body").scrollTop = 0;
  if (section && section !== "set-cal") requestAnimationFrame(() => setSection(section, true));
  else markSection("set-cal");
}
function markSection(id) { $$("#set-nav a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === `#${id}`)); }
function setSection(id, instant) {
  if (id === "set-adv") $("#adv-more").open = true;
  const body = $("#set-body"), el = $(`#${id}`);
  body.scrollTo({ top: el.offsetTop - body.offsetTop - 4, behavior: instant ? "instant" : "smooth" });
  markSection(id);
}
$$("#set-nav a").forEach((a) => a.onclick = (e) => { e.preventDefault(); setSection(a.getAttribute("href").slice(1)); });
$("#set-body").addEventListener("scroll", () => {
  const body = $("#set-body");
  if (body.scrollTop + body.clientHeight >= body.scrollHeight - 4) { markSection("set-adv"); return; }
  let cur = "set-cal";
  for (const sec of $$(".set-sec", body)) if (sec.offsetTop - body.offsetTop - 40 <= body.scrollTop) cur = sec.id;
  markSection(cur);
}, { passive: true });
function syncCustom() {
  const f = $("#settings-form");
  f.querySelector(".custom-fields").hidden = f.querySelector('[name="provider"]:checked')?.value !== "custom";
  const local = f.querySelector('[name="stt"]:checked')?.value === "local";
  f.querySelector(".stt-local").hidden = !local;
  f.querySelector(".stt-provider").hidden = local;
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
$("#btn-settings").onclick = () => openSettings();
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
    const line = (label, x) => `<p class="${x.ok ? "ok" : "bad"}">${ic(x.ok ? "check" : "x")} ${label}: ${esc(x.detail)}</p>`;
    out.innerHTML = line("Notes model", r.notes) + line("Speech-to-text", r.stt);
  } catch (e) { out.innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
};

// ---- routing ---------------------------------------------------------------

$("#btn-record-phone").onclick = () => openRecordDialog();
function newAction(kind) {
  if (kind === "bot") openBotDialog();
  else if (kind === "upload") pickFile();
  else openRecordDialog(kind);
}
function bindNew(root) {
  $$("[data-new]", root).forEach((b) => b.onclick = (e) => { e.stopPropagation(); closeMenus(); newAction(b.dataset.new); });
}
function closeMenus() {
  $$(".menu").forEach((m) => { m.hidden = true; });
  $$("[aria-expanded=true]").forEach((b) => b.setAttribute("aria-expanded", "false"));
}
$("#btn-new").onclick = (e) => {
  e.stopPropagation();
  const m = $("#new-menu"), open = m.hidden;
  closeMenus();
  if (recording) { openRecordDialog(); return; }
  m.hidden = !open;
  $("#btn-new").setAttribute("aria-expanded", String(open));
  if (open) m.querySelector("button").focus();
};
bindNew($(".side"));
document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) closeMenus(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenus(); });
$("#main").addEventListener("scroll", () => $("#main").classList.toggle("scrolled", $("#main").scrollTop > 4), { passive: true });
let searchTimer;
$("#search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadList, 200); };

function route() {
  const h = location.hash;
  let m;
  if ((m = h.match(/^#\/m\/([a-z0-9]{10})(?:\/t\/(\d+))?$/))) openMeeting(m[1], m[2] !== undefined ? Number(m[2]) : null);
  else if (h === "#/actions") showActions();
  else if (h === "#/calls") showCalls();
  else if (h === "#/share") takeSharedFile();
  else if (h === "#/share-missed") { history.replaceState(null, "", "#/"); showHome(); toast("Open Notetaker once from the Home Screen, then share the recording again.", "bad"); }
  else showHome();
}
window.addEventListener("hashchange", route);

(async () => {
  await loadState();
  route();
  if ("serviceWorker" in navigator && isSecureContext) navigator.serviceWorker.register("/sw.js").catch(() => {});
  setInterval(() => { if (!recording && document.visibilityState === "visible") { loadList(); refreshBot(); } }, 15000);
})();
