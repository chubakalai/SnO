#!/usr/bin/env python3
"""
MultiLongDCA-Bot — Multi-Symbol Minute-Trigger DCA Long Bot.
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
import xml.etree.ElementTree as ET
import xml.sax.saxutils as _saxutils
from typing import Deque, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── constants ─────────────────────────────────────────────────────────────────

UTC = datetime.timezone.utc

MEXC_KEY = os.getenv("MEXC")
MEXC_SECRET = os.getenv("MEXCSECRET")
MEXC_BASE = "https://api.mexc.co"

# ── symbol configuration ──────────────────────────────────────────────────────

SYMBOLS: List[str] = [
    "USOIL_USDT",       # proxy for UOILUSD (WTI)
    "URNM_USDT",        # proxy for URNMUSD
    "BTC_USDT",         # proxy for BTCUSD
    "ETH_USDT",         # proxy for ETHUSD
    "SOL_USDT",         # proxy for SOLUSD
    "XRP_USDT",         # proxy for XRPUSD
    "TRX_USDT",
    "NGAS_USDT",        # Natural Gas
    "XPD_USDT",         # Palladium
    "XAU_USDT",         # Gold
    "MSTRSTOCK_USDT",   # MicroStrategy
    "UNITREE_USDT",
    "SPX500_USDT",      # S&P 500 Index
    "EWJ_USDT",         # iShares MSCI Japan ETF
    "EWY_USDT",         # iShares MSCI South Korea ETF
    "HK0700_USDT",      # Tencent Holdings (0700.HK)
    "INDA_USDT",        # iShares MSCI India ETF
    "EWT_USDT",         # iShares MSCI Taiwan ETF
    "SMH_USDT",         # VanEck Semiconductor ETF
    "COPPER_USDT",      # Copper
    "BKRSTOCK_USDT",    # Baker Hughes
]

LEVERAGE = 20

# ── minute-trigger engine constants ───────────────────────────────────────────

BUDGET_DAILY_ACCRUAL_MULT = 1.0
TRIGGER_STACK_USD = 1.0
ORDER_SIZE_BUDGET_R_DIVISOR = 1000.0

ROLL_MINUTES_SHORT = 2 * 24 * 60   # 2 days, in minutes -> "2d low"
ROLL_MINUTES_LONG = 9 * 24 * 60    # 9 days, in minutes -> "9d low"

MINUTE_CHECK_SECOND = 1  # run the check at :01 past each minute

# ── contribution-weighting constants ──────────────────────────────────────────

BASE_TRIGGER_USD = 1.0
CONTRIB_LOOKBACK_DAYS = 90
CONTRIB_VOL_CAP = 1.0
CONTRIB_VOL_FLOOR = 0.0001
CONTRIB_ITERATIONS = 50
CONTRIB_MIN_COMMON_DATES = 2

OVERVIEW_FILENAME = "portfolio_overview_90d.txt"
SVG_FILENAME = "portfolio_allocation_matrix_90d.svg"

# ── chart constants ────────────────────────────────────────────────────────────

CHART_MINUTES = 10 * 24 * 60
CHART_RESAMPLE_MIN = 15

BUFFER_MAX_MINUTES = max(ROLL_MINUTES_LONG, CHART_MINUTES) + 60

CHART_W = 1200
CHART_H = 420
CHART_MARGIN_L = 60
CHART_MARGIN_R = 20
CHART_MARGIN_T = 40
CHART_MARGIN_B = 40

# ── overview table constants ────────────────────────────────────────────────────

TABLE_COLUMNS: List[Tuple[str, str]] = [
    ("Sym", "Symbol"),
    ("Trg", "Total Triggers"),
    ("Ctb", "Contribution/Trigger (USD)"),
    ("BudR", "BudgetR (USD)"),
    ("MOS", "Min Order Size (contracts)"),
    ("Acc", "Accumulator (USD)"),
    ("AvgEnt", "Average Entry Price"),
    ("Exec", "Executed Orders"),
    ("Fail", "Failed Orders"),
    ("Exp", "Total Exposure (USD)"),
]

TABLE_ROW_H = 26
TABLE_HEADER_H = 26
TABLE_LEGEND_H = 34
TABLE_TITLE_H = 30
TABLE_MARGIN = 16
TABLE_COL_W: Dict[str, int] = {
    "Sym": 150,
    "Trg": 60,
    "Ctb": 90,
    "BudR": 100,
    "MOS": 100,
    "Acc": 90,
    "AvgEnt": 110,
    "Exec": 70,
    "Fail": 70,
    "Exp": 110,
}
TABLE_W = TABLE_MARGIN * 2 + sum(TABLE_COL_W.values())

# ── daily activity report / ntfy constants ────────────────────────────────────

NTFY_TOPIC = "1618091301200506091401140305"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
REPORT_HOUR_UTC = 14
REPORT_MINUTE_UTC = 0
REPORT_MIN_INTERVAL_HOURS = 20

# ── timing & startup test order ───────────────────────────────────────────────

HOURLY_SLEEP_FLOOR_SEC = 5
TEST_ORDER_DISCOUNT = 0.90
TEST_ORDER_WAIT_SEC = 20

# ── failed-symbol tracking ────────────────────────────────────────────────────

FAILED_SYMBOLS: set = set()
_FAILED_LOCK = threading.Lock()


def _xml_escape(s: str) -> str:
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


# ── HTTP server configuration ──────────────────────────────────────────────────

HTTP_HOST = "0.0.0.0"
HTTP_PORT = int(os.getenv("PORT", "8080"))

# ── persistence ──────────────────────────────────────────────────────────────

STATE_FILE = os.getenv(
    "DCA_STATE_FILE",
    "/data/multi_dca_fire_history.json"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s"
)

log = logging.getLogger()

specs: Dict[str, Dict] = {}

# ── contribution-weighting calculation logic ──────────────────────────────────


def fetch_30d_daily_closes(symbol: str) -> Dict[int, float]:
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
    by_date: Dict[datetime.date, float] = {}
    for ts in sorted(price_dict.keys()):
        d = datetime.datetime.fromtimestamp(ts, tz=UTC).date()
        by_date[d] = price_dict[ts]
    return by_date


def align_price_series_by_date(
    price_dicts: Dict[str, Dict[int, float]]
) -> Tuple[List[datetime.date], Dict[str, "collections.OrderedDict[datetime.date, float]"]]:
    date_keyed: Dict[str, Dict[datetime.date, float]] = {
        sym: _to_date_keyed(pd) for sym, pd in price_dicts.items()
    }

    non_empty = [d for d in date_keyed.values() if d]

    if not non_empty:
        return [], {sym: collections.OrderedDict() for sym in price_dicts}

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

    return ordered_dates, aligned


def compute_daily_returns_ordered(series: "collections.OrderedDict") -> List[float]:
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
    n = len(returns)
    if n < 2:
        return 0.0, 0.0
    mean_ret = sum(returns) / n
    variance = sum((r - mean_ret) ** 2 for r in returns) / (n - 1)
    daily_vol = math.sqrt(variance)
    annualized_vol = daily_vol * math.sqrt(365)
    return daily_vol, annualized_vol


def calculate_pearson_correlation(x: List[float], y: List[float]) -> float:
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


def _write_contribution_overview_report(
    symbols: List[str],
    common_dates: List[datetime.date],
    corr_matrix: Dict[str, Dict[str, float]],
    col_averages: Dict[str, float],
    vol_data: Dict[str, Dict[str, float]],
    z_scores: Dict[str, float],
    weights: Dict[str, float],
    order_contrib: Dict[str, float],
    filename: str = OVERVIEW_FILENAME,
):
    try:
        num_assets = len(symbols)

        off_diag = [
            corr_matrix[s1][s2]
            for s1 in symbols
            for s2 in symbols
            if s1 != s2
        ]
        mean_off_diag = sum(off_diag) / len(off_diag) if off_diag else 0.0
        min_off_diag = min(off_diag) if off_diag else 0.0
        max_off_diag = max(off_diag) if off_diag else 0.0

        capped_count = sum(
            1 for v in vol_data.values() if v["annualized"] > CONTRIB_VOL_CAP
        )

        lines = []
        lines.append("=" * 88)
        lines.append(
            f"PORTFOLIO OVERVIEW — {CONTRIB_LOOKBACK_DAYS}-Day Lookback, "
            "Date-Aligned, Iterative Weighting"
        )
        lines.append("=" * 88)
        lines.append(
            f"Generated (UTC): {datetime.datetime.now(UTC).isoformat()}"
        )
        lines.append(f"Requested symbols: {len(SYMBOLS)}")
        lines.append(f"Symbols with usable data: {num_assets}")

        missing = [s for s in SYMBOLS if s not in symbols]
        if missing:
            lines.append(
                f"Symbols excluded (no data or fetch error): "
                f"{', '.join(missing)}"
            )

        if common_dates:
            lines.append(
                f"Common aligned date range: {common_dates[0].isoformat()} "
                f"to {common_dates[-1].isoformat()} "
                f"({len(common_dates)} trading days, "
                f"{len(common_dates) - 1} return observations)"
            )
        else:
            lines.append(
                "Common aligned date range: NONE "
                "(no overlapping dates across symbols)"
            )

        lines.append(
            f"Annualized volatility cap applied to iteration divisor: "
            f"{CONTRIB_VOL_CAP * 100:.0f}% "
            f"({capped_count} of {num_assets} symbol(s) exceeded this "
            "cap and were clamped)"
        )

        lines.append("")
        lines.append("-" * 88)
        lines.append("Correlation Matrix Summary")
        lines.append("-" * 88)
        lines.append(f"Mean off-diagonal correlation : {mean_off_diag:+.4f}")
        lines.append(f"Min off-diagonal correlation   : {min_off_diag:+.4f}")
        lines.append(f"Max off-diagonal correlation   : {max_off_diag:+.4f}")

        lines.append("")
        lines.append("-" * 88)
        lines.append("Per-Symbol Metrics")
        lines.append("-" * 88)
        header = (
            f"{'Symbol':<18} | {'Avg Corr':>9} | {'Ann Vol':>9} | "
            f"{'Vol Used':>9} | {'z Score':>9} | {'Weight':>8} | "
            f"{'Contrib/Trig':>12}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for sym in symbols:
            vol_used = min(vol_data[sym]["annualized"], CONTRIB_VOL_CAP)
            flag = "*" if vol_data[sym]["annualized"] > CONTRIB_VOL_CAP else " "
            lines.append(
                f"{sym:<18} | {col_averages[sym]:>9.4f} | "
                f"{vol_data[sym]['annualized']:>9.4f} | "
                f"{vol_used:>8.4f}{flag} | {z_scores[sym]:>9.4f} | "
                f"{weights[sym] * 100:>7.2f}% | "
                f"{order_contrib[sym]:>11.3f}"
            )
        if capped_count:
            lines.append("")
            lines.append(
                "* Ann Vol exceeded the cap; the capped value "
                "(Vol Used) was applied as the iteration divisor."
            )

        lines.append("")
        lines.append(
            "Sum of weights (sanity check, should equal 1.0000): "
            f"{sum(weights.values()):.4f}"
        )
        lines.append("=" * 88)

        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        log.info(f"contribution-weighting overview report saved to: {filename}")

    except Exception as e:
        log.error(f"failed to write contribution overview report: {e}")


def _build_contribution_svg_heatmap(
    symbols: List[str],
    corr_matrix: Dict[str, Dict[str, float]],
    col_averages: Dict[str, float],
    vol_data: Dict[str, Dict[str, float]],
    z_scores: Dict[str, float],
    weights: Dict[str, float],
    order_contrib: Dict[str, float],
    filename: str = SVG_FILENAME,
):
    try:
        n = len(symbols)
        cell_size = 45
        margin_l, margin_t, margin_r, margin_b = 170, 130, 40, 270
        width = margin_l + n * cell_size + margin_r
        height = margin_t + (n + 5) * cell_size + margin_b

        svg = ET.Element('svg', {
            'xmlns': 'http://www.w3.org/2000/svg',
            'width': str(width),
            'height': str(height),
            'viewBox': f'0 0 {width} {height}',
            'style': 'background-color: #ffffff; font-family: sans-serif;'
        })

        title = ET.SubElement(svg, 'text', {
            'x': str(width / 2), 'y': '40', 'text-anchor': 'middle',
            'font-size': '16', 'font-weight': 'bold', 'fill': '#333333'
        })
        title.text = (
            f"{CONTRIB_LOOKBACK_DAYS}-Day Date-Aligned Correlation Matrix, "
            "Parameter z & Contribution per Trigger"
        )

        for i, sym1 in enumerate(symbols):
            lbl_y = ET.SubElement(svg, 'text', {
                'x': str(margin_l - 10),
                'y': str(margin_t + i * cell_size + cell_size / 1.5),
                'text-anchor': 'end', 'font-size': '9',
                'font-weight': 'bold', 'fill': '#333333'
            })
            lbl_y.text = sym1

            lbl_x = ET.SubElement(svg, 'text', {
                'x': str(margin_l + i * cell_size + cell_size / 2),
                'y': str(margin_t - 10),
                'text-anchor': 'start', 'font-size': '9',
                'font-weight': 'bold', 'fill': '#333333',
                'transform': (
                    f'rotate(-45, {margin_l + i * cell_size + cell_size / 2}, '
                    f'{margin_t - 10})'
                )
            })
            lbl_x.text = sym1

            for j, sym2 in enumerate(symbols):
                val = corr_matrix[sym1][sym2]
                r, g, b = (
                    (255, int(255 * (1 - val)), int(255 * (1 - val)))
                    if val >= 0
                    else (int(255 * (1 + val)), int(255 * (1 + val)), 255)
                )

                ET.SubElement(svg, 'rect', {
                    'x': str(margin_l + j * cell_size),
                    'y': str(margin_t + i * cell_size),
                    'width': str(cell_size - 1), 'height': str(cell_size - 1),
                    'fill': f'rgb({r},{g},{b})', 'rx': '2'
                })

                txt = ET.SubElement(svg, 'text', {
                    'x': str(margin_l + j * cell_size + cell_size / 2),
                    'y': str(margin_t + i * cell_size + cell_size / 1.6),
                    'text-anchor': 'middle', 'font-size': '8',
                    'fill': '#000000' if abs(val) < 0.7 else '#ffffff'
                })
                txt.text = f"{val:.2f}"

        sep_y = margin_t + n * cell_size + 10
        ET.SubElement(svg, 'line', {
            'x1': str(margin_l), 'y1': str(sep_y),
            'x2': str(margin_l + n * cell_size), 'y2': str(sep_y),
            'stroke': '#333333', 'stroke-width': '2'
        })

        summary_rows = [
            ("Relative Portfolio Corr.", col_averages, "#d9534f",
             lambda v: f"{v:.2f}"),
            ("Ann. Volatility (%)",
             {k: v["annualized"] * 100 for k, v in vol_data.items()},
             "#0275d8", lambda v: f"{v:.1f}%"),
            ("Parameter z", z_scores, "#5cb85c", lambda v: f"{v:.3f}"),
            ("Normalized Weight", weights, "#f0ad4e",
             lambda v: f"{v * 100:.1f}%"),
            ("Contrib. per Trigger", order_contrib, "#9c27b0",
             lambda v: f"{v:.3f}"),
        ]

        base_y = margin_t + n * cell_size + 20

        for r_idx, (label, data_dict, color, formatter) in enumerate(summary_rows):
            row_y = base_y + r_idx * (cell_size + 4)
            lbl = ET.SubElement(svg, 'text', {
                'x': str(margin_l - 10), 'y': str(row_y + cell_size / 1.5),
                'text-anchor': 'end', 'font-size': '10',
                'font-weight': 'bold', 'fill': color
            })
            lbl.text = label

            for j, sym in enumerate(symbols):
                val = data_dict[sym]
                ET.SubElement(svg, 'rect', {
                    'x': str(margin_l + j * cell_size), 'y': str(row_y),
                    'width': str(cell_size - 1), 'height': str(cell_size - 1),
                    'fill': '#f9f9f9', 'stroke': color,
                    'stroke-width': '1.5', 'rx': '2'
                })

                txt = ET.SubElement(svg, 'text', {
                    'x': str(margin_l + j * cell_size + cell_size / 2),
                    'y': str(row_y + cell_size / 1.6),
                    'text-anchor': 'middle', 'font-size': '9',
                    'font-weight': 'bold', 'fill': color
                })
                txt.text = formatter(val)

        tree = ET.ElementTree(svg)
        ET.indent(tree, space="  ", level=0)
        tree.write(filename, encoding='utf-8', xml_declaration=True)
        log.info(f"contribution-weighting heatmap SVG saved to: {filename}")

    except Exception as e:
        log.error(f"failed to write contribution heatmap SVG: {e}")


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
                f"[{sym}] fetched {n_candles} candles, short of requested {CONTRIB_LOOKBACK_DAYS}d"
            )
        if closes:
            any_fetch_succeeded = True

        raw_closes[sym] = closes

    if not any_fetch_succeeded:
        log.error("contribution-weighting recompute: ALL daily-close fetches failed")
        return None

    common_dates, aligned = align_price_series_by_date(raw_closes)

    log.info(
        f"contribution-weighting: date-aligned to {len(common_dates)} common UTC dates"
    )

    if len(common_dates) < CONTRIB_MIN_COMMON_DATES:
        log.error(
            f"contribution-weighting recompute: only {len(common_dates)} dates survived"
        )
        return None

    valid_symbols = [sym for sym in symbols if raw_closes.get(sym)]

    if len(valid_symbols) < 2:
        log.error("contribution-weighting recompute: fewer than 2 symbols with usable data")
        return None

    returns_data: Dict[str, List[float]] = {}
    vol_data: Dict[str, Dict[str, float]] = {}

    for sym in valid_symbols:
        series = aligned.get(sym, collections.OrderedDict())
        rets = compute_daily_returns_ordered(series)
        returns_data[sym] = rets
        d_vol, a_vol = calculate_volatility(rets)
        vol_data[sym] = {"daily": d_vol, "annualized": a_vol}

    num_assets = len(valid_symbols)
    corr_matrix: Dict[str, Dict[str, float]] = {s1: {} for s1 in valid_symbols}

    for sym1 in valid_symbols:
        for sym2 in valid_symbols:
            if sym1 == sym2:
                corr_matrix[sym1][sym2] = 1.0
            else:
                corr_matrix[sym1][sym2] = calculate_pearson_correlation(
                    returns_data[sym1], returns_data[sym2]
                )

    col_averages: Dict[str, float] = {}
    for col_sym in valid_symbols:
        if num_assets > 1:
            col_sum = sum(
                corr_matrix[row_sym][col_sym]
                for row_sym in valid_symbols
                if row_sym != col_sym
            )
            col_averages[col_sym] = col_sum / (num_assets - 1)
        else:
            col_averages[col_sym] = 0.0

    vol_divisor: Dict[str, float] = {}
    for sym in valid_symbols:
        raw_ann_vol = vol_data[sym]["annualized"]
        capped = _clamp(raw_ann_vol, 0.0, CONTRIB_VOL_CAP)

        if raw_ann_vol > CONTRIB_VOL_CAP:
            log.warning(
                f"[{sym}] annualized vol {raw_ann_vol:.3f} > cap {CONTRIB_VOL_CAP:.3f} — clamped"
            )

        vol_divisor[sym] = max(capped, CONTRIB_VOL_FLOOR)

    weights: Dict[str, float] = {sym: 1.0 / num_assets for sym in valid_symbols}

    for _ in range(CONTRIB_ITERATIONS):
        new_weights: Dict[str, float] = {}
        for sym1 in valid_symbols:
            w_sum = sum(
                (1 - corr_matrix[sym1][sym2]) * (1 - weights[sym2])
                for sym2 in valid_symbols
                if sym1 != sym2
            )
            new_weights[sym1] = (1.0 / vol_divisor[sym1]) * w_sum

        total_new_w = sum(new_weights.values())
        if total_new_w > 0:
            weights = {sym: w / total_new_w for sym, w in new_weights.items()}

    z_scores: Dict[str, float] = {}
    for sym1 in valid_symbols:
        w_sum = sum(
            (1 - corr_matrix[sym1][sym2]) * (1 - weights[sym2])
            for sym2 in valid_symbols
            if sym1 != sym2
        )
        z_scores[sym1] = (1.0 / vol_divisor[sym1]) * w_sum

    contrib_per_trigger_usd: Dict[str, float] = {
        sym: BASE_TRIGGER_USD * num_assets * weights[sym]
        for sym in valid_symbols
    }

    for sym in symbols:
        if sym not in contrib_per_trigger_usd:
            log.warning(
                f"[{sym}] excluded from iterative weighting — using flat ${BASE_TRIGGER_USD:.2f}"
            )
            contrib_per_trigger_usd[sym] = BASE_TRIGGER_USD

    log.info(
        "contribution-weighting recompute complete: "
        + ", ".join(
            f"{sym}=${contrib_per_trigger_usd[sym]:.3f}"
            for sym in symbols
        )
    )

    _write_contribution_overview_report(
        valid_symbols, common_dates, corr_matrix, col_averages,
        vol_data, z_scores, weights, contrib_per_trigger_usd
    )
    _build_contribution_svg_heatmap(
        valid_symbols, corr_matrix, col_averages, vol_data,
        z_scores, weights, contrib_per_trigger_usd
    )

    return contrib_per_trigger_usd


# ── shared state ──────────────────────────────────────────────────────────────


class SharedState:

    def __init__(self):
        self._lock = threading.Lock()
        self._svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' width='600' height='100'>"
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
                "<svg xmlns='http://www.w3.org/2000/svg' width='400' height='60'>"
                "<text x='10' y='30' font-family='Courier New'>Loading chart...</text></svg>"
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
        "budget_r": {},
        "last_trigger_low": {},
        "trigger_count": {},
        "accumulator": {},
        "last_accrual_date": {},
        "last_seen_minute": {},
        "daily_stats": {},
        "last_report_sent_at": None,
        "contrib_per_trigger_usd": {},
        "contrib_last_computed_date": None,
        "lifetime_orders_ok": {},
        "lifetime_orders_failed": {},
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


def get_budget_r(sym: str) -> float:
    return float(STATE_DATA["budget_r"].get(sym, 0.0))


def init_budget_r_if_absent(sym: str):
    with _STATE_DATA_LOCK:
        if sym in STATE_DATA["budget_r"]:
            return
        starting_value = float(STATE_DATA["budget"].get(sym, 0.0))
        STATE_DATA["budget_r"][sym] = starting_value
        _persist()
        log.info(f"[{sym}] BudgetR initialized to current budget: ${starting_value:.2f}")


def get_last_trigger_low(sym: str) -> Optional[float]:
    v = STATE_DATA["last_trigger_low"].get(sym)
    return float(v) if v is not None else None


def get_trigger_count(sym: str) -> int:
    return int(STATE_DATA["trigger_count"].get(sym, 0))


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

        contrib = float(
            STATE_DATA["contrib_per_trigger_usd"].get(sym, TRIGGER_STACK_USD)
        )
        accrual = BUDGET_DAILY_ACCRUAL_MULT * contrib

        prev_budget = get_budget(sym)
        new_budget = prev_budget + accrual
        STATE_DATA["budget"][sym] = new_budget
        STATE_DATA["last_accrual_date"][sym] = today.isoformat()

        _persist()

        log.info(
            f"[{sym}] daily budget accrual: {prev_budget:.2f} + {accrual:.2f} = {new_budget:.2f}"
        )


def update_budget_r_on_trigger(sym: str, candle_low: float) -> float:
    with _STATE_DATA_LOCK:
        prev_low = get_last_trigger_low(sym)

        is_new_trigger_low = (
            prev_low is not None and candle_low < prev_low
        )

        if is_new_trigger_low:
            new_budget_r = get_budget_r(sym)
            log.info(
                f"[{sym}] BudgetR unchanged (low {candle_low:.4f} < prev {prev_low:.4f}): BudgetR=${new_budget_r:.2f}"
            )
        else:
            new_budget_r = get_budget(sym)
            STATE_DATA["budget_r"][sym] = new_budget_r
            log.info(
                f"[{sym}] BudgetR reset to live budget: BudgetR=${new_budget_r:.2f}"
            )

        STATE_DATA["last_trigger_low"][sym] = candle_low
        STATE_DATA["trigger_count"][sym] = get_trigger_count(sym) + 1

        _persist()
        return new_budget_r


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

    log.info(f"contribution-weighting recompute due (last={last}, today={today})")
    result = compute_contribution_weights(SYMBOLS)

    if result is None:
        log.error("contribution-weighting recompute FAILED — using flat fallback")
        result = {sym: BASE_TRIGGER_USD for sym in SYMBOLS}

    set_contrib_per_trigger_usd(result, today)


def compute_order_size_usd(sym: str, budget_r: float) -> float:
    contribution = get_contrib_per_trigger_usd(sym)
    order_size_usd = contribution + (budget_r / ORDER_SIZE_BUDGET_R_DIVISOR)

    log.info(
        f"[{sym}] order-sizing: contrib ${contribution:.3f} + "
        f"(BudgetR ${budget_r:.2f} / {ORDER_SIZE_BUDGET_R_DIVISOR:.0f}) = ${order_size_usd:.4f}"
    )

    return order_size_usd


def add_order_size_to_accumulator(sym: str, order_size_usd: float) -> float:
    with _STATE_DATA_LOCK:
        prev = float(STATE_DATA["accumulator"].get(sym, 0.0))
        new = prev + order_size_usd
        STATE_DATA["accumulator"][sym] = new
        _persist()
        return new


def get_accumulator(sym: str) -> float:
    return float(STATE_DATA["accumulator"].get(sym, 0.0))


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
        log.info(f"[{sym}] budget spent ${usd:.2f}: {prev:.2f} -> {new:.2f}")


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


def executed_orders_for_sym(sym: str) -> List[Dict]:
    return [
        o for o in STATE_DATA["orders"]
        if o.get("symbol") == sym and "reference_window" in o
    ]


def record_lifetime_order_outcome(sym: str, success: bool):
    with _STATE_DATA_LOCK:
        key = "lifetime_orders_ok" if success else "lifetime_orders_failed"
        STATE_DATA[key][sym] = int(STATE_DATA[key].get(sym, 0)) + 1
        _persist()


def get_lifetime_order_counts(sym: str) -> Tuple[int, int]:
    ok = int(STATE_DATA["lifetime_orders_ok"].get(sym, 0))
    failed = int(STATE_DATA["lifetime_orders_failed"].get(sym, 0))
    return ok, failed


# ── daily stats ───────────────────────────────────────────────────────────────


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


def _http(method, url, headers=None, data=None, params=None):
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


def mexc(method, endpoint, params=None, body=None):
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
    rows = mexc("GET", "/api/v1/contract/detail").get("data") or []

    if not rows:
        log.error("empty contract detail response from MEXC — flagging all symbols failed")
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
        mv = float(match.get("minVol", vu))  # MOS logic: minimum order contracts
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
            "mv": mv,
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
    return specs.get(sym, {}).get("mv", specs.get(sym, {}).get("vu", 1.0))


def _effective_leverage(sym: str) -> int:
    max_lev = specs.get(sym, {}).get("max_lev")

    if max_lev is None:
        return LEVERAGE

    if max_lev < LEVERAGE:
        log.info(
            f"[{sym}] global leverage {LEVERAGE}x exceeds exchange max {max_lev:.0f}x — using {max_lev:.0f}x"
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
            f"[{sym}] size {vol} < min {_mos(sym)} (${usd_amount:.2f}) — order skipped"
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
        log.error(f"[{sym}] unexpected 'data' shape: {data!r}")
        return None

    oid = data.get("orderId")

    if not oid:
        log.error(f"[{sym}] order/create succeeded but no 'orderId' in data: {data!r}")
        return None

    oid = str(oid)

    log.info(
        f"[{sym}] limit LONG {_rfmt_vol(sym, vol)} @ {_rfmt_price(sym, limit_price)} "
        f"leverage={leverage}x id={oid}"
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

    def rolling_low(
        self,
        window_minutes: int,
        exclude_latest: bool = False
    ) -> Optional[float]:
        with self.lock:
            if not self.bars:
                return None

            cutoff = int(time.time()) - window_minutes * 60

            bars = self.bars

            if exclude_latest:
                bars = list(bars)[:-1]

            window = [b["l"] for b in bars if b["t"] >= cutoff]

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
            log.info(f"[{sym}] seed chunk returned no bars — stopping")
            break

        for b in bars:
            all_bars[b["t"]] = b

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
        f"[{sym}] minute buffer seeded: {MINUTE_BUFFERS[sym].size()} bars spanning ~{span_days:.1f}d"
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
        log.warning(f"[{sym}] no closed 1-minute candle available yet — skipping")
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

    ref_low = buf.rolling_low(ref_window, exclude_latest=True)

    if ref_low is None:
        log.warning(f"[{sym}] insufficient prior-candle data for {ref_label} low")
        return None

    triggered = candle_low < ref_low

    log.info(
        f"[{sym}] minute check {candle_dt.isoformat()}: "
        f"low={candle_low:.4f} {ref_label}Low(prior)={ref_low:.4f} "
        f"budget={budget:.2f} failed={is_failed(sym)} trigger={triggered}"
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

    budget_r_now = update_budget_r_on_trigger(sym, candle_low)

    if failed:
        log.info(
            f"[{sym}] TRIGGER (failed symbol, marker-only) @ price={candle_low:.4f} BudgetR=${budget_r_now:.2f}"
        )
        return

    order_size_usd = compute_order_size_usd(sym, budget_r_now)
    pending = add_order_size_to_accumulator(sym, order_size_usd)

    log.info(
        f"[{sym}] TRIGGER — accumulator now ${pending:.2f} (+$${order_size_usd:.4f}) @ price={candle_low:.4f}"
    )

    vol_at_price = _contracts(sym, pending, candle_low)

    if vol_at_price < _mos(sym):
        log.info(
            f"[{sym}] accumulator ${pending:.2f} below min size ({_mos(sym)} contracts @ {candle_low:.4f}) — stacking"
        )
        return

    log.info(
        f"[{sym}] accumulator ${pending:.2f} reaches min size — attempting limit LONG @ {candle_low:.4f}"
    )

    oid = place_long(
        sym,
        candle_low,
        candle_low,
        pending
    )

    if oid == "SKIP" or oid is None:
        record_attempt_stat(sym, candle_low, success=False)
        record_lifetime_order_outcome(sym, success=False)
        log.warning(f"[{sym}] placement unfulfilled — carrying accumulator forward")
        return

    record_attempt_stat(sym, candle_low, success=True, usd_if_success=pending)
    record_lifetime_order_outcome(sym, success=True)
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
            log.error(f"[{sym}] minute check failed: {e}", exc_info=True)


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
            f"{window_start_dt.strftime('%Y-%m-%d %H:%M')} UTC to {now_utc.strftime('%Y-%m-%d %H:%M')} UTC"
        )
    else:
        header = f"Daily Activity Report — as of {now_utc.strftime('%Y-%m-%d %H:%M')} UTC"

    contrib_date = get_contrib_last_computed_date()
    contrib_note = (
        f"Contribution weights last computed: {contrib_date.isoformat()}"
        if contrib_date is not None
        else "Contribution weights: not yet computed"
    )

    active_count = sum(1 for sym in SYMBOLS if not is_failed(sym))
    failed_count_total = len(SYMBOLS) - active_count
    roster_note = f"Symbols covered: {len(SYMBOLS)} total ({active_count} trading, {failed_count_total} failed/excluded)"

    lines = [header, contrib_note, roster_note, ""]

    for sym in SYMBOLS:
        stats = get_daily_stats_snapshot(sym)
        triggers = stats["triggers"]
        order_value = stats["order_value_usd"]
        ok = stats["orders_ok"]
        failed_count = stats["orders_failed"]
        attempt_count = stats["attempt_count"]
        attempt_sum = stats["attempt_price_sum"]

        avg_price = attempt_sum / attempt_count if attempt_count > 0 else None
        avg_price_str = f"{avg_price:,.4f}" if avg_price is not None else "n/a"

        excluded_note = " [EXCLUDED — not traded]" if is_failed(sym) else ""
        contrib = get_contrib_per_trigger_usd(sym)

        lines.append(
            f"{sym}: triggers={triggers}  order_value=${order_value:,.2f}  "
            f"ok={ok}  failed={failed_count}  avg_attempt_price={avg_price_str}  "
            f"contrib/trigger=${contrib:.3f}{excluded_note}"
        )

    return "\n".join(lines)


def maybe_send_daily_report(now_utc: datetime.datetime):
    at_or_after_report_time = (
        (now_utc.hour, now_utc.minute) >= (REPORT_HOUR_UTC, REPORT_MINUTE_UTC)
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
        log.error("daily report send failed — will retry next minute")


# ── startup test orders ───────────────────────────────────────────────────────


def _open_test_order(sym: str) -> Optional[Dict]:
    if sym not in specs:
        return None

    try:
        mark = get_mark(sym)
        if mark <= 0:
            flag_failed(sym, f"invalid mark price ({mark}) at startup test")
            return None

        test_price = mark * TEST_ORDER_DISCOUNT
        min_vol = _mos(sym)

        log.info(
            f"[{sym}] test order OPEN: mark={mark:.4f} limit={test_price:.4f} "
            f"vol={min_vol} leverage={_effective_leverage(sym)}x"
        )

        oid = place_long_min_size(sym, test_price)
        if not oid:
            flag_failed(sym, "startup test order creation rejected by MEXC")
            return None

        return {"symbol": sym, "order_id": oid, "limit_price": test_price}
    except Exception as e:
        flag_failed(sym, f"exception during test order creation: {e}")
        return None


def run_startup_test_orders():
    log.info("=== STARTING BATCH TEST ORDERS FOR ALL SYMBOLS ===")
    opened_orders = []

    for sym in SYMBOLS:
        if is_failed(sym):
            continue
        res = _open_test_order(sym)
        if res:
            opened_orders.append(res)

    if not opened_orders:
        log.info("No test orders opened.")
        return

    log.info(f"Waiting {TEST_ORDER_WAIT_SEC}s for test orders...")
    time.sleep(TEST_ORDER_WAIT_SEC)

    log.info("=== CHECKING AND CANCELING STARTUP TEST ORDERS ===")
    for item in opened_orders:
        sym = item["symbol"]
        oid = item["order_id"]

        if is_filled(sym, oid):
            log.info(f"[{sym}] test order id={oid} FILLED during wait")
        else:
            cancel_ok = cancel_order(sym, oid)
            if not cancel_ok:
                flag_failed(sym, f"failed to cancel test order id={oid}")
            else:
                log.info(f"[{sym}] test order id={oid} successfully cancelled")

    log.info("=== STARTUP TEST ORDERS COMPLETE ===")


# ── chart & overview rendering ────────────────────────────────────────────────


def render_symbol_chart_svg(sym: str) -> str:
    buf = MINUTE_BUFFERS[sym]
    bars = buf.snapshot()
    if not bars:
        return (
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{CHART_W}' height='{CHART_H}'>"
            f"<text x='20' y='30' fill='#000000' font-family='Courier New'>No chart data for {_xml_escape(sym)}</text>"
            f"</svg>"
        )

    resampled = resample_ohlc(bars, CHART_RESAMPLE_MIN)
    if not resampled:
        return (
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{CHART_W}' height='{CHART_H}'>"
            f"<text x='20' y='30' fill='#000000' font-family='Courier New'>No resampled data for {_xml_escape(sym)}</text>"
            f"</svg>"
        )

    lows = [b["l"] for b in resampled]
    highs = [b["h"] for b in resampled]
    min_p = min(lows)
    max_p = max(highs)
    if min_p == max_p:
        min_p -= 1.0
        max_p += 1.0
    p_range = max_p - min_p

    plot_w = CHART_W - CHART_MARGIN_L - CHART_MARGIN_R
    plot_h = CHART_H - CHART_MARGIN_T - CHART_MARGIN_B

    def y_coord(price: float) -> float:
        return CHART_MARGIN_T + plot_h * (1.0 - (price - min_p) / p_range)

    n_candles = len(resampled)
    candle_w = max(1.0, (plot_w / max(n_candles, 1)) * 0.7)
    step_w = plot_w / max(n_candles, 1)

    elements = []
    elements.append(f"<rect width='{CHART_W}' height='{CHART_H}' fill='#ffffff'/>")

    budget = get_budget(sym)
    ref_win = ROLL_MINUTES_SHORT if budget >= 0 else ROLL_MINUTES_LONG
    ref_label = "2d" if budget >= 0 else "9d"
    ref_low = buf.rolling_low(ref_win, exclude_latest=False)
    ref_str = f"{ref_low:.4f}" if ref_low is not None else "n/a"
    failed_str = " [FAILED]" if is_failed(sym) else ""

    title_text = (
        f"{_xml_escape(sym)}{failed_str} — 15m Resampled ({CHART_MINUTES // 1440}d) | "
        f"Budget: ${budget:.2f} | {ref_label} Ref Low: {ref_str}"
    )
    elements.append(
        f"<text x='{CHART_MARGIN_L}' y='24' font-family='Courier New' "
        f"font-size='14' font-weight='bold' fill='#000000'>{title_text}</text>"
    )

    y_ticks = 5
    for i in range(y_ticks + 1):
        price_val = min_p + (p_range * i / y_ticks)
        y = y_coord(price_val)
        elements.append(
            f"<line x1='{CHART_MARGIN_L}' y1='{y:.1f}' x2='{CHART_W - CHART_MARGIN_R}' y2='{y:.1f}' "
            f"stroke='#e0e0e0' stroke-width='1'/>"
        )
        elements.append(
            f"<text x='{CHART_MARGIN_L - 8}' y='{y + 4:.1f}' font-family='Courier New' "
            f"font-size='10' fill='#555555' text-anchor='end'>{price_val:.4f}</text>"
        )

    if ref_low is not None and min_p <= ref_low <= max_p:
        ref_y = y_coord(ref_low)
        elements.append(
            f"<line x1='{CHART_MARGIN_L}' y1='{ref_y:.1f}' x2='{CHART_W - CHART_MARGIN_R}' y2='{ref_y:.1f}' "
            f"stroke='#0000ff' stroke-width='1.5' stroke-dasharray='4,4'/>"
        )

    for idx, b in enumerate(resampled):
        cx = CHART_MARGIN_L + idx * step_w + step_w / 2.0
        yo = y_coord(b["o"])
        yh = y_coord(b["h"])
        yl = y_coord(b["l"])
        yc = y_coord(b["c"])

        color = "#2e7d32" if b["c"] >= b["o"] else "#c62828"

        elements.append(
            f"<line x1='{cx:.1f}' y1='{yh:.1f}' x2='{cx:.1f}' y2='{yl:.1f}' "
            f"stroke='{color}' stroke-width='1'/>"
        )
        body_top = min(yo, yc)
        body_h = max(abs(yc - yo), 1.0)
        elements.append(
            f"<rect x='{cx - candle_w / 2.0:.1f}' y='{body_top:.1f}' "
            f"width='{candle_w:.1f}' height='{body_h:.1f}' fill='{color}'/>"
        )

    with _STATE_DATA_LOCK:
        triggers_for_sym = [t for t in STATE_DATA["triggers"] if t.get("symbol") == sym]
        orders_for_sym = [o for o in STATE_DATA["orders"] if o.get("symbol") == sym and "reference_window" in o]

    res_ts = [b["t"] for b in resampled]
    min_chart_ts = res_ts[0] if res_ts else 0
    max_chart_ts = res_ts[-1] + CHART_RESAMPLE_MIN * 60 if res_ts else 0

    for trg in triggers_for_sym:
        t_ts = _safe_ts(trg.get("candle_time"))
        t_price = float(trg.get("price", 0))
        if t_ts and min_chart_ts <= t_ts <= max_chart_ts and t_price:
            frac = (t_ts - min_chart_ts) / max(max_chart_ts - min_chart_ts, 1)
            cx = CHART_MARGIN_L + frac * plot_w
            cy = y_coord(t_price)
            elements.append(
                f"<line x1='{cx - 4:.1f}' y1='{cy:.1f}' x2='{cx + 4:.1f}' y2='{cy:.1f}' "
                f"stroke='#ff6d00' stroke-width='2'/>"
            )

    for ord_rec in orders_for_sym:
        o_ts = _safe_ts(ord_rec.get("candle_time")) or _safe_ts(ord_rec.get("timestamp"))
        o_price = float(ord_rec.get("limit_price", 0))
        if o_ts and min_chart_ts <= o_ts <= max_chart_ts and o_price:
            frac = (o_ts - min_chart_ts) / max(max_chart_ts - min_chart_ts, 1)
            cx = CHART_MARGIN_L + frac * plot_w
            cy = y_coord(o_price)
            elements.append(
                f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='4' fill='#2962ff' stroke='#ffffff' stroke-width='1'/>"
            )

    svg_content = "\n".join(elements)
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{CHART_W}' height='{CHART_H}'>\n"
        f"{svg_content}\n"
        f"</svg>"
    )


def render_svg() -> str:
    num_rows = len(SYMBOLS)
    total_h = (
        TABLE_TITLE_H
        + TABLE_LEGEND_H
        + TABLE_HEADER_H
        + num_rows * TABLE_ROW_H
        + TABLE_MARGIN * 2
    )

    now_str = datetime.datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    elements = []
    elements.append(
        f"<rect width='{TABLE_W}' height='{total_h}' fill='#ffffff'/>"
    )

    title_y = TABLE_MARGIN + 20
    elements.append(
        f"<text x='{TABLE_MARGIN}' y='{title_y}' font-family='Courier New, monospace' "
        f"font-size='16' font-weight='bold' fill='#000000'>"
        f"MultiLongDCA Bot Overview — {now_str}</text>"
    )

    legend_y = title_y + TABLE_LEGEND_H - 10
    legend_str = " | ".join(f"{abbr}:{full}" for abbr, full in TABLE_COLUMNS)
    elements.append(
        f"<text x='{TABLE_MARGIN}' y='{legend_y}' font-family='Courier New, monospace' "
        f"font-size='11' fill='#333333'>{_xml_escape(legend_str)}</text>"
    )

    table_top_y = title_y + TABLE_LEGEND_H

    elements.append(
        f"<rect x='{TABLE_MARGIN}' y='{table_top_y}' width='{sum(TABLE_COL_W.values())}' "
        f"height='{TABLE_HEADER_H}' fill='#f0f0f0' stroke='#cccccc' stroke-width='1'/>"
    )

    curr_x = TABLE_MARGIN
    for abbr, _ in TABLE_COLUMNS:
        col_w = TABLE_COL_W[abbr]
        elements.append(
            f"<text x='{curr_x + 6}' y='{table_top_y + 18}' font-family='Courier New, monospace' "
            f"font-size='12' font-weight='bold' fill='#000000'>{abbr}</text>"
        )
        curr_x += col_w

    row_top = table_top_y + TABLE_HEADER_H
    for idx, sym in enumerate(SYMBOLS):
        y_pos = row_top + idx * TABLE_ROW_H

        elements.append(
            f"<rect x='{TABLE_MARGIN}' y='{y_pos}' width='{sum(TABLE_COL_W.values())}' "
            f"height='{TABLE_ROW_H}' fill='#ffffff' stroke='#e0e0e0' stroke-width='1'/>"
        )

        failed = is_failed(sym)
        sym_disp = f"{sym}[F]" if failed else sym
        trg = get_trigger_count(sym)
        ctb = get_contrib_per_trigger_usd(sym)
        bud_r = get_budget_r(sym)
        mos_val = _mos(sym)
        acc = get_accumulator(sym)

        exec_orders = executed_orders_for_sym(sym)
        exec_cnt_hist, fail_cnt_hist = get_lifetime_order_counts(sym)

        if exec_orders:
            avg_p = sum(float(o.get("limit_price", 0)) for o in exec_orders) / len(exec_orders)
            avg_p_str = f"${avg_p:.4f}"
            exp_val = sum(float(o.get("usd", 0)) for o in exec_orders)
            exp_str = f"${exp_val:.2f}"
        else:
            avg_p_str = "n/a"
            exp_str = "$0.00"

        cell_values = {
            "Sym":    sym_disp,
            "Trg":    str(trg),
            "Ctb":    f"${ctb:.3f}",
            "BudR":   f"${bud_r:.2f}",
            "MOS":    f"{mos_val:g}",
            "Acc":    f"${acc:.2f}",
            "AvgEnt": avg_p_str,
            "Exec":   str(exec_cnt_hist),
            "Fail":   str(fail_cnt_hist),
            "Exp":    exp_str,
        }

        curr_x = TABLE_MARGIN
        for abbr, _ in TABLE_COLUMNS:
            col_w = TABLE_COL_W[abbr]
            val = cell_values[abbr]

            if abbr == "Sym":
                elements.append(
                    f"<a href='/chart/{sym}.svg'>"
                    f"<text x='{curr_x + 6}' y='{y_pos + 18}' font-family='Courier New, monospace' "
                    f"font-size='12' fill='#000000' text-decoration='underline'>{_xml_escape(val)}</text>"
                    f"</a>"
                )
            else:
                elements.append(
                    f"<text x='{curr_x + 6}' y='{y_pos + 18}' font-family='Courier New, monospace' "
                    f"font-size='12' fill='#000000'>{_xml_escape(val)}</text>"
                )
            curr_x += col_w

    svg_body = "\n".join(elements)
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{TABLE_W}' height='{total_h}'>\n"
        f"{svg_body}\n"
        f"</svg>"
    )


# ── HTTP request handler ──────────────────────────────────────────────────────


class RequestHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/chart.svg"):
            content = STATE.get_svg().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        elif path.startswith("/chart/"):
            sym = path[7:].rstrip(".svg")
            if sym in SYMBOLS:
                content = STATE.get_chart_svg(sym).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404, "Symbol not found")

        elif path == "/health":
            body = json.dumps({"status": STATE.get_status()}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        pass


def run_http_server():
    server_address = (HTTP_HOST, HTTP_PORT)
    httpd = http.server.HTTPServer(server_address, RequestHandler)
    log.info(f"HTTP server running on {HTTP_HOST}:{HTTP_PORT}")
    httpd.serve_forever()


# ── main engine loop ──────────────────────────────────────────────────────────


def update_all_charts():
    try:
        main_svg = render_svg()
        STATE.set_svg(main_svg)
    except Exception as e:
        log.error(f"failed to render main overview SVG: {e}")

    for sym in SYMBOLS:
        try:
            chart_svg = render_symbol_chart_svg(sym)
            STATE.set_chart_svg(sym, chart_svg)
        except Exception as e:
            log.error(f"failed to render chart SVG for {sym}: {e}")


def main_loop():
    STATE.set_status("starting")

    log.info("Loading MEXC contract specifications...")
    load_specs()

    for sym in SYMBOLS:
        init_budget_r_if_absent(sym)

    log.info("Seeding 1-minute OHLC buffers for all symbols...")
    for sym in SYMBOLS:
        try:
            seed_minute_buffer(sym)
        except Exception as e:
            log.error(f"[{sym}] buffer seeding failed: {e}")

    today = datetime.datetime.now(UTC).date()
    recompute_contributions_if_due(today)

    log.info("Running startup test orders...")
    run_startup_test_orders()

    STATE.set_status("running")
    log.info("Entering minute-trigger engine main loop...")

    update_all_charts()

    while True:
        try:
            now_utc = datetime.datetime.now(UTC)

            if now_utc.second == MINUTE_CHECK_SECOND:
                recompute_contributions_if_due(now_utc.date())
                run_minute_checks(now_utc)
                maybe_send_daily_report(now_utc)
                update_all_charts()

                time.sleep(1.0)
            else:
                time.sleep(0.2)

        except Exception as e:
            log.error(f"Error in main loop: {e}", exc_info=True)
            time.sleep(1.0)


if __name__ == "__main__":
    server_thread = threading.Thread(target=run_http_server, daemon=True)
    server_thread.start()

    main_loop()