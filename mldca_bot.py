#!/usr/bin/env python3
"""
MultiLongDCA-Bot — Multi-Symbol Minute-Trigger DCA Long Bot.

Every symbol is priced and sized independently. Instead of firing
once a day at a fixed daily slice, this engine checks EVERY MINUTE
(at :01 past the minute) whether the most recently closed 1-minute
candle's low is a new rolling low over a reference window, and if
so stacks a per-symbol trigger contribution toward that symbol's
pending order.

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
EVERY symbol, including FAILED ones — see FAILED SYMBOLS below):
  1. Look at the most recently CLOSED 1-minute candle.
  2. Choose the reference window based on the symbol's CURRENT
     running budget at the moment of the check:
       - budget >= 0  -> reference is the rolling 2-day low
       - budget <  0  -> reference is the rolling 9-day low
     Both are computed from the trailing window of closed 1-minute
     candles (2 days = 2880 minutes, 9 days = 12960 minutes).
  3. If the closed candle's low is <= that reference low, it is a
     TRIGGER.
  4. For NON-FAILED symbols only: a trigger adds that symbol's
     CURRENT per-trigger contribution (see CONTRIBUTION WEIGHTING
     below) to the pending accumulator, and if the accumulator's
     contract-equivalent at the triggering candle's low is >= the
     exchange's minimum order size, a real limit LONG is placed at
     exactly that low price for the full accumulated amount; the
     accumulator resets to 0 and the amount is subtracted from the
     running budget.
  5. For FAILED symbols: the trigger is evaluated and recorded (so
     it can be marked on the chart) but NO budget accrual, NO
     accumulator, and NO order is ever attempted — see FAILED
     SYMBOLS below.

Rolling 1-minute OHLC candle buffer (per symbol):
  - Seeded ONCE at startup with ~10 days of 1-minute history, for
    EVERY symbol including failed ones (so their charts have data).
  - Updated every minute by fetching only the most recently closed
    candle(s) and appending them, for every symbol regardless of
    failed status.
  - Powers both the trigger-reference lows AND the 15m-resampled
    10-day chart (see CHARTS below).

═══════════════════════════════════════════════════════════════════
CONTRIBUTION WEIGHTING (per-symbol $ per trigger)
═══════════════════════════════════════════════════════════════════

Instead of a flat $1.00 added to the accumulator on every trigger
for every symbol equally, each symbol's per-trigger contribution is
scaled by a daily-recomputed allocation weight, derived from a
30-day trailing correlation / volatility analysis across all traded
symbols:

  1. For each symbol, fetch 30 days of daily closes and compute
     daily returns.
  2. DATE-ALIGN all symbols' daily-close series onto the intersection
     of UTC calendar dates present across every symbol, sorted
     chronologically, BEFORE computing returns — so that return
     series are compared date-for-date rather than index-for-index.
     (See DATE ALIGNMENT below.)
  3. Build the full pairwise Pearson correlation matrix across all
     symbols' (now date-aligned) daily returns.
  4. For each symbol, compute its "relative portfolio correlation"
     as the column average of the correlation matrix (including the
     1.0 self-correlation term).
  5. Compute annualized volatility from the daily return series.
  6. Compute parameter z = (1 - avg_correlation_capped) *
     (1 - ann_vol_capped), where avg_correlation_capped and
     ann_vol_capped are each clamped to the range [-1.0, 1.0] and
     [0.0, 1.0] respectively before use — see VOLATILITY / CORRELATION
     CAPPING below. Symbols that are less correlated with the rest
     of the book and less volatile score a higher z.
  7. Normalize z across all symbols into a weight w_x (sums to 1).
  8. Convert to a per-trigger USD contribution:
       contribution = BASE_TRIGGER_USD * num_symbols * w_x
     Under equal weighting this reduces to exactly BASE_TRIGGER_USD
     ($1.00) per symbol, matching prior flat-$1 behavior. A symbol
     weighted above the equal-weight baseline contributes more than
     $1.00 per trigger; a symbol weighted below contributes less.

This is recomputed ONCE PER UTC CALENDAR DAY (piggybacking on the
same daily cadence as the budget accrual) and cached in persisted
state. Every minute-trigger check within that day uses the cached
per-symbol value — the 30-day-history network fetch is never done
inside the per-minute hot path.

If the recompute fails outright (e.g. the exchange's daily-kline
endpoint is unreachable), EVERY symbol falls back to flat
BASE_TRIGGER_USD ($1.00) for that day, and recompute is retried at
the next daily cycle. This is a pure fallback, not a cached carry-
forward — a failed recompute does not reuse yesterday's values.

═══════════════════════════════════════════════════════════════════
VOLATILITY / CORRELATION CAPPING (z-score stability)
═══════════════════════════════════════════════════════════════════

The z-score formula z = (1 - avg_correlation) * (1 - ann_vol) is
only well-behaved — i.e. guaranteed to stay non-negative — when both
avg_correlation and ann_vol stay within [-1, 1] and [0, 1]
respectively. Annualized volatility, in particular, is UNBOUNDED
ABOVE: for a genuinely volatile instrument (crypto majors, single
stocks, or any symbol going through a turbulent 30-day window),
ann_vol can readily exceed 1.0 (100%). Left uncapped, the term
(1 - ann_vol) goes negative, which can drive that symbol's z-score
negative, which in turn:
  - produces a negative or distorted weight w_x, and therefore a
    negative or distorted per-trigger USD contribution for that
    symbol, potentially causing add_trigger_dollar to shrink rather
    than grow the accumulator on a trigger, and
  - can drag total_z toward zero or negative across the whole book,
    silently forcing every symbol back to equal-weighting for that
    cycle (masking the underlying issue rather than resolving it).

To prevent this, BOTH inputs to the z-score are clamped immediately
before use, every recompute cycle:
  - ann_vol is clamped to [0.0, CONTRIB_VOL_CAP]      (CONTRIB_VOL_CAP = 1.0)
  - avg_cor is clamped to [-CONTRIB_CORR_CAP, CONTRIB_CORR_CAP]
                                                       (CONTRIB_CORR_CAP = 1.0)
This guarantees (1 - ann_vol_capped) in [0, 1] and
(1 - avg_cor_capped) in [0, 2], so z_scores[sym] is always >= 0,
total_z is always >= 0, and no symbol can receive a negative
per-trigger contribution from this mechanism. A symbol whose raw
volatility is clamped is logged so the event is visible; the clamp
does not change the underlying raw volatility figure used elsewhere
(e.g. in log output), only the number fed into the z-score.

═══════════════════════════════════════════════════════════════════
DATE ALIGNMENT (contribution-weighting inputs)
═══════════════════════════════════════════════════════════════════

Per-symbol daily closes are fetched independently, and different
instruments (equities-style proxies vs. commodities vs. crypto) can
have gapped, missing, or differently-timestamped daily candles. To
avoid comparing returns at the same ARRAY INDEX but different
CALENDAR DATES (which silently corrupts the correlation matrix),
each symbol's raw {timestamp: close} map is first converted to a
{UTC date: close} map (collapsing any intraday timestamp noise onto
the calendar date). The set of common dates present for EVERY
symbol (the intersection) is then computed, sorted chronologically,
and every symbol's return series is derived from that same ordered
date list. If the resulting intersection is too small to compute a
meaningful return series (fewer than 2 common dates), that symbol
contributes an empty return series for this cycle, exactly as if
its fetch had failed, and its z-score is computed from n=0 returns
(daily/annualized volatility 0.0, correlation 0.0 vs. all peers).

═══════════════════════════════════════════════════════════════════
FAILED SYMBOLS
═══════════════════════════════════════════════════════════════════

A symbol that fails its startup test order is flagged FAILED for
the remainder of the process's lifetime. This excludes it from
TRADING ONLY:
  - No budget accrual.
  - No accumulator, no order attempts, ever.

It does NOT exclude the symbol from CHARTING:
  - Its 1-minute buffer keeps refreshing every minute, same as any
    other symbol.
  - Its chart keeps rendering every minute, same as any other
    symbol, and is linked from the main overview page exactly like
    a healthy symbol.
  - Trigger conditions are still evaluated every minute purely for
    charting purposes (an "X marks the spot where this WOULD have
    triggered" reference) and shown as small trigger markers on the
    chart. Because failed symbols never call place_long, they never
    have order markers — the chart legend distinguishes trigger
    markers (small tick marks) from order markers (circles), and a
    failed symbol's chart will only ever show the former.

═══════════════════════════════════════════════════════════════════
CHARTS
═══════════════════════════════════════════════════════════════════

Each symbol — failed or not — gets its own SVG candlestick chart:
the trailing 10 days of 1-minute candles, resampled to 15-minute
OHLC candles (~960 candles). The chart marks:
  - Whichever reference low is currently active (2d or 9d, based on
    that symbol's budget sign) as a dashed horizontal threshold
    line. For failed symbols (budget frozen at 0 or whatever it was
    at time of failure), this still reflects budget sign the same
    way.
  - Every TRIGGER (candle low <= reference low) as a small tick
    marker on the price axis at that candle's time/price.
  - Every REAL ORDER placed for that symbol (never happens for
    failed symbols) as a filled circle marker at its fire
    time/price.

Charts are re-rendered every minute, AFTER the minute-trigger
trading logic runs, so chart rendering never delays order placement.
Served at /chart/<SYMBOL>.svg and linked from the main overview page
for every symbol, failed or not.

═══════════════════════════════════════════════════════════════════
DAILY ACTIVITY REPORT (ntfy)
═══════════════════════════════════════════════════════════════════

Once per UTC calendar day, at or after 14:00 UTC, a plain-text
activity report is pushed to the ntfy.sh topic
"1618091301200506091401140305" (https://ntfy.sh/<topic>), one line
per symbol, covering activity SINCE THE LAST SUCCESSFULLY SENT
REPORT (not since UTC midnight):
  - number of triggers
  - order value (sum of USD on successfully placed orders)
  - number of successful order placements
  - number of unsuccessful order placements (rejected or below
    minimum size)
  - average price across all attempted order placements
  - current per-trigger contribution in USD (as of report time)

Per-symbol daily counters are NOT reset at UTC midnight. They reset
ONLY immediately after a report has been successfully sent, so the
reporting window is always "since the last report" — under normal
operation this is ~24h (14:00 UTC to 14:00 UTC the next day), with
no gap and no double-counted period. A restart near 14:00 UTC cannot
cause a duplicate send: the guard requires both (a) current time is
at/after 14:00 UTC, AND (b) at least 20 hours have passed since the
last successful send.

Failed symbols are still counted in the report (their trigger count
will be nonzero if price action crossed their reference low; their
order counts will always be 0/0 since no orders are ever attempted)
so the report reflects that they're being watched but not traded.

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
    remainder of this process's lifetime — see FAILED SYMBOLS above
    for exactly what that does and doesn't exclude. A test order
    that fills during the wait is NOT a failure.

═══════════════════════════════════════════════════════════════════
LEVERAGE
═══════════════════════════════════════════════════════════════════

A single global LEVERAGE constant expresses the desired leverage
for every symbol. However, each MEXC contract independently caps
the maximum leverage it will accept (reported as "maxLeverage" in
the contract-detail response fetched at startup). At every order
placement (startup test orders AND minute-trigger real orders),
the effective leverage submitted to the exchange is:

    effective_leverage = min(LEVERAGE, symbol's maxLeverage)

If the exchange does not report a maxLeverage for a given contract,
the global LEVERAGE is used unmodified for that symbol as a safe
fallback (no artificial cap is invented).

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
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.sax.saxutils as _saxutils
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

SYMBOLS: List[str] = [
    # Existing
    "USOIL_USDT",
    "URNM_USDT",
    "BTC_USDT",
    "ETH_USDT",
    "SOL_USDT",
    "XRP_USDT",
    "INDA_USDT",
    "NGAS_USDT",
    "XPD_USDT",
    "XAU_USDT",
    "MSTRSTOCK_USDT",
    "UNITREE_USDT",

    # Broad equity indices / ETFs
    "SPY_USDT",
    "SPX500_USDT",
    "IWM_USDT",
    "US30_USDT",
    "NAS100_USDT",
    "QQQSTOCK_USDT",
    "JP225_USDT",
    "KOSPI_USDT",
    "HK50_USDT",
    "EWY_USDT",
    "EWJ_USDT",
    "EWZ_USDT",
    "EWT_USDT",

    # China / Hong Kong
    "TENCENTSTOCK_USDT",
    "HK0700_USDT",
    "BABASTOCK_USDT",
    "PDDSTOCK_USDT",
    "BIDUSTOCK_USDT",
    "MEITUANSTOCK_USDT",
    "KUAISHOUSTOCK_USDT",
    "XIAOMISTOCK_USDT",
    "NIOSTOCK_USDT",
    "HK1810_USDT",
    "POPMARTSTOCK_USDT",
    "NEO_USDT",

    # Precious metals / commodities
    "XAUT_USDT",
    "PAXG_USDT",
    "SILVER_USDT",
    "XPT_USDT",
    "UKOIL_USDT",
    "USO_USDT",
    "COPPER_USDT",
    "ALUMINUM_USDT",
    "NICKEL_USDT",
    "ZINC_USDT",

    # Sector ETFs
    "XLE_USDT",
    "XLI_USDT",
    "XLK_USDT",
    "XLV_USDT",
    "XLU_USDT",
    "SOXX_USDT",
    "SMH_USDT",
    "IBB_USDT",
    "XBI_USDT",
    "VGT_USDT",
    "VUG_USDT",
    "ARKK_USDT",
    "GDX_USDT",

    # US financials
    "JPMSTOCK_USDT",
    "BACSTOCK_USDT",
    "GSSTOCK_USDT",
    "BLKSTOCK_USDT",
    "IBKRSTOCK_USDT",
    "COFSTOCK_USDT",
    "AXPSTOCK_USDT",
    "SOFISTOCK_USDT",

    # US healthcare
    "LLYSTOCK_USDT",
    "UNHSTOCK_USDT",
    "AMGNSTOCK_USDT",
    "BIIBSTOCK_USDT",
    "VRTXSTOCK_USDT",
    "GILDSTOCK_USDT",
    "ISRGSTOCK_USDT",

    # US consumer
    "WMTSTOCK_USDT",
    "COSTSTOCK_USDT",
    "MCDSTOCK_USDT",
    "KHCSTOCK_USDT",
    "SBUXSTOCK_USDT",

    # US industrials / aerospace
    "CATSTOCK_USDT",
    "GEVSTOCK_USDT",
    "LMTSTOCK_USDT",
    "RTXSTOCK_USDT",
    "GEOD_USDT",
    "LHXSTOCK_USDT",

    # US energy
    "XOMSTOCK_USDT",
    "CVXSTOCK_USDT",
    "COPSTOCK_USDT",
    "SLBSTOCK_USDT",
    "VLOSTOCK_USDT",
    "MPCSTOCK_USDT",
    "BKRSTOCK_USDT",
    "OXYSTOCK_USDT",

    # US technology
    "AAPLSTOCK_USDT",
    "MSFTSTOCK_USDT",
    "GOOGLSTOCK_USDT",
    "AMZNSTOCK_USDT",
    "NVIDIA_USDT",
    "AMDSTOCK_USDT",
    "INTCSTOCK_USDT",
    "ORCLSTOCK_USDT",
    "CRMSTOCK_USDT",
    "PLTRSTOCK_USDT",
    "DELLSTOCK_USDT",

    # Semiconductors / AI infrastructure
    "TSMSTOCK_USDT",
    "ASMLSTOCK_USDT",
    "MRVLSTOCK_USDT",
    "AMATSTOCK_USDT",
    "LRCXSTOCK_USDT",
    "KLACSTOCK_USDT",
    "TERSTOCK_USDT",
    "ARMSTOCK_USDT",
    "MUSTOCK_USDT",
    "SKHYNIXSTOCK_USDT",
    "SAMSUNGSTOCK_USDT",
    "SAMSUNGEMSTOCK_USDT",
    "QCOMSTOCK_USDT",
    "CDNSSTOCK_USDT",
    "SNPSSTOCK_USDT",
    "CIENSTOCK_USDT",
    "ANETSTOCK_USDT",

    # Korea
    "SKHYNIXSTOCK_USDC",
    "HYUNDAISTOCK_USDT",
    "HANMISTOCK_USDT",
    "LGELECTRONICS_USDT",
    "NAVERSTOCK_USDT",

    # Japan
    "JPY_USDT",
    "SONYSTOCK_USDT",
    "TOKYOELSTOCK_USDT",
    "ADVANTESTSTOCK_USDT",
    "MUFGSTOCK_USDT",
    "LASERTECSTOCK_USDT",

    # Real estate
    "O_USDT",
    "RE_USDT",

    # FX
    "EUR_USDT",
    "GBP_USDT",
    "CHF_USDT",
    "AUD_USDT",
    "CAD_USDT",
    "BRL_USDT",
    "TRY_USDT",

    # Leveraged / inverse / volatility
    "TMF_USDT",
    "TBT_USDT",
    "UPRO_USDT",
    "SQQQ_USDT",
    "SOXL_USDT",
    "SOXS_USDT",
    "TZA_USDT",
    "DRV_USDT",
    "UVXY_USDT",

    # Other crypto explicitly mentioned
    "BNB_USDT",
    "AVAX_USDT",
    "ADA_USDT",
    "DOGE_USDT",
    "LINK_USDT",
    "SUI_USDT",
    "TAO_USDT",
    "HYPE_USDT",
    "NEAR_USDT",
    "AAVE_USDT",
    "UNI_USDT",
    "LTC_USDT",
    "BCH_USDT",
    "XMR_USDT",
]

LEVERAGE = 20


# ── minute-trigger engine constants ───────────────────────────────────────────

BUDGET_DAILY_ACCRUAL_USD = 10.0      # added to running budget at every UTC midnight
TRIGGER_STACK_USD        = 1.0       # fallback flat $ added per trigger if the
                                      # contribution-weighting recompute has never
                                      # succeeded for a symbol

ROLL_MINUTES_SHORT = 2 * 24 * 60     # 2 days, in minutes -> "2d low"
ROLL_MINUTES_LONG  = 9 * 24 * 60     # 9 days, in minutes -> "9d low"

MINUTE_CHECK_SECOND = 1              # run the check at :01 past each minute


# ── contribution-weighting constants ──────────────────────────────────────────

BASE_TRIGGER_USD = 1.0   # equal-weight baseline; matches legacy TRIGGER_STACK_USD

CONTRIB_LOOKBACK_DAYS = 90

# Caps applied to annualized volatility and average correlation
# immediately before they enter the z-score formula
# z = (1 - avg_cor) * (1 - ann_vol). Without these caps, ann_vol in
# particular is unbounded above and can exceed 1.0 for genuinely
# volatile symbols, driving (1 - ann_vol) — and therefore the whole
# z-score and downstream per-trigger USD contribution — negative.
# See "VOLATILITY / CORRELATION CAPPING" in the module docstring.
CONTRIB_VOL_CAP  = 1.0   # ann_vol clamped to [0.0, CONTRIB_VOL_CAP]
CONTRIB_CORR_CAP = 1.0   # avg_cor clamped to [-CONTRIB_CORR_CAP, CONTRIB_CORR_CAP]


# ── chart constants ────────────────────────────────────────────────────────────

CHART_MINUTES       = 10 * 24 * 60   # 10 days of 1-minute history for the chart
CHART_RESAMPLE_MIN  = 15             # resample 1m -> 15m OHLC candles

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

REPORT_MIN_INTERVAL_HOURS = 20


# ── timing ────────────────────────────────────────────────────────────────────

HOURLY_SLEEP_FLOOR_SEC = 5


# ── startup test order ────────────────────────────────────────────────────────

TEST_ORDER_DISCOUNT = 0.90
TEST_ORDER_WAIT_SEC = 20


# ── failed-symbol tracking ────────────────────────────────────────────────────

FAILED_SYMBOLS: set = set()
_FAILED_LOCK = threading.Lock()


def _xml_escape(s: str) -> str:
    """Escapes text for safe embedding inside SVG/XML text content."""
    return _saxutils.escape(str(s))

def flag_failed(sym: str, reason: str):
    with _FAILED_LOCK:
        FAILED_SYMBOLS.add(sym)

    log.error(
        f"[{sym}] FLAGGED FAILED — {reason} — "
        "excluded from trading (buffer/chart/trigger-marking continue)"
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


# ── contribution-weighting calculation logic ──────────────────────────────────

def fetch_30d_daily_closes(symbol: str) -> Dict[int, float]:
    """Fetches trailing N-day daily kline data from MEXC API.

    Returns a dict of {unix_timestamp: close}. Callers that need to
    compare series across symbols must date-align first — see
    align_price_series_by_date — rather than relying on raw
    timestamps or array position, since different symbols' daily
    candles are not guaranteed to fall on identical timestamps.
    """
    now_s = int(time.time())
    start_s = now_s - (CONTRIB_LOOKBACK_DAYS * 86400)
    url = (
        f"{MEXC_BASE}/api/v1/contract/kline/{symbol}"
        f"?interval=Day1&start={start_s}&end={now_s}"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            res = json.loads(response.read().decode())
            if not res.get("success"):
                return {}
            data = res.get("data", {})
            times = data.get("time", [])
            closes = data.get("realClose") or data.get("close") or []
            return {
                int(t): float(c)
                for t, c in zip(times, closes)
                if float(c) > 0
            }
    except Exception as e:
        log.error(f"[{symbol}] contribution-weighting daily-close fetch failed: {e}")
        return {}


def _to_date_keyed(price_dict: Dict[int, float]) -> Dict[datetime.date, float]:
    """Collapses a {timestamp: close} map onto {UTC date: close}.

    If more than one timestamp falls on the same UTC calendar date
    (should not normally happen for Day1 candles, but is possible
    around exchange maintenance/backfill artifacts), the
    latest-timestamped close for that date wins.
    """
    by_date: Dict[datetime.date, float] = {}
    for ts in sorted(price_dict.keys()):
        d = datetime.datetime.fromtimestamp(ts, tz=UTC).date()
        by_date[d] = price_dict[ts]
    return by_date


def align_price_series_by_date(
    price_dicts: Dict[str, Dict[int, float]]
) -> Dict[str, "collections.OrderedDict[datetime.date, float]"]:
    """Date-aligns multiple symbols' {timestamp: close} maps.

    Converts every symbol's raw timestamp-keyed series to a
    date-keyed series, computes the intersection of UTC calendar
    dates present across ALL symbols that returned any data, sorts
    that intersection chronologically, and returns each symbol's
    series restricted to exactly that common, ordered date list.

    This guarantees that index i in every returned series refers to
    the same calendar date for every symbol, so that downstream
    return/volatility/correlation calculations are comparing
    like-for-like periods rather than misaligned array positions.

    Symbols with no data at all are returned with an empty series.
    """
    date_keyed: Dict[str, Dict[datetime.date, float]] = {
        sym: _to_date_keyed(pd) for sym, pd in price_dicts.items()
    }

    non_empty = [d for d in date_keyed.values() if d]

    if not non_empty:
        return {sym: collections.OrderedDict() for sym in price_dicts}

    common_dates = set(non_empty[0].keys())
    for d in non_empty[1:]:
        common_dates &= set(d.keys())

    ordered_dates = sorted(common_dates)

    aligned: Dict[str, "collections.OrderedDict[datetime.date, float]"] = {}
    for sym, dkeyed in date_keyed.items():
        series = collections.OrderedDict()
        for d in ordered_dates:
            if d in dkeyed:
                series[d] = dkeyed[d]
        aligned[sym] = series

    return aligned


def compute_daily_returns(price_dict: Dict[int, float]) -> List[float]:
    """Calculates daily percentage returns from a chronologically
    ordered {key: close} mapping (keys may be timestamps or dates;
    only insertion/sort order of the values matters here)."""
    sorted_keys = sorted(price_dict.keys())
    returns = []
    for i in range(1, len(sorted_keys)):
        prev_p = price_dict[sorted_keys[i - 1]]
        curr_p = price_dict[sorted_keys[i]]
        returns.append((curr_p - prev_p) / prev_p)
    return returns


def compute_daily_returns_ordered(
    series: "collections.OrderedDict"
) -> List[float]:
    """Calculates daily percentage returns from an already
    date-ordered OrderedDict, preserving its existing order rather
    than re-sorting by key. Used for date-aligned series where the
    keys are datetime.date objects already in chronological order."""
    values = list(series.values())
    returns = []
    for i in range(1, len(values)):
        prev_p = values[i - 1]
        curr_p = values[i]
        if prev_p == 0:
            continue
        returns.append((curr_p - prev_p) / prev_p)
    return returns


def calculate_volatility(returns: List[float]) -> tuple:
    """Calculates daily standard deviation and annualized volatility.

    Both returned values are raw (uncapped) figures. Callers that
    feed annualized volatility into the z-score formula must apply
    CONTRIB_VOL_CAP themselves — see compute_contribution_weights —
    rather than relying on this function to clamp, so that raw
    volatility remains available for logging/diagnostics undistorted.
    """
    n = len(returns)
    if n < 2:
        return 0.0, 0.0
    mean_ret = sum(returns) / n
    variance = sum((r - mean_ret) ** 2 for r in returns) / (n - 1)
    daily_vol = math.sqrt(variance)
    annualized_vol = daily_vol * math.sqrt(365)
    return daily_vol, annualized_vol


def calculate_pearson_correlation(x: List[float], y: List[float]) -> float:
    """Calculates Pearson correlation coefficient.

    Callers are expected to pass in date-aligned return series (same
    length, each index i referring to the same calendar-date
    transition for both x and y). This function itself only
    truncates to the shorter length as a defensive fallback; the
    real alignment guarantee is provided upstream by
    align_price_series_by_date.
    """
    n = min(len(x), len(y))
    if n < 2:
        return 0.0
    x, y = x[:n], y[:n]
    mean_x, mean_y = sum(x) / n, sum(y) / n

    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    var_x = sum((x[i] - mean_x) ** 2 for i in range(n))
    var_y = sum((y[i] - mean_y) ** 2 for i in range(n))

    std_dev = math.sqrt(var_x * var_y)
    return cov / std_dev if std_dev != 0 else 0.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_contribution_weights(symbols: List[str]) -> Optional[Dict[str, float]]:
    raw_closes: Dict[str, Dict[int, float]] = {}
    any_fetch_succeeded = False

    for sym in symbols:
        closes = fetch_30d_daily_closes(sym)
        n_candles = len(closes)
        log.info(
            f"[{sym}] contribution-weighting fetch: "
            f"{n_candles} daily candles received "
            f"(lookback={CONTRIB_LOOKBACK_DAYS}d)"
        )

        if n_candles < CONTRIB_LOOKBACK_DAYS:
            log.warning(
                f"[{sym}] fetched {n_candles} candles, short of the "
                f"requested {CONTRIB_LOOKBACK_DAYS}d — MEXC may not "
                "have that much history, or the request was "
                "truncated/rate-limited"
            )
        if closes:
            any_fetch_succeeded = True

        raw_closes[sym] = closes

    if not any_fetch_succeeded:
        log.error(
            "contribution-weighting recompute: ALL daily-close "
            "fetches failed — total outage, caller will fall back "
            "to flat BASE_TRIGGER_USD for every symbol"
        )
        return None

    # Date-align every symbol's series onto the common intersection
    # of UTC calendar dates BEFORE computing returns, so that return
    # series are compared date-for-date rather than by raw array
    # index (which could otherwise silently mix mismatched dates
    # across symbols with different trading calendars/candle gaps).
    aligned = align_price_series_by_date(raw_closes)

    common_date_count = max((len(s) for s in aligned.values()), default=0)
    log.info(
        f"contribution-weighting: date-aligned to "
        f"{common_date_count} common UTC calendar date(s) "
        f"across {len(symbols)} symbols"
    )

    returns_data: Dict[str, List[float]] = {}
    vol_data: Dict[str, Dict[str, float]] = {}

    for sym in symbols:
        series = aligned.get(sym, collections.OrderedDict())

        if len(series) < 2:
            log.warning(
                f"[{sym}] fewer than 2 dates survived alignment to "
                "the common date set — treating as empty return "
                "series for this recompute cycle"
            )

        rets = compute_daily_returns_ordered(series)
        returns_data[sym] = rets
        d_vol, a_vol = calculate_volatility(rets)
        vol_data[sym] = {"daily": d_vol, "annualized": a_vol}

    num_assets = len(symbols)
    corr_matrix: Dict[str, Dict[str, float]] = {s1: {} for s1 in symbols}

    for i, sym1 in enumerate(symbols):
        for j, sym2 in enumerate(symbols):
            if i == j:
                corr_matrix[sym1][sym2] = 1.0
            else:
                corr_matrix[sym1][sym2] = calculate_pearson_correlation(
                    returns_data[sym1], returns_data[sym2]
                )

    col_averages: Dict[str, float] = {}
    for col_sym in symbols:
        col_sum = sum(corr_matrix[row_sym][col_sym] for row_sym in symbols)
        col_averages[col_sym] = col_sum / num_assets

    # ── z-score computation, with volatility/correlation capping ──
    #
    # ann_vol is unbounded above and routinely exceeds 1.0 (100%)
    # for genuinely volatile symbols. Left uncapped, (1 - ann_vol)
    # goes negative, which can drive z negative and, downstream, a
    # negative or distorted per-trigger USD contribution. Both
    # inputs are clamped immediately before entering the formula;
    # the raw (uncapped) annualized volatility is still used for
    # logging so the true figure remains visible in operational
    # output even when the capped figure is what drives sizing.
    z_scores: Dict[str, float] = {}
    for sym in symbols:
        raw_avg_cor = col_averages[sym]
        raw_ann_vol = vol_data[sym]["annualized"]

        avg_cor = _clamp(raw_avg_cor, -CONTRIB_CORR_CAP, CONTRIB_CORR_CAP)
        ann_vol = _clamp(raw_ann_vol, 0.0, CONTRIB_VOL_CAP)

        if raw_ann_vol > CONTRIB_VOL_CAP:
            log.warning(
                f"[{sym}] annualized volatility {raw_ann_vol:.3f} "
                f"exceeds cap {CONTRIB_VOL_CAP:.3f} — clamped to "
                f"{ann_vol:.3f} for z-score purposes (raw figure "
                "retained for diagnostics)"
            )

        if abs(raw_avg_cor) > CONTRIB_CORR_CAP:
            log.warning(
                f"[{sym}] avg correlation {raw_avg_cor:.3f} exceeds "
                f"cap magnitude {CONTRIB_CORR_CAP:.3f} — clamped to "
                f"{avg_cor:.3f} for z-score purposes"
            )

        z_scores[sym] = (1 - avg_cor) * (1 - ann_vol)

    total_z = sum(z_scores.values())

    weights: Dict[str, float] = {
        sym: (z / total_z) if total_z > 0 else (1 / num_assets)
        for sym, z in z_scores.items()
    }

    contrib_per_trigger_usd: Dict[str, float] = {
        sym: BASE_TRIGGER_USD * num_assets * weights[sym]
        for sym in symbols
    }

    log.info(
        "contribution-weighting recompute complete: "
        + ", ".join(
            f"{sym}=${contrib_per_trigger_usd[sym]:.3f}"
            for sym in symbols
        )
    )

    return contrib_per_trigger_usd


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

def _default_daily_stats(now_iso: str) -> Dict:
    return {
        "window_start": now_iso,
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
        "triggers": [],
        "budget": {},
        "accumulator": {},
        "last_accrual_date": {},
        "last_seen_minute": {},
        "daily_stats": {},
        "last_report_sent_at": None,
        "contrib_per_trigger_usd": {},
        "contrib_last_computed_date": None,
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
        log.info(f"no state file at {STATE_FILE} — starting fresh")
        return _default_state()

    except Exception as e:
        log.error(f"state file at {STATE_FILE} unreadable ({e}) — starting fresh")
        return _default_state()


def save_state(state: Dict):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error(f"failed to persist state to {STATE_FILE}: {e}")


STATE_DATA: Dict = load_state()
_STATE_DATA_LOCK = threading.Lock()


def get_budget(sym: str) -> float:
    return float(STATE_DATA["budget"].get(sym, 0.0))


def get_accumulator(sym: str) -> float:
    return float(STATE_DATA["accumulator"].get(sym, 0.0))


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


# ── contribution-weighting cache accessors ────────────────────────────────────

def get_contrib_per_trigger_usd(sym: str) -> float:
    return float(
        STATE_DATA["contrib_per_trigger_usd"].get(sym, TRIGGER_STACK_USD)
    )


def get_contrib_last_computed_date() -> Optional[datetime.date]:
    s = STATE_DATA.get("contrib_last_computed_date")
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None


def set_contrib_per_trigger_usd(values: Dict[str, float], today: datetime.date):
    with _STATE_DATA_LOCK:
        STATE_DATA["contrib_per_trigger_usd"] = dict(values)
        STATE_DATA["contrib_last_computed_date"] = today.isoformat()
        _persist()


def recompute_contributions_if_due(today: datetime.date, force: bool = False):
    last = get_contrib_last_computed_date()

    if not force and last == today:
        return

    log.info(
        f"contribution-weighting recompute due "
        f"(last={last}, today={today}, force={force}) — running "
        f"{CONTRIB_LOOKBACK_DAYS}-day analysis over "
        f"{len(SYMBOLS)} symbols"
    )

    result = compute_contribution_weights(SYMBOLS)

    if result is None:
        log.error(
            "contribution-weighting recompute FAILED (total outage) "
            f"— falling back to flat ${BASE_TRIGGER_USD:.2f} for "
            "every symbol today"
        )
        result = {sym: BASE_TRIGGER_USD for sym in SYMBOLS}

    set_contrib_per_trigger_usd(result, today)


def add_trigger_dollar(sym: str) -> float:
    with _STATE_DATA_LOCK:
        prev = get_accumulator(sym)
        contribution = get_contrib_per_trigger_usd(sym)
        new = prev + contribution
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


def record_trigger_marker(
    sym: str,
    candle_dt: datetime.datetime,
    price: float,
    window_label: str
):
    with _STATE_DATA_LOCK:
        STATE_DATA["triggers"].append({
            "symbol": sym,
            "candle_time": candle_dt.isoformat(),
            "price": price,
            "window": window_label,
        })

        cutoff = time.time() - CHART_MINUTES * 60 - 3600

        STATE_DATA["triggers"] = [
            t for t in STATE_DATA["triggers"]
            if _safe_ts(t.get("candle_time")) is None
            or _safe_ts(t.get("candle_time")) >= cutoff
        ]

        _persist()


def _safe_ts(iso_str: Optional[str]) -> Optional[float]:
    if not iso_str:
        return None
    try:
        return datetime.datetime.fromisoformat(iso_str).timestamp()
    except Exception:
        return None


def total_orders_count() -> int:
    return len(STATE_DATA["orders"])


# ── daily stats (activity report counters) ────────────────────────────────────

def _ensure_daily_stats_initialized(sym: str):
    with _STATE_DATA_LOCK:
        if sym not in STATE_DATA["daily_stats"]:
            now_iso = datetime.datetime.now(UTC).isoformat()
            STATE_DATA["daily_stats"][sym] = _default_daily_stats(now_iso)
            _persist()


def record_trigger_stat(sym: str):
    _ensure_daily_stats_initialized(sym)
    with _STATE_DATA_LOCK:
        STATE_DATA["daily_stats"][sym]["triggers"] += 1
        _persist()


def record_attempt_stat(
    sym: str,
    price: float,
    success: bool,
    usd_if_success: float = 0.0
):
    _ensure_daily_stats_initialized(sym)
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


def get_daily_stats_snapshot(sym: str) -> Dict:
    _ensure_daily_stats_initialized(sym)
    with _STATE_DATA_LOCK:
        return dict(STATE_DATA["daily_stats"].get(
            sym, _default_daily_stats(datetime.datetime.now(UTC).isoformat())
        ))


def reset_daily_stats_all(now_utc: datetime.datetime):
    now_iso = now_utc.isoformat()
    with _STATE_DATA_LOCK:
        for sym in SYMBOLS:
            STATE_DATA["daily_stats"][sym] = _default_daily_stats(now_iso)
        _persist()


def get_last_report_sent_at() -> Optional[datetime.datetime]:
    s = STATE_DATA.get("last_report_sent_at")
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def set_last_report_sent_at(dt: datetime.datetime):
    with _STATE_DATA_LOCK:
        STATE_DATA["last_report_sent_at"] = dt.isoformat()
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
        url += "?" + urllib.parse.urlencode(sorted(params.items()))

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()

    return json.loads(body) if body.strip() else {}


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


# ── MEXC signed requests ─────────────────────────────────────────────────────

def mexc(
    method,
    endpoint,
    params=None,
    body=None
):
    params = params or {}
    ts = str(int(time.time() * 1000))

    sp = (
        "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        if method == "GET"
        else (
            json.dumps(body, separators=(",", ":"), sort_keys=True)
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
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        if body and method not in ("GET", "DELETE")
        else None
    )

    try:
        return _http(
            method,
            MEXC_BASE + endpoint,
            headers=hdr,
            data=raw,
            params=params if method in ("GET", "DELETE") else None
        )
    except Exception as e:
        log.error(f"mexc {method} {endpoint}: {e}")
        return {}


# ── ntfy ──────────────────────────────────────────────────────────────────────

def ntfy_send(message: str, title: Optional[str] = None) -> bool:
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
        return True
    except Exception as e:
        log.error(f"ntfy: failed to send report: {e}")
        return False


# ── contract specifications ───────────────────────────────────────────────────

def load_specs():
    rows = (
        mexc("GET", "/api/v1/contract/detail").get("data") or []
    )

    if not rows:
        log.error(
            "empty contract detail response from MEXC — "
            "flagging all symbols failed"
        )
        for sym in SYMBOLS:
            flag_failed(sym, "empty contract detail response from MEXC")
        return

    by_sym = {c.get("symbol", "").upper(): c for c in rows}

    for sym in SYMBOLS:
        match = by_sym.get(sym)
        if match is None:
            flag_failed(sym, "symbol not found in MEXC contract detail")
            continue

        vu = float(match.get("volUnit", 1))
        pu = float(match.get("priceUnit", 0.01))
        cs = float(match.get("contractSize", vu))

        raw = f"{vu:.10f}".rstrip("0")
        p = len(raw.split(".")[1]) if "." in raw else 0

        max_lev_raw = match.get("maxLeverage")
        try:
            max_lev = float(max_lev_raw) if max_lev_raw is not None else None
            if max_lev is not None and max_lev <= 0:
                max_lev = None
        except Exception:
            max_lev = None

        specs[sym] = {
            "p": p,
            "t": pu,
            "vu": vu,
            "cs": cs,
            "max_lev": max_lev,
        }

        log.info(f"loaded specs for {sym}: {specs[sym]}")


def _tick(sym):
    return specs.get(sym, {}).get("t", 0.01)


def _prec(sym):
    return specs.get(sym, {}).get("p", 0)


def _rfmt_price(sym, v):
    t = _tick(sym)
    r = round(v / t) * t
    s = f"{t:.10f}".rstrip("0")
    dec = len(s.split(".")[1]) if "." in s else 0
    return f"{r:.{dec}f}"


def _rfmt_vol(sym, v):
    p = _prec(sym)
    if p >= 0:
        return f"{round(v, p):.{p}f}"
    d = 10 ** abs(p)
    return str(int(round(v / d) * d))


def _contracts(sym, usd, price):
    cs = specs.get(sym, {}).get("cs", 1.0)
    return float(
        _rfmt_vol(sym, max(0, usd / (cs * price)))
    )


def _mos(sym):
    return specs.get(sym, {}).get("vu", 1.0)


def _effective_leverage(sym: str) -> int:
    """Returns the leverage to actually submit with orders for this
    symbol: the global LEVERAGE, capped at the exchange-reported
    maxLeverage for that contract if one is known. Falls back to
    the global LEVERAGE unmodified when maxLeverage was not reported
    or could not be parsed at spec-load time."""
    max_lev = specs.get(sym, {}).get("max_lev")

    if max_lev is None:
        return LEVERAGE

    if max_lev < LEVERAGE:
        log.info(
            f"[{sym}] global leverage {LEVERAGE}x exceeds exchange "
            f"maximum {max_lev:.0f}x for this symbol — using "
            f"{max_lev:.0f}x instead"
        )
        return int(max_lev)

    return LEVERAGE


# ── open orders ───────────────────────────────────────────────────────────────

def _open_orders_for_sym(sym: str) -> List[Dict]:
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
        data = data.get("resultList", [])

    return [o for o in data if o.get("symbol", "").upper() == sym]


def _open_ids(sym: str) -> set:
    return {str(o.get("orderId", "")) for o in _open_orders_for_sym(sym)}


# ── order placement ──────────────────────────────────────────────────────────

def place_long(
    sym: str,
    limit_price: float,
    sizing_price: float,
    usd_amount: float
) -> Optional[str]:
    vol = _contracts(sym, usd_amount, sizing_price)

    if vol < _mos(sym):
        log.warning(
            f"[{sym}] size {vol} < min "
            f"{_mos(sym)} (${usd_amount:.2f}) "
            "— order skipped"
        )
        return "SKIP"

    return _place_long_contracts(sym, limit_price, vol)


def place_long_min_size(sym: str, limit_price: float) -> Optional[str]:
    vol = _mos(sym)
    return _place_long_contracts(sym, limit_price, vol)


def _place_long_contracts(
    sym: str,
    limit_price: float,
    vol: float
) -> Optional[str]:
    leverage = _effective_leverage(sym)

    body = {
        "leverage": leverage,
        "openType": 2,
        "positionMode": 1,
        "price": _rfmt_price(sym, limit_price),
        "side": 1,
        "symbol": sym,
        "type": 1,
        "vol": _rfmt_vol(sym, vol),
    }

    r = mexc("POST", "/api/v1/private/order/create", body=body)

    if not r.get("success"):
        log.error(f"[{sym}] long order rejected: {r}")
        return None

    data = r.get("data") or {}

    if not isinstance(data, dict):
        log.error(f"[{sym}] unexpected 'data' shape from order/create: {data!r}")
        return None

    oid = data.get("orderId")

    if not oid:
        log.error(f"[{sym}] order/create succeeded but no 'orderId' in data: {data!r}")
        return None

    oid = str(oid)

    log.info(
        f"[{sym}] limit LONG "
        f"{_rfmt_vol(sym, vol)} "
        f"@ {_rfmt_price(sym, limit_price)} "
        f"leverage={leverage}x "
        f"id={oid}"
    )

    return oid


# ── cancel order ──────────────────────────────────────────────────────────────

def cancel_order(sym: str, oid: str) -> bool:
    body = [oid]
    r = mexc("POST", "/api/v1/private/order/cancel", body=body)
    ok = bool(r.get("success"))

    if ok:
        log.info(f"[{sym}] cancelled order id={oid}")
    else:
        log.error(f"[{sym}] cancel failed for id={oid}: {r}")

    return ok


def is_filled(sym: str, oid: str) -> bool:
    return oid not in _open_ids(sym)


# ── mark price ────────────────────────────────────────────────────────────────

def get_mark(sym: str) -> float:
    d = (
        mexc(
            "GET",
            "/api/v1/contract/ticker",
            params={"symbol": sym}
        ).get("data") or {}
    )

    return float(
        d.get("fairPrice", d.get("lastPrice", 0)) or 0
    )


# ── 1-minute klines ───────────────────────────────────────────────────────────

def fetch_minute_bars(
    sym: str,
    start_s: int,
    end_s: int
) -> List[Dict]:
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
        log.error(f"[{sym}] minute kline fetch failed: {e}")
        return []

    if not raw.get("success"):
        log.error(f"[{sym}] minute kline fetch unsuccessful: {raw}")
        return []

    d = raw.get("data") or {}

    times  = d.get("time") or []
    opens  = d.get("realOpen")  or d.get("open")  or []
    highs  = d.get("realHigh")  or d.get("high")  or []
    lows   = d.get("realLow")   or d.get("low")   or []
    closes = d.get("realClose") or d.get("close") or []

    n = min(len(times), len(opens), len(highs), len(lows), len(closes))
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
            existing_ts = {b["t"] for b in self.bars}

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
            window = [b["l"] for b in self.bars if b["t"] >= cutoff]

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


SEED_CHUNK_MINUTES = 1500
SEED_MAX_CHUNKS = (BUFFER_MAX_MINUTES // SEED_CHUNK_MINUTES) + 3


def seed_minute_buffer(sym: str):
    now_s = int(time.time())
    window_start_s = now_s - BUFFER_MAX_MINUTES * 60
    all_bars: Dict[int, Dict] = {}
    chunk_end_s = now_s
    chunks_fetched = 0

    while chunk_end_s > window_start_s and chunks_fetched < SEED_MAX_CHUNKS:
        chunk_start_s = max(
            window_start_s,
            chunk_end_s - SEED_CHUNK_MINUTES * 60
        )

        bars = fetch_minute_bars(sym, chunk_start_s, chunk_end_s)
        chunks_fetched += 1

        if not bars:
            log.info(
                f"[{sym}] seed chunk "
                f"[{chunk_start_s}, {chunk_end_s}) returned no bars "
                "— stopping (likely reached start of available history)"
            )
            break

        for b in bars:
            all_bars[b["t"]] = b

        log.info(
            f"[{sym}] seed chunk "
            f"[{chunk_start_s}, {chunk_end_s}): "
            f"{len(bars)} bars fetched, "
            f"{len(all_bars)} total so far"
        )

        earliest_returned = min(b["t"] for b in bars)
        chunk_end_s = earliest_returned

        if chunk_end_s >= chunk_start_s + SEED_CHUNK_MINUTES * 60:
            break

    sorted_bars = [all_bars[t] for t in sorted(all_bars.keys())]
    MINUTE_BUFFERS[sym].seed(sorted_bars)

    span_days = (
        (sorted_bars[-1]["t"] - sorted_bars[0]["t"]) / 86400.0
        if len(sorted_bars) >= 2
        else 0.0
    )

    log.info(
        f"[{sym}] minute buffer seeded: "
        f"{MINUTE_BUFFERS[sym].size()} bars "
        f"across {chunks_fetched} chunk(s), "
        f"spanning ~{span_days:.1f} days "
        f"(target: {BUFFER_MAX_MINUTES / 1440:.1f} days)"
    )

    if span_days < (BUFFER_MAX_MINUTES / 1440.0) * 0.9:
        log.warning(
            f"[{sym}] seeded buffer spans only ~{span_days:.1f} days, "
            f"short of the ~{BUFFER_MAX_MINUTES / 1440:.1f}-day target — "
            "9d-low reference and 10d chart will be based on "
            "incomplete history until the buffer naturally fills in "
            "over the next few days of minute-by-minute updates"
        )


def refresh_minute_buffer(sym: str):
    now_s = int(time.time())
    start_s = now_s - 5 * 60
    bars = fetch_minute_bars(sym, start_s, now_s)
    if bars:
        MINUTE_BUFFERS[sym].append_new(bars)


# ── resampling for charts ─────────────────────────────────────────────────────

def resample_ohlc(bars: List[Dict], bucket_minutes: int) -> List[Dict]:
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

def evaluate_trigger(sym: str, now_utc: datetime.datetime):
    refresh_minute_buffer(sym)
    buf = MINUTE_BUFFERS[sym]
    latest = buf.latest_closed()

    if latest is None:
        log.warning(
            f"[{sym}] no closed 1-minute candle available yet "
            "— skipping this minute"
        )
        return None

    candle_dt = datetime.datetime.fromtimestamp(latest["t"], tz=UTC)
    last_seen = get_last_seen_minute(sym)

    if last_seen is not None and candle_dt <= last_seen:
        return None

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
        return None

    triggered = candle_low <= ref_low

    log.info(
        f"[{sym}] minute check {candle_dt.isoformat()}: "
        f"low={candle_low:.4f} "
        f"{ref_label}Low={ref_low:.4f} "
        f"budget={budget:.2f} "
        f"failed={is_failed(sym)} "
        f"trigger={triggered}"
    )

    return candle_dt, candle_low, ref_label, triggered


def process_symbol_minute(sym: str, now_utc: datetime.datetime):
    failed = is_failed(sym)

    if not failed:
        today = now_utc.date()
        accrue_daily_budget_if_due(sym, today)

    result = evaluate_trigger(sym, now_utc)

    if result is None:
        return

    candle_dt, candle_low, ref_label, triggered = result

    if not triggered:
        return

    record_trigger_marker(sym, candle_dt, candle_low, ref_label)
    record_trigger_stat(sym)

    if failed:
        log.info(
            f"[{sym}] TRIGGER (failed symbol, marker-only, "
            f"no accumulation) @ price={candle_low:.4f}"
        )
        return

    pending = add_trigger_dollar(sym)

    log.info(
        f"[{sym}] TRIGGER — accumulator now ${pending:.2f} "
        f"(contribution=${get_contrib_per_trigger_usd(sym):.3f}/trigger) "
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
        record_attempt_stat(sym, candle_low, success=False)
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

    record_attempt_stat(sym, candle_low, success=True, usd_if_success=pending)
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
            process_symbol_minute(sym, now_utc)
        except Exception as e:
            log.error(
                f"[{sym}] minute check failed: {e}",
                exc_info=True
            )


# ── daily activity report ─────────────────────────────────────────────────────

def build_daily_report_text(now_utc: datetime.datetime) -> str:
    window_start = None

    for sym in SYMBOLS:
        stats = get_daily_stats_snapshot(sym)
        ws = _safe_ts(stats.get("window_start"))
        if ws is not None:
            window_start = ws
            break

    if window_start is not None:
        window_start_dt = datetime.datetime.fromtimestamp(window_start, tz=UTC)
        header = (
            f"Daily Activity Report — "
            f"{window_start_dt.strftime('%Y-%m-%d %H:%M')} UTC "
            f"to {now_utc.strftime('%Y-%m-%d %H:%M')} UTC"
        )
    else:
        header = (
            f"Daily Activity Report — as of "
            f"{now_utc.strftime('%Y-%m-%d %H:%M')} UTC"
        )

    contrib_date = get_contrib_last_computed_date()
    contrib_note = (
        f"Contribution weights last computed: {contrib_date.isoformat()}"
        if contrib_date is not None
        else "Contribution weights: not yet computed (flat fallback in effect)"
    )

    lines = [header, contrib_note, ""]

    for sym in SYMBOLS:
        stats = get_daily_stats_snapshot(sym)
        triggers = stats["triggers"]
        order_value = stats["order_value_usd"]
        ok = stats["orders_ok"]
        failed_count = stats["orders_failed"]
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

        excluded_note = " [EXCLUDED — not traded]" if is_failed(sym) else ""
        contrib = get_contrib_per_trigger_usd(sym)

        lines.append(
            f"{sym}: triggers={triggers}  "
            f"order_value=${order_value:,.2f}  "
            f"ok={ok}  failed={failed_count}  "
            f"avg_attempt_price={avg_price_str}  "
            f"contrib/trigger=${contrib:.3f}"
            f"{excluded_note}"
        )

    return "\n".join(lines)


def maybe_send_daily_report(now_utc: datetime.datetime):
    at_or_after_report_time = (
        (now_utc.hour, now_utc.minute)
        >= (REPORT_HOUR_UTC, REPORT_MINUTE_UTC)
    )

    if not at_or_after_report_time:
        return

    last_sent = get_last_report_sent_at()

    if last_sent is not None:
        hours_since = (now_utc - last_sent).total_seconds() / 3600.0
        if hours_since < REPORT_MIN_INTERVAL_HOURS:
            return

    report_text = build_daily_report_text(now_utc)
    log.info(f"sending daily activity report:\n{report_text}")

    sent_ok = ntfy_send(
        report_text,
        title=f"DCA Bot Daily Report {now_utc.date().isoformat()}"
    )

    if sent_ok:
        set_last_report_sent_at(now_utc)
        reset_daily_stats_all(now_utc)
    else:
        log.error(
            "daily report send failed — counters NOT reset, "
            "will retry next minute"
        )


# ── startup test orders ───────────────────────────────────────────────────────

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
            f"vol={min_vol} (exchange minimum) "
            f"leverage={_effective_leverage(sym)}x"
        )

        oid = place_long_min_size(sym, test_price)

        if oid is None:
            flag_failed(sym, "test order rejected by MEXC")
            return None

        log.info(f"[{sym}] test order placed id={oid}")

        return {
            "sym": sym,
            "oid": oid,
            "limit_price": test_price,
            "vol": min_vol,
        }

    except Exception as e:
        flag_failed(sym, f"exception during test order open: {e}")
        log.error(f"[{sym}] test order open failed: {e}", exc_info=True)
        return None


def _close_test_order(pending: Dict):
    sym = pending["sym"]
    oid = pending["oid"]

    try:
        if is_filled(sym, oid):
            log.warning(
                f"[{sym}] test order id={oid} FILLED during the "
                f"{TEST_ORDER_WAIT_SEC}s wait. "
                "This is now a real open long position. Symbol remains validated."
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

        cancelled = cancel_order(sym, oid)

        if cancelled:
            log.info(f"[{sym}] test order id={oid} cancelled successfully — symbol validated")
        else:
            flag_failed(sym, f"test order id={oid} could not be cancelled")

    except Exception as e:
        flag_failed(sym, f"exception during test order close: {e}")
        log.error(f"[{sym}] test order close failed: {e}", exc_info=True)


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

    log.info("══ startup test orders: phase 3/3: closing ══")

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
    contrib_date = get_contrib_last_computed_date()
    contrib_date_str = (
        contrib_date.isoformat() if contrib_date is not None else "pending"
    )

    title_text = _xml_escape(
        f'MultiLongDCA-Bot — {len(SYMBOLS)} symbols — '
        f'minute-trigger engine — {now_str} — '
        f'contrib weights: {contrib_date_str}'
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
            f'{title_text}'
            f'</text>'
        ),
    ]

    y = 50

    for sym in SYMBOLS:
        failed = is_failed(sym)
        budget = get_budget(sym)
        accum = get_accumulator(sym)
        contrib = get_contrib_per_trigger_usd(sym)

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

        eff_lev = _effective_leverage(sym)

        if failed:
            clr = "#cc0000"
            line = (
                f"{sym:<18} "
                "*** FAILED — EXCLUDED FROM TRADING "
                "(chart & triggers still tracked) ***  "
                f"2dLow={low2d_str:>12}  9dLow={low9d_str:>12}"
            )
        else:
            clr = "#1155cc" if budget >= 0 else "#cc7a00"
            line = (
                f"{sym:<18} "
                f"budget=${budget:>8,.2f}  "
                f"accum=${accum:>5,.2f}  "
                f"contrib=${contrib:>5,.3f}/trig  "
                f"lev={eff_lev:>3}x  "
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
            f'{_xml_escape(line)}'
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

    failed = is_failed(sym)
    budget = get_budget(sym)
    contrib = get_contrib_per_trigger_usd(sym)

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

    title_suffix = _xml_escape(
        (" [FAILED — excluded from trading]" if failed else "")
        + f" [contrib: ${contrib:.3f}/trigger]"
        + f" [lev: {_effective_leverage(sym)}x]"
    )

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
            f'fill="{"#cc0000" if failed else "#333"}" font-weight="bold">'
            f'{sym} — 10d, 15m candles — {now_str}{title_suffix}</text>'
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
            f'fill="#888">{_xml_escape(f"{price:,.3f}")}</text>'
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
            f'{_xml_escape(f"{ref_label} low threshold: {ref_low:,.4f}")}</text>'
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

    trigger_markers = [
        t for t in STATE_DATA["triggers"]
        if t.get("symbol") == sym
    ]

    for t in trigger_markers:
        ts = _safe_ts(t.get("candle_time"))

        if ts is None or ts < t0 or ts > t1:
            continue

        tx = x_of(int(ts))
        ty = y_of(t["price"])
        sz = 3.5

        svg.append(
            f'<line x1="{tx - sz:.1f}" y1="{ty - sz:.1f}" '
            f'x2="{tx + sz:.1f}" y2="{ty + sz:.1f}" '
            f'stroke="#7a3fb8" stroke-width="1.3"/>'
        )

        svg.append(
            f'<line x1="{tx - sz:.1f}" y1="{ty + sz:.1f}" '
            f'x2="{tx + sz:.1f}" y2="{ty - sz:.1f}" '
            f'stroke="#7a3fb8" stroke-width="1.3"/>'
        )

    orders = [
        o for o in STATE_DATA["orders"]
        if o.get("symbol") == sym
        and "limit_price" in o
        and ("candle_time" in o or "timestamp" in o)
    ]

    for o in orders:
        ts_str = o.get("candle_time") or o.get("timestamp")
        ts = _safe_ts(ts_str)

        if ts is None or ts < t0 or ts > t1:
            continue

        ox = x_of(int(ts))
        oy = y_of(o["limit_price"])

        is_real_fire = "reference_window" in o
        marker_color = "#0044cc" if is_real_fire else "#888"

        svg.append(
            f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="4" '
            f'fill="{marker_color}" stroke="#fff" stroke-width="1"/>'
        )

    legend_y = CHART_H - 8

    svg.append(
        f'<line x1="{CHART_MARGIN_L}" y1="{legend_y - 4}" '
        f'x2="{CHART_MARGIN_L + 8}" y2="{legend_y + 4}" '
        f'stroke="#7a3fb8" stroke-width="1.3"/>'
    )
    svg.append(
        f'<line x1="{CHART_MARGIN_L}" y1="{legend_y + 4}" '
        f'x2="{CHART_MARGIN_L + 8}" y2="{legend_y - 4}" '
        f'stroke="#7a3fb8" stroke-width="1.3"/>'
    )
    svg.append(
        f'<text x="{CHART_MARGIN_L + 14}" y="{legend_y + 4}" '
        f'font-family="Courier New" font-size="10" fill="#555">'
        f'trigger</text>'
    )

    svg.append(
        f'<circle cx="{CHART_MARGIN_L + 90}" cy="{legend_y}" r="4" '
        f'fill="#0044cc" stroke="#fff" stroke-width="1"/>'
    )
    svg.append(
        f'<text x="{CHART_MARGIN_L + 100}" y="{legend_y + 4}" '
        f'font-family="Courier New" font-size="10" fill="#555">'
        f'order placed</text>'
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

    try:
        recompute_contributions_if_due(now_utc.date())
    except Exception as e:
        log.error(
            f"contribution-weighting recompute check failed: {e}",
            exc_info=True
        )

    run_minute_checks(now_utc)

    svg = render_svg(now_utc)
    STATE.set_svg(svg)

    for sym in SYMBOLS:
        try:
            chart_svg = render_symbol_chart_svg(sym)
            STATE.set_chart_svg(sym, chart_svg)
        except Exception as e:
            log.error(
                f"[{sym}] chart render failed: {e}",
                exc_info=True
            )

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

    now_utc = datetime.datetime.now(UTC)
    log.info("recomputing contribution weights on startup")
    try:
        recompute_contributions_if_due(now_utc.date(), force=True)
    except Exception as e:
        log.error(
            f"startup contribution recompute failed: {e}",
            exc_info=True
        )

    run_startup_test_orders()

    log.info(
        "seeding 1-minute candle buffers for ALL symbols "
        f"(including failed) (~{BUFFER_MAX_MINUTES} minutes each)"
    )

    for sym in SYMBOLS:
        try:
            seed_minute_buffer(sym)
        except Exception as e:
            log.error(
                f"[{sym}] failed to seed minute buffer: {e}",
                exc_info=True
            )

    log.info("engine starting — running initial cycle")

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

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
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
                    "contrib_per_trigger_usd": STATE_DATA["contrib_per_trigger_usd"],
                    "contrib_last_computed_date": STATE_DATA["contrib_last_computed_date"],
                },
                indent=2
            ).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/stats.json":
            body = json.dumps(
                {
                    sym: get_daily_stats_snapshot(sym)
                    for sym in SYMBOLS
                },
                indent=2
            ).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/leverage.json":
            body = json.dumps(
                {
                    sym: {
                        "global_leverage": LEVERAGE,
                        "max_leverage": specs.get(sym, {}).get("max_lev"),
                        "effective_leverage": _effective_leverage(sym),
                    }
                    for sym in SYMBOLS
                },
                indent=2
            ).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path in ("/", ""):
            status = STATE.get_status()

            chart_links = " · ".join(
                (
                    f'<a href="/chart/{sym}.svg" target="_blank">'
                    f'{sym}{" (failed)" if is_failed(sym) else ""}'
                    f'</a>'
                )
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
                "<a href='/budget.json'>budget/accumulator/contribution</a>"
                " · "
                "<a href='/stats.json'>current stats</a>"
                " · "
                "<a href='/leverage.json'>leverage per symbol</a>"
                " · "
                "<a href='/failed.json'>failed symbols</a>"
                "</p>"
                "</body>"
                "</html>"
            )

            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
    log.info(f"server listening on {HTTP_HOST}:{HTTP_PORT}")
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
