#!/usr/bin/env python3
"""
MultiLongDCA-Bot — Multi-Symbol DCA Long Bot, each symbol priced and
sized independently at its own rolling 9-day low, each with its own
independent budget and window.

Single-process, single-machine bot for Fly.io.

This is a multi-symbol extension of OilLongDCA-Bot. Every symbol
in SYMBOLS is treated fully independently: its own budget, its own
window, its own fire history, its own 9-day-low computation.
Nothing is pooled or shared across symbols except the process, the
HTTP status server, and the state file.

Symbols and their DCA parameters (budget / days / start date) are
configured in one place — SYMBOL_CONFIG, near the top of this file.
To add or remove a symbol, only edit SYMBOL_CONFIG.

Behavior:
  - On startup: run a one-time TEST order (limit LONG at market-10%,
    sized at that symbol's own exchange-reported minimum order size)
    against EVERY symbol in SYMBOLS, in three flat batch phases
    (no per-symbol threads):

      1. OPEN  — send a test limit LONG for every symbol, one after
         another.
      2. WAIT  — sleep once, for TEST_ORDER_WAIT_SEC seconds, for
         the whole batch.
      3. CLOSE — for every symbol that successfully opened, check
         fill status and cancel if unfilled, one after another.

    ANY failure at any phase — invalid/zero mark price, a rejected
    order, a failed cancel, or any exception — flags that symbol as
    FAILED for the remainder of this process's lifetime: it is
    excluded from the daily DCA engine and shown in a visually
    distinct flagged state on the SVG status page. A symbol whose
    test order fills during the wait is NOT a failure — that's a
    real open position, logged as such, and the symbol remains
    validated and continues trading.

    Total startup delay is roughly TEST_ORDER_WAIT_SEC plus the time
    to fire off 2 x len(SYMBOLS) sequential HTTP requests — much
    less than testing symbols one-at-a-time with a full wait each.

  - Every hour on the hour: refresh mark prices + rolling 9d lows
    for all non-failed symbols and refresh the in-memory SVG status
    table (failed symbols are still shown, flagged, with no
    mark/low refreshed).

  - Only at hour == 00 UTC: for each symbol NOT flagged failed, if
    today's calendar date falls within THAT SYMBOL'S OWN DCA window
    (per-symbol start dates in SYMBOL_CONFIG) AND that symbol has
    not already fired today (per persisted fire-history), place a
    limit LONG priced AND SIZED at that symbol's current trailing
    9-day low (including today's not-yet-closed bar via the daily
    klines fetch — see rolling_9d_low) for that day's slice (that
    symbol's own budget / that symbol's own DCA_DAYS).

    Each symbol's daily slice is computed from ITS OWN budget and day
    count (DCA_BUDGET_USD[sym] / DCA_DAYS[sym]), not a shared pool —
    symbols can run different budgets or different windows without
    restructuring the code.

    Sizing uses the 9d-low price (the limit price itself), NOT mark.

    Reasoning: a resting limit long fills at its limit price or
    better. The 9d-low is therefore the price this order is intended
    to transact at if it fills. Sizing off mark while pricing off the
    9d-low would make the filled notional smaller than the intended
    daily dollar slice. Sizing off the 9d-low itself makes the
    notional-at-fill approximately equal to the daily slice amount.

    Mark is still fetched and logged for visibility/context, just not
    used for DCA sizing.

    The order is left open with no timeout — if a previous day's
    order for that symbol is still unfilled, it is left resting and
    a new order is placed on top of it (orders stack; nothing is ever
    cancelled by the daily engine).

  - Pricing every day's DCA slice at the rolling 9d low rather than
    at mark is deliberate: it reaches for a better-than-market long
    entry every day and naturally scales with each symbol's own
    volatility.

    The tradeoff is that in a sustained downtrend the 9d low chases
    price downward and fills may not be much better than mark, while
    resting orders may take a long time or never fill.

  - If a symbol's daily klines can't be fetched or return fewer than
    ROLL_DAYS closed bars on a given midnight wake, that symbol is
    skipped for the day (not fired, not marked as fired) rather than
    falling back to any placeholder price. It will be retried at the
    next midnight wake.

  - A restart cannot double-fire a given symbol on a given UTC date:
    fire history (symbol -> list of ISO dates already fired) is
    persisted to a local JSON file and checked before every fire.

  - A restart also reruns the full startup test-order suite for
    every symbol, including ones that failed before — FAILED_SYMBOLS
    is in-memory only and is not persisted, so a symbol that failed
    due to a transient issue can recover on the next restart.

  - Every placed order is logged (id, symbol, price, usd, contracts)
    and recorded into the same local JSON state file alongside fire
    history, so open orders can be cross-checked against MEXC's
    open-orders API at any time (see / status page, /orders.json,
    /failed.json, and logs).

No CLI arguments. No config files beyond the state file (which stores
history/records, not config). No web UI for configuration. All
parameters are hardcoded constants below. A second thread runs a
small public HTTP server that exposes the current status table as an
SVG and a minimal HTML wrapper page.

Environment (secrets only, not behavior):
  MEXC        - MEXC API key
  MEXCSECRET  - MEXC API secret

IMPORTANT:
  Contract specifications are fetched live from MEXC at startup.
  The bot does not hardcode priceUnit, volUnit, or contractSize.

  The daily kline response is expected to expose parallel arrays
  including 'time' and 'realLow'/'low'. The bot excludes the current
  still-open daily candle and calculates the minimum low over the
  latest ROLL_DAYS closed candles.
"""

