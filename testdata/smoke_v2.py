#!/usr/bin/env python3
"""v2 flows over the HTTP API, against fakes of the Uno Work App API and the
Telegram Bot API (see the report of 25.09 for the fakes):

  python3 smoke_v2.py http://localhost:18430 <password> <fake-app-api> <fake-telegram>
"""
import json, os, re, sys, time, urllib.request
from http.cookiejar import CookieJar
BASE, PW, APPAPI, TG = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3], sys.argv[4]
HERE = os.path.dirname(os.path.abspath(__file__))
jar = CookieJar(); op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def call(m, p, body=None, raw=None, base=BASE, opener=op):
    h, d = {}, None
    if raw is not None: d, h["Content-Type"] = raw, "application/octet-stream"
    elif body is not None: d, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    with opener.open(urllib.request.Request(base + p, data=d, method=m, headers=h), timeout=200) as r:
        t = r.read().decode()
        return json.loads(t) if "json" in r.headers.get("content-type", "") else t

def wait(mid):
    t0 = time.time()
    while time.time() - t0 < 600:
        m = call("GET", f"/api/meetings/{mid}")
        if m["status"] in ("done", "error"): return m, time.time() - t0
        time.sleep(2)
    raise SystemExit("timeout")

def upload(name, template="general"):
    m = call("POST", "/api/meetings", {"source": "upload", "title": "", "template": template, "tz_offset": -180})
    call("PUT", f"/api/meetings/{m['id']}/tracks/upload?ext={name.rsplit('.', 1)[1]}&offset=0",
         raw=open(os.path.join(HERE, name), "rb").read())
    call("POST", f"/api/meetings/{m['id']}/finish")
    return m["id"]

ok = lambda cond, what: print(("PASS " if cond else "FAIL ") + what) or cond
call("POST", "/login", {"password": PW})
st = call("GET", "/api/state")
ok(st["provider"]["route"] == "app" and st["inbox"], f"route=app, inbox available ({st['provider']['route']})")

en = upload("dialog-en.m4a"); m, took = wait(en)
ok(m["status"] == "done", f"EN upload done in {took:.0f}s: {m['title']!r} (created_at {m['created_at']})")
ok("-03:00" in m["created_at"], "created_at carries the browser's offset")
acts = m["actions"]; ok(len(acts) >= 2, f"{len(acts)} action items parsed: " + "; ".join(f"{a['owner']}|{a['text']}|{a['due']}|{a['ts']}" for a in acts))
ok(m["talk_time"] and m["talk_time"][0]["share"] > 0, "talk time: " + ", ".join(f"{x['speaker']} {x['share']:.0%}" for x in m["talk_time"]))
log = call("GET", "/_log", base=APPAPI)
ok(any("notify" in x for x in log), "Inbox notify: " + json.dumps([x for x in log if "notify" in x][-1:], ensure_ascii=False))
ok(m.get("delivery", {}).get("inbox") == "sent", f"delivery recorded: {m.get('delivery')}")

# toggle + across meetings
a0 = acts[0]
call("PATCH", f"/api/meetings/{en}/actions/{a0['id']}", {"done": True})
notes = call("GET", f"/api/meetings/{en}")["notes"]
ok("- [x]" in notes, "done ticks the checkbox in notes.md")
open_all = call("GET", "/api/actions")["actions"]
ok(all(x["id"] != a0["id"] for x in open_all) and len(open_all) == len(acts) - 1, f"/api/actions open = {len(open_all)}")
call("PATCH", f"/api/meetings/{en}/actions/{a0['id']}", {"done": False})

# hand to AI
r = call("POST", f"/api/meetings/{en}/actions/{acts[1]['id']}/ai")
task = [x for x in call("GET", "/_log", base=APPAPI) if "task" in x][-1]["task"]
ok(r["task"] == "task_1" and task["tools"] == "ask" and task["cwd"].startswith("~/Meetings/"), f"task: tools={task['tools']} cwd={task['cwd']!r} title={task['title']!r}")

