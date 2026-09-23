# Vendored from uno-work sdk/python/uno_app.py (Uno App SDK) — keep in sync; do not edit here.
"""Uno App SDK — "the AI of this computer" for any app on it.

One stdlib-only file (Python >= 3.9). The Uno Work daemon keeps a copy at
~/.uno/sdk/python/uno_app.py on every machine; the same file is the `uno-app`
package. Contract: docs/app-sdk.md in the uno-work repo.

    import sys, os; sys.path.insert(0, os.path.expanduser("~/.uno/sdk/python"))
    import uno_app
    print(uno_app.ask("Translate to English: привет"))

Config, first match wins: explicit arguments → env UNO_APP_API_URL +
UNO_APP_TOKEN → the folder in env UNO_APP_KEY_DIR → /run/uno-app (docker
mount: token + api.json) → ~/.uno/app-keys/<app_id>/ (app_id = argument or
env UNO_APP_ID). Inside a container the api.json "dockerUrl" is used. A
missing token file is re-read for up to 15 s (the daemon writes it seconds
after the manifest appears).

Every failure raises UnoAppError(status, code, message).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterator, List, Optional, Union

__version__ = "0.1.0"
__all__ = [
    "DEFAULT_URL", "UnoAppError", "Client", "Task", "resolve_config", "find_config",
    "ask", "stream", "chat", "transcribe", "transcribe_json", "task", "get_task", "tasks",
    "whoami", "models",
]

DEFAULT_URL = "http://127.0.0.1:3779"
DOCKER_KEY_DIR = "/run/uno-app"
TOKEN_WAIT_S = 15.0
_TOKEN_POLL_S = 0.5
_TASK_POLL_WAIT_MS = 30000
_TASK_WAITING_PAUSE_S = 2.0

Messages = Union[str, List[Dict[str, Any]]]

# The App API is local (loopback / docker bridge): never go through HTTP(S)_PROXY.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class UnoAppError(Exception):
    """An error answered by the App API ({"error":{"type","code","message"}}),
    or a config / connection problem (status 0)."""

    def __init__(self, status: int, code: str, message: str, type: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.type = type

    def __repr__(self) -> str:
        return f"UnoAppError(status={self.status!r}, code={self.code!r}, message={self.message!r})"


# ---- config ------------------------------------------------------------------

def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _read_key_dir(d: str) -> Dict[str, Any]:
    api: Dict[str, Any] = {}
    raw = _read(os.path.join(d, "api.json"))
    if raw:
        try:
            loaded = json.loads(raw)
            api = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            api = {}
    token = _read(os.path.join(d, "token")) or str(api.get("token") or "")
    return {"token": token, "api": api}


def resolve_config(url: Optional[str] = None, token: Optional[str] = None,
                   app_id: Optional[str] = None, wait: float = TOKEN_WAIT_S) -> Dict[str, str]:
    """→ {"url", "token", "app_id", "source"}; raises UnoAppError(0, "no_app_token")."""
    app_id = app_id or os.environ.get("UNO_APP_ID", "")
    env_url = os.environ.get("UNO_APP_API_URL", "")
    tok = token or os.environ.get("UNO_APP_TOKEN", "")
    if tok:
        return {"url": (url or env_url or DEFAULT_URL).rstrip("/"), "token": tok,
                "app_id": app_id, "source": "options" if token else "env"}

    dirs: List[str] = []
    if os.environ.get("UNO_APP_KEY_DIR"):
        dirs.append(os.environ["UNO_APP_KEY_DIR"])
    if os.path.isdir(DOCKER_KEY_DIR):
        dirs.append(DOCKER_KEY_DIR)
    if app_id:
        dirs.append(os.path.join(os.path.expanduser("~"), ".uno", "app-keys", app_id))
    in_docker = os.path.exists("/.dockerenv")
    deadline = time.monotonic() + (wait if dirs else 0)
    while True:
        for d in dirs:
            k = _read_key_dir(d)
            if not k["token"]:
                continue
            api = k["api"]
            docker = in_docker or d == DOCKER_KEY_DIR
            file_url = (docker and api.get("dockerUrl")) or api.get("url") or ""
            return {"url": str(url or env_url or file_url or DEFAULT_URL).rstrip("/"),
                    "token": k["token"], "app_id": app_id or str(api.get("appId") or ""),
                    "source": d}
        if time.monotonic() >= deadline:
            break
        time.sleep(_TOKEN_POLL_S)
    raise UnoAppError(0, "no_app_token",
                      f'No Uno app token. Add "ai": {{"chat": true}} to ~/.uno/apps/{app_id or "<id>"}.json')


def find_config(url: Optional[str] = None, token: Optional[str] = None,
                app_id: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Like resolve_config() but without waiting; None when there is no token."""
    try:
        return resolve_config(url, token, app_id, wait=0)
    except UnoAppError:
        return None


