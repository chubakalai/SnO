#!/usr/bin/env python3
"""
MultiLongDCA-Bot — Multi-Symbol Minute-Trigger DCA Long Bot.

Every symbol is priced and sized independently. Instead of firing
once a day at a fixed daily slice, this engine checks EVERY MINUTE
(at :01 past the minute) whether the most recently closed 1-minute
candle's low is a new rolling low over a reference window, and if
so stacks a $1 trigger toward that symbol's pending order.

Single-process, single-machine bot for Fly.io.

Symbols are configured in one place — SYMBOL_CONFIG, near the top
of this file. To add or remove a symbol, only edit SYMBOL_CONFIG.

═══════════════════════════════════════════════════════════════════
MINUTE-TRIGGER ENGINE (replaces the old fixed daily-budget DCA)
═══════════════════════════════════════════════════════════════════

Per-symbol running budget:
  - Starts at $0.
  - At every UTC midnight, a flat +$10 is added to the symbol's
    running budget (accrual, not a fixed pool — this replaces the
    old $1000 / 90-day budget entirely).
  - Every time a real order is placed for a symbol, its USD amount
    is subtracted from that symbol's running budget. The budget can
    go negative.

Per-minute check (runs once per minute, at :01 past the minute, for
every non-failed symbol independently):
  1. Look at the most recently CLOSED 1-minute candle (the one that
     just ended before this minute began — never the still-forming
     candle).
  2. Choose the reference window based on the symbol's CURRENT
     running budget at the moment of the check:
       - budget >= 0  -> reference is the rolling 2-day low
       - budget <  0  -> reference is the rolling 9-day low
     Both are computed from the trailing window of closed 1-minute
     candles (2 days = 2880 minutes, 9 days = 12960 minutes).
  3. If the closed candle's low is <= that reference low (i.e. it
     ties or sets a new low over the chosen window), it is a
     TRIGGER.
  4. On trigger: add $1 to the symbol's pending accumulator.
  5. If the accumulator's contract-equivalent at the triggering
     candle's low price is >= the exchange's minimum order size for
     that symbol, a real limit LONG is placed:
       - price = exactly the triggering candle's low (no discount)
       - USD amount = the full accumulated pending balance
     The accumulator resets to 0, and the placed USD amount is
     subtracted from the symbol's running budget.
     If the accumulator is still below the minimum order size, no
     order is placed and the accumulator carries forward untouched
     to the next trigger.

Rolling 1-minute candle buffer (per symbol):
  - Refetching 9 days of 1-minute candles (12,960 candles) every
    single minute, for every symbol, would be wasteful and could
    trip exchange rate limits. Instead each symbol keeps an
    in-memory rolling buffer of its own closed 1-minute candles:
      - Seeded ONCE at startup with a full ~9-day history fetch.
      - Updated every minute by fetching only the 1-2 most recently
        closed candles and appending them (candles older than 9
        days are dropped from the buffer).
  - The 2d-low and 9d-low are both computed as a plain min() over a
    suffix of this same buffer (last 2880 / last 12960 minutes) —
    no repeated bulk refetching.

Failed symbols (see startup test orders, below) are fully frozen:
they accrue no budget, and the minute-check does not run for them
at all, for the remainder of this process's lifetime.

All budget / accumulator / rolling-buffer state is persisted to the
local JSON state file, so a restart resumes correctly rather than
re-triggering everything (though the 1-minute buffer itself is
always re-seeded fresh from MEXC at startup, since it's a derived
cache, not authoritative history).

═══════════════════════════════════════════════════════════════════
STARTUP TEST ORDERS (unchanged in spirit from prior versions)
═══════════════════════════════════════════════════════════════════

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
    excluded from the minute-trigger engine entirely (no budget
    accrual, no candle checks) and shown in a visually distinct
    flagged state on the SVG status page. A symbol whose test order
    fills during the wait is NOT a failure — that's a real position,
    logged as such, and the symbol remains validated.

Environment (secrets only, not behavior):
  MEXC        - MEXC API key
  MEXCSECRET  - MEXC API secret

IMPORTANT:
  Contract specifications are fetched live from MEXC at startup.
  The bot does not hardcode priceUnit, volUnit, or contractSize.
"""