# rename speaker
spk = next((x["speaker"] for x in m["talk_time"] if x["speaker"].startswith("Speaker")), None)
if spk:
    m2 = call("POST", f"/api/meetings/{en}/speakers", {"from": spk, "to": "Mike"})
    ok(any(s["speaker"] == "Mike" for s in m2["segments"]) and spk not in m2["notes"], f"renamed {spk} → Mike in transcript and notes")

ru = upload("dialog-ru.m4a", "client"); m, took = wait(ru)
ok(m["status"] == "done", f"RU upload done in {took:.0f}s: {m['title']!r}, {len(m['actions'])} actions")

# search
hits = call("GET", "/api/meetings?q=" + urllib.request.quote("apple pay"))["meetings"]
ok(hits and hits[0]["id"] == en and hits[0]["hits"], "search 'apple pay': " + json.dumps(hits[0]["hits"][:2], ensure_ascii=False) if hits else "search 'apple pay': nothing")
hits = call("GET", "/api/meetings?q=" + urllib.request.quote("ндс"))["meetings"]
ok(hits and hits[0]["id"] == ru, f"search 'ндс' → {[h['title'] for h in hits]}")
ok(not call("GET", "/api/meetings?q=" + urllib.request.quote("apple ндс"))["meetings"], "all words must match")

# widget
k = re.search(r"k=([0-9a-f]+)", json.dumps(st)) or None
man = json.load(open("/tmp/nt-home/apps/notetaker.json"))
anon = urllib.request.build_opener()
w = call("GET", man["widget"]["path"], opener=anon)
ok("action item" in w and m["title"][:20] in w, "widget with key shows the last meeting")
try:
    call("GET", "/widget?k=wrong", opener=anon); ok(False, "widget without key refused")
except urllib.error.HTTPError as e: ok(e.code == 401, "widget without key refused")

# export
md = call("GET", f"/api/meetings/{en}/export?what=all")
ok("# Transcript" in md and "## Summary" in md, f"export all: {len(md)} chars")

# telegram: connect, pair, voice in, notes out, /todo
t = call("POST", "/api/telegram/connect", {"token": "123:fake"})
ok(t.get("code") and "uno_notes_test_bot" in t.get("link", ""), f"telegram connect → {t.get('link')}")
call("POST", "/_push", {"message_id": 1, "chat": {"id": 555, "type": "private", "first_name": "Misha"}, "text": f"/start {t['code']}"}, base=TG)
for _ in range(20):
    if call("GET", "/api/telegram")["paired"]: break
    time.sleep(0.5)
ok(call("GET", "/api/telegram")["paired"], "paired with chat 555")
call("POST", "/_push", {"message_id": 2, "chat": {"id": 777, "type": "private"}, "text": "/todo"}, base=TG)
call("POST", "/_file", {"id": "voice1", "path": os.path.join(HERE, "dialog-en.m4a")}, base=TG)
call("POST", "/_push", {"message_id": 3, "chat": {"id": 555, "type": "private"}, "caption": "Sync with Sarah",
                        "voice": {"file_id": "voice1", "duration": 58, "file_size": 475831}}, base=TG)
time.sleep(4)
tgm = [x for x in call("GET", "/api/meetings")["meetings"] if x["source"] == "telegram"]
ok(tgm, "voice message → meeting " + (tgm[0]["title"] if tgm else "?"))
if tgm:
    m, took = wait(tgm[0]["id"]); time.sleep(2)
sent = call("GET", "/_sent", base=TG)["result"]
ok(any(s["chat_id"] == "777" and "private" in s["text"] for s in sent), "stranger gets 'private bot'")
notes_msg = [s for s in sent if s["chat_id"] == "555" and "Action items" in s["text"]]
ok(notes_msg and notes_msg[-1].get("reply_parameters", {}).get("message_id") == 3, "notes sent back as a reply to the voice message")
if notes_msg: print(notes_msg[-1]["text"])
call("POST", "/_push", {"message_id": 4, "chat": {"id": 555, "type": "private"}, "text": "/todo"}, base=TG)
time.sleep(3)
todo = [s for s in call("GET", "/_sent", base=TG)["result"] if "Open action items" in s["text"]]
ok(todo, "/todo answered")
print("ids", en, ru)
