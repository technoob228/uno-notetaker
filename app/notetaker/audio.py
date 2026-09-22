"""ffmpeg helpers: normalise, find speech, cut into transcription chunks.

Why chunks by pauses: the Uno Gateway returns plain text (no timestamps), has
a 25 MB request cap, and bills by audio seconds. Cutting on pauses gives every
paragraph a real start time, never splits a word, and never sends long
silences to be billed (or hallucinated on by Whisper).
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

SAMPLE_RATE = 16000


def run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def duration(path: str) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", path], timeout=60)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def to_wav(src: str, dst: str) -> None:
    """Any audio/video → 16 kHz mono wav (the working copy for cutting)."""
    res = run(["ffmpeg", "-nostdin", "-y", "-i", src, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
               "-af", "highpass=f=70", "-c:a", "pcm_s16le", dst])
    if res.returncode != 0 or not os.path.exists(dst):
        tail = (res.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("could not read this file as audio: " + " ".join(tail))


def mix_for_playback(sources: list[str], dst: str) -> None:
    """One listenable file (opus) from one or more tracks."""
    args = ["ffmpeg", "-nostdin", "-y"]
    for s in sources:
        args += ["-i", s]
    if len(sources) > 1:
        args += ["-filter_complex", f"amix=inputs={len(sources)}:duration=longest:normalize=0"]
    args += ["-vn", "-ac", "1", "-ar", "24000", "-c:a", "libopus", "-b:a", "32k", dst]
    res = run(args)
    if res.returncode != 0:
        raise RuntimeError("could not build the playback file")


_SIL_START = re.compile(r"silence_start: (-?[\d.]+)")
_SIL_END = re.compile(r"silence_end: ([\d.]+)")


def speech_regions(wav: str, total: float, noise_db: int = -35, min_silence: float = 0.45) -> list[tuple[float, float]]:
    """Speech = everything that is not a silence of at least `min_silence` s."""
    res = run(["ffmpeg", "-nostdin", "-i", wav, "-af",
               f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"])
    silences: list[tuple[float, float]] = []
    start = None
    for line in res.stderr.splitlines():
        m = _SIL_START.search(line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = _SIL_END.search(line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    if start is not None:
        silences.append((start, total))
    regions, cursor = [], 0.0
    for s, e in silences:
        if s - cursor > 0.3:
            regions.append((cursor, s))
        cursor = e
    if total - cursor > 0.3:
        regions.append((cursor, total))
    return regions


@dataclass
class Chunk:
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


def plan_chunks(regions: list[tuple[float, float]], total: float,
                target: float = 6.0, hard_max: float = 30.0, turn_gap: float = 0.65,
                pad: float = 0.25) -> list[Chunk]:
    """Glue speech regions into utterance-sized chunks.

    A chunk closes at a pause of `turn_gap` seconds or more (people take turns
    there; on a per-person track it means someone else is talking), or at the
    first pause after `target` seconds. A region longer than hard_max (a
    monologue without pauses, or music) is cut at hard_max.
    """
    chunks: list[Chunk] = []
    cur: Chunk | None = None
    for s, e in regions:
        if cur is not None and (s - cur.end >= turn_gap or cur.length >= target or e - cur.start > hard_max):
            chunks.append(cur)
            cur = None
        while e - s > hard_max:  # monologue without pauses
            if cur:
                chunks.append(cur)
                cur = None
            chunks.append(Chunk(s, s + hard_max))
            s += hard_max
        if cur is None:
            cur = Chunk(s, e)
        else:
            cur.end = e
    if cur:
        chunks.append(cur)
    out = []
    for c in chunks:
        if c.length < 0.5:
            continue
        out.append(Chunk(max(0.0, c.start - pad), min(total, c.end + pad)))
    return out


def cut(wav: str, chunk: Chunk, dst: str) -> None:
    """One chunk → small opus file (≈3 KB/s; far below any upload cap)."""
    res = run(["ffmpeg", "-nostdin", "-y", "-ss", f"{chunk.start:.2f}", "-t", f"{chunk.length:.2f}",
               "-i", wav, "-ac", "1", "-c:a", "libopus", "-b:a", "24k", dst], timeout=120)
    if res.returncode != 0:
        raise RuntimeError("could not cut audio")


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
