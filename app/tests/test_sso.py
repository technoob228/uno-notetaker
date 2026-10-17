"""Sign in with Uno (OIDC) against a fake Uno console.

    python3 -m unittest discover -s app/tests       (from the repo root)

Needs the app's requirements (fastapi, httpx, PyJWT[crypto]); skipped without.
The fake provider speaks what fishcode back/internal/api/oidc.go speaks:
discovery, JWKS, authorize (code + state + iss), token (client_secret_basic,
PKCE S256 checked), RS256 id_token with nonce/azp.
"""
import base64
import hashlib
import importlib.util
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

HAS_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx", "jwt", "cryptography", "numpy"))

TMP = tempfile.mkdtemp(prefix="nt-sso-test-")
os.environ.setdefault("MEETINGS_DIR", os.path.join(TMP, "Meetings"))
os.environ.setdefault("STATE_DIR", os.path.join(TMP, "state"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

PASSWORD = "owner-password-for-tests"
CLIENT_ID = "uno-app-7-notetaker"
CLIENT_SECRET = "s3cret-for-tests"
OWNER_SUB = "uno-85"
APP = "https://testserver"
REDIRECT = APP + "/auth/uno/callback"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class FakeUno:
    """A tiny Uno console: one client, one key, knobs for bad tokens."""

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        import jwt
        self.jwt = jwt
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.codes: dict[str, dict] = {}
        self.claims_override: dict = {}
        self.sign_with_other_key = False
        self.token_calls = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/oidc/.well-known/openid-configuration":
                    return self._json(200, fake.discovery())
                if u.path == "/oidc/jwks":
                    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(fake.key.public_key()))
                    jwk.update(kid=fake.kid, use="sig", alg="RS256")
                    return self._json(200, {"keys": [jwk]})
                if u.path == "/oidc/authorize":
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    code = secrets.token_urlsafe(16)
                    fake.codes[code] = q
                    loc = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q.get("state", ""),
                                                               "iss": fake.issuer})
                    self.send_response(302)
                    self.send_header("Location", loc)
                    self.end_headers()
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length", "0"))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                if u.path != "/oidc/token":
                    return self._json(404, {"error": "not found"})
                fake.token_calls += 1
                auth = self.headers.get("Authorization", "")
                want = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
                if auth != want:
                    return self._json(401, {"error": "invalid_client"})
                q = fake.codes.pop(form.get("code", ""), None)
                if not q or form.get("redirect_uri") != q["redirect_uri"]:
                    return self._json(400, {"error": "invalid_grant"})
                ch = _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
                if q.get("code_challenge_method") != "S256" or ch != q.get("code_challenge"):
                    return self._json(400, {"error": "invalid_grant", "error_description": "PKCE"})
                now = int(time.time())
                claims = {"iss": fake.issuer, "aud": [CLIENT_ID], "azp": CLIENT_ID, "sub": OWNER_SUB,
                          "email": "owner@example.com", "iat": now, "exp": now + 600,
                          "nonce": q.get("nonce", ""), "token_use": "id"}
                claims.update(fake.claims_override)
                key = fake.other_key if fake.sign_with_other_key else fake.key
                idt = jwt.encode(claims, key, algorithm="RS256", headers={"kid": fake.kid})
                self._json(200, {"access_token": "at", "token_type": "Bearer", "expires_in": 600,
                                 "id_token": idt, "scope": "openid email profile"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.issuer = f"http://127.0.0.1:{self.server.server_address[1]}/oidc"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def discovery(self):
        i = self.issuer
        return {"issuer": i, "authorization_endpoint": i + "/authorize", "token_endpoint": i + "/token",
                "userinfo_endpoint": i + "/userinfo", "jwks_uri": i + "/jwks",
                "code_challenge_methods_supported": ["S256"], "id_token_signing_alg_values_supported": ["RS256"]}

    def env(self):
        return {"UNO_OIDC_ISSUER": self.issuer,
                "UNO_OIDC_DISCOVERY_URL": self.issuer + "/.well-known/openid-configuration",
                "UNO_OIDC_AUTH_URL": self.issuer + "/authorize", "UNO_OIDC_TOKEN_URL": self.issuer + "/token",
                "UNO_OIDC_CLIENT_ID": CLIENT_ID, "UNO_OIDC_CLIENT_SECRET": CLIENT_SECRET,
                "UNO_OIDC_OWNER_SUB": OWNER_SUB}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


OIDC_ENV = ("UNO_OIDC_ISSUER", "UNO_OIDC_DISCOVERY_URL", "UNO_OIDC_AUTH_URL", "UNO_OIDC_TOKEN_URL",
            "UNO_OIDC_USERINFO_URL", "UNO_OIDC_CLIENT_ID", "UNO_OIDC_CLIENT_SECRET", "UNO_OIDC_OWNER_SUB")


@unittest.skipUnless(HAS_DEPS, "needs the app's requirements (fastapi, httpx, PyJWT[crypto], numpy)")
class SSOBase(unittest.TestCase):
    sso_on = True

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from notetaker import main, sso
        cls.main, cls.sso, cls.TestClient = main, sso, TestClient
        cls.fake = FakeUno()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in OIDC_ENV}
        for k in OIDC_ENV:
            os.environ.pop(k, None)
        if self.sso_on:
            os.environ.update(self.fake.env())
        self.patches = [mock.patch.object(self.main, "ADMIN_PASSWORD", PASSWORD),
                        mock.patch.object(self.main, "APP_URL", "")]
        for p in self.patches:
            p.start()
        self.sso.reset_cache()
        self.fake.claims_override = {}
        self.fake.sign_with_other_key = False
        self.c = self.TestClient(self.main.app, base_url=APP, follow_redirects=False)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # The browser's trip: app → Uno (authorize) → app callback.
    def go_to_uno(self):
        r = self.c.get("/auth/uno/start")
        self.assertEqual(r.status_code, 303, r.text)
        loc = r.headers["location"]
        self.assertTrue(loc.startswith(self.fake.issuer + "/authorize?"), loc)
        return r, loc

    def come_back(self, authorize_url):
        import httpx
        r = httpx.get(authorize_url, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        back = urlparse(r.headers["location"])
        self.assertEqual(f"{back.scheme}://{back.netloc}{back.path}", REDIRECT)
        return back.query

    def callback(self, query):
        return self.c.get("/auth/uno/callback?" + query)

    def assert_failed(self, r, code):
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(r.headers["location"], f"/login?password=1&error={code}")
        self.assertNotIn("nt_session", self.c.cookies)
        self.assertEqual(self.c.get("/api/state").status_code, 401)


class TestSignInWithUno(SSOBase):
    def test_success_is_the_same_session_as_the_password(self):
        r = self.c.get("/")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/auth/uno/start"))
        r = self.c.get("/login")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/auth/uno/start"))
        r, loc = self.go_to_uno()
        q = {k: v[0] for k, v in parse_qs(urlparse(loc).query).items()}
        self.assertEqual(q["client_id"], CLIENT_ID)
        self.assertEqual(q["redirect_uri"], REDIRECT)
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(len(q["code_challenge"]), 43)
        self.assertTrue(q["state"] and q["nonce"])
        self.assertIn("openid", q["scope"].split())
        tx_cookie = r.headers["set-cookie"]
        for attr in ("nt_oidc=", "Path=/auth/uno/", "Secure", "HttpOnly", "SameSite=None", "Partitioned"):
            self.assertIn(attr, tx_cookie)

        r = self.callback(self.come_back(loc))
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(r.headers["location"], "/auth/uno/done?next=/")
        cookies = r.headers.get_list("set-cookie")
        session = [h for h in cookies if h.startswith("nt_session=")]
        self.assertEqual(len(session), 1)
        for attr in ("Path=/", "Secure", "HttpOnly", "SameSite=None", "Partitioned"):
            self.assertIn(attr, session[0])
        self.assertTrue(any(h.startswith("nt_oidc=;") and "Max-Age=0" in h for h in cookies))

        r = self.c.get("/auth/uno/done?next=/")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/"))
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Sign in with Uno", r.text)  # the app, not the login page
        self.assertEqual(self.c.get("/api/state").status_code, 200)
        # Same cookie the password gives: the password check accepts it.
        self.assertTrue(self.c.cookies["nt_session"].startswith("v1."))

    def test_next_path_survives_but_only_on_this_app(self):
        r = self.c.get("/auth/uno/start?next=/m/abc")
        self.assertEqual(self.callback(self.come_back(r.headers["location"])).headers["location"],
                         "/auth/uno/done?next=/m/abc")
        r = self.c.get("/auth/uno/start?next=//evil.example/x")
        self.assertEqual(self.callback(self.come_back(r.headers["location"])).headers["location"],
                         "/auth/uno/done?next=/")
        self.assertEqual(self.c.get("/auth/uno/done?next=https://evil.example").headers["location"], "/")

    def test_wrong_state(self):
        _, loc = self.go_to_uno()
        q = parse_qs(self.come_back(loc))
        q["state"] = ["forged"]
        calls = self.fake.token_calls
        r = self.callback(urlencode({k: v[0] for k, v in q.items()}))
        self.assert_failed(r, "state")
        self.assertEqual(self.fake.token_calls, calls)  # the code was never spent

    def test_no_transaction_cookie(self):
        _, loc = self.go_to_uno()
        query = self.come_back(loc)
        self.c.cookies.clear()
        self.assert_failed(self.callback(query), "expired")

    def test_answer_from_another_issuer(self):
        _, loc = self.go_to_uno()
        q = {k: v[0] for k, v in parse_qs(self.come_back(loc)).items()}
        q["iss"] = "https://evil.example/oidc"
        self.assert_failed(self.callback(urlencode(q)), "issuer")

    def test_id_token_from_another_issuer(self):
        self.fake.claims_override = {"iss": "https://evil.example/oidc"}
        _, loc = self.go_to_uno()
        self.assert_failed(self.callback(self.come_back(loc)), "token")

    def test_id_token_for_another_app(self):
        self.fake.claims_override = {"aud": ["uno-app-7-memos"], "azp": "uno-app-7-memos"}
        _, loc = self.go_to_uno()
        self.assert_failed(self.callback(self.come_back(loc)), "token")

    def test_id_token_azp_of_another_app(self):
        self.fake.claims_override = {"azp": "uno-app-7-memos"}
        _, loc = self.go_to_uno()
        self.assert_failed(self.callback(self.come_back(loc)), "token")

    def test_replayed_nonce(self):
        self.fake.claims_override = {"nonce": "someone-elses"}
        _, loc = self.go_to_uno()
        self.assert_failed(self.callback(self.come_back(loc)), "token")

    def test_expired_id_token(self):
        self.fake.claims_override = {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 4000}
        _, loc = self.go_to_uno()
        self.assert_failed(self.callback(self.come_back(loc)), "token")

    def test_signed_with_a_key_not_in_jwks(self):
        self.fake.sign_with_other_key = True
        _, loc = self.go_to_uno()
        self.assert_failed(self.callback(self.come_back(loc)), "token")

    def test_someone_else_is_refused(self):
        self.fake.claims_override = {"sub": "uno-2606"}
        _, loc = self.go_to_uno()
        self.assert_failed(self.callback(self.come_back(loc)), "not_owner")

    def test_no_owner_configured_lets_nobody_in(self):
        os.environ["UNO_OIDC_OWNER_SUB"] = ""
        try:
            _, loc = self.go_to_uno()
            self.assert_failed(self.callback(self.come_back(loc)), "not_owner")
        finally:
            os.environ["UNO_OIDC_OWNER_SUB"] = OWNER_SUB

    def test_uno_said_no(self):
        _, loc = self.go_to_uno()
        state = parse_qs(urlparse(loc).query)["state"][0]
        r = self.callback(urlencode({"error": "access_denied", "state": state, "iss": self.fake.issuer}))
        self.assert_failed(r, "denied")

    def test_cookie_blocked_does_not_loop(self):
        r = self.c.get("/auth/uno/done?next=/")
        self.assertEqual(r.headers["location"], "/login?password=1&error=cookie")
        r = self.c.get("/login?password=1&error=cookie")
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-sso="1"', r.text)

    def test_password_still_works_as_fallback(self):
        r = self.c.get("/login?password=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-sso="1"', r.text)
        self.assertIn("Use the owner password", r.text)
        self.assertEqual(self.c.post("/login", json={"password": "nope"}).status_code, 401)
        r = self.c.post("/login", json={"password": PASSWORD})
        self.assertEqual(r.status_code, 200)
        for attr in ("Secure", "HttpOnly", "SameSite=None", "Partitioned"):
            self.assertIn(attr, r.headers["set-cookie"])
        self.assertEqual(self.c.get("/api/state").status_code, 200)

    def test_plain_http_goes_to_the_https_address_first(self):
        c = self.TestClient(self.main.app, base_url="http://box.local:8430", follow_redirects=False)
        with mock.patch.object(self.main, "APP_URL", "https://notetaker-x.app.uno4.dev"):
            r = c.get("/auth/uno/start?next=/m/1")
        self.assertEqual(r.headers["location"], "https://notetaker-x.app.uno4.dev/auth/uno/start?next=/m/1")
        r = c.get("/auth/uno/start")
        self.assertEqual(r.headers["location"], "/login?password=1&error=https")

    def test_cross_site_writes_are_refused(self):
        def logout(**headers):
            self.c.post("/login", json={"password": PASSWORD})
            return self.c.post("/api/logout", headers=headers).status_code

        self.assertEqual(logout(Origin="https://evil.example"), 403)
        # Another app on *.app.uno4.dev is the same site, but not the same origin.
        self.assertEqual(logout(Origin="https://other-app.app.uno4.dev", **{"Sec-Fetch-Site": "same-site"}), 403)
        self.assertEqual(logout(Origin=APP), 200)
        # A proxy that rewrote Host: the browser still says same-origin.
        self.assertEqual(logout(Origin="https://x.example", **{"Sec-Fetch-Site": "same-origin"}), 200)
        self.assertEqual(logout(), 200)  # not a browser (the bot, curl)
        self.assertEqual(self.c.post("/login", json={"password": PASSWORD},
                                     headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_public_paths_stay_public(self):
        self.assertEqual(self.c.get("/healthz").status_code, 200)
        for path in ("/s/nope", "/m/nope", "/manifest.webmanifest", "/sw.js", "/static/style.css"):
            r = self.c.get(path)
            self.assertNotEqual(r.status_code, 401, path)
            self.assertNotEqual(r.headers.get("location"), "/auth/uno/start", path)
        # The Home widget: its own key, shown in a third-party frame without cookies.
        self.assertEqual(self.c.get("/widget?k=" + self.main.widget_key()).status_code, 200)
        self.assertIsNone(self.c.get("/widget").headers.get("location"))
        r = self.c.get("/api/bot/worker/work")  # the bot's own token, not the login
        self.assertEqual(r.status_code, 401)
        self.assertNotEqual(r.json().get("error"), "login required")


class TestWithoutUno(SSOBase):
    """No UNO_OIDC_* — the app behaves exactly as before: password only."""
    sso_on = False

    def test_password_page_as_before(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-sso="0"', r.text)
        self.assertEqual(self.c.get("/login").headers["location"], "/")
        self.assertEqual(self.c.get("/auth/uno/start").headers["location"], "/")
        self.assertEqual(self.c.get("/auth/uno/callback?code=x&state=y").headers["location"], "/")
        self.assertEqual(self.c.get("/api/state").status_code, 401)
        self.assertEqual(self.c.post("/login", json={"password": PASSWORD}).status_code, 200)
        self.assertEqual(self.c.get("/api/state").status_code, 200)

    def test_plain_http_cookie_as_before(self):
        c = self.TestClient(self.main.app, base_url="http://box.local:8430", follow_redirects=False)
        r = c.post("/login", json={"password": PASSWORD})
        h = r.headers["set-cookie"].lower()
        self.assertIn("samesite=lax", h)
        self.assertNotIn("secure", h)
        self.assertNotIn("partitioned", h)


if __name__ == "__main__":
    unittest.main()
