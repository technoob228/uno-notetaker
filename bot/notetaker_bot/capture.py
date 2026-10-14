"""The call's sound: one PulseAudio sink per call, recorded by ffmpeg.

Each call's Chromium plays into its own null sink (PULSE_SINK), so two calls
at once never mix. ffmpeg records that sink's monitor to Ogg/Opus on stdout
(nothing seeks back, so the bytes can be sent as they come) and runs
silencedetect, which tells the call loop when nobody has spoken for a while.

The microphone every browser sees is a sink nobody plays into: silence,
whatever a platform does with our mic.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time

log = logging.getLogger("bot.capture")
SILENCE = "nt_silence"  # a sink nothing plays into
MIC = "nt_mic"          # …remapped as a real source: Chromium ignores "monitor" sources, and
                        # with no microphone at all some platforms join without audio


def _pactl(*args: str) -> str:
    return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=10, check=True).stdout.strip()


def setup_pulse() -> None:
    """Once per container: a silent microphone, and a default sink that goes
    nowhere (sound outside any call — there should be none)."""
    sinks = _pactl("list", "short", "sinks")
    if SILENCE not in sinks:
        _pactl("load-module", "module-null-sink", f"sink_name={SILENCE}",
               f"sink_properties=device.description={SILENCE}")
    if "nt_void" not in sinks:
        _pactl("load-module", "module-null-sink", "sink_name=nt_void", "sink_properties=device.description=nt_void")
    if MIC not in _pactl("list", "short", "sources"):
        _pactl("load-module", "module-remap-source", f"master={SILENCE}.monitor", f"source_name={MIC}",
               "source_properties=device.description=Microphone")
    _pactl("set-default-source", MIC)
    _pactl("set-default-sink", "nt_void")


class Sink:
    def __init__(self, sid: str):
        self.name = f"nt_{re.sub(r'[^a-z0-9]', '', sid.lower())[:16]}"
        self.module = ""

    def __enter__(self) -> "Sink":
        self.module = _pactl("load-module", "module-null-sink", f"sink_name={self.name}",
                             f"sink_properties=device.description={self.name}")
        return self

    def __exit__(self, *exc) -> None:
        if self.module:
            try:
                _pactl("unload-module", self.module)
            except (subprocess.SubprocessError, OSError) as e:
                log.warning("unload %s: %s", self.name, e)


class Recorder:
    """ffmpeg: sink monitor → Ogg/Opus pieces in `self.pending`, and the
    loudness of every half second on stderr (when did anyone last speak)."""

    SPEECH_DB = -55.0  # RMS above this is someone talking (a quiet call sits near -90 or -inf)

    def __init__(self, sink: str):
        self.sink = sink
        self.proc: asyncio.subprocess.Process | None = None
        self.pending = bytearray()
        self.total = 0
        self.started = 0.0
        self.last_sound = 0.0
        self.heard_anything = False
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.started = self.last_sound = time.time()
        meter = ("[0:a]asplit=2[out][m];[m]aformat=channel_layouts=mono,asetnsamples=n=24000:p=0,"
                 "astats=metadata=1:reset=1,"
                 "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=/dev/stderr,anullsink")
        self.proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-f", "pulse", "-fragment_size", "4096", "-i", f"{self.sink}.monitor",
            "-filter_complex", meter, "-map", "[out]", "-ac", "1", "-ar", "48000",
            "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
            "-f", "ogg", "-flush_packets", "1", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._tasks = [asyncio.create_task(self._read_audio()), asyncio.create_task(self._read_log())]

    async def _read_audio(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                return
            self.pending += chunk
            self.total += len(chunk)

    async def _read_log(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            s = line.decode(errors="replace").strip()
            if "RMS_level=" in s:
                try:
                    db = float(s.split("=", 1)[1])
                except ValueError:
                    continue  # "-inf"
                if db > self.SPEECH_DB:
                    self.last_sound = time.time()
                    self.heard_anything = True
            elif s and not s.startswith(("frame:", "lavfi.")) and "Guessed Channel Layout" not in s:
                log.warning("ffmpeg: %s", s)

    def quiet_for(self) -> float:
        """Seconds since the last sound (from the start, if none yet)."""
        return time.time() - self.last_sound

    def take(self) -> bytes:
        data = bytes(self.pending)
        self.pending.clear()
        return data

    async def stop(self) -> None:
        """SIGINT: ffmpeg closes the Ogg stream properly, then the readers drain."""
        if not self.proc:
            return
        if self.proc.returncode is None:
            try:
                self.proc.send_signal(2)
                await asyncio.wait_for(self.proc.wait(), 10)
            except (asyncio.TimeoutError, ProcessLookupError):
                self.proc.kill()
        for t in self._tasks:
            try:
                await asyncio.wait_for(t, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                t.cancel()
