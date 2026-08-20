#!/usr/bin/env python3
"""
MultiLongDCA-Bot — Multi-Symbol Minute-Trigger DCA Long Bot.

Every symbol is priced and sized independently. Instead of firing
once a day at a fixed daily slice, this engine checks EVERY MINUTE
(at :01 past the minute) whether the most recently closed 1-minute
candle's low is a new rolling low over a reference window, and if
so stacks a $1 trigger toward that symbol's pending order.

Single-process, single-machine bot for Fly.io.

Symbols are configured in one place — SYMBOLS, near the top of this
file. To add or remove a symbol, only edit SYMBOLS.

═══════════════════════════════════════════════════════════════════
MINUTE-TRIGGER ENGINE
═══════════════════════════════════════════════════════════════════

Per-symbol running budget:
  - Starts at $0.
  - At every UTC midnight, a flat +$10 is added to the symbol's
    running budget (accrual, not a fixed pool).
  - Every time a real order is placed for a symbol, its USD amount
    is subtracted from that symbol's running budget. The budget can
    go negative.

Per-minute check (runs once per minute, at :01 past the minute, for
every non-failed symbol independently):
  1. Look at the most recently CLOSED 1-minute candle.
  2. Choose the reference window based on the symbol's CURRENT
     running budget at the moment of the check:
       - budget >= 0  -> reference is the rolling 2-day low
       - budget <  0  -> reference is the rolling 9-day low
     Both are computed from the trailing window of closed 1-minute
     candles (2 days = 2880 minutes, 9 days = 12960 minutes).
  3. If the closed candle's low is <= that reference low, it is a
     TRIGGER: add $1 to the symbol's pending accumulator.
  4. If the accumulator's contract-equivalent at the triggering
     candle's low price is >= the exchange's minimum order size, a
     real limit LONG is placed at exactly that low price for the
     full accumulated amount; the accumulator resets to 0 and the
     amount is subtracted from the running budget. Otherwise the
     accumulator carries forward untouched.

Rolling 1-minute OHLC candle buffer (per symbol):
  - Seeded ONCE at startup with ~10 days of 1-minute history.
  - Updated every minute by fetching only the most recently closed
    candle(s) and appending them; candles older than the buffer
    window are dropped.
  - Powers both the trigger-reference lows AND the 15m-resampled
    10-day chart (see CHARTS below).

Failed symbols (see startup test orders) are fully frozen: no
budget accrual, no candle checks, for the remainder of the process's
lifetime.

═══════════════════════════════════════════════════════════════════
CHARTS
═══════════════════════════════════════════════════════════════════

Each symbol gets its own SVG candlestick chart: the trailing 10 days
of 1-minute candles, resampled to 15-minute OHLC candles (~960
candles). The chart marks:
  - Whichever reference low is currently active (2d or 9d, based on
    that symbol's budget sign) as a dashed horizontal threshold line.
  - Every order placed for that symbol as a marker at its fire time
    and price.

Charts are re-rendered every minute, AFTER the minute-trigger
trading logic runs, so chart rendering never delays order placement.
Served at /chart/<SYMBOL>.svg and linked from the main overview page.

═══════════════════════════════════════════════════════════════════
DAILY ACTIVITY REPORT (ntfy)
═══════════════════════════════════════════════════════════════════

Once per UTC calendar day, at 14:00 UTC, a plain-text activity
report is pushed to the ntfy.sh topic
"1618091301200506091401140305" (https://ntfy.sh/<topic>), one line
per symbol, covering that UTC day's activity up to send time:
  - number of triggers
  - order value (sum of USD on successfully placed orders)
  - number of successful order placements
  - number of unsuccessful order placements (rejected or below
    minimum size)
  - average price across all attempted order placements
    (successful + unsuccessful)

Daily counters reset at UTC midnight (independent of, but aligned
with, the budget accrual reset). The report send is itself
idempotent per UTC date via a persisted "last report date" so a
restart near 14:00 UTC cannot double-send.

═══════════════════════════════════════════════════════════════════
STARTUP TEST ORDERS
═══════════════════════════════════════════════════════════════════

  - On startup: run a one-time TEST order (limit LONG at market-10%,
    sized at that symbol's own exchange-reported minimum order size)
    against EVERY symbol, in three flat batch phases (no threads):
      1. OPEN  — send a test limit LONG for every symbol.
      2. WAIT  — sleep once, for TEST_ORDER_WAIT_SEC seconds.
      3. CLOSE — check fill status and cancel/confirm for every
         symbol that opened.

    ANY failure at any phase flags that symbol FAILED for the
    remainder of this process's lifetime: fully excluded from the
    minute-trigger engine and shown flagged on the status page. A
    test order that fills during the wait is NOT a failure.

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
# remove a symbol, only edit SYMBOLS below.
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

MINUTE_CHECK_SECOND = 1              # run the check at :01 past each minute


# ── chart constants ────────────────────────────────────────────────────────────

CHART_MINUTES       = 10 * 24 * 60   # 10 days of 1-minute history for the chart
CHART_RESAMPLE_MIN  = 15             # resample 1m -> 15m OHLC candles

# Buffer must cover the longest of: 9d-low window, or the 10d chart
# window, plus a little slack for trimming.
BUFFER_MAX_MINUTES = max(ROLL_MINUTES_LONG, CHART_MINUTES) + 60

CHART_W = 1200
CHART_H = 420
CHART_MARGIN_L = 60
CHART_MARGIN_R = 20
CHART_MARGIN_T = 40
CHART_MARGIN_B = 40


# ── daily activity report / ntfy constants ────────────────────────────────────

NTFY_TOPIC     = "1618091301200506091401140305"
NTFY_URL       = f"https://ntfy.sh/{NTFY_TOPIC}"
REPORT_HOUR_UTC   = 14
REPORT_MINUTE_UTC = 0


# ── timing ────────────────────────────────────────────────────────────────────

HOURLY_SLEEP_FLOOR_SEC = 5


# ── startup test order ────────────────────────────────────────────────────────

TEST_ORDER_DISCOUNT = 0.90
TEST_ORDER_WAIT_SEC = 20


# ── failed-symbol tracking ────────────────────────────────────────────────────

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

        self._chart_svgs: Dict[str, str] = {}

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

    def set_chart_svg(self, sym: str, svg: str):
        with self._lock:
            self._chart_svgs[sym] = svg

    def get_chart_svg(self, sym: str) -> str:
        with self._lock:
            return self._chart_svgs.get(
                sym,
                "<svg xmlns='http://www.w3.org/2000/svg' width='400' "
                "height='60'><text x='10' y='30' "
                "font-family='Courier New'>Loading chart...</text></svg>"
            )


STATE = SharedState()


# ── persisted state ──────────────────────────────────────────────────────────
#
# {
#   "orders": [...],
#   "budget": {"USOIL_USDT": 3.50, ...},
#   "accumulator": {"USOIL_USDT": 0.0, ...},
#   "last_accrual_date": {"USOIL_USDT": "2026-08-20", ...},
#   "last_seen_minute": {"USOIL_USDT": "2026-08-20T14:07:00+00:00", ...},
#   "daily_stats": {
#       "USOIL_USDT": {
#           "date": "2026-08-20",
#           "triggers": 0,
#           "order_value_usd": 0.0,
#           "orders_ok": 0,
#           "orders_failed": 0,
#           "attempt_price_sum": 0.0,
#           "attempt_count": 0
#       },
#       ...
#   },
#   "last_report_date": "2026-08-19"
# }

def _default_daily_stats() -> Dict:
    return {
        "date": None,
        "triggers": 0,
        "order_value_usd": 0.0,
        "orders_ok": 0,
        "orders_failed": 0,
        "attempt_price_sum": 0.0,
        "attempt_count": 0,
    }


def _default_state() -> Dict:
    return {
        "orders": [],
        "budget": {},
        "accumulator": {},
        "last_accrual_date": {},
        "last_seen_minute": {},
        "daily_stats": {},
        "last_report_date": None,
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


# ── daily stats (activity report counters) ────────────────────────────────────

def _ensure_daily_stats_current(sym: str, today: datetime.date):

    """
    Resets a symbol's daily counters if the stored date doesn't
    match today (UTC). Must be called under _STATE_DATA_LOCK by
    callers, OR called standalone (it takes the lock itself) —
    see call sites below, all of which call it standalone.
    """

    with _STATE_DATA_LOCK:

        stats = STATE_DATA["daily_stats"].get(sym)

        if stats is None or stats.get("date") != today.isoformat():

            fresh = _default_daily_stats()
            fresh["date"] = today.isoformat()

            STATE_DATA["daily_stats"][sym] = fresh

            _persist()


def record_trigger_stat(sym: str, today: datetime.date):

    _ensure_daily_stats_current(sym, today)

    with _STATE_DATA_LOCK:

        STATE_DATA["daily_stats"][sym]["triggers"] += 1

        _persist()


def record_attempt_stat(
    sym: str,
    today: datetime.date,
    price: float,
    success: bool,
    usd_if_success: float = 0.0
):

    """
    Records one order-placement ATTEMPT (call to place_long),
    whether it succeeded or not, for the daily report.
    """

    _ensure_daily_stats_current(sym, today)

    with _STATE_DATA_LOCK:

        stats = STATE_DATA["daily_stats"][sym]

        stats["attempt_price_sum"] += price
        stats["attempt_count"] += 1

        if success:
            stats["orders_ok"] += 1
            stats["order_value_usd"] += usd_if_success
        else:
            stats["orders_failed"] += 1

        _persist()


def get_daily_stats_snapshot(sym: str, today: datetime.date) -> Dict:

    _ensure_daily_stats_current(sym, today)

    with _STATE_DATA_LOCK:

        return dict(STATE_DATA["daily_stats"].get(sym, _default_daily_stats()))


def get_last_report_date() -> Optional[datetime.date]:

    s = STATE_DATA.get("last_report_date")

    if not s:
        return None

    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None


def set_last_report_date(d: datetime.date):

    with _STATE_DATA_LOCK:

        STATE_DATA["last_report_date"] = d.isoformat()

        _persist()


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


# ── ntfy ──────────────────────────────────────────────────────────────────────

def ntfy_send(message: str, title: Optional[str] = None):

    """
    Pushes a plain-text message to the configured ntfy.sh topic via
    a simple HTTP POST. Best-effort — failures are logged, never
    raised, since a failed notification should not affect trading.
    """

    headers = {"Content-Type": "text/plain; charset=utf-8"}

    if title:
        headers["Title"] = title

    try:

        req = urllib.request.Request(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers=headers,
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=10) as r:

            r.read()

        log.info(f"ntfy: report sent to {NTFY_URL}")

    except Exception as e:

        log.error(f"ntfy: failed to send report: {e}")


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
    USD notional. Used for startup test orders.
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
    Fetch 1-minute OHLC candles for [start_s, end_s) (unix seconds).

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

    times  = d.get("time") or []
    opens  = d.get("realOpen")  or d.get("open")  or []
    highs  = d.get("realHigh")  or d.get("high")  or []
    lows   = d.get("realLow")   or d.get("low")   or []
    closes = d.get("realClose") or d.get("close") or []

    n = min(
        len(times), len(opens), len(highs), len(lows), len(closes)
    )

    bars = []

    for i in range(n):

        t_s = int(times[i])

        if t_s + 60 > now_s:
            continue

        try:
            o = float(opens[i])
            h = float(highs[i])
            l = float(lows[i])
            c = float(closes[i])
        except Exception:
            continue

        if l <= 0:
            continue

        bars.append({"t": t_s, "o": o, "h": h, "l": l, "c": c})

    bars.sort(key=lambda b: b["t"])

    return bars


# ── per-symbol rolling 1-minute OHLC buffer ───────────────────────────────────
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

    def snapshot(self) -> List[Dict]:

        with self.lock:
            return list(self.bars)

    def size(self) -> int:

        with self.lock:
            return len(self.bars)


MINUTE_BUFFERS: Dict[str, MinuteBuffer] = {
    sym: MinuteBuffer() for sym in SYMBOLS
}


def seed_minute_buffer(sym: str):

    """
    One-time startup seed of ~10 days of 1-minute history for sym.
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
    Per-minute incremental update: fetch only the last few minutes
    (covers the just-closed candle plus margin in case of any gap)
    and merge into the existing buffer.
    """

    now_s = int(time.time())

    start_s = now_s - 5 * 60

    bars = fetch_minute_bars(sym, start_s, now_s)

    if bars:
        MINUTE_BUFFERS[sym].append_new(bars)


# ── resampling for charts ─────────────────────────────────────────────────────

def resample_ohlc(bars: List[Dict], bucket_minutes: int) -> List[Dict]:

    """
    Aggregates consecutive 1-minute OHLC bars into fixed-size
    buckets of bucket_minutes each, aligned to UTC clock boundaries.
    """

    if not bars:
        return []

    bucket_s = bucket_minutes * 60

    buckets: Dict[int, List[Dict]] = {}

    for b in bars:

        bucket_start = (b["t"] // bucket_s) * bucket_s

        buckets.setdefault(bucket_start, []).append(b)

    out = []

    for bucket_start in sorted(buckets.keys()):

        group = sorted(buckets[bucket_start], key=lambda b: b["t"])

        out.append({
            "t": bucket_start,
            "o": group[0]["o"],
            "h": max(g["h"] for g in group),
            "l": min(g["l"] for g in group),
            "c": group[-1]["c"],
        })

    return out


# ── minute-trigger engine ─────────────────────────────────────────────────────

def process_symbol_minute_check(sym: str, now_utc: datetime.datetime):

    """
    The core per-minute logic for one symbol:

      1. Ensure today's daily budget accrual has happened.
      2. Refresh the symbol's rolling 1-minute candle buffer.
      3. Look at the latest closed candle. If already processed,
         skip.
      4. Pick reference window based on current running budget.
      5. If the candle's low <= reference low, it's a trigger:
         stack $1 into the accumulator, record trigger stat.
      6. If the accumulator's contract-equivalent at the candle's
         low >= exchange minimum, attempt a real order at that
         price for the full accumulated amount. Record the attempt
         (success or failure) for the daily report. On success,
         reset accumulator and subtract from running budget.
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

    record_trigger_stat(sym, today)

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
        f"size — attempting limit LONG @ {candle_low:.4f}"
    )

    oid = place_long(
        sym,
        candle_low,
        candle_low,
        pending
    )

    if oid == "SKIP" or oid is None:

        record_attempt_stat(
            sym, today, candle_low, success=False
        )

        if oid == "SKIP":

            log.warning(
                f"[{sym}] fire skipped by place_long despite "
                "passing pre-check — leaving accumulator intact"
            )

        else:

            log.error(
                f"[{sym}] minute-trigger order rejected by MEXC — "
                "leaving accumulator intact, will retry on next "
                "trigger"
            )

        return

    record_attempt_stat(
        sym, today, candle_low, success=True, usd_if_success=pending
    )

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


