"""KIS realtime websocket client for the ETF NAV-disparity system.

Wire protocol verified against the official koreainvestment/open-trading-api
repo via kis-code-assistant-mcp (auth_ws_token.py, etf_nav_trend.py,
asking_price_krx.py) and the official examples_llm/kis_auth.py KISWebSocket:

  - Approval key: POST {KIS_URL_REST}/oauth2/Approval with JSON body
    {"grant_type": "client_credentials", "appkey": ..., "secretkey": ...}
    (NB: the field really is "secretkey", not "appsecret"). Response JSON
    carries "approval_key". Official code re-issues after 24h; we cache for
    ~23h in .kis_ws_approval_cache.json (mode 0600), same style as the
    kis_common token cache.
  - Connect URL: {KIS_URL_WS}/tryitout (official KISWebSocket api_url).
  - Subscribe envelope (official data_fetch()):
      {"header": {"content-type": "utf-8", "approval_key": ...,
                  "tr_type": "1"(reg) | "2"(unreg), "custtype": "P"},
       "body": {"input": {"tr_id": ..., "tr_key": ...}}}
  - Realtime data frame: text starting with '0' (plain) or '1' (encrypted):
      encrypt|tr_id|count|payload  with payload fields '^'-delimited,
      count records concatenated. H0STNAV0 record = 8 fields
      (mksc_shrn_iscd, nav, nav_prdy_vrss_sign, nav_prdy_vrss,
       nav_prdy_ctrt, oprc_nav, hprc_nav, lprc_nav); H0STASP0 record = 59
      fields (MKSC_SHRN_ISCD, BSOP_HOUR, HOUR_CLS_CODE, ASKP1..10,
       BIDP1..10, ASKP_RSQN1..10, BIDP_RSQN1..10, totals/expected...).
  - Everything else is a JSON control frame; PINGPONG is answered with a
    websocket pong carrying the raw text (official: `await ws.pong(raw)`).

The client exposes `events()` - an async iterator of typed dicts - and
handles reconnect (exponential backoff 1s -> 60s) with full resubscribe,
re-issuing the approval key when a subscribe rejection hints at an
authorization problem.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
import websockets

from kis_common import KisApiError, REQUEST_TIMEOUT_SECONDS, sanitize

APPROVAL_URL_PATH = "/oauth2/Approval"
APPROVAL_REUSE_SECONDS = 23 * 3600  # official kis_auth.py re-auths at 24h
WS_API_PATH = "/tryitout"

TR_ID_NAV = "H0STNAV0"
TR_ID_ASP = "H0STASP0"

# Payload record lengths per tr_id (verified field lists, see module docstring)
NAV_RECORD_LEN = 8
ASP_RECORD_LEN = 59

# H0STNAV0 indices
NAV_IDX_CODE = 0
NAV_IDX_NAV = 1
# H0STASP0 indices. The frame carries 10 price levels per side; field order
# (verified against the official asking_price_krx sample): MKSC_SHRN_ISCD[0],
# BSOP_HOUR[1], HOUR_CLS_CODE[2], ASKP1..10[3..12], BIDP1..10[13..22],
# ASKP_RSQN1..10[23..32], BIDP_RSQN1..10[33..42], then totals/expected.
ASP_IDX_CODE = 0
ASP_IDX_HOUR_CLS = 2
ASP_IDX_ASKP1 = 3          # ASKP1..10  -> 3..12
ASP_IDX_BIDP1 = 13         # BIDP1..10  -> 13..22
ASP_IDX_ASKP_RSQN1 = 23    # ASKP_RSQN1..10 -> 23..32
ASP_IDX_BIDP_RSQN1 = 33    # BIDP_RSQN1..10 -> 33..42
ASP_LEVELS = 10

SUBSCRIBE_GAP_SECONDS = 0.1
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
HEALTHY_CONNECTION_SECONDS = 30.0  # connection older than this resets backoff

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APPROVAL_CACHE_PATH = PROJECT_ROOT / ".kis_ws_approval_cache.json"


# ---------------------------------------------------------------- approval key

def _read_approval_cache(cache_path: Path) -> str | None:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    key = data.get("approval_key")
    issued_at = data.get("issued_at")
    if not key or issued_at is None:
        return None
    try:
        if time.time() - float(issued_at) < APPROVAL_REUSE_SECONDS:
            return str(key)
    except (TypeError, ValueError):
        return None
    return None


def _write_approval_cache(cache_path: Path, approval_key: str) -> None:
    data = {"approval_key": approval_key, "issued_at": time.time()}
    try:
        fd = os.open(cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass  # best-effort cache


def get_approval_key(
    app_key: str,
    app_secret: str,
    rest_base_url: str,
    cache_path: Path = DEFAULT_APPROVAL_CACHE_PATH,
    force_new: bool = False,
) -> str:
    """Issue (or reuse from cache) a websocket approval key.

    POST /oauth2/Approval with body field "secretkey" (verified against the
    official auth_ws_token.py - it is NOT "appsecret" here).
    """
    if not force_new:
        cached = _read_approval_cache(cache_path)
        if cached:
            return cached

    url = f"{rest_base_url}{APPROVAL_URL_PATH}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
    }
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "secretkey": app_secret,
    }
    secrets = [app_key, app_secret]

    try:
        resp = httpx.post(
            url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except httpx.RequestError as e:
        raise KisApiError(
            f"웹소켓 접속키 발급 네트워크 오류: {sanitize(str(e), secrets)}"
        ) from None

    if resp.status_code != 200:
        raise KisApiError(
            f"웹소켓 접속키 발급 실패 HTTP {resp.status_code}: "
            f"{sanitize(resp.text, secrets)[:300]}"
        )

    try:
        approval_key = str(resp.json()["approval_key"])
    except (json.JSONDecodeError, KeyError, TypeError):
        raise KisApiError("웹소켓 접속키 응답에 approval_key가 없습니다") from None
    if not approval_key:
        raise KisApiError("웹소켓 접속키 응답의 approval_key가 비어 있습니다")

    _write_approval_cache(cache_path, approval_key)
    return approval_key


# ---------------------------------------------------------------- frame parsing

def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_data_frame(raw: str, ts: float) -> tuple[list[dict[str, Any]], str | None]:
    """Parse a realtime data frame (raw[0] in '0'/'1') into typed events.

    Returns (events, error). error is a short description when the frame
    could not be fully parsed (events already parsed are still returned).
    """
    parts = raw.split("|", 3)
    if len(parts) < 4:
        return [], "구분자 부족 (|<4)"
    encrypt_flag, tr_id, count_str, payload = parts
    if encrypt_flag == "1":
        # H0STNAV0/H0STASP0 are plain; we don't carry an AES dependency.
        return [], f"암호화 프레임 수신(미지원): {tr_id}"

    if tr_id == TR_ID_NAV:
        record_len = NAV_RECORD_LEN
    elif tr_id == TR_ID_ASP:
        record_len = ASP_RECORD_LEN
    else:
        return [], f"알 수 없는 tr_id: {tr_id}"

    fields = payload.split("^")
    try:
        count = max(1, int(count_str))
    except ValueError:
        count = 1
    if len(fields) < record_len:
        return [], f"{tr_id} 필드 수 부족: {len(fields)} < {record_len}"
    if len(fields) != count * record_len:
        # Field count doesn't match count*record_len - parse what fits.
        count = len(fields) // record_len

    events: list[dict[str, Any]] = []
    error: str | None = None
    for i in range(count):
        rec = fields[i * record_len:(i + 1) * record_len]
        if tr_id == TR_ID_NAV:
            nav = _to_float(rec[NAV_IDX_NAV])
            code = rec[NAV_IDX_CODE]
            if nav is None or not code:
                error = f"{TR_ID_NAV} 레코드 파싱 실패"
                continue
            events.append({"type": "nav", "code": code, "nav": nav, "ts": ts})
        else:
            code = rec[ASP_IDX_CODE]
            ask1 = _to_int(rec[ASP_IDX_ASKP1])
            bid1 = _to_int(rec[ASP_IDX_BIDP1])
            ask1_qty = _to_int(rec[ASP_IDX_ASKP_RSQN1])
            bid1_qty = _to_int(rec[ASP_IDX_BIDP_RSQN1])
            hour_cls = rec[ASP_IDX_HOUR_CLS]
            if not code or None in (ask1, bid1, ask1_qty, bid1_qty):
                error = f"{TR_ID_ASP} 레코드 파싱 실패"
                continue
            # Full ladders: a valid ladder stops at the first empty level
            # (price or qty is 0/None). ladder[0] == (ask1, ask1_qty).
            ask_ladder = _parse_ladder(
                rec, ASP_IDX_ASKP1, ASP_IDX_ASKP_RSQN1
            )
            bid_ladder = _parse_ladder(
                rec, ASP_IDX_BIDP1, ASP_IDX_BIDP_RSQN1
            )
            events.append(
                {
                    "type": "quote",
                    "code": code,
                    "ask1": ask1,
                    "ask1_qty": ask1_qty,
                    "bid1": bid1,
                    "bid1_qty": bid1_qty,
                    "ask_ladder": ask_ladder,
                    "bid_ladder": bid_ladder,
                    "hour_cls_code": hour_cls,
                    "ts": ts,
                }
            )
    return events, error


def _parse_ladder(
    rec: list[str], price_base: int, qty_base: int
) -> list[tuple[int, int]]:
    """Extract [(price, qty), ...] for up to ASP_LEVELS levels, stopping at
    the first empty level (price or qty is 0/None). Best level first."""
    ladder: list[tuple[int, int]] = []
    for lvl in range(ASP_LEVELS):
        price = _to_int(rec[price_base + lvl])
        qty = _to_int(rec[qty_base + lvl])
        if not price or not qty or price <= 0 or qty <= 0:
            break
        ladder.append((price, qty))
    return ladder


def _looks_like_auth_failure(msg_cd: str, msg1: str) -> bool:
    text = f"{msg_cd} {msg1}".lower()
    return any(
        token in text
        for token in ("approval", "인증", "접속키", "unauthorized", "invalid key")
    )


# ---------------------------------------------------------------- client

class KisWsClient:
    """Async KIS websocket client: subscribe NAV+호가 for a fixed code list.

    Usage:
        client = KisWsClient(ws_url, app_key, app_secret, rest_base_url, codes,
                             journal_fn=journal.append)
        async for event in client.events():
            ...  # {"type": "nav"|"quote", ...}

    events() never returns on its own; it reconnects forever (exponential
    backoff 1s->60s, full resubscribe). Cancel the surrounding task to stop.
    """

    def __init__(
        self,
        ws_url: str,
        app_key: str,
        app_secret: str,
        rest_base_url: str,
        codes: list[str],
        journal_fn: Callable[[str, dict[str, Any]], None] | None = None,
        approval_cache_path: Path = DEFAULT_APPROVAL_CACHE_PATH,
    ):
        self._ws_url = ws_url.rstrip("/") + WS_API_PATH
        self._app_key = app_key
        self._app_secret = app_secret
        self._rest_base_url = rest_base_url
        self._codes = list(codes)
        self._journal = journal_fn or (lambda _e, _p: None)
        self._approval_cache_path = approval_cache_path
        self._force_new_key = False
        self.reconnect_count = 0

    # -- helpers ------------------------------------------------------------

    def _subscribe_msg(self, approval_key: str, tr_id: str, tr_key: str,
                       tr_type: str = "1") -> str:
        return json.dumps(
            {
                "header": {
                    "approval_key": approval_key,
                    "custtype": "P",
                    "tr_type": tr_type,
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
            }
        )

    async def _subscribe_all(self, ws, approval_key: str) -> None:
        """Register H0STNAV0 + H0STASP0 for every code, throttled."""
        for code in self._codes:
            for tr_id in (TR_ID_NAV, TR_ID_ASP):
                await ws.send(self._subscribe_msg(approval_key, tr_id, code))
                await asyncio.sleep(SUBSCRIBE_GAP_SECONDS)

    def _handle_control_frame(self, raw: str) -> str | None:
        """Journal a JSON control frame; return 'PINGPONG' when it is one.

        Raises KisApiError when the frame is a subscribe rejection that
        looks like an approval-key/auth failure (so the caller reconnects
        with a fresh key).
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._journal("ws_control_unparsed", {"raw": raw[:200]})
            return None

        header = data.get("header") or {}
        tr_id = str(header.get("tr_id", ""))
        if tr_id == "PINGPONG":
            self._journal(
                "ws_pingpong", {"datetime": str(header.get("datetime", ""))}
            )
            return "PINGPONG"

        body = data.get("body") or {}
        rt_cd = str(body.get("rt_cd", ""))
        msg_cd = str(body.get("msg_cd", ""))
        msg1 = str(body.get("msg1", ""))
        tr_key = str(header.get("tr_key", ""))

        event = "ws_subscribe_ok" if rt_cd == "0" else "ws_control_error"
        self._journal(
            event,
            {"tr_id": tr_id, "tr_key": tr_key, "rt_cd": rt_cd,
             "msg_cd": msg_cd, "msg1": msg1},
        )
        if rt_cd not in ("", "0") and _looks_like_auth_failure(msg_cd, msg1):
            self._force_new_key = True
            raise KisApiError(
                f"웹소켓 구독 인증 거부 (msg_cd={msg_cd}): 접속키 재발급 후 재접속"
            )
        return None

    # -- main loop ----------------------------------------------------------

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed nav/quote events forever, reconnecting as needed."""
        backoff = BACKOFF_INITIAL_SECONDS
        first_connect = True

        while True:
            try:
                approval_key = await asyncio.to_thread(
                    get_approval_key,
                    self._app_key,
                    self._app_secret,
                    self._rest_base_url,
                    self._approval_cache_path,
                    self._force_new_key,
                )
                self._force_new_key = False

                connected_at = time.monotonic()
                async with websockets.connect(self._ws_url) as ws:
                    if not first_connect:
                        self.reconnect_count += 1
                    self._journal(
                        "ws_connected",
                        {"url": self._ws_url,
                         "reconnect_count": self.reconnect_count},
                    )
                    first_connect = False
                    await self._subscribe_all(ws, approval_key)
                    self._journal(
                        "ws_subscribed_all",
                        {"codes": len(self._codes),
                         "registrations": len(self._codes) * 2},
                    )

                    async for raw in ws:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        now = time.time()
                        if raw[:1] in ("0", "1"):
                            events, error = parse_data_frame(raw, now)
                            if error:
                                self._journal(
                                    "ws_parse_error",
                                    {"error": error, "raw": raw[:200]},
                                )
                            for ev in events:
                                yield ev
                        else:
                            kind = self._handle_control_frame(raw)
                            if kind == "PINGPONG":
                                # Official kis_auth.py echoes the raw text
                                # as a pong payload.
                                await ws.pong(raw)

                        if time.monotonic() - connected_at > HEALTHY_CONNECTION_SECONDS:
                            backoff = BACKOFF_INITIAL_SECONDS

                # Server closed the connection cleanly -> reconnect.
                self._journal("ws_disconnected", {"reason": "정상 종료 프레임 수신"})

            except asyncio.CancelledError:
                raise
            except (
                KisApiError,
                websockets.exceptions.WebSocketException,
                OSError,
            ) as e:
                self._journal(
                    "ws_disconnected",
                    {
                        "reason": sanitize(
                            f"{type(e).__name__}: {e}",
                            [self._app_key, self._app_secret],
                        )[:300],
                        "retry_in_seconds": backoff,
                    },
                )

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