import datetime
import hashlib
import hmac
import http.server
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ── constants ─────────────────────────────────────────────────────────────────

UTC = datetime.timezone.utc

MEXC_KEY    = os.getenv("MEXC")
MEXC_SECRET = os.getenv("MEXCSECRET")
MEXC_BASE   = "https://api.mexc.co"


# ── symbol configuration ──────────────────────────────────────────────────────
#
# Single source of truth for which symbols the bot trades and each
# symbol's own independent DCA parameters. To add or remove a symbol,
# only edit SYMBOL_CONFIG below — nothing else in the file needs to
# change.
#
# Each symbol is completely independent: its own budget, its own
# window, its own fire history, its own 9-day-low computation.
# Nothing is pooled or shared across symbols except the process, the
# HTTP status server, and the state file.
#
# Fields:
#   budget_usd  - total USD to deploy over the window
#   days        - length of the DCA window, in days
#   start_date  - first calendar date (UTC) eligible to fire
#
# USOIL_USDT     = WTI Crude Oil
# UKOIL_USDT     = Brent Crude Oil
# SPCXSTOCK_USDT = SPCX
# COPPER_USDT    = Copper
# SILVER_USDT    = Silver
# XAU_USDT       = Gold
# URNM_USDT      = Uranium

SYMBOL_CONFIG: Dict[str, Dict] = {
    "USOIL_USDT": {
        "budget_usd": 1000.0,
        "days": 90,
        "start_date": datetime.date(2026, 8, 10),
    },
    "UKOIL_USDT": {
        "budget_usd": 1000.0,
        "days": 90,
        "start_date": datetime.date(2026, 8, 10),
    },
    "SPCXSTOCK_USDT": {
        "budget_usd": 1000.0,
        "days": 90,
        "start_date": datetime.date(2026, 8, 10),
    },
    "COPPER_USDT": {
        "budget_usd": 1000.0,
        "days": 90,
        "start_date": datetime.date(2026, 8, 10),
    },
    "SILVER_USDT": {
        "budget_usd": 1000.0,
        "days": 90,
        "start_date": datetime.date(2026, 8, 10),
    },
    "XAU_USDT": {
        "budget_usd": 1000.0,
        "days": 90,
        "start_date": datetime.date(2026, 8, 10),
    },
    "URNM_USDT": {
        "budget_usd": 1000.0,
        "days": 90,
        "start_date": datetime.date(2026, 8, 10),
    },
}

# Derived — do not edit directly. Change SYMBOL_CONFIG instead.
SYMBOLS: List[str] = list(SYMBOL_CONFIG.keys())

DCA_BUDGET_USD: Dict[str, float] = {
    sym: cfg["budget_usd"] for sym, cfg in SYMBOL_CONFIG.items()
}

DCA_DAYS: Dict[str, int] = {
    sym: cfg["days"] for sym, cfg in SYMBOL_CONFIG.items()
}

DCA_START_DATE: Dict[str, datetime.date] = {
    sym: cfg["start_date"] for sym, cfg in SYMBOL_CONFIG.items()
}

DCA_DAILY_USD: Dict[str, float] = {
    sym: DCA_BUDGET_USD[sym] / DCA_DAYS[sym]
    for sym in SYMBOLS
}


def in_dca_window(sym: str, d: datetime.date) -> bool:
    start = DCA_START_DATE[sym]
    end = start + datetime.timedelta(days=DCA_DAYS[sym] - 1)
    return start <= d <= end


LEVERAGE  = 30
ROLL_DAYS = 9


# ── timing ────────────────────────────────────────────────────────────────────

HOURLY_SLEEP_FLOOR_SEC = 5


# ── startup test order ────────────────────────────────────────────────────────
#
# All symbols are tested in one batch, three flat phases (open all,
# wait once, close all) — no per-symbol threads, no per-symbol wait.
#
# Test orders are sized at each symbol's own exchange-reported
# minimum order size (in contracts), not a fixed USD amount.

TEST_ORDER_DISCOUNT = 0.90
TEST_ORDER_WAIT_SEC = 20


# ── failed-symbol tracking ────────────────────────────────────────────────────
#
# Populated during startup test orders. Any symbol in this set is
# excluded from the DCA engine and rendering treats it as flagged,
# for the remainder of this process's lifetime. Resets on restart
# (the whole test suite reruns on every startup).

FAILED_SYMBOLS: set = set()
_FAILED_LOCK = threading.Lock()


