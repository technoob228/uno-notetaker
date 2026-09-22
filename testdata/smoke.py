#!/usr/bin/env python3
"""End-to-end smoke test over the HTTP API (what the UI does).

  python3 smoke.py https://notetaker-<box>.app.uno4.dev <password>

1. upload dialog-ru.m4a       → transcript + notes (client-call template)
2. upload dialog-en.m4a       → transcript + notes (general)
3. "browser recording": dialog-en-mic.webm + dialog-en-tab.webm streamed in
   5-second-ish pieces as two tracks, exactly like MediaRecorder uploads
4. Ask about meeting 3, share meeting 1 and open the public link.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE, PASSWORD = sys.argv[1].rstrip("/"), sys.argv[2]
HERE = os.path.dirname(os.path.abspath(__file__))
jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
UA = {"User-Agent": "uno-notetaker-smoke/0.1"}


def call(method, path, body=None, raw=None):
    headers = dict(UA)
    data = None
    if raw is not None:
        data = raw
        headers["Content-Type"] = "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with opener.open(req, timeout=180) as r:
            txt = r.read().decode()
            return json.loads(txt) if r.headers.get("content-type", "").startswith("application/json") else txt
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} → {e.code} {e.read()[:300]!r}")


def wait(mid, limit=600):
    t0 = time.time()
    last = ""
    while time.time() - t0 < limit:
        m = call("GET", f"/api/meetings/{mid}")
        line = f"{m['status']} {m['progress']}"
        if line != last:
            print(f"   {time.time() - t0:5.1f}s  {line}")
            last = line
        if m["status"] in ("done", "error"):
            return m, time.time() - t0
        time.sleep(2)
    raise SystemExit("timeout")


def upload(name, template, language="auto"):
    m = call("POST", "/api/meetings", {"source": "upload", "title": "", "template": template, "language": language})
    data = open(os.path.join(HERE, name), "rb").read()
    ext = name.rsplit(".", 1)[1]
    call("PUT", f"/api/meetings/{m['id']}/tracks/upload?ext={ext}&offset=0", raw=data)
    call("POST", f"/api/meetings/{m['id']}/finish")
    return m["id"]


def browser_like(template):
    m = call("POST", "/api/meetings", {"source": "browser", "title": "", "template": template, "language": "auto"})
    tracks = {t: open(os.path.join(HERE, f"dialog-en-{t}.webm"), "rb").read() for t in ("mic", "tab")}
    offs = {t: 0 for t in tracks}
    piece = 20_000  # ~5 s of 32 kbps opus, like MediaRecorder timeslice=5000
    while any(offs[t] < len(tracks[t]) for t in tracks):
        for t, data in tracks.items():
            if offs[t] < len(data):
                r = call("PUT", f"/api/meetings/{m['id']}/tracks/{t}?ext=webm&offset={offs[t]}",
                         raw=data[offs[t]: offs[t] + piece])
                offs[t] = r["bytes"]
    call("POST", f"/api/meetings/{m['id']}/finish")
    return m["id"]


def show(m, took):
    print(f"== {m['title']!r} status={m['status']} duration={m['duration']}s took={took:.0f}s")
    if m["error"]:
        print("   ERROR:", m["error"])
    for s in m["segments"][:40]:
        print(f"   [{int(s['start'])//60:02d}:{int(s['start'])%60:02d}] {s.get('speaker') or '':>10} | {s['text'][:110]}")
    print("   --- notes ---")
    print("   " + m["notes"].replace("\n", "\n   ")[:2500])
    print("   usage:", m.get("usage"))


print("login:", call("POST", "/login", {"password": PASSWORD}))
st = call("GET", "/api/state")
print("provider:", st["provider"])
print("test:", json.dumps(call("POST", "/api/settings/test"), ensure_ascii=False))
ids = {}
for label, fn in (("ru-upload", lambda: upload("dialog-ru.m4a", "client")),
                  ("en-upload", lambda: upload("dialog-en.m4a", "general")),
                  ("en-browser-2track", lambda: browser_like("standup"))):
    print(f"\n### {label}")
    mid = fn()
    m, took = wait(mid)
    show(m, took)
    ids[label] = mid

print("\n### ask")
r = call("POST", f"/api/meetings/{ids['en-browser-2track']}/ask",
         {"question": "Who will fix the Android login crash and when? Answer in one sentence."})
print("  ", r["answer"])
print("\n### share")
s = call("POST", f"/api/meetings/{ids['ru-upload']}/share", {"transcript": True})
url = s["url"] if s["url"].startswith("http") else BASE + s["url"]
html = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read().decode()
print("  ", url, "→", len(html), "bytes; has notes:", "<h1>" in html)
print("\nsearch 'НДС':", [m["title"] for m in call("GET", "/api/meetings?q=%D0%9D%D0%94%D0%A1")["meetings"]])
print("ids:", json.dumps(ids))
