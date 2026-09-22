"""Recording → transcript → notes. One meeting at a time (RAM on a 2 GB computer)."""
from __future__ import annotations

import difflib
import json
import logging
import os
import queue
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import ai, audio, store
from .config import ai_provider, load_settings

log = logging.getLogger("notetaker")
_jobs: "queue.Queue[tuple[str, str]]" = queue.Queue()
_queued: set[tuple[str, str]] = set()
_qlock = threading.Lock()

SPEAKER = {"mic": "Me", "tab": "Others", "upload": "", "bot": ""}
STT_WORKERS = 4


def enqueue(mid: str, kind: str = "full") -> None:
    with _qlock:
        if (mid, kind) in _queued:
            return
        _queued.add((mid, kind))
    _jobs.put((mid, kind))


def start_worker() -> None:
    def loop():
        while True:
            mid, kind = _jobs.get()
            with _qlock:
                _queued.discard((mid, kind))
            try:
                if kind == "full":
                    transcribe(mid)
                    summarize(mid)
                elif kind == "notes":
                    summarize(mid)
            except Exception as exc:  # noqa: BLE001 — every failure must reach the UI
                log.exception("job %s %s failed", kind, mid)
                msg = str(exc) if isinstance(exc, (ai.AIError, RuntimeError)) else f"Unexpected error: {exc}"
                store.update(mid, status="error", error=msg, progress="")

    threading.Thread(target=loop, daemon=True, name="notetaker-worker").start()
    # Resume what a restart interrupted.
    for m in store.list_all():
        if m["status"] in ("queued", "transcribing"):
            enqueue(m["id"], "full")
        elif m["status"] == "summarizing":
            enqueue(m["id"], "notes")


def _norm(text: str) -> str:
    return re.sub(r"[^\w ]+", "", text.lower())


def drop_echo(segments: list[dict]) -> list[dict]:
    """Without headphones the mic hears the call too: drop a "Me" line that
    repeats an "Others" line said at the same time."""
    others = [s for s in segments if s["speaker"] == "Others"]
    out = []
    for s in segments:
        if s["speaker"] == "Me":
            dup = False
            for o in others:
                if o["start"] - 3 <= s["start"] <= o["end"] + 3:
                    if difflib.SequenceMatcher(None, _norm(s["text"]), _norm(o["text"])).ratio() > 0.55:
                        dup = True
                        break
            if dup:
                continue
        out.append(s)
    return out


def guess_speakers(segments: list[dict], s) -> list[dict]:
    """One recording, several people: ask the notes model who said each line.

    Cheap (the answer is one short label per line) and optional — any failure
    leaves the transcript without names. Lines are utterances cut at pauses,
    so a line rarely mixes two people.
    """
    if len(segments) > 900:
        return segments
    numbered = "\n".join(f"{i}. {x['text']}" for i, x in enumerate(segments))
    prompt = ("Below are numbered consecutive utterances from one meeting recording. Decide who says each. "
              "Use a person's real name when the conversation reveals it (someone is addressed by name or "
              "introduces themselves), otherwise 'Speaker 1', 'Speaker 2', …. The same person must always get "
              "the same label. Reply with JSON only: {\"labels\": [label for 0, label for 1, …]} — exactly "
              f"{len(segments)} labels.")
    try:
        text, _ = ai.chat(ai_provider(s), s, [{"role": "system", "content": prompt},
                                              {"role": "user", "content": numbered}],
                          max_tokens=min(8000, 40 + 12 * len(segments)), temperature=0)
        m = re.search(r"\{.*\}", text, re.S)
        labels = json.loads(m.group(0))["labels"] if m else None
    except (ai.AIError, ValueError, KeyError, TypeError) as exc:
        log.warning("speaker guess skipped: %s", exc)
        return segments
    if not isinstance(labels, list) or len(labels) != len(segments):
        log.warning("speaker guess skipped: %s labels for %s lines", len(labels or []), len(segments))
        return segments
    for seg, label in zip(segments, labels):
        seg["speaker"] = str(label)[:40].strip()
        seg["guessed"] = True
    return segments


def transcript_md(meta: dict, segments: list[dict]) -> str:
    title = meta.get("title") or "Meeting"
    lines = [f"# Transcript — {title}", "",
             f"_{meta['created_at'].replace('T', ' ')[:16]} · {audio.fmt_ts(meta.get('duration', 0))}_", ""]
    for s in segments:
        who = f" {s['speaker']}:" if s.get("speaker") else ""
        lines.append(f"**[{audio.fmt_ts(s['start'])}]**{who} {s['text']}")
        lines.append("")
    return "\n".join(lines)


def transcript_for_ai(segments: list[dict]) -> str:
    return "\n".join(f"[{audio.fmt_ts(s['start'])}]" + (f" {s['speaker']}:" if s.get("speaker") else "")
                     + f" {s['text']}" for s in segments)


