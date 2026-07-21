"""Shared KIS (Korea Investment & Securities) Open API primitives.

Extracted from kis_price.py so the same secret-sanitization, credential
loading, token-cache, and OAuth token-issuance logic can serve both:
  - the real-trading flow (kis_price.py: KIS_APP_KEY / KIS_APP_SECRET /
    KIS_URL_REST / .kis_token_cache.json)
  - the paper-trading flow (order_buy_005930.py: KIS_PAPER_APP_KEY /
    KIS_PAPER_APP_SECRET / KIS_URL_REST_PAPER / .kis_paper_token_cache.json)

Every function here is parameterized by app key, app secret, base URL and/or
token-cache path rather than hardcoding which credential set to use - callers
decide which environment (real vs paper) they're operating in.

Price-fetching/formatting logic (get_current_price, format_price_output)
stays in kis_price.py; only the genuinely shared primitives live here.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

TOKEN_URL_PATH = "/oauth2/tokenP"
TOKEN_EXPIRY_BUFFER_SECONDS = 300  # renew 5 min before actual expiry
REQUEST_TIMEOUT_SECONDS = 10.0

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = SCRIPT_DIR / ".env"


class KisApiError(Exception):
    """Raised for KIS API/auth failures. Message must never contain secrets."""


def sanitize(text: str, secrets: list[str]) -> str:
    """Strip any known secret values out of a string before it is surfaced."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def load_credentials(
    required: list[str], env_path: Path | None = None
) -> dict[str, str]:
    """Load and validate the given required env var names from .env.

    Returns a dict of name -> value for every required var. Raises
    KisApiError (listing the missing names, never any values) if any
    required var is absent/blank.
    """
    load_dotenv(env_path or DEFAULT_ENV_PATH)

    values: dict[str, str] = {}
    missing: list[str] = []
    for name in required:
        val = os.environ.get(name, "").strip()
        if not val:
            missing.append(name)
        else:
            values[name] = val

    if missing:
        raise KisApiError(
            "Missing required environment variable(s) in .env: "
            + ", ".join(missing)
        )

    return values


def read_token_cache(cache_path: Path) -> dict[str, Any] | None:
    """Read a token-cache JSON file. Returns None if absent/corrupt."""
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        # Corrupt/unreadable cache -> treat as absent, re-issue a token.
        return None


def write_token_cache(cache_path: Path, access_token: str, expires_in: int) -> None:
    """Persist an issued token to cache_path with mode 0600."""
    data = {
        "access_token": access_token,
        "issued_at": time.time(),
        "expires_in": expires_in,
    }
    try:
        fd = os.open(cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        # Caching is a best-effort optimization; failing to write it should
        # not break the caller's request.
        pass


def _cached_token_if_valid(cache: dict[str, Any] | None) -> str | None:
    if not cache:
        return None
    token = cache.get("access_token")
    issued_at = cache.get("issued_at")
    expires_in = cache.get("expires_in")
    if not token or issued_at is None or expires_in is None:
        return None
    try:
        expiry = float(issued_at) + float(expires_in)
    except (TypeError, ValueError):
        return None
    if time.time() < expiry - TOKEN_EXPIRY_BUFFER_SECONDS:
        return token
    return None


def get_access_token(
    app_key: str, app_secret: str, base_url: str, cache_path: Path
) -> str:
    """Obtain a KIS OAuth access token, reusing a cached one if still valid.

    cache_path selects which token cache to read/write, so real-trading and
    paper-trading credentials never share (or clobber) each other's cache.
    """
    cached = _cached_token_if_valid(read_token_cache(cache_path))
    if cached:
        return cached

    url = f"{base_url}{TOKEN_URL_PATH}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
    }
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret,
    }

    try:
        resp = httpx.post(
            url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except httpx.RequestError as e:
        raise KisApiError(
            f"Network error while requesting OAuth token: {sanitize(str(e), [app_key, app_secret])}"
        ) from None

    if resp.status_code != 200:
        raise KisApiError(
            "OAuth token issuance failed with HTTP "
            f"{resp.status_code}: {sanitize(resp.text, [app_key, app_secret])[:300]}"
        )

    try:
        payload = resp.json()
        access_token = payload["access_token"]
        expires_in = int(payload["expires_in"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise KisApiError(
            "OAuth token issuance returned an unexpected response shape"
        ) from None

    write_token_cache(cache_path, access_token, expires_in)
    return access_token
