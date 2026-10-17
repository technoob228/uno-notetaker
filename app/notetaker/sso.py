"""Sign in with Uno — OpenID Connect against the Uno console.

The App Store registers each install as an OIDC client of the console and
passes UNO_OIDC_* (fishcode back/internal/apps/catalog.go). The console only
hands a code to the computer's owner and the people the owner shared the app
with ("Share app"); everyone else sees "You don't have access" there and never
comes back here. So a valid code = a person allowed in.

Flow (authorization code + PKCE S256 + state + nonce):
  GET /auth/uno/start     → signed transaction cookie (state, nonce, verifier,
                            redirect_uri, next) → the provider's /authorize
  GET /auth/uno/callback  → check state/iss → code for tokens (client_secret)
                            → id_token checked: RS256 with the provider's JWKS,
                            iss, aud, azp, exp, nonce → the same session cookie
                            as after the password → /auth/uno/done
  GET /auth/uno/done      → the cookie stuck? → the app; no (a browser that
                            blocks cookies in a frame) → the password page with
                            a hint, never a redirect loop.

Without UNO_OIDC_CLIENT_ID the app behaves as before (password only).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

log = logging.getLogger("notetaker.sso")

START_PATH = "/auth/uno/start"
CALLBACK_PATH = "/auth/uno/callback"
DONE_PATH = "/auth/uno/done"
TX_COOKIE = "nt_oidc"
TX_TTL = 600                 # a sign-in has 10 minutes to come back
SCOPE = "openid email profile"
HTTP_TIMEOUT = 10.0
LEEWAY = 60                  # clock skew between this computer and the console, seconds
META_TTL = 3600
JWKS_MIN_REFRESH = 60        # an unknown kid refetches the keys at most once a minute


class SSOError(Exception):
    """Sign-in failed. code is what the login page shows (a fixed message per code)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def issuer() -> str:
    return _env("UNO_OIDC_ISSUER").rstrip("/")


def client_id() -> str:
    return _env("UNO_OIDC_CLIENT_ID")


def enabled() -> bool:
    """Sign in with Uno is on: the install got an OIDC client."""
    return bool(client_id() and _env("UNO_OIDC_CLIENT_SECRET") and issuer())


def owner_sub() -> str:
    return _env("UNO_OIDC_OWNER_SUB")


# ---- provider metadata and keys ------------------------------------------------

@dataclass
class Meta:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    pkce: bool


_lock = threading.Lock()
_meta: tuple[float, str, Meta] | None = None          # (fetched at, issuer, meta)
_jwks: tuple[float, str, dict] | None = None          # (fetched at, jwks_uri, {kid: key})


def reset_cache() -> None:
    global _meta, _jwks
    with _lock:
        _meta = _jwks = None


async def metadata(client: httpx.AsyncClient) -> Meta:
    """Discovery (cached for an hour); the UNO_OIDC_*_URL env if it is unreachable."""
    global _meta
    iss = issuer()
    with _lock:
        if _meta and _meta[1] == iss and time.time() - _meta[0] < META_TTL:
            return _meta[2]
    url = _env("UNO_OIDC_DISCOVERY_URL") or iss + "/.well-known/openid-configuration"
    doc: dict = {}
    try:
        r = await client.get(url)
        r.raise_for_status()
        doc = r.json()
        if doc.get("issuer", "").rstrip("/") != iss:
            raise SSOError("provider", f"discovery issuer {doc.get('issuer')!r} is not {iss!r}")
    except SSOError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("OIDC discovery %s failed (%s) — using the UNO_OIDC_* addresses", url, exc)
        doc = {}
    m = Meta(
        authorization_endpoint=doc.get("authorization_endpoint") or _env("UNO_OIDC_AUTH_URL") or iss + "/authorize",
        token_endpoint=doc.get("token_endpoint") or _env("UNO_OIDC_TOKEN_URL") or iss + "/token",
        jwks_uri=doc.get("jwks_uri") or iss + "/jwks",
        # The Uno console does S256 (and refuses plain). Without discovery we
        # know it is the Uno console — use it too.
        pkce="S256" in doc.get("code_challenge_methods_supported", ["S256"]),
    )
    if doc:
        with _lock:
            _meta = (time.time(), iss, m)
    return m