# ── daily activity report ─────────────────────────────────────────────────────

def build_daily_report_text(today: datetime.date) -> str:

    lines = [
        f"Daily Activity Report — {today.isoformat()} "
        f"(as of {REPORT_HOUR_UTC:02d}:{REPORT_MINUTE_UTC:02d} UTC)",
        "",
    ]

    for sym in SYMBOLS:

        if is_failed(sym):

            lines.append(f"{sym}: FAILED — excluded from trading")

            continue

        stats = get_daily_stats_snapshot(sym, today)

        triggers = stats["triggers"]
        order_value = stats["order_value_usd"]
        ok = stats["orders_ok"]
        failed = stats["orders_failed"]
        attempt_count = stats["attempt_count"]
        attempt_sum = stats["attempt_price_sum"]

        avg_price = (
            attempt_sum / attempt_count
            if attempt_count > 0
            else None
        )

        avg_price_str = (
            f"{avg_price:,.4f}" if avg_price is not None else "n/a"
        )

        lines.append(
            f"{sym}: triggers={triggers}  "
            f"order_value=${order_value:,.2f}  "
            f"ok={ok}  failed={failed}  "
            f"avg_attempt_price={avg_price_str}"
        )

    return "\n".join(lines)


def maybe_send_daily_report(now_utc: datetime.datetime):

    """
    Sends the daily activity report exactly once per UTC calendar
    date, at/after REPORT_HOUR_UTC:REPORT_MINUTE_UTC. Idempotent via
    a persisted last-report-date, so a restart near the report time
    cannot cause a duplicate send.
    """

    today = now_utc.date()

    at_or_after_report_time = (
        (now_utc.hour, now_utc.minute)
        >= (REPORT_HOUR_UTC, REPORT_MINUTE_UTC)
    )

    if not at_or_after_report_time:
        return

    if get_last_report_date() == today:
        return

    report_text = build_daily_report_text(today)

    log.info(f"sending daily activity report:\n{report_text}")

    ntfy_send(
        report_text,
        title=f"DCA Bot Daily Report {today.isoformat()}"
    )

    set_last_report_date(today)


