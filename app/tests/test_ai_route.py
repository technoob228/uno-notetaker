"""Which way the AI goes (App API / gateway key / custom) and what errors say.

    python3 -m unittest discover -s app/tests       (from the repo root)

Stdlib only; the tests that call ai.py need httpx (skipped without it).
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from notetaker import config  # noqa: E402
from notetaker.config import Settings, ai_provider, notes_model  # noqa: E402

HAS_HTTPX = importlib.util.find_spec("httpx") is not None
ENV = ("UNO_APP_API_URL", "UNO_APP_TOKEN", "UNO_APP_KEY_DIR", "UNO_APP_ID", "UNO_LLM_API_KEY")


class RouteBase(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.saved_settings = config.UNO_WORK_SETTINGS
        config.UNO_WORK_SETTINGS = os.path.join(self.tmp, "no-such-settings.json")

    def tearDown(self):
        config.UNO_WORK_SETTINGS = self.saved_settings
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def key_dir(self, url="http://host.docker.internal:3779", token="uno_app_x"):
        d = os.path.join(self.tmp, "app-keys")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "api.json"), "w") as fh:
            json.dump({"appId": "notetaker", "url": url, "dockerUrl": url, "token": token}, fh)
        with open(os.path.join(d, "token"), "w") as fh:
            fh.write(token)
        os.environ["UNO_APP_KEY_DIR"] = d
        return d


class TestRoute(RouteBase):
    def test_app_api_when_the_key_folder_has_a_token(self):
        self.key_dir(url="http://127.0.0.1:3779")
        os.environ["UNO_LLM_API_KEY"] = "unollm_fallback"  # the App API wins
        p = ai_provider(Settings(provider="uno"))
        self.assertEqual((p.name, p.route, p.base_url, p.api_key), ("uno", "app", "http://127.0.0.1:3779", "uno_app_x"))
        self.assertTrue(p.ready)
        self.assertEqual(p.route_label, "AI of this computer (Uno Work)")

    def test_app_api_from_env(self):
        os.environ["UNO_APP_API_URL"] = "http://127.0.0.1:3779"
        os.environ["UNO_APP_TOKEN"] = "uno_app_env"
        p = ai_provider(Settings(provider="uno"))
        self.assertEqual((p.route, p.api_key), ("app", "uno_app_env"))

    def test_empty_key_folder_falls_back_to_the_gateway_key_at_once(self):
        os.environ["UNO_APP_KEY_DIR"] = self.tmp  # exists, no token yet
        os.environ["UNO_LLM_API_KEY"] = "unollm_app"
        p = ai_provider(Settings(provider="uno"))
        self.assertEqual((p.route, p.base_url, p.api_key), ("gateway", config.UNO_GATEWAY_URL, "unollm_app"))
        self.assertEqual(p.route_label, "Uno gateway key")

    def test_machine_key_from_uno_work_settings(self):
        with open(config.UNO_WORK_SETTINGS, "w") as fh:
            json.dump({"uno": {"apiKey": "unollm_machine"}}, fh)
        p = ai_provider(Settings(provider="uno"))
        self.assertEqual((p.route, p.api_key), ("gateway", "unollm_machine"))

    def test_nothing_configured(self):
        p = ai_provider(Settings(provider="uno"))
        self.assertEqual(p.route, "gateway")
        self.assertFalse(p.ready)

    def test_custom_ignores_the_app_api(self):
        self.key_dir()
        p = ai_provider(Settings(provider="custom", custom_base_url="https://api.example.com/v1/",
                                 custom_api_key="sk-1"))
        self.assertEqual((p.name, p.route, p.base_url, p.api_key),
                         ("custom", "custom", "https://api.example.com/v1", "sk-1"))
        self.assertEqual(p.route_label, "Custom")

    def test_notes_model(self):
        self.key_dir()
        app = ai_provider(Settings())
        self.assertEqual(notes_model(Settings(), app), "default")
        self.assertEqual(notes_model(Settings(model="moonshotai/kimi-k2.6"), app), "moonshotai/kimi-k2.6")
        os.environ.pop("UNO_APP_KEY_DIR")
        gw = ai_provider(Settings())
        self.assertEqual(notes_model(Settings(), gw), config.DEFAULT_MODEL)
        custom = ai_provider(Settings(provider="custom", custom_base_url="https://x/v1", custom_api_key="k"))
        self.assertEqual(notes_model(Settings(provider="custom"), custom), config.DEFAULT_MODEL)


class FakeAppAPI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list = []
    reply = (200, {})

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        FakeAppAPI.seen.append((self.path, self.headers.get("Authorization"), body))
        status, payload = FakeAppAPI.reply
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@unittest.skipUnless(HAS_HTTPX, "httpx is not installed")
class TestAppAPICalls(RouteBase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAppAPI)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_chat_goes_through_the_sdk_with_model_default(self):
        from notetaker import ai
        self.key_dir(url=self.url, token="uno_app_t")
        FakeAppAPI.reply = (200, {"model": "deepseek/deepseek-v3.2",
                                  "choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 3}})
        s = Settings()
        text, usage = ai.chat(ai_provider(s), s, [{"role": "user", "content": "hi"}], max_tokens=5)
        self.assertEqual(text, "OK")
        self.assertEqual(usage["model"], "deepseek/deepseek-v3.2")
        path, auth, body = FakeAppAPI.seen[-1]
        self.assertEqual((path, auth), ("/v1/chat/completions", "Bearer uno_app_t"))
        self.assertEqual(json.loads(body)["model"], "default")

    def test_app_limit_reached(self):
        from notetaker import ai
        self.key_dir(url=self.url)
        FakeAppAPI.reply = (402, {"error": {"type": "limit", "code": "app_limit_reached", "message": "limit"}})
        s = Settings()
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(ai_provider(s), s, [{"role": "user", "content": "hi"}])
        self.assertEqual(cm.exception.status, 402)
        self.assertIn("used its AI limit", str(cm.exception))
        self.assertIn("Uno Work → Settings → Apps", str(cm.exception))

    def test_transcription_401_keeps_status_for_local_fallback(self):
        from notetaker import ai
        self.key_dir(url=self.url)
        FakeAppAPI.reply = (401, {"error": {"type": "auth", "code": "invalid_app_token", "message": "revoked"}})
        audio = os.path.join(self.tmp, "a.ogg")
        with open(audio, "wb") as fh:
            fh.write(b"OggS")
        s = Settings()
        with self.assertRaises(ai.AIError) as cm:
            ai.transcribe_remote(ai_provider(s), s, audio)
        self.assertEqual(cm.exception.status, 401)  # pipeline falls back to local Whisper on 401/403
        path, _, body = FakeAppAPI.seen[-1]
        self.assertEqual(path, "/v1/audio/transcriptions")
        self.assertIn(b'filename="chunk.ogg"', body)

    def test_error_texts(self):
        from notetaker import ai
        self.assertIn("used its AI limit", str(ai.explain(402, "app_limit_reached", "", "AI notes")))
        self.assertIn("spending limit", str(ai.explain(402, "key_limit_reached", "", "AI notes")))
        self.assertIn("not enough AI credits", str(ai.explain(402, "insufficient_credits", "", "AI notes")))
        self.assertIn("not enough AI credits", str(ai.explain(402, "", "", "AI notes")))
        self.assertIn("not accepted", str(ai.explain(401, "", "bad", "AI notes")))



@unittest.skipUnless(HAS_HTTPX and all(importlib.util.find_spec(m) for m in ("fastapi", "markdown", "numpy")),
                     "the app's requirements are not installed")
class TestManifestAndState(RouteBase):
    def test_manifest_asks_for_ai_and_state_shows_the_route(self):
        from notetaker import main
        os.environ["UNO_APPS_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "UNO_APPS_DIR", None)
        main.write_manifest()
        with open(os.path.join(self.tmp, "notetaker.json")) as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["ai"], {"chat": True, "tasks": True, "limitUsd": 10})
        self.assertTrue(manifest["notify"])
        self.assertRegex(manifest["widget"]["path"], r"^/widget\?k=[0-9a-f]{32}$")
        self.key_dir()
        saved_state = config.STATE_DIR
        config.STATE_DIR = self.tmp  # no settings.json there → defaults
        self.addCleanup(setattr, config, "STATE_DIR", saved_state)
        st = main._state()["provider"]
        self.assertEqual((st["route"], st["route_label"], st["model"]),
                         ("app", "AI of this computer (Uno Work)", "default"))

if __name__ == "__main__":
    unittest.main()
