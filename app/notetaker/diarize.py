"""Who spoke when — by voice, on the CPU, in well under a second per minute.

Every transcript line is already one utterance (chunks are cut at pauses), so
diarization is: a voice fingerprint (NeMo TitaNet-small speaker embedding via
sherpa-onnx, 40 MB model, no GPU) per line → group lines whose voices match. The notes
model then only has to put names on the groups, which it does well; asked to
tell speakers apart from text alone, it invents people.
"""
from __future__ import annotations

import logging
import os
import wave

import numpy as np

log = logging.getLogger("notetaker.diarize")
MODEL = os.environ.get("SPEAKER_MODEL", "/opt/models/speaker.onnx")
# TitaNet-small on the test meetings: same person ≥ 0.84, different people ≤ 0.46
# (even one TTS voice pitched down). 0.6 sits between with room on both sides.
SAME_VOICE = 0.6      # cosine similarity above which two groups are one person
MIN_EMBED_S = 0.8     # shorter lines get the voice of the closest group afterwards

_extractor = None


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(MODEL)


def _get_extractor():
    global _extractor
    if _extractor is None:
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=2)
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    return _extractor


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0, rate


def embed(samples: np.ndarray, rate: int, start: float, end: float) -> np.ndarray | None:
    if end - start < MIN_EMBED_S:
        return None
    piece = samples[int(start * rate): int(end * rate)]
    ex = _get_extractor()
    stream = ex.create_stream()
    stream.accept_waveform(rate, piece)
    stream.input_finished()
    if not ex.is_ready(stream):
        return None
    v = np.array(ex.compute(stream), dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else None


def cluster(vectors: list[np.ndarray], threshold: float = SAME_VOICE) -> list[int]:
    """Agglomerative clustering on centroids, merging the closest pair while
    they are more alike than `threshold`. Returns a group index per vector."""
    groups = [[i] for i in range(len(vectors))]
    cents = np.stack(vectors)
    while len(groups) > 1:
        sims = cents @ cents.T
        np.fill_diagonal(sims, -1.0)
        a, b = np.unravel_index(np.argmax(sims), sims.shape)
        if sims[a, b] < threshold:
            break
        a, b = min(a, b), max(a, b)
        groups[a] += groups.pop(b)
        c = np.mean([vectors[i] for i in groups[a]], axis=0)
        cents[a] = c / np.linalg.norm(c)
        cents = np.delete(cents, b, axis=0)
    labels = [0] * len(vectors)
    # biggest group first → "Speaker 1" is whoever talks most
    for gi, g in enumerate(sorted(groups, key=len, reverse=True)):
        for i in g:
            labels[i] = gi
    return labels


def diarize(wav_path: str, segments: list[dict]) -> list[int] | None:
    """A group number per segment (same order), or None if it can't tell."""
    if not available() or len(segments) < 2:
        return None
    samples, rate = read_wav(wav_path)
    embs = [embed(samples, rate, s["start"], s["end"]) for s in segments]
    idx = [i for i, e in enumerate(embs) if e is not None]
    if len(idx) < 2:
        return None
    labels_known = cluster([embs[i] for i in idx])
    groups = max(labels_known) + 1
    out: list[int | None] = [None] * len(segments)
    for i, lab in zip(idx, labels_known):
        out[i] = lab
    # short lines: take the group of the nearest line in time that has one
    for i in range(len(out)):
        if out[i] is None:
            near = min(idx, key=lambda j: abs(segments[j]["start"] - segments[i]["start"]))
            out[i] = out[near]
    log.info("diarization: %d lines → %d voices", len(segments), groups)
    return out  # type: ignore[return-value]