# ---- HTTP --------------------------------------------------------------------

def _error_from(status: int, raw: bytes) -> UnoAppError:
    text = raw.decode("utf-8", "replace")
    code, message, typ = "", "", ""
    try:
        err = json.loads(text).get("error")
        if isinstance(err, dict):
            typ = str(err.get("type") or "")
            # The Uno gateway puts the machine code in "type" and the HTTP status in "code".
            code = err.get("code") if isinstance(err.get("code"), str) and err.get("code") else typ
            message = str(err.get("message") or "")
        elif isinstance(err, str):
            message = err
    except (ValueError, AttributeError):
        message = text[:300]
    return UnoAppError(status, code or f"http_{status}", message or f"HTTP {status}", typ)


def _messages(prompt_or_messages: Messages, system: Optional[str]) -> List[Dict[str, Any]]:
    if isinstance(prompt_or_messages, str):
        msgs: List[Dict[str, Any]] = [{"role": "user", "content": prompt_or_messages}]
    else:
        msgs = list(prompt_or_messages)
    if system:
        msgs.insert(0, {"role": "system", "content": system})
    return msgs


def _sse(resp: Any) -> Iterator[Dict[str, str]]:
    """{"event", "data"} records of a Server-Sent Events response."""
    event, data = "", []  # type: str, List[str]
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data:
                yield {"event": event or "message", "data": "\n".join(data)}
            event, data = "", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            v = line[5:]
            data.append(v[1:] if v.startswith(" ") else v)
    if data:
        yield {"event": event or "message", "data": "\n".join(data)}