# ── startup test orders ───────────────────────────────────────────────────────
#
# No threads. Three flat phases across the whole symbol batch:
#   1. OPEN  — send a test limit LONG for every symbol, back to back
#   2. WAIT  — sleep once, for TEST_ORDER_WAIT_SEC, for the whole batch
#   3. CLOSE — check fill status and cancel/confirm for every symbol,
#              back to back
#
# Any symbol that fails at any phase is flagged failed and excluded
# from the minute-trigger engine entirely. A symbol whose test order
# fills during the wait is NOT a failure.

def _open_test_order(sym: str) -> Optional[Dict]:

    if sym not in specs:

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


# ── main overview SVG ─────────────────────────────────────────────────────────

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


# ── per-symbol chart SVG ───────────────────────────────────────────────────────

def render_symbol_chart_svg(sym: str) -> str:

    buf = MINUTE_BUFFERS[sym]

    bars_1m = buf.snapshot()

    now_s = int(time.time())
    cutoff = now_s - CHART_MINUTES * 60

    bars_1m = [b for b in bars_1m if b["t"] >= cutoff]

    candles = resample_ohlc(bars_1m, CHART_RESAMPLE_MIN)

    if not candles:

        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{CHART_W}" height="{CHART_H}">'
            f'<rect width="{CHART_W}" height="{CHART_H}" fill="#fafafa"/>'
            f'<text x="20" y="40" font-family="Courier New" '
            f'font-size="14" fill="#888">'
            f'{sym}: no chart data yet</text></svg>'
        )

    budget = get_budget(sym)

    ref_window = ROLL_MINUTES_SHORT if budget >= 0 else ROLL_MINUTES_LONG
    ref_label = "2d" if budget >= 0 else "9d"
    ref_low = buf.rolling_low(ref_window)

    lo = min(c["l"] for c in candles)
    hi = max(c["h"] for c in candles)

    if ref_low is not None:
        lo = min(lo, ref_low)
        hi = max(hi, ref_low)

    span = (hi - lo) or 1.0

    lo -= span * 0.05
    hi += span * 0.05
    span = hi - lo

    plot_w = CHART_W - CHART_MARGIN_L - CHART_MARGIN_R
    plot_h = CHART_H - CHART_MARGIN_T - CHART_MARGIN_B

    t0 = candles[0]["t"]
    t1 = candles[-1]["t"] + CHART_RESAMPLE_MIN * 60
    t_span = (t1 - t0) or 1

    def x_of(t: int) -> float:
        return CHART_MARGIN_L + (t - t0) / t_span * plot_w

    def y_of(price: float) -> float:
        return CHART_MARGIN_T + (hi - price) / span * plot_h

    now_str = datetime.datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    svg = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {CHART_W} {CHART_H}" '
            f'width="100%" style="max-width:{CHART_W}px;display:block">'
        ),
        f'<rect width="{CHART_W}" height="{CHART_H}" fill="#fafafa"/>',
        (
            f'<text x="{CHART_MARGIN_L}" y="20" '
            f'font-family="Courier New" font-size="13" '
            f'fill="#333" font-weight="bold">'
            f'{sym} — 10d, 15m candles — {now_str}</text>'
        ),
    ]

    for i in range(6):

        price = lo + span * i / 5
        y = y_of(price)

        svg.append(
            f'<line x1="{CHART_MARGIN_L}" y1="{y:.1f}" '
            f'x2="{CHART_W - CHART_MARGIN_R}" y2="{y:.1f}" '
            f'stroke="#e0e0e0" stroke-width="1"/>'
        )

        svg.append(
            f'<text x="4" y="{y + 4:.1f}" '
            f'font-family="Courier New" font-size="9" '
            f'fill="#888">{price:,.3f}</text>'
        )

    if ref_low is not None:

        ry = y_of(ref_low)

        svg.append(
            f'<line x1="{CHART_MARGIN_L}" y1="{ry:.1f}" '
            f'x2="{CHART_W - CHART_MARGIN_R}" y2="{ry:.1f}" '
            f'stroke="#cc0000" stroke-width="1.2" '
            f'stroke-dasharray="6,3"/>'
        )

        svg.append(
            f'<text x="{CHART_W - CHART_MARGIN_R - 4}" y="{ry - 4:.1f}" '
            f'font-family="Courier New" font-size="10" '
            f'fill="#cc0000" text-anchor="end">'
            f'{ref_label} low threshold: {ref_low:,.4f}</text>'
        )

    candle_px_w = max(1.5, plot_w / len(candles) * 0.7)

    for c in candles:

        x = x_of(c["t"]) + (plot_w / len(candles)) / 2

        up = c["c"] >= c["o"]
        color = "#1a8a1a" if up else "#cc2200"

        y_high = y_of(c["h"])
        y_low = y_of(c["l"])

        y_open = y_of(c["o"])
        y_close = y_of(c["c"])

        body_top = min(y_open, y_close)
        body_h = max(1.0, abs(y_close - y_open))

        svg.append(
            f'<line x1="{x:.1f}" y1="{y_high:.1f}" '
            f'x2="{x:.1f}" y2="{y_low:.1f}" '
            f'stroke="{color}" stroke-width="1"/>'
        )

        svg.append(
            f'<rect x="{x - candle_px_w / 2:.1f}" y="{body_top:.1f}" '
            f'width="{candle_px_w:.1f}" height="{body_h:.1f}" '
            f'fill="{color}"/>'
        )

    orders = [
        o for o in STATE_DATA["orders"]
        if o.get("symbol") == sym
        and "limit_price" in o
        and ("candle_time" in o or "timestamp" in o)
    ]

    for o in orders:

        ts_str = o.get("candle_time") or o.get("timestamp")

        try:
            odt = datetime.datetime.fromisoformat(ts_str)
        except Exception:
            continue

        ot = int(odt.timestamp())

        if ot < t0 or ot > t1:
            continue

        ox = x_of(ot)
        oy = y_of(o["limit_price"])

        is_real_fire = "reference_window" in o

        marker_color = "#0044cc" if is_real_fire else "#888"

        svg.append(
            f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="4" '
            f'fill="{marker_color}" stroke="#fff" stroke-width="1"/>'
        )

    svg.append(
        f'<rect x="{CHART_MARGIN_L}" y="{CHART_MARGIN_T}" '
        f'width="{plot_w}" height="{plot_h}" '
        f'fill="none" stroke="#999" stroke-width="1"/>'
    )

    svg.append("</svg>")

    return "\n".join(svg)


