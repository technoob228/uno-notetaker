"""Settings and where the AI key comes from.

Two places hold state:
  MEETINGS_DIR  ~/Meetings on the computer — one folder per meeting, plain
                files (audio, transcript.md, notes.md) so Files and the AI
                on the machine see them;
  STATE_DIR     a docker volume — settings (may hold an external API key),
                the cookie secret, share links. Never in the home folder.

Where the AI comes from (provider "uno"), first match wins:
  1. "AI of this computer" — the Uno Work App API (docs/app-sdk.md in
     uno-work), through the vendored uno_app SDK. The manifest asks for it
     ("ai" in ~/.uno/apps/notetaker.json); the daemon writes this app's own
     token to ~/.uno/app-keys/notetaker/, which compose mounts read-only at
     /run/uno-app (or env UNO_APP_API_URL + UNO_APP_TOKEN). Metered per app,
     the limit is in Uno Work → Settings → Apps.
  2. A Uno gateway key, for computers whose Uno Work has no App API yet:
     UNO_LLM_API_KEY in the environment (App Store install mints a key just
     for this app), else the machine's own key read-only from the Uno Work
     settings file (UNO_WORK_SETTINGS, mounted by compose).
Provider "custom" is any OpenAI-compatible endpoint + key from Settings.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field

from . import uno_app

MEETINGS_DIR = os.environ.get("MEETINGS_DIR", "/meetings")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
UNO_WORK_SETTINGS = os.environ.get("UNO_WORK_SETTINGS", "/uno-work/settings.json")
UNO_GATEWAY_URL = os.environ.get("UNO_LLM_BASE_URL", "https://api.getuno.xyz/v1").rstrip("/")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
APP_URL = os.environ.get("UNO_APP_URL", "")
APP_VERSION = "0.3.0"
USER_AGENT = f"uno-notetaker/{APP_VERSION}"

DEFAULT_MODEL = "deepseek/deepseek-v3.2"
MODEL_CHOICES = [
    ("deepseek/deepseek-v3.2", "DeepSeek V3.2 — fast, cheap (default)"),
    ("moonshotai/kimi-k2.6", "Kimi K2.6 — stronger, slower"),
    ("deepseek/deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),
]


@dataclass
class Settings:
    provider: str = "uno"              # "uno" | "custom"
    custom_base_url: str = ""
    custom_api_key: str = ""
    model: str = ""                    # "" = the default: this computer's choice (App API), else DEFAULT_MODEL
    stt: str = "provider"              # "provider" (the AI provider's whisper) | "local"
    stt_model: str = "whisper-1"
    local_model: str = "base"          # tiny | base | small
    language: str = "auto"             # auto | ru | en — speech language hint
    notes_language: str = "auto"       # auto (= speech) | ru | en
    template: str = "general"
    # When notes are ready: a notification in the Uno Work Inbox (the bell)
    # and a message to the person's Telegram (if a bot is connected).
    notify_inbox: bool = True
    notify_telegram: bool = True
    # Telegram: the person's own bot (from @BotFather). Only the paired chat
    # may talk to it: send recordings in, get notes back.
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_chat_name: str = ""
    telegram_bot_name: str = ""
    # The person's time zone (minutes east of UTC) as their browser last said;
    # for meetings that arrive without a browser (Telegram). -10000 = unknown.
    tz_offset_min: int = -10000
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        d = asdict(self)
        d["custom_api_key"] = ("•" * 8 + self.custom_api_key[-4:]) if self.custom_api_key else ""
        d["custom_api_key_set"] = bool(self.custom_api_key)
        d["telegram_token"] = ("•" * 8 + self.telegram_token[-4:]) if self.telegram_token else ""
        d["telegram_token_set"] = bool(self.telegram_token)
        d.pop("extra", None)
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


# Set by the app itself (Telegram pairing), never by the Settings form.
INTERNAL_SETTINGS = ("telegram_chat_id", "telegram_chat_name", "telegram_bot_name", "tz_offset_min")


def save_settings(update: dict, internal: bool = False) -> Settings:
    with _lock:
        s = load_settings()
        for k, v in update.items():
            if k in INTERNAL_SETTINGS and not internal:
                continue
            if k in ("custom_api_key", "telegram_token") and (v is None or str(v).startswith("•")):
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


APP_ID = "notetaker"

# How the AI is reached; shown in Settings and the status pill.
ROUTE_APP = "app"          # the Uno Work App API: "AI of this computer"
ROUTE_GATEWAY = "gateway"  # a Uno gateway key (older Uno Work, or outside Uno)
ROUTE_CUSTOM = "custom"    # the person's own OpenAI-compatible provider
ROUTE_LABELS = {
    ROUTE_APP: "AI of this computer (Uno Work)",
    ROUTE_GATEWAY: "Uno gateway key",
    ROUTE_CUSTOM: "Custom",
}


def app_api_config() -> dict | None:
    """This app's App API address + token, or None (no waiting: a computer
    without the App API must fall back at once)."""
    return uno_app.find_config(app_id=APP_ID)


@dataclass
class Provider:
    name: str        # "uno" | "custom" — what the person chose
    base_url: str
    api_key: str
    source: str
    route: str = ROUTE_GATEWAY

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def route_label(self) -> str:
        return ROUTE_LABELS.get(self.route, self.route)


def ai_provider(s: Settings | None = None) -> Provider:
    s = s or load_settings()
    if s.provider == "custom":
        return Provider("custom", s.custom_base_url.rstrip("/"), s.custom_api_key, "your provider",
                        ROUTE_CUSTOM)
    app = app_api_config()
    if app:
        return Provider("uno", app["url"], app["token"],
                        "this computer's AI (limit in Uno Work → Settings → Apps)", ROUTE_APP)
    key, source = uno_gateway_key()
    return Provider("uno", UNO_GATEWAY_URL, key, source, ROUTE_GATEWAY)


def notes_model(s: Settings, p: Provider) -> str:
    """The model to ask for: the person's pick, else the computer's choice
    ("default" on the App API), else DEFAULT_MODEL."""
    if s.model:
        return s.model
    return "default" if p.route == ROUTE_APP else DEFAULT_MODEL