def flag_failed(sym: str, reason: str):

    with _FAILED_LOCK:
        FAILED_SYMBOLS.add(sym)

    log.error(
        f"[{sym}] FLAGGED FAILED — {reason} — "
        "excluded from DCA engine"
    )


def is_failed(sym: str) -> bool:

    with _FAILED_LOCK:
        return sym in FAILED_SYMBOLS


# ── HTTP server ───────────────────────────────────────────────────────────────

HTTP_HOST = "0.0.0.0"
HTTP_PORT = int(os.getenv("PORT", "8080"))


# ── persistence ──────────────────────────────────────────────────────────────

STATE_FILE = os.getenv(
    "DCA_STATE_FILE",
    "/data/multi_dca_fire_history.json"
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s"
)

log = logging.getLogger()


specs: Dict[str, Dict] = {}


# ── shared state ──────────────────────────────────────────────────────────────

class SharedState:

    def __init__(self):
        self._lock = threading.Lock()

        self._svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' "
            "width='600' height='100'>"
            "<text x='10' y='50'>Initializing...</text>"
            "</svg>"
        )

        self._status = "initializing"

    def set_svg(self, svg: str):
        with self._lock:
            self._svg = svg

    def get_svg(self) -> str:
        with self._lock:
            return self._svg

    def set_status(self, status: str):
        with self._lock:
            self._status = status

    def get_status(self) -> str:
        with self._lock:
            return self._status


STATE = SharedState()


# ── persisted state ──────────────────────────────────────────────────────────

def _default_state() -> Dict:
    return {
        "fired": {},
        "orders": []
    }


def load_state() -> Dict:
    """
    Load:

        {
            "fired": {
                "USOIL_USDT": ["2026-08-10", ...],
                "UKOIL_USDT": ["2026-08-10", ...],
                ...
            },
            "orders": [...]
        }

    from STATE_FILE.

    Missing or corrupt file -> fresh empty state.
    """

    try:

        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("state file did not contain a dict")

        data.setdefault("fired", {})
        data.setdefault("orders", [])

        return data

    except FileNotFoundError:

        log.info(
            f"no state file at {STATE_FILE} — starting fresh"
        )

        return _default_state()

    except Exception as e:

        log.error(
            f"state file at {STATE_FILE} unreadable ({e}) "
            f"— starting fresh"
        )

        return _default_state()


def save_state(state: Dict):

    try:

        os.makedirs(
            os.path.dirname(STATE_FILE) or ".",
            exist_ok=True
        )

        tmp = STATE_FILE + ".tmp"

        with open(tmp, "w") as f:
            json.dump(state, f)

        os.replace(tmp, STATE_FILE)

    except Exception as e:

        log.error(
            f"failed to persist state to {STATE_FILE}: {e}"
        )


STATE_DATA: Dict = load_state()
_STATE_DATA_LOCK = threading.Lock()


def has_fired_today(
    sym: str,
    d: datetime.date
) -> bool:

    return d.isoformat() in STATE_DATA["fired"].get(sym, [])


def mark_fired(
    sym: str,
    d: datetime.date,
    order_record: Dict
):

    with _STATE_DATA_LOCK:

        STATE_DATA["fired"].setdefault(sym, []).append(
            d.isoformat()
        )

        STATE_DATA["orders"].append(order_record)

        save_state(STATE_DATA)


def record_order_only(order_record: Dict):

    """
    Record an order (e.g. a startup test order that filled) into
    the state file's order log without marking any symbol as fired
    for DCA purposes.
    """

    with _STATE_DATA_LOCK:

        STATE_DATA["orders"].append(order_record)

        save_state(STATE_DATA)


def fired_count(sym: str) -> int:

    return len(
        STATE_DATA["fired"].get(sym, [])
    )


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http(
    method,
    url,
    headers=None,
    data=None,
    params=None
):

    if params:

        url += "?" + urllib.parse.urlencode(
            sorted(params.items())
        )

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method=method
    )

    try:

        with urllib.request.urlopen(
            req,
            timeout=10
        ) as r:

            body = r.read()

    except urllib.error.HTTPError as e:

        body = e.read()

    return (
        json.loads(body)
        if body.strip()
        else {}
    )


def _get(url):

    with urllib.request.urlopen(
        url,
        timeout=10
    ) as r:

        return json.loads(r.read())


# ── MEXC signed requests ─────────────────────────────────────────────────────