def _remote_track(mid: str, wav: str, total: float, speaker: str, tmp: str, done_counter: list) -> tuple[list, float]:
    s = load_settings()
    p = ai_provider(s)
    regions = audio.speech_regions(wav, total)
    chunks = audio.plan_chunks(regions, total)
    done_counter[1] += len(chunks)
    billed = sum(c.length for c in chunks)

    def one(i_chunk):
        i, c = i_chunk
        path = os.path.join(tmp, f"{speaker or 'x'}-{i}.ogg")
        audio.cut(wav, c, path)
        res = ai.transcribe_remote(p, s, path)
        os.remove(path)
        done_counter[0] += 1
        store.update(mid, progress=f"Transcribing… {done_counter[0]}/{done_counter[1]}")
        if res["segments"]:
            return [{"start": c.start + x["start"], "end": c.start + x["end"], "speaker": speaker,
                     "text": ai.clean_text(x["text"])} for x in res["segments"]]
        return [{"start": c.start, "end": c.end, "speaker": speaker, "text": ai.clean_text(res["text"])}]

    with ThreadPoolExecutor(max_workers=STT_WORKERS) as pool:
        parts = list(pool.map(one, enumerate(chunks)))
    return [seg for part in parts for seg in part if seg["text"]], billed


def transcribe(mid: str) -> None:
    meta = store.get(mid)
    if not meta:
        return
    tracks = meta.get("tracks") or {}
    if not tracks:
        raise RuntimeError("Nothing was recorded — the recording is empty.")
    store.update(mid, status="transcribing", progress="Preparing audio…", error="")
    s = load_settings()
    started = time.time()
    tmp = tempfile.mkdtemp(prefix="nt-", dir=os.path.join(store.folder(mid), "audio"))
    try:
        sources, wavs = [], {}
        for track, info in tracks.items():
            src = store.track_path(mid, track, info["ext"])
            if not os.path.exists(src) or os.path.getsize(src) == 0:
                continue
            wav = os.path.join(tmp, f"{track}.wav")
            audio.to_wav(src, wav)
            sources.append(src)
            wavs[track] = wav
        if not wavs:
            raise RuntimeError("Nothing was recorded — the recording is empty.")
        total = max(audio.duration(w) for w in wavs.values())
        store.update(mid, duration=round(total, 1), progress="Preparing audio…")
        audio.mix_for_playback(sources, os.path.join(store.folder(mid), "audio", "recording.ogg"))

        segments: list[dict] = []
        billed = 0.0
        counter = [0, 0]
        for track, wav in wavs.items():
            speaker = SPEAKER.get(track, "") if len(wavs) > 1 else ""
            if s.stt == "local":
                store.update(mid, progress=f"Transcribing on this computer (Whisper {s.local_model})…")
                segs = [{"start": x["start"], "end": x["end"], "speaker": speaker,
                         "text": ai.clean_text(x["text"])} for x in ai.transcribe_local(s, wav)]
                segments += [x for x in segs if x["text"]]
            else:
                segs, b = _remote_track(mid, wav, total, speaker, tmp, counter)
                segments += segs
                billed += b
        segments.sort(key=lambda x: x["start"])
        if len(wavs) > 1:
            segments = drop_echo(segments)
        elif segments:
            store.update(mid, progress="Telling speakers apart…")
            segments = guess_speakers(segments, s)
        if not segments:
            raise RuntimeError("No speech was found in the recording.")
        meta = store.get(mid)
        store.write_text(mid, "transcript.json", json.dumps(segments, ensure_ascii=False, indent=1))
        store.write_text(mid, "transcript.md", transcript_md(meta, segments))
        usage = meta.get("usage") or {}
        usage.update({"stt": "local" if s.stt == "local" else ai_provider(s).name,
                      "stt_audio_seconds": round(billed, 1),
                      "stt_wall_seconds": round(time.time() - started, 1)})
        store.update(mid, usage=usage, status="summarizing", progress="Writing notes…")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def summarize(mid: str) -> None:
    meta = store.get(mid)
    try:
        segments = json.loads(store.read_text(mid, "transcript.json") or "[]")
    except ValueError:
        segments = []
    if not segments:
        raise RuntimeError("There is no transcript yet.")
    store.update(mid, status="summarizing", progress="Writing notes…", error="")
    s = load_settings()
    p = ai_provider(s)
    started = time.time()
    text, usage = ai.chat(p, s, [
        {"role": "system", "content": ai.notes_prompt(meta.get("template") or s.template,
                                                      meta.get("language") if meta.get("language") in ("ru", "en")
                                                      else s.notes_language,
                                                      two_track=any(x.get("speaker") == "Me" for x in segments))},
        {"role": "user", "content": transcript_for_ai(segments)},
    ])
    title = meta.get("title") or ""
    m = re.match(r"\s*#\s+(.+)", text)
    if m and not title:
        title = m.group(1).strip()[:80]
    store.write_text(mid, "notes.md", text + "\n")
    u = meta.get("usage") or {}
    u.update({"model": s.model, "notes_prompt_tokens": usage.get("prompt_tokens"),
              "notes_completion_tokens": usage.get("completion_tokens"),
              "notes_cost": usage.get("cost"), "notes_seconds": round(time.time() - started, 1)})
    store.update(mid, status="done", progress="", title=title, usage=u)
    if title and meta.get("title") != title:
        # the transcript header carries the title too
        store.write_text(mid, "transcript.md", transcript_md(store.get(mid), segments))