# ── engine timing ─────────────────────────────────────────────────────────────

def _seconds_until_next_minute_mark() -> float:

    now = time.time()

    next_mark = (
        (int(now) // 60 + 1) * 60
        + MINUTE_CHECK_SECOND
    )

    return next_mark - now


# ── engine cycle ─────────────────────────────────────────────────────────────

def engine_cycle():

    now_utc = datetime.datetime.now(UTC)

    # Trading logic first — never delayed by chart rendering or
    # report sending.
    run_minute_checks(now_utc)

    svg = render_svg(now_utc)
    STATE.set_svg(svg)

    # Charts rendered AFTER trading logic, per symbol.
    for sym in SYMBOLS:

        if is_failed(sym):
            continue

        try:

            chart_svg = render_symbol_chart_svg(sym)
            STATE.set_chart_svg(sym, chart_svg)

        except Exception as e:

            log.error(
                f"[{sym}] chart render failed: {e}",
                exc_info=True
            )

    # Daily report check — also after trading logic, best-effort.
    try:

        maybe_send_daily_report(now_utc)

    except Exception as e:

        log.error(
            f"daily report check failed: {e}",
            exc_info=True
        )

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

        elif self.path.startswith("/chart/") and self.path.endswith(".svg"):

            sym = self.path[len("/chart/"):-len(".svg")]

            if sym not in SYMBOLS:

                self.send_response(404)
                self.end_headers()
                return

            svg = STATE.get_chart_svg(sym).encode("utf-8")

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

        elif self.path == "/stats.json":

            today = datetime.datetime.now(UTC).date()

            body = json.dumps(
                {
                    sym: get_daily_stats_snapshot(sym, today)
                    for sym in SYMBOLS
                    if not is_failed(sym)
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

            chart_links = " · ".join(
                f'<a href="/chart/{sym}.svg" target="_blank">{sym}</a>'
                for sym in SYMBOLS
            )

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
                f"<p>charts: {chart_links}</p>"
                "<p>"
                "<a href='/orders.json'>order records</a>"
                " · "
                "<a href='/budget.json'>budget/accumulator</a>"
                " · "
                "<a href='/stats.json'>today's stats</a>"
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