async def _signing_key(client: httpx.AsyncClient, jwks_uri: str, kid: str | None):
    global _jwks

    def pick(keys: dict):
        if kid is None:  # a token without kid: fine only if the provider has one key
            return next(iter(keys.values())) if len(keys) == 1 else None
        return keys.get(kid)

    with _lock:
        cached = _jwks if _jwks and _jwks[1] == jwks_uri else None
    if cached:
        k = pick(cached[2])
        if k is not None:
            return k
        if time.time() - cached[0] < JWKS_MIN_REFRESH:
            raise SSOError("token", f"no signing key {kid!r} in the provider's JWKS")
    try:
        r = await client.get(jwks_uri)
        r.raise_for_status()
        keys = {}
        for k in r.json().get("keys", []):
            if k.get("kty") == "RSA" and k.get("use", "sig") == "sig":
                keys[k.get("kid")] = jwt.PyJWK(k, algorithm="RS256").key
    except (httpx.HTTPError, ValueError, jwt.PyJWKError) as exc:
        raise SSOError("provider", f"JWKS {jwks_uri}: {exc}") from exc
    with _lock:
        _jwks = (time.time(), jwks_uri, keys)
    k = pick(keys)
    if k is None:
        raise SSOError("token", f"no signing key {kid!r} in the provider's JWKS")
    return k


# ---- the signed transaction cookie --------------------------------------------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def pack_tx(secret: bytes, tx: dict) -> str:
    body = _b64(json.dumps(tx, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(secret, b"nt-oidc-tx." + body.encode(), hashlib.sha256).digest())


def unpack_tx(secret: bytes, raw: str) -> dict | None:
    try:
        body, sig = raw.split(".")
        want = _b64(hmac.new(secret, b"nt-oidc-tx." + body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            return None
        tx = json.loads(_unb64(body))
        if not isinstance(tx, dict) or time.time() - float(tx.get("t", 0)) > TX_TTL:
            return None
        return tx
    except (ValueError, TypeError):
        return None


def safe_next(p: str | None) -> str:
    """Only a path on this app (no //host, no scheme): where to land after sign-in."""
    p = (p or "/").strip()
    if not p.startswith("/") or p.startswith("//") or "\\" in p or any(c in p for c in "\r\n\t"):
        return "/"
    return p[:512]


def new_transaction(redirect_uri: str, next_path: str) -> tuple[dict, str]:
    """(tx for the cookie, code_challenge)."""
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    tx = {"s": secrets.token_urlsafe(24), "n": secrets.token_urlsafe(24), "v": verifier,
          "r": redirect_uri, "x": safe_next(next_path), "t": int(time.time())}
    return tx, challenge


def authorize_url(meta: Meta, tx: dict, challenge: str) -> str:
    q = {"response_type": "code", "client_id": client_id(), "redirect_uri": tx["r"], "scope": SCOPE,
         "state": tx["s"], "nonce": tx["n"]}
    if meta.pkce:
        q.update(code_challenge=challenge, code_challenge_method="S256")
    sep = "&" if "?" in meta.authorization_endpoint else "?"
    return meta.authorization_endpoint + sep + urlencode(q)


# ---- the callback ---------------------------------------------------------------

async def finish(params: dict, tx: dict | None) -> dict:
    """Checks the provider's answer and returns the verified id_token claims."""
    if tx is None:
        raise SSOError("expired", "no or bad transaction cookie")
    if not hmac.compare_digest(str(params.get("state", "")), tx["s"]):
        raise SSOError("state", "state does not match")
    iss = issuer()
    # RFC 9207: the console names itself in the answer — mix-up protection.
    if params.get("iss") and params["iss"].rstrip("/") != iss:
        raise SSOError("issuer", f"answer from {params['iss']!r}")
    if params.get("error"):
        raise SSOError("denied" if params["error"] == "access_denied" else "provider",
                       f"{params['error']}: {params.get('error_description', '')}")
    code = params.get("code", "")
    if not code:
        raise SSOError("provider", "no code")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
        meta = await metadata(client)
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": tx["r"]}
        if meta.pkce:
            form["code_verifier"] = tx["v"]
        try:
            r = await client.post(meta.token_endpoint, data=form,
                                  auth=(client_id(), _env("UNO_OIDC_CLIENT_SECRET")),
                                  headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise SSOError("provider", f"token endpoint: {exc}") from exc
        if r.status_code != 200:
            raise SSOError("token", f"token endpoint {r.status_code}: {r.text[:200]}")
        try:
            id_token = r.json().get("id_token", "")
        except ValueError as exc:
            raise SSOError("token", "token endpoint answered not JSON") from exc
        if not id_token:
            raise SSOError("token", "no id_token")
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise SSOError("token", f"id_token: {exc}") from exc
        if header.get("alg") != "RS256":
            raise SSOError("token", f"id_token alg {header.get('alg')!r}")
        key = await _signing_key(client, meta.jwks_uri, header.get("kid"))
    try:
        claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=client_id(), issuer=iss,
                            leeway=LEEWAY, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.PyJWTError as exc:
        raise SSOError("token", f"id_token: {exc}") from exc
    if claims.get("azp") and claims["azp"] != client_id():
        raise SSOError("token", "id_token azp is another client")
    if not hmac.compare_digest(str(claims.get("nonce", "")), tx["n"]):
        raise SSOError("token", "nonce does not match")
    return claims