import collections
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
from typing import Deque, Dict, List, Optional

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
# Single source of truth for which symbols the bot trades. To add or
# remove a symbol, only edit SYMBOLS below — nothing else in the
# file needs to change.
#
# There is no per-symbol budget/window configuration anymore: every
# symbol starts its running budget at $0 and accrues +$10 at every
# UTC midnight (see BUDGET_DAILY_ACCRUAL_USD below). All symbols are
# fully independent from each other.
#
# USOIL_USDT     = WTI Crude Oil
# UKOIL_USDT     = Brent Crude Oil
# SPCXSTOCK_USDT = SPCX
# COPPER_USDT    = Copper
# SILVER_USDT    = Silver
# XAU_USDT       = Gold
# URNM_USDT      = Uranium

SYMBOLS: List[str] = [
    "USOIL_USDT",
    "UKOIL_USDT",
    "SPCXSTOCK_USDT",
    "COPPER_USDT",
    "SILVER_USDT",
    "XAU_USDT",
    "URNM_USDT",
]

LEVERAGE = 30


# ── minute-trigger engine constants ───────────────────────────────────────────

BUDGET_DAILY_ACCRUAL_USD = 10.0      # added to running budget at every UTC midnight
TRIGGER_STACK_USD        = 1.0       # added to accumulator per trigger

ROLL_MINUTES_SHORT = 2 * 24 * 60     # 2 days, in minutes -> "2d low"
ROLL_MINUTES_LONG  = 9 * 24 * 60     # 9 days, in minutes -> "9d low"

# Buffer keeps a little extra beyond the longest window so trimming
# has slack and we're never exactly on the edge of insufficient data.
BUFFER_MAX_MINUTES = ROLL_MINUTES_LONG + 60

MINUTE_CHECK_SECOND = 1              # run the check at :01 past each minute


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
# fully frozen — excluded from the minute-trigger engine (no budget
# accrual, no candle checks) — and rendering treats it as flagged,
# for the remainder of this process's lifetime. Resets on restart
# (the whole test suite reruns on every startup).

FAILED_SYMBOLS: set = set()
_FAILED_LOCK = threading.Lock()