def mexc(
    method,
    endpoint,
    params=None,
    body=None
):

    params = params or {}

    ts = str(
        int(time.time() * 1000)
    )

    sp = (
        "&".join(
            f"{k}={v}"
            for k, v in sorted(params.items())
        )
        if method == "GET"
        else (
            json.dumps(
                body,
                separators=(",", ":"),
                sort_keys=True
            )
            if body
            else ""
        )
    )

    sig = hmac.new(
        MEXC_SECRET.encode(),
        (MEXC_KEY + ts + sp).encode(),
        hashlib.sha256
    ).hexdigest()

    hdr = {
        "ApiKey": MEXC_KEY,
        "Request-Time": ts,
        "Signature": sig,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    raw = (
        json.dumps(
            body,
            separators=(",", ":"),
            sort_keys=True
        ).encode()
        if body and method not in ("GET", "DELETE")
        else None
    )

    try:

        return _http(
            method,
            MEXC_BASE + endpoint,
            headers=hdr,
            data=raw,
            params=params
            if method in ("GET", "DELETE")
            else None
        )

    except Exception as e:

        log.error(
            f"mexc {method} {endpoint}: {e}"
        )

        return {}


# ── contract specifications ───────────────────────────────────────────────────

def load_specs():

    """
    Fetch contract specifications once for every symbol.

    Nothing is hardcoded for:
      - priceUnit
      - volUnit
      - contractSize

    The live MEXC contract detail response is authoritative.

    A symbol whose specs cannot be loaded is flagged failed rather
    than aborting the whole process, so other symbols can still
    trade.
    """

    rows = (
        mexc(
            "GET",
            "/api/v1/contract/detail"
        ).get("data") or []
    )

    if not rows:

        log.error(
            "empty contract detail response from MEXC — "
            "flagging all symbols failed"
        )

        for sym in SYMBOLS:

            flag_failed(
                sym,
                "empty contract detail response from MEXC"
            )

        return

    by_sym = {
        c.get("symbol", "").upper(): c
        for c in rows
    }

    for sym in SYMBOLS:

        match = by_sym.get(sym)

        if match is None:

            flag_failed(
                sym,
                "symbol not found in MEXC contract detail"
            )

            continue

        vu = float(
            match.get("volUnit", 1)
        )

        pu = float(
            match.get("priceUnit", 0.01)
        )

        cs = float(
            match.get("contractSize", vu)
        )

        raw = (
            f"{vu:.10f}"
            .rstrip("0")
        )

        p = (
            len(raw.split(".")[1])
            if "." in raw
            else 0
        )

        specs[sym] = {
            "p": p,
            "t": pu,
            "vu": vu,
            "cs": cs,
        }

        log.info(
            f"loaded specs for {sym}: "
            f"{specs[sym]}"
        )


def _tick(sym):

    return specs.get(
        sym,
        {}
    ).get("t", 0.01)


def _prec(sym):

    return specs.get(
        sym,
        {}
    ).get("p", 0)


def _rfmt_price(sym, v):

    t = _tick(sym)

    r = round(v / t) * t

    s = (
        f"{t:.10f}"
        .rstrip("0")
    )

    dec = (
        len(s.split(".")[1])
        if "." in s
        else 0
    )

    return f"{r:.{dec}f}"


def _rfmt_vol(sym, v):

    p = _prec(sym)

    if p >= 0:

        return (
            f"{round(v, p):.{p}f}"
        )

    d = 10 ** abs(p)

    return str(
        int(round(v / d) * d)
    )


def _contracts(
    sym,
    usd,
    price
):

    """
    Contract count so that approximately `usd`
    dollars of notional trades at `price`.
    """

    cs = specs.get(
        sym,
        {}
    ).get("cs", 1.0)

    return float(
        _rfmt_vol(
            sym,
            max(
                0,
                usd / (cs * price)
            )
        )
    )


def _mos(sym):

    """
    Minimum order size, in contracts, as reported by the exchange
    (volUnit).
    """

    return specs.get(
        sym,
        {}
    ).get("vu", 1.0)


# ── open orders ───────────────────────────────────────────────────────────────

def _open_orders_for_sym(
    sym: str
) -> List[Dict]:

    """
    MEXC's symbol query parameter is not treated
    as authoritative here, so filter client-side.
    """

    data = (
        mexc(
            "GET",
            "/api/v1/private/order/list/open_orders",
            params={
                "symbol": sym,
                "page_num": 1,
                "page_size": 100,
            }
        ).get("data") or []
    )

    if isinstance(data, dict):

        data = data.get(
            "resultList",
            []
        )

    return [
        o for o in data
        if o.get(
            "symbol",
            ""
        ).upper() == sym
    ]


def _open_ids(sym: str) -> set:

    return {
        str(o.get("orderId", ""))
        for o in _open_orders_for_sym(sym)
    }


# ── order placement ──────────────────────────────────────────────────────────

def place_long(
    sym: str,
    limit_price: float,
    sizing_price: float,
    usd_amount: float
) -> Optional[str]:

    """
    Place a limit LONG / buy-to-open order, sized in USD notional
    (converted to contracts via sizing_price).

    limit_price:
        Actual order limit price.

    sizing_price:
        Price used to calculate contracts.

    For normal DCA orders both are the 9-day low.
    """

    vol = _contracts(
        sym,
        usd_amount,
        sizing_price
    )

    if vol < _mos(sym):

        log.warning(
            f"[{sym}] size {vol} < min "
            f"{_mos(sym)} (${usd_amount:.2f}) "
            "— order skipped"
        )

        return "SKIP"

    return _place_long_contracts(
        sym,
        limit_price,
        vol
    )


def place_long_min_size(
    sym: str,
    limit_price: float
) -> Optional[str]:

    """
    Place a limit LONG sized at exactly this symbol's own
    exchange-reported minimum order size (contracts), regardless of
    USD notional. Used for startup test orders so every symbol's
    test is as small as the exchange allows.
    """

    vol = _mos(sym)

    return _place_long_contracts(
        sym,
        limit_price,
        vol
    )


def _place_long_contracts(
    sym: str,
    limit_price: float,
    vol: float
) -> Optional[str]:

    body = {
        "leverage": LEVERAGE,
        "openType": 2,
        "positionMode": 1,
        "price": _rfmt_price(
            sym,
            limit_price
        ),
        "side": 1,
        "symbol": sym,
        "type": 1,
        "vol": _rfmt_vol(
            sym,
            vol
        ),
    }

    r = mexc(
        "POST",
        "/api/v1/private/order/create",
        body=body
    )

    if not r.get("success"):

        log.error(
            f"[{sym}] long order rejected: {r}"
        )

        return None

    data = r.get("data") or {}

    if not isinstance(data, dict):

        log.error(
            f"[{sym}] unexpected 'data' shape "
            f"from order/create: {data!r}"
        )

        return None

    oid = data.get("orderId")

    if not oid:

        log.error(
            f"[{sym}] order/create succeeded "
            f"but no 'orderId' in data: {data!r}"
        )

        return None

    oid = str(oid)

    log.info(
        f"[{sym}] limit LONG "
        f"{_rfmt_vol(sym, vol)} "
        f"@ {_rfmt_price(sym, limit_price)} "
        f"id={oid}"
    )

    return oid


# ── cancel order ──────────────────────────────────────────────────────────────

def cancel_order(
    sym: str,
    oid: str
) -> bool:

    body = [oid]

    r = mexc(
        "POST",
        "/api/v1/private/order/cancel",
        body=body
    )

    ok = bool(
        r.get("success")
    )

    if ok:

        log.info(
            f"[{sym}] cancelled order "
            f"id={oid}"
        )

    else:

        log.error(
            f"[{sym}] cancel failed "
            f"for id={oid}: {r}"
        )

    return ok


def is_filled(
    sym: str,
    oid: str
) -> bool:

    return oid not in _open_ids(sym)


# ── mark price ────────────────────────────────────────────────────────────────

def get_mark(
    sym: str
) -> float:

    d = (
        mexc(
            "GET",
            "/api/v1/contract/ticker",
            params={"symbol": sym}
        ).get("data") or {}
    )

    return float(
        d.get(
            "fairPrice",
            d.get("lastPrice", 0)
        ) or 0
    )


# ── daily klines ──────────────────────────────────────────────────────────────

def fetch_daily_bars(
    sym: str,
    lookback_days: int
) -> List[Dict]:

    """
    Fetch daily candles for the specified symbol.

    The current still-open daily candle is excluded.
    """

    now_s = int(
        time.time()
    )

    start_s = (
        now_s
        - (lookback_days + 2) * 86400
    )

    url = (
        f"{MEXC_BASE}/api/v1/contract/kline/{sym}"
        f"?interval=Day1"
        f"&start={start_s}"
        f"&end={now_s}"
    )

    try:

        raw = _get(url)

    except Exception as e:

        log.error(
            f"[{sym}] daily kline fetch failed: {e}"
        )

        return []

    if not raw.get("success"):

        log.error(
            f"[{sym}] daily kline fetch "
            f"unsuccessful: {raw}"
        )

        return []

    d = raw.get("data") or {}

    times = d.get("time") or []

    lows = (
        d.get("realLow")
        or d.get("low")
        or []
    )

    bars = []

    for i in range(
        min(len(times), len(lows))
    ):

        t_s = int(
            times[i]
        )

        if t_s + 86400 > now_s:

            continue

        try:

            low = float(
                lows[i]
            )

        except Exception:

            continue

        if low <= 0:

            continue

        bars.append({
            "t": t_s * 1000,
            "l": low,
        })

    bars.sort(
        key=lambda b: b["t"]
    )

    return bars


def rolling_9d_low(
    sym: str
) -> Optional[float]:

    """
    Minimum low of the latest ROLL_DAYS
    closed daily bars.
    """

    bars = fetch_daily_bars(
        sym,
        ROLL_DAYS + 3
    )

    if len(bars) < ROLL_DAYS:

        log.error(
            f"[{sym}] only {len(bars)} "
            f"closed daily bars available, "
            f"need {ROLL_DAYS} "
            "— cannot compute 9d low"
        )

        return None

    window = bars[-ROLL_DAYS:]

    return min(
        b["l"]
        for b in window
    )


# ── startup test orders ───────────────────────────────────────────────────────
#
# No threads. Three flat phases across the whole symbol batch:
#   1. OPEN  — send a test limit LONG for every symbol, back to back
#   2. WAIT  — sleep once, for TEST_ORDER_WAIT_SEC, for the whole batch
#   3. CLOSE — check fill status and cancel/confirm for every symbol,
#              back to back
#
# Any symbol that fails at any phase (invalid mark, rejected order,
# cancel failure, or an exception) is flagged failed and excluded
# from the DCA engine for the rest of this process's lifetime. A
# symbol whose test order fills during the wait is NOT a failure —
# it's a real position, logged as such, and the symbol stays
# validated.

def _open_test_order(sym: str) -> Optional[Dict]:

    """
    Phase 1 for one symbol: fetch mark, place the test order.

    Returns a dict describing the pending test order on success, or
    None if the symbol was flagged failed during this phase.
    """

    if sym not in specs:

        # Already flagged failed in load_specs().
        return None

    try:

        mark = get_mark(sym)

        if mark <= 0:

            flag_failed(
                sym,
                f"invalid mark price ({mark}) at startup test"
            )

            return None

        test_price = mark * TEST_ORDER_DISCOUNT
        min_vol = _mos(sym)

        log.info(
            f"[{sym}] test order OPEN: "
            f"mark={mark:.4f} "
            f"limit={test_price:.4f} "
            f"(-{(1 - TEST_ORDER_DISCOUNT) * 100:.0f}%) "
            f"vol={min_vol} (exchange minimum)"
        )

        oid = place_long_min_size(
            sym,
            test_price
        )

        if oid is None:

            flag_failed(
                sym,
                "test order rejected by MEXC"
            )

            return None

        log.info(
            f"[{sym}] test order placed id={oid}"
        )

        return {
            "sym": sym,
            "oid": oid,
            "limit_price": test_price,
            "vol": min_vol,
        }

    except Exception as e:

        flag_failed(
            sym,
            f"exception during test order open: {e}"
        )

        log.error(
            f"[{sym}] test order open failed: {e}",
            exc_info=True
        )

        return None


def _close_test_order(pending: Dict):

    """
    Phase 3 for one symbol: check fill status, cancel if unfilled,
    flag failed if cancel fails.
    """

    sym = pending["sym"]
    oid = pending["oid"]

    try:

        if is_filled(sym, oid):

            log.warning(
                f"[{sym}] test order id={oid} "
                f"FILLED during the "
                f"{TEST_ORDER_WAIT_SEC}s wait. "
                "This is now a real open long "
                "position. Symbol remains validated."
            )

            record_order_only({
                "symbol": sym,
                "date": datetime.datetime.now(UTC).date().isoformat(),
                "order_id": oid,
                "kind": "startup_test_filled",
                "limit_price": pending["limit_price"],
                "vol": pending["vol"],
            })

            return

        cancelled = cancel_order(
            sym,
            oid
        )

        if cancelled:

            log.info(
                f"[{sym}] test order id={oid} "
                "cancelled successfully — symbol validated"
            )

        else:

            flag_failed(
                sym,
                f"test order id={oid} could not be cancelled"
            )

    except Exception as e:

        flag_failed(
            sym,
            f"exception during test order close: {e}"
        )

        log.error(
            f"[{sym}] test order close failed: {e}",
            exc_info=True
        )


def run_startup_test_orders():

    """
    Batch startup validation, three flat phases, no threads:

      1. Send an OPEN test order for every symbol, one after another.
      2. Sleep once for TEST_ORDER_WAIT_SEC, for the whole batch.
      3. Send a CLOSE (fill-check + cancel) for every symbol that
         successfully opened, one after another.

    Total wall-clock time is roughly TEST_ORDER_WAIT_SEC plus the
    time to fire off 2 x len(SYMBOLS) sequential requests — much
    less than the old one-symbol-at-a-time design.
    """

    log.info(
        f"══ startup test orders: {len(SYMBOLS)} symbols — "
        f"phase 1/3: opening ══"
    )

    pending = []

    for sym in SYMBOLS:

        result = _open_test_order(sym)

        if result is not None:

            pending.append(result)

    log.info(
        f"══ startup test orders: {len(pending)}/{len(SYMBOLS)} "
        f"opened — phase 2/3: waiting {TEST_ORDER_WAIT_SEC}s ══"
    )

    time.sleep(TEST_ORDER_WAIT_SEC)

    log.info(
        "══ startup test orders: phase 3/3: closing ══"
    )

    for p in pending:

        _close_test_order(p)

    ok = [s for s in SYMBOLS if not is_failed(s)]
    failed = [s for s in SYMBOLS if is_failed(s)]

    log.info(
        "══ startup test orders: all symbols done — "
        f"{len(ok)} ok, {len(failed)} failed "
        f"{failed if failed else ''} ══"
    )


# ── DCA trigger ──────────────────────────────────────────────────────────────

def run_daily_dca(
    now_utc: datetime.datetime
):

    """
    Fire each eligible, non-failed symbol independently.

    The order is:
        LONG
        limit price = 9-day low
        sizing price = 9-day low
        USD amount = symbol's own daily allocation

    Previous unfilled orders are never cancelled.
    """

    today = now_utc.date()

    for sym in SYMBOLS:

        if is_failed(sym):

            log.info(
                f"[{sym}] skipped — flagged failed "
                "at startup"
            )

            continue

        if not in_dca_window(
            sym,
            today
        ):

            continue

        if has_fired_today(
            sym,
            today
        ):

            log.info(
                f"[{sym}] DCA already fired "
                f"for {today.isoformat()} "
                "— skipping"
            )

            continue

        mark = get_mark(sym)

        if mark <= 0:

            log.error(
                f"[{sym}] DCA invalid mark "
                f"price ({mark}) "
                "— skipping today"
            )

            continue

        target = rolling_9d_low(sym)

        if target is None:

            log.error(
                f"[{sym}] DCA could not compute "
                "9d low — skipping today"
            )

            continue

        daily_usd = DCA_DAILY_USD[sym]

        log.info(
            f"[{sym}] DCA fire "
            f"{today.isoformat()}: "
            f"limit LONG ${daily_usd:.2f} "
            f"@ 9dLow={target:.4f} "
            f"(sized off 9dLow, "
            f"not mark={mark:.4f})"
        )

        oid = place_long(
            sym,
            target,
            target,
            daily_usd
        )

        if oid == "SKIP":

            log.warning(
                f"[{sym}] DCA fire skipped "
                "— below minimum contract size; "
                "NOT marked as fired"
            )

            continue

        if oid is None:

            log.error(
                f"[{sym}] DCA fire rejected "
                "by MEXC; NOT marked as fired"
            )

            continue

        mark_fired(
            sym,
            today,
            {
                "symbol": sym,
                "date": today.isoformat(),
                "order_id": oid,
                "limit_price": target,
                "sizing_price": target,
                "mark_at_fire": mark,
                "usd": daily_usd,
            }
        )


# ── SVG status ────────────────────────────────────────────────────────────────

def render_svg(
    marks: Dict[str, float],
    lows: Dict[str, Optional[float]],
    today: datetime.date
) -> str:

    W = 1100
    H = 60 + 30 * len(SYMBOLS)

    now_str = datetime.datetime.now(
        UTC
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    svg = [

        '<?xml version="1.0" encoding="UTF-8"?>',

        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {W} {H}" '
            f'width="100%" '
            f'style="max-width:{W}px;display:block">'
        ),

        f'<rect width="{W}" height="{H}" fill="#fafafa"/>',

        (
            f'<text x="20" y="24" '
            f'font-family="Courier New" '
            f'font-size="13" '
            f'fill="#333" '
            f'font-weight="bold">'
            f'MultiLongDCA-Bot — {len(SYMBOLS)} symbols — '
            f'own 9d-low pricing/sizing — {now_str}'
            f'</text>'
        ),
    ]

    y = 50

    for sym in SYMBOLS:

        if is_failed(sym):

            line = (
                f"{sym:<16} "
                "*** FAILED STARTUP TEST — "
                "EXCLUDED FROM TRADING ***"
            )

            svg.append(
                f'<text x="20" y="{y}" '
                f'font-family="Courier New" '
                f'font-size="11" '
                f'font-weight="bold" '
                f'fill="#cc0000">'
                f'{line}'
                f'</text>'
            )

            y += 30

            continue

        mark = marks.get(
            sym,
            0.0
        )

        low = lows.get(sym)

        low_str = (
            f"{low:,.4f}"
            if low is not None
            else "n/a"
        )

        start = DCA_START_DATE[sym]
        days = DCA_DAYS[sym]
        budget = DCA_BUDGET_USD[sym]
        daily = DCA_DAILY_USD[sym]

        end = (
            start
            + datetime.timedelta(
                days=days - 1
            )
        )

        n_fired = fired_count(sym)

        active = in_dca_window(
            sym,
            today
        )

        fired_today = has_fired_today(
            sym,
            today
        )

        remaining_usd = max(
            0.0,
            budget - n_fired * daily
        )

        if today < start:

            phase = (
                f"not started "
                f"(begins {start.isoformat()})"
            )

        elif today > end:

            phase = (
                f"window complete "
                f"({end.isoformat()})"
            )

        else:

            phase = (
                f"day {(today - start).days + 1}"
                f"/{days}"
            )

            if fired_today:

                phase += " — fired today"

            elif active:

                phase += " — pending today"

        clr = (
            "#1a8a1a"
            if fired_today
            else (
                "#1155cc"
                if active
                else "#888"
            )
        )

        line = (
            f"{sym:<16} "
            f"mark={mark:>12,.4f}  "
            f"9dLow={low_str:>12}   "
            f"fired={n_fired:>3}/{days}   "
            f"remaining=${remaining_usd:>8,.2f}   "
            f"{phase}"
        )

        svg.append(
            f'<text x="20" y="{y}" '
            f'font-family="Courier New" '
            f'font-size="11" '
            f'fill="{clr}">'
            f'{line}'
            f'</text>'
        )

        y += 30

    svg.append(
        "</svg>"
    )

    return "\n".join(svg)


# ── engine timing ─────────────────────────────────────────────────────────────

def _seconds_until_next_hour() -> float:

    now = time.time()

    return (
        (int(now) // 3600 + 1) * 3600
        + HOURLY_SLEEP_FLOOR_SEC
        - now
    )


# ── engine cycle ─────────────────────────────────────────────────────────────

def engine_cycle():

    now_utc = datetime.datetime.now(
        UTC
    )

    active_symbols = [
        sym for sym in SYMBOLS
        if not is_failed(sym)
    ]

    marks = {
        sym: get_mark(sym)
        for sym in active_symbols
    }

    lows = {
        sym: rolling_9d_low(sym)
        for sym in active_symbols
    }

    if now_utc.hour == 0:

        run_daily_dca(
            now_utc
        )

    svg = render_svg(
        marks,
        lows,
        now_utc.date()
    )

    STATE.set_svg(svg)

    n_fired_total = sum(
        len(v)
        for v in STATE_DATA["fired"].values()
    )

    n_failed = len(FAILED_SYMBOLS)

    STATE.set_status(
        f"ok  "
        f"{now_utc.strftime('%Y-%m-%d %H:%M UTC')}  "
        f"total_fires={n_fired_total}  "
        f"failed_symbols={n_failed}"
    )


# ── engine ────────────────────────────────────────────────────────────────────

def run_engine():

    load_specs()

    run_startup_test_orders()

    log.info(
        "engine starting — "
        "running initial cycle"
    )

    try:

        engine_cycle()

    except Exception as e:

        log.error(
            f"initial engine cycle failed: {e}",
            exc_info=True
        )

        STATE.set_status(
            f"error: {e}"
        )

    while True:

        wait_s = _seconds_until_next_hour()

        time.sleep(
            max(0, wait_s)
        )

        try:

            engine_cycle()

        except Exception as e:

            log.error(
                f"engine cycle failed: {e}",
                exc_info=True
            )

            STATE.set_status(
                f"error: {e}"
            )


# ── HTTP server ───────────────────────────────────────────────────────────────

class Handler(
    http.server.BaseHTTPRequestHandler
):

    def log_message(
        self,
        fmt,
        *args
    ):

        pass

    def do_GET(self):

        if self.path == "/chart.svg":

            svg = STATE.get_svg().encode(
                "utf-8"
            )

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "image/svg+xml"
            )

            self.send_header(
                "Content-Length",
                str(len(svg))
            )

            self.send_header(
                "Cache-Control",
                "no-cache"
            )

            self.end_headers()

            self.wfile.write(svg)

        elif self.path == "/orders.json":

            body = json.dumps(
                STATE_DATA["orders"],
                indent=2
            ).encode(
                "utf-8"
            )

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(body)

        elif self.path == "/failed.json":

            body = json.dumps(
                sorted(FAILED_SYMBOLS),
                indent=2
            ).encode(
                "utf-8"
            )

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(body)

        elif (
            self.path == "/"
            or self.path == ""
        ):

            status = STATE.get_status()

            html = (
                "<!doctype html>"
                "<html>"
                "<head>"
                "<meta charset='utf-8'>"
                "<meta http-equiv='refresh' content='300'>"
                "<title>MultiLongDCA-Bot Overview</title>"
                "<style>"
                "body{font-family:monospace;"
                "background:#fafafa;margin:24px}"
                "img{max-width:100%;height:auto;"
                "border:1px solid #ccc}"
                "</style>"
                "</head>"
                "<body>"
                "<h3>"
                "MultiLongDCA-Bot — "
                "Multi-Symbol DCA Long Bot"
                "</h3>"
                f"<p>status: {status}</p>"
                "<img src='/chart.svg' "
                "alt='overview table'/>"
                "<p>"
                "<a href='/orders.json'>"
                "order records (JSON)"
                "</a>"
                " · "
                "<a href='/failed.json'>"
                "failed symbols (JSON)"
                "</a>"
                "</p>"
                "</body>"
                "</html>"
            )

            body = html.encode(
                "utf-8"
            )

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(body)

        else:

            self.send_response(404)

            self.end_headers()


# ── HTTP server thread ────────────────────────────────────────────────────────

def run_server():

    server = (
        http.server.ThreadingHTTPServer(
            (HTTP_HOST, HTTP_PORT),
            Handler
        )
    )

    log.info(
        f"server listening on "
        f"{HTTP_HOST}:{HTTP_PORT}"
    )

    server.serve_forever()


# ── entrypoint ────────────────────────────────────────────────────────────────

def main():

    if not MEXC_KEY or not MEXC_SECRET:

        log.error(
            "MEXC / MEXCSECRET not set"
        )

        raise SystemExit(1)

    server_thread = threading.Thread(
        target=run_server,
        daemon=True
    )

    server_thread.start()

    run_engine()


if __name__ == "__main__":

    main()
