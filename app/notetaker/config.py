"""Settings and where the AI key comes from.

Two places hold state:
  MEETINGS_DIR  ~/Meetings on the computer — one folder per meeting, plain
                files (audio, transcript.md, notes.md) so Files and the AI
                on the machine see them;
  STATE_DIR     a docker volume — settings (may hold an external API key),
                the cookie secret, share links. Never in the home folder.

The Uno Gateway key is found, in order:
  1. UNO_LLM_API_KEY in the environment (App Store install mints a key just
     for this app);
  2. the Uno Work machine's own gateway key, read-only from its settings
     file (UNO_WORK_SETTINGS, mounted by compose) — what makes the app work
     on a Uno computer with zero setup today.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field

MEETINGS_DIR = os.environ.get("MEETINGS_DIR", "/meetings")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
UNO_WORK_SETTINGS = os.environ.get("UNO_WORK_SETTINGS", "/uno-work/settings.json")
UNO_GATEWAY_URL = os.environ.get("UNO_LLM_BASE_URL", "https://api.getuno.xyz/v1").rstrip("/")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
APP_URL = os.environ.get("UNO_APP_URL", "")
APP_VERSION = "0.1.0"
USER_AGENT = f"uno-notetaker/{APP_VERSION}"

DEFAULT_MODEL = "deepseek/deepseek-v3.2"
MODEL_CHOICES = [
    ("deepseek/deepseek-v3.2", "DeepSeek V3.2 — fast, cheap (default)"),
    ("~anthropic/claude-sonnet-latest", "Claude Sonnet — best quality"),
    ("~anthropic/claude-haiku-latest", "Claude Haiku — quick"),
    ("qwen/qwen3.5-plus-20260420", "Qwen 3.5 Plus"),
]


@dataclass
class Settings:
    provider: str = "uno"              # "uno" | "custom"
    custom_base_url: str = ""
    custom_api_key: str = ""
    model: str = DEFAULT_MODEL
    stt: str = "provider"              # "provider" (the AI provider's whisper) | "local"
    stt_model: str = "whisper-1"
    local_model: str = "base"          # tiny | base | small
    language: str = "auto"             # auto | ru | en — speech language hint
    notes_language: str = "auto"       # auto (= speech) | ru | en
    template: str = "general"
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        d = asdict(self)
        d["custom_api_key"] = ("•" * 8 + self.custom_api_key[-4:]) if self.custom_api_key else ""
        d["custom_api_key_set"] = bool(self.custom_api_key)
        return d


_lock = threading.Lock()


def _settings_path() -> str:
    return os.path.join(STATE_DIR, "settings.json")


def load_settings() -> Settings:
    try:
        with open(_settings_path()) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    s = Settings()
    for k, v in raw.items():
        if hasattr(s, k) and isinstance(v, type(getattr(s, k))):
            setattr(s, k, v)
    return s


def save_settings(update: dict) -> Settings:
    with _lock:
        s = load_settings()
        for k, v in update.items():
            if k == "custom_api_key" and (v is None or str(v).startswith("•")):
                continue  # masked value came back from the form: keep the stored key
            if hasattr(s, k) and k != "extra" and isinstance(v, type(getattr(s, k))):
                setattr(s, k, v.strip() if isinstance(v, str) else v)
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = _settings_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(asdict(s), fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _settings_path())
        return s


def cookie_secret() -> bytes:
    path = os.path.join(STATE_DIR, "cookie.key")
    try:
        with open(path, "rb") as fh:
            key = fh.read()
            if len(key) >= 32:
                return key
    except OSError:
        pass
    os.makedirs(STATE_DIR, exist_ok=True)
    key = secrets.token_bytes(32)
    with open(path, "wb") as fh:
        fh.write(key)
    os.chmod(path, 0o600)
    return key


def uno_gateway_key() -> tuple[str, str]:
    """(key, where it came from) — empty key when this computer has none."""
    env = os.environ.get("UNO_LLM_API_KEY", "").strip()
    if env:
        return env, "app key from the App Store install"
    try:
        with open(UNO_WORK_SETTINGS) as fh:
            key = ((json.load(fh).get("uno") or {}).get("apiKey") or "").strip()
        if key:
            return key, "this computer's Uno AI key"
    except (OSError, ValueError, AttributeError):
        pass
    return "", ""


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    source: str

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key)


def ai_provider(s: Settings | None = None) -> Provider:
    s = s or load_settings()
    if s.provider == "custom":
        return Provider("custom", s.custom_base_url.rstrip("/"), s.custom_api_key, "your provider")
    key, source = uno_gateway_key()
    return Provider("uno", UNO_GATEWAY_URL, key, source)