class Client:
    """A client bound to one App API address and token (resolved lazily)."""

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None,
                 app_id: Optional[str] = None, wait: float = TOKEN_WAIT_S, timeout: float = 300.0):
        self._url, self._token, self._app_id = url, token, app_id
        self._wait = wait
        self.timeout = timeout
        self._config: Optional[Dict[str, str]] = None

    @property
    def config(self) -> Dict[str, str]:
        if self._config is None:
            self._config = resolve_config(self._url, self._token, self._app_id, self._wait)
        return self._config

    # -- low level

    def _open(self, method: str, path: str, body: Optional[bytes] = None,
              headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None) -> Any:
        for attempt in range(2):
            cfg = self.config
            h = {"Authorization": f"Bearer {cfg['token']}", "Accept": "application/json"}
            h.update(headers or {})
            req = urllib.request.Request(cfg["url"] + path, data=body, headers=h, method=method)
            try:
                return _OPENER.open(req, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                exc.close()
                # The person pressed Revoke (token rotated): re-read the key files once.
                if exc.code == 401 and attempt == 0 and cfg["source"] not in ("options", "env"):
                    self._config = None
                    try:
                        if self.config["token"] != cfg["token"]:
                            continue
                    except UnoAppError:
                        self._config = cfg
                raise _error_from(exc.code, raw) from None
            except (urllib.error.URLError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise UnoAppError(0, "unreachable",
                                  f"Cannot reach the Uno App API at {cfg['url']}: {reason}") from None
        raise UnoAppError(0, "unreachable", "unreachable")  # pragma: no cover

    def _json(self, method: str, path: str, payload: Any = None,
              timeout: Optional[float] = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        with self._open(method, path, body, headers, timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else None

    # -- chat

    def chat(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Raw OpenAI-compatible POST /v1/chat/completions → the parsed JSON."""
        return self._json("POST", "/v1/chat/completions", body)

    @staticmethod
    def _chat_body(prompt_or_messages: Messages, model: Optional[str], system: Optional[str],
                   temperature: Optional[float], max_tokens: Optional[int]) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": model or "default",
                                "messages": _messages(prompt_or_messages, system)}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return body

    def ask(self, prompt_or_messages: Messages, model: Optional[str] = None,
            system: Optional[str] = None, temperature: Optional[float] = None,
            max_tokens: Optional[int] = None) -> str:
        """One answer as text. model None / "default" = the person's choice for apps."""
        data = self.chat(self._chat_body(prompt_or_messages, model, system, temperature, max_tokens))
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        return content if isinstance(content, str) else ""

    def stream(self, prompt_or_messages: Messages, model: Optional[str] = None,
               system: Optional[str] = None, temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> Iterator[str]:
        """Text deltas as they arrive."""
        body = self._chat_body(prompt_or_messages, model, system, temperature, max_tokens)
        body["stream"] = True
        resp = self._open("POST", "/v1/chat/completions", json.dumps(body).encode("utf-8"),
                          {"Content-Type": "application/json", "Accept": "text/event-stream"})
        with resp:
            for ev in _sse(resp):
                if ev["data"].strip() == "[DONE]":
                    return
                try:
                    chunk = json.loads(ev["data"])
                except ValueError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    err = chunk["error"]
                    if isinstance(err, dict):
                        raise UnoAppError(0, str(err.get("code") or "stream_error"),
                                          str(err.get("message") or err))
                    raise UnoAppError(0, "stream_error", str(err))
                try:
                    delta = chunk["choices"][0]["delta"].get("content")
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue
                if isinstance(delta, str) and delta:
                    yield delta

    # -- speech to text

    def transcribe_json(self, path_or_bytes: Union[str, bytes, bytearray, "os.PathLike[str]"],
                        filename: Optional[str] = None, model: Optional[str] = None,
                        language: Optional[str] = None, response_format: str = "json",
                        extra: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """POST /v1/audio/transcriptions → the whole JSON (e.g. segments of verbose_json)."""
        if isinstance(path_or_bytes, (bytes, bytearray)):
            data, name = bytes(path_or_bytes), filename or "audio"
        else:
            p = os.fspath(path_or_bytes)
            with open(p, "rb") as fh:
                data = fh.read()
            name = filename or os.path.basename(p)
        fields = {"response_format": response_format}
        if model:  # omitted = the App API's own speech-to-text default
            fields["model"] = model
        if language:
            fields["language"] = language
        fields.update(extra or {})
        boundary = "----uno-app-" + uuid.uuid4().hex
        parts: List[bytes] = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                         .encode("utf-8"))
        safe = name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
        parts.append(data)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        with self._open("POST", "/v1/audio/transcriptions", b"".join(parts),
                        {"Content-Type": f"multipart/form-data; boundary={boundary}"}) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            out = json.loads(raw)
            return out if isinstance(out, dict) else {"text": str(out)}
        except ValueError:
            return {"text": raw}

    def transcribe(self, path_or_bytes: Union[str, bytes, bytearray, "os.PathLike[str]"],
                   filename: Optional[str] = None, model: Optional[str] = None,
                   language: Optional[str] = None) -> str:
        """Speech to text of a file path or bytes."""
        text = self.transcribe_json(path_or_bytes, filename, model, language).get("text")
        return text if isinstance(text, str) else ""

    # -- tasks

    def task(self, prompt: str, cwd: Optional[str] = None, title: Optional[str] = None,
             harness: Optional[str] = None, tools: Optional[str] = None) -> "Task":
        """Start an agent task — a Work chat the person can see, approve and stop."""
        body: Dict[str, Any] = {"prompt": prompt}
        for k, v in (("cwd", cwd), ("title", title), ("harness", harness), ("tools", tools)):
            if v is not None:
                body[k] = v
        return Task(self, self._json("POST", "/v1/tasks", body))

    def get_task(self, task_id: str) -> "Task":
        return Task(self, self._json("GET", f"/v1/tasks/{urllib.parse.quote(task_id, safe='')}"))

    def tasks(self) -> Any:
        """The app's own tasks."""
        return self._json("GET", "/v1/tasks")

    # -- info

    def whoami(self) -> Dict[str, Any]:
        return self._json("GET", "/v1/whoami")

    def models(self) -> Any:
        return self._json("GET", "/v1/models")


class Task:
    """A running agent task. .data is the latest task JSON."""

    def __init__(self, client: Client, data: Dict[str, Any]):
        self._client = client
        self.data: Dict[str, Any] = dict(data or {})
        self.id: str = str(self.data.get("id", ""))

    @property
    def thread_id(self) -> str:
        return str(self.data.get("threadId") or "")

    @property
    def status(self) -> str:
        return str(self.data.get("status") or "")

    @property
    def tools(self) -> Optional[str]:
        return self.data.get("tools")

    @property
    def harness(self) -> Optional[str]:
        return self.data.get("harness")

    @property
    def result(self) -> Optional[Dict[str, Any]]:
        return self.data.get("result")

    def __repr__(self) -> str:
        return f"Task(id={self.id!r}, status={self.status!r})"

    def _path(self, suffix: str = "") -> str:
        return f"/v1/tasks/{urllib.parse.quote(self.id, safe='')}{suffix}"

    def refresh(self) -> Dict[str, Any]:
        self.data.update(self._client._json("GET", self._path()) or {})
        return self.data

    def wait(self, timeout: Optional[float] = None, until_done: bool = True) -> Dict[str, Any]:
        """Long-poll until the task ends → the final task JSON.

        until_done=True (default) keeps waiting while the task is "waiting" for
        the person (they approve in Work); False returns as soon as it stops
        running. After `timeout` seconds returns the latest state as is.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            left_ms = _TASK_POLL_WAIT_MS if deadline is None else \
                max(0, min(_TASK_POLL_WAIT_MS, int((deadline - time.monotonic()) * 1000)))
            data = self._client._json("GET", self._path(f"?waitMs={left_ms}"),
                                      timeout=left_ms / 1000 + 60)
            self.data.update(data or {})
            s = self.status
            if s != "running" and (s != "waiting" or not until_done):
                return self.data
            if deadline is not None and time.monotonic() >= deadline:
                return self.data
            if s == "waiting":
                pause = _TASK_WAITING_PAUSE_S if deadline is None else \
                    max(0.0, min(_TASK_WAITING_PAUSE_S, deadline - time.monotonic()))
                time.sleep(pause)

    def events(self) -> Iterator[Dict[str, Any]]:
        """{"event": "status"|"message"|"activity"|"done", "data": ...}; ends after "done"."""
        resp = self._client._open("GET", self._path("/events"), headers={"Accept": "text/event-stream"},
                                  timeout=3600)
        with resp:
            for ev in _sse(resp):
                try:
                    data: Any = json.loads(ev["data"])
                except ValueError:
                    data = ev["data"]
                yield {"event": ev["event"], "data": data}
                if ev["event"] == "done":
                    if isinstance(data, dict):
                        self.data.update(data)
                    return

    def stop(self) -> Any:
        out = self._client._json("POST", self._path("/stop"), {})
        if isinstance(out, dict):
            self.data.update(out)
        return out


# ---- module-level functions on a lazily created default client ---------------

_default: Optional[Client] = None


def _client() -> Client:
    global _default
    if _default is None:
        _default = Client()
    return _default


def ask(prompt_or_messages: Messages, model: Optional[str] = None, system: Optional[str] = None,
        temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> str:
    return _client().ask(prompt_or_messages, model, system, temperature, max_tokens)


def stream(prompt_or_messages: Messages, model: Optional[str] = None, system: Optional[str] = None,
           temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> Iterator[str]:
    return _client().stream(prompt_or_messages, model, system, temperature, max_tokens)


def chat(body: Dict[str, Any]) -> Dict[str, Any]:
    return _client().chat(body)


def transcribe(path_or_bytes: Union[str, bytes, bytearray, "os.PathLike[str]"],
               filename: Optional[str] = None, model: Optional[str] = None,
               language: Optional[str] = None) -> str:
    return _client().transcribe(path_or_bytes, filename, model, language)


def transcribe_json(path_or_bytes: Union[str, bytes, bytearray, "os.PathLike[str]"],
                    filename: Optional[str] = None, model: Optional[str] = None,
                    language: Optional[str] = None, response_format: str = "json") -> Dict[str, Any]:
    return _client().transcribe_json(path_or_bytes, filename, model, language, response_format)


def task(prompt: str, cwd: Optional[str] = None, title: Optional[str] = None,
         harness: Optional[str] = None, tools: Optional[str] = None) -> Task:
    return _client().task(prompt, cwd, title, harness, tools)


def get_task(task_id: str) -> Task:
    return _client().get_task(task_id)


def tasks() -> Any:
    return _client().tasks()


def whoami() -> Dict[str, Any]:
    return _client().whoami()


def models() -> Any:
    return _client().models()