def flag_failed(sym: str, reason: str):

    with _FAILED_LOCK:
        FAILED_SYMBOLS.add(sym)

    log.error(
        f"[{sym}] FLAGGED FAILED — {reason} — "
        "excluded from minute-trigger engine"
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
#
# {
#   "orders": [...],
#   "budget": {"USOIL_USDT": 3.50, ...},        running budget, USD
#   "accumulator": {"USOIL_USDT": 0.0, ...},     pending stacked $, USD
#   "last_accrual_date": {"USOIL_USDT": "2026-08-20", ...},
#   "last_seen_minute": {"USOIL_USDT": "2026-08-20T14:07:00+00:00", ...}
# }
#
# "last_seen_minute" prevents double-processing the same closed
# candle across restarts / repeated cycles.

def _default_state() -> Dict:
    return {
        "orders": [],
        "budget": {},
        "accumulator": {},
        "last_accrual_date": {},
        "last_seen_minute": {},
    }


def load_state() -> Dict:

    try:

        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("state file did not contain a dict")

        defaults = _default_state()

        for k, v in defaults.items():
            data.setdefault(k, v)

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


def get_budget(sym: str) -> float:

    return float(
        STATE_DATA["budget"].get(sym, 0.0)
    )


def get_accumulator(sym: str) -> float:

    return float(
        STATE_DATA["accumulator"].get(sym, 0.0)
    )


def get_last_accrual_date(sym: str) -> Optional[datetime.date]:

    s = STATE_DATA["last_accrual_date"].get(sym)

    if not s:
        return None

    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None


def get_last_seen_minute(sym: str) -> Optional[datetime.datetime]:

    s = STATE_DATA["last_seen_minute"].get(sym)

    if not s:
        return None

    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def _persist():

    save_state(STATE_DATA)


def accrue_daily_budget_if_due(sym: str, today: datetime.date):

    """
    Adds BUDGET_DAILY_ACCRUAL_USD to sym's running budget once per
    UTC calendar date. Safe to call every minute — only actually
    accrues the first time it's called on a new date.
    """

    with _STATE_DATA_LOCK:

        last = get_last_accrual_date(sym)

        if last == today:
            return

        prev_budget = get_budget(sym)

        new_budget = prev_budget + BUDGET_DAILY_ACCRUAL_USD

        STATE_DATA["budget"][sym] = new_budget

        STATE_DATA["last_accrual_date"][sym] = today.isoformat()

        _persist()

        log.info(
            f"[{sym}] daily budget accrual: "
            f"{prev_budget:.2f} + {BUDGET_DAILY_ACCRUAL_USD:.2f} "
            f"= {new_budget:.2f}"
        )


def add_trigger_dollar(sym: str) -> float:

    """
    Adds TRIGGER_STACK_USD to sym's accumulator and persists.
    Returns the new accumulator value.
    """

    with _STATE_DATA_LOCK:

        prev = get_accumulator(sym)

        new = prev + TRIGGER_STACK_USD

        STATE_DATA["accumulator"][sym] = new

        _persist()

        return new


def reset_accumulator(sym: str):

    with _STATE_DATA_LOCK:

        STATE_DATA["accumulator"][sym] = 0.0

        _persist()


def spend_budget(sym: str, usd: float):

    with _STATE_DATA_LOCK:

        prev = get_budget(sym)

        new = prev - usd

        STATE_DATA["budget"][sym] = new

        _persist()

        log.info(
            f"[{sym}] budget spent ${usd:.2f}: "
            f"{prev:.2f} -> {new:.2f}"
        )


def set_last_seen_minute(sym: str, minute_dt: datetime.datetime):

    with _STATE_DATA_LOCK:

        STATE_DATA["last_seen_minute"][sym] = minute_dt.isoformat()

        _persist()


def record_order(order_record: Dict):

    with _STATE_DATA_LOCK:

        STATE_DATA["orders"].append(order_record)

        _persist()


def total_orders_count() -> int:

    return len(STATE_DATA["orders"])


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


# ── 1-minute klines ───────────────────────────────────────────────────────────

def fetch_minute_bars(
    sym: str,
    start_s: int,
    end_s: int
) -> List[Dict]:

    """
    Fetch 1-minute candles for [start_s, end_s) (unix seconds).

    Only fully CLOSED candles are returned — any candle whose end
    time is after "now" is excluded.
    """

    now_s = int(time.time())

    url = (
        f"{MEXC_BASE}/api/v1/contract/kline/{sym}"
        f"?interval=Min1"
        f"&start={start_s}"
        f"&end={end_s}"
    )

    try:

        raw = _get(url)

    except Exception as e:

        log.error(
            f"[{sym}] minute kline fetch failed: {e}"
        )

        return []

    if not raw.get("success"):

        log.error(
            f"[{sym}] minute kline fetch "
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

        t_s = int(times[i])

        # Exclude any candle that hasn't fully closed yet.
        if t_s + 60 > now_s:
            continue

        try:
            low = float(lows[i])
        except Exception:
            continue

        if low <= 0:
            continue

        bars.append({
            "t": t_s,       # unix seconds, candle open time
            "l": low,
        })

    bars.sort(key=lambda b: b["t"])

    return bars


# ── per-symbol rolling 1-minute buffer ────────────────────────────────────────
#
# In-memory only (not persisted across restarts — re-seeded fresh
# from MEXC every startup, since it's a derived cache).

class MinuteBuffer:

    def __init__(self):
        self.bars: Deque[Dict] = collections.deque()
        self.lock = threading.Lock()

    def seed(self, bars: List[Dict]):

        with self.lock:
            self.bars = collections.deque(bars)
            self._trim_locked()

    def append_new(self, bars: List[Dict]):

        """
        Appends any bars not already present (by open time), keeping
        order, then trims to BUFFER_MAX_MINUTES.
        """

        with self.lock:

            existing_ts = {
                b["t"] for b in self.bars
            }

            for b in bars:

                if b["t"] not in existing_ts:

                    self.bars.append(b)
                    existing_ts.add(b["t"])

            self._sort_and_trim_locked()

    def _sort_and_trim_locked(self):

        self.bars = collections.deque(
            sorted(self.bars, key=lambda b: b["t"])
        )

        self._trim_locked()

    def _trim_locked(self):

        cutoff = int(time.time()) - BUFFER_MAX_MINUTES * 60

        while self.bars and self.bars[0]["t"] < cutoff:
            self.bars.popleft()

    def latest_closed(self) -> Optional[Dict]:

        with self.lock:

            if not self.bars:
                return None

            return self.bars[-1]

    def rolling_low(self, window_minutes: int) -> Optional[float]:

        with self.lock:

            if not self.bars:
                return None

            cutoff = int(time.time()) - window_minutes * 60

            window = [
                b["l"] for b in self.bars
                if b["t"] >= cutoff
            ]

            if not window:
                return None

            return min(window)

    def size(self) -> int:

        with self.lock:
            return len(self.bars)


MINUTE_BUFFERS: Dict[str, MinuteBuffer] = {
    sym: MinuteBuffer() for sym in SYMBOLS
}


def seed_minute_buffer(sym: str):

    """
    One-time startup seed of ~9 days of 1-minute history for sym.
    """

    now_s = int(time.time())

    start_s = now_s - BUFFER_MAX_MINUTES * 60

    bars = fetch_minute_bars(sym, start_s, now_s)

    MINUTE_BUFFERS[sym].seed(bars)

    log.info(
        f"[{sym}] minute buffer seeded: "
        f"{MINUTE_BUFFERS[sym].size()} bars"
    )


def refresh_minute_buffer(sym: str):

    """
    Per-minute incremental update: fetch only the last couple of
    minutes (covers the just-closed candle plus one margin candle
    in case of any gap) and merge into the existing buffer.
    """

    now_s = int(time.time())

    start_s = now_s - 5 * 60   # small overlap window for safety

    bars = fetch_minute_bars(sym, start_s, now_s)

    if bars:
        MINUTE_BUFFERS[sym].append_new(bars)


# ── minute-trigger engine ─────────────────────────────────────────────────────

def process_symbol_minute_check(sym: str, now_utc: datetime.datetime):

    """
    The core per-minute logic for one symbol:

      1. Ensure today's daily budget accrual has happened.
      2. Refresh the symbol's rolling 1-minute candle buffer.
      3. Look at the latest closed candle. If already processed
         (same open time as last_seen_minute), skip.
      4. Pick reference window based on current running budget.
      5. If the candle's low <= reference low, it's a trigger:
         stack $1 into the accumulator.
      6. If the accumulator's contract-equivalent at the candle's
         low >= exchange minimum, fire a real order at that price
         for the full accumulated amount, reset accumulator, and
         subtract the fired USD from the running budget.
    """

    if is_failed(sym):
        return

    today = now_utc.date()

    accrue_daily_budget_if_due(sym, today)

    refresh_minute_buffer(sym)

    buf = MINUTE_BUFFERS[sym]

    latest = buf.latest_closed()

    if latest is None:

        log.warning(
            f"[{sym}] no closed 1-minute candle available yet "
            "— skipping this minute"
        )

        return

    candle_dt = datetime.datetime.fromtimestamp(
        latest["t"], tz=UTC
    )

    last_seen = get_last_seen_minute(sym)

    if last_seen is not None and candle_dt <= last_seen:

        # Already processed this (or an older) candle — nothing new.
        return

    set_last_seen_minute(sym, candle_dt)

    candle_low = latest["l"]

    budget = get_budget(sym)

    if budget >= 0:

        ref_window = ROLL_MINUTES_SHORT
        ref_label = "2d"

    else:

        ref_window = ROLL_MINUTES_LONG
        ref_label = "9d"

    ref_low = buf.rolling_low(ref_window)

    if ref_low is None:

        log.warning(
            f"[{sym}] insufficient buffer data to compute "
            f"{ref_label} low — skipping this minute"
        )

        return

    triggered = candle_low <= ref_low

    log.info(
        f"[{sym}] minute check {candle_dt.isoformat()}: "
        f"low={candle_low:.4f} "
        f"{ref_label}Low={ref_low:.4f} "
        f"budget={budget:.2f} "
        f"trigger={triggered}"
    )

    if not triggered:
        return

    pending = add_trigger_dollar(sym)

    log.info(
        f"[{sym}] TRIGGER — accumulator now ${pending:.2f} "
        f"@ price={candle_low:.4f}"
    )

    vol_at_price = _contracts(sym, pending, candle_low)

    if vol_at_price < _mos(sym):

        log.info(
            f"[{sym}] accumulator ${pending:.2f} still below "
            f"min order size ({_mos(sym)} contracts @ "
            f"{candle_low:.4f}) — stacking, no order placed"
        )

        return

    log.info(
        f"[{sym}] accumulator ${pending:.2f} reaches min order "
        f"size — firing limit LONG @ {candle_low:.4f}"
    )

    oid = place_long(
        sym,
        candle_low,
        candle_low,
        pending
    )

    if oid == "SKIP":

        # Shouldn't normally happen since we just checked, but
        # formatting/rounding could still push it under — leave
        # the accumulator untouched and try again next trigger.
        log.warning(
            f"[{sym}] fire skipped by place_long despite passing "
            "pre-check — leaving accumulator intact"
        )

        return

    if oid is None:

        log.error(
            f"[{sym}] minute-trigger order rejected by MEXC — "
            "leaving accumulator intact, will retry on next trigger"
        )

        return

    reset_accumulator(sym)

    spend_budget(sym, pending)

    record_order({
        "symbol": sym,
        "timestamp": now_utc.isoformat(),
        "candle_time": candle_dt.isoformat(),
        "order_id": oid,
        "limit_price": candle_low,
        "usd": pending,
        "reference_window": ref_label,
    })


def run_minute_checks(now_utc: datetime.datetime):

    for sym in SYMBOLS:

        try:

            process_symbol_minute_check(sym, now_utc)

        except Exception as e:

            log.error(
                f"[{sym}] minute check failed: {e}",
                exc_info=True
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
# from the minute-trigger engine entirely. A symbol whose test order
# fills during the wait is NOT a failure — it's a real position,
# logged as such, and the symbol stays validated.

def _open_test_order(sym: str) -> Optional[Dict]:

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

            record_order({
                "symbol": sym,
                "timestamp": datetime.datetime.now(UTC).isoformat(),
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


# ── SVG status ────────────────────────────────────────────────────────────────

def render_svg(now_utc: datetime.datetime) -> str:

    W = 1200
    H = 60 + 30 * len(SYMBOLS)

    now_str = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

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
            f'minute-trigger engine — {now_str}'
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

        budget = get_budget(sym)
        accum = get_accumulator(sym)

        buf = MINUTE_BUFFERS[sym]

        low2d = buf.rolling_low(ROLL_MINUTES_SHORT)
        low9d = buf.rolling_low(ROLL_MINUTES_LONG)

        low2d_str = f"{low2d:,.4f}" if low2d is not None else "n/a"
        low9d_str = f"{low9d:,.4f}" if low9d is not None else "n/a"

        ref = "2d" if budget >= 0 else "9d"

        n_orders = sum(
            1 for o in STATE_DATA["orders"]
            if o.get("symbol") == sym and "reference_window" in o
        )

        clr = "#1155cc" if budget >= 0 else "#cc7a00"

        line = (
            f"{sym:<16} "
            f"budget=${budget:>8,.2f}  "
            f"accum=${accum:>5,.2f}  "
            f"ref={ref}  "
            f"2dLow={low2d_str:>12}  "
            f"9dLow={low9d_str:>12}  "
            f"fires={n_orders:>4}  "
            f"buf={buf.size():>6}m"
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

    svg.append("</svg>")

    return "\n".join(svg)


# ── engine timing ─────────────────────────────────────────────────────────────

def _seconds_until_next_minute_mark() -> float:

    """
    Seconds until the next MINUTE_CHECK_SECOND-past-the-minute mark.
    """

    now = time.time()

    next_mark = (
        (int(now) // 60 + 1) * 60
        + MINUTE_CHECK_SECOND
    )

    return next_mark - now


# ── engine cycle ─────────────────────────────────────────────────────────────

def engine_cycle():

    now_utc = datetime.datetime.now(UTC)

    run_minute_checks(now_utc)

    svg = render_svg(now_utc)

    STATE.set_svg(svg)

    n_orders = total_orders_count()
    n_failed = len(FAILED_SYMBOLS)

    STATE.set_status(
        f"ok  "
        f"{now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}  "
        f"total_orders={n_orders}  "
        f"failed_symbols={n_failed}"
    )


# ── engine ────────────────────────────────────────────────────────────────────

def run_engine():

    load_specs()

    run_startup_test_orders()

    log.info(
        "seeding 1-minute candle buffers "
        f"(~{BUFFER_MAX_MINUTES} minutes each)"
    )

    for sym in SYMBOLS:

        if is_failed(sym):
            continue

        try:

            seed_minute_buffer(sym)

        except Exception as e:

            log.error(
                f"[{sym}] failed to seed minute buffer: {e}",
                exc_info=True
            )

    log.info(
        "engine starting — running initial cycle"
    )

    try:

        engine_cycle()

    except Exception as e:

        log.error(
            f"initial engine cycle failed: {e}",
            exc_info=True
        )

        STATE.set_status(f"error: {e}")

    while True:

        wait_s = _seconds_until_next_minute_mark()

        time.sleep(max(0, wait_s))

        try:

            engine_cycle()

        except Exception as e:

            log.error(
                f"engine cycle failed: {e}",
                exc_info=True
            )

            STATE.set_status(f"error: {e}")


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

            svg = STATE.get_svg().encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(svg)

        elif self.path == "/orders.json":

            body = json.dumps(
                STATE_DATA["orders"], indent=2
            ).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/failed.json":

            body = json.dumps(
                sorted(FAILED_SYMBOLS), indent=2
            ).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/budget.json":

            body = json.dumps(
                {
                    "budget": STATE_DATA["budget"],
                    "accumulator": STATE_DATA["accumulator"],
                },
                indent=2
            ).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
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
                "<meta http-equiv='refresh' content='60'>"
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
                "Multi-Symbol Minute-Trigger DCA Long Bot"
                "</h3>"
                f"<p>status: {status}</p>"
                "<img src='/chart.svg' "
                "alt='overview table'/>"
                "<p>"
                "<a href='/orders.json'>order records</a>"
                " · "
                "<a href='/budget.json'>budget/accumulator</a>"
                " · "
                "<a href='/failed.json'>failed symbols</a>"
                "</p>"
                "</body>"
                "</html>"
            )

            body = html.encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type", "text/html; charset=utf-8"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:

            self.send_response(404)
            self.end_headers()


# ── HTTP server thread ────────────────────────────────────────────────────────

def run_server():

    server = http.server.ThreadingHTTPServer(
        (HTTP_HOST, HTTP_PORT),
        Handler
    )

    log.info(
        f"server listening on {HTTP_HOST}:{HTTP_PORT}"
    )

    server.serve_forever()


# ── entrypoint ────────────────────────────────────────────────────────────────

def main():

    if not MEXC_KEY or not MEXC_SECRET:

        log.error("MEXC / MEXCSECRET not set")

        raise SystemExit(1)

    server_thread = threading.Thread(
        target=run_server,
        daemon=True
    )

    server_thread.start()

    run_engine()


if __name__ == "__main__":

    main()
