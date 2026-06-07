"""
One-off backfill of data/history.json from ETF daily closes.

Runs through every market in scrape_msci.MARKETS, pulls max ETF history via
yfinance, walks forward weekly (or daily) from ~18 months ago, and reconstructs
each point's (day, mtd, threeMtd, ytd, oneYr) just like the live scraper does.

Usage:
    python scripts/backfill_history.py            # weekly cadence, default
    python scripts/backfill_history.py --daily    # daily (heavier)
    python scripts/backfill_history.py --months 12

Run this once before the first GitHub Action commits a fresh history.json so
the dashboard has real data for sparklines / Compare from day one.
"""

import argparse
import json
import sys
import time
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

from scrape_msci import (
    MARKETS,
    HISTORY_CAP,
    HISTORY_SCHEMA_VERSION,
    HISTORY_METRIC_KEYS,
)


def _anchor_close(closes_index_dates, closes_values, target_date):
    """Closest close on or before target_date; fall back to closest overall."""
    if not closes_index_dates:
        return None
    # Prefer the most recent close on or before target_date.
    le = [(d, v) for d, v in zip(closes_index_dates, closes_values) if d <= target_date]
    if le:
        return le[-1][1]
    # Otherwise, the earliest close we have (the ticker is newer than target_date).
    return closes_values[0]


def _ann_return(last_close, anchor, years):
    if anchor is None or anchor <= 0 or years <= 0:
        return None
    return round(((last_close / anchor) ** (1.0 / years) - 1) * 100, 2)


def reconstruct_point(closes_index_dates, closes_values, as_of_date):
    """Reconstruct a single history point as if scrape ran on as_of_date."""
    upto = [(d, v) for d, v in zip(closes_index_dates, closes_values) if d <= as_of_date]
    if len(upto) < 2:
        return None

    last_date, last_close = upto[-1]
    prev_close = upto[-2][1]

    month_start = as_of_date.replace(day=1)
    prev_month = [(d, v) for d, v in upto if d < month_start]
    month_anchor = prev_month[-1][1] if prev_month else upto[0][1]

    three_m_target = as_of_date - timedelta(days=91)
    three_m_anchor = _anchor_close([d for d, _ in upto], [v for _, v in upto], three_m_target)

    year_start = as_of_date.replace(month=1, day=1)
    prev_year = [(d, v) for d, v in upto if d < year_start]
    year_anchor = prev_year[-1][1] if prev_year else upto[0][1]

    one_yr_anchor = _anchor_close([d for d, _ in upto], [v for _, v in upto], as_of_date - timedelta(days=365))

    point = {'d': as_of_date.isoformat()}
    point['day'] = round((last_close / prev_close - 1) * 100, 2) if prev_close else None
    point['mtd'] = round((last_close / month_anchor - 1) * 100, 2) if month_anchor else None
    point['threeMtd'] = round((last_close / three_m_anchor - 1) * 100, 2) if three_m_anchor else None
    point['ytd'] = round((last_close / year_anchor - 1) * 100, 2) if year_anchor else None
    point['oneYr'] = round((last_close / one_yr_anchor - 1) * 100, 2) if one_yr_anchor else None

    # Long-horizon metrics for the Compare trendlines. 6M is a simple return;
    # 3/5/10Y are annualised and only emitted when enough price history exists
    # before this date, so we never pass off a since-inception return as a true
    # multi-year figure (mirrors the live scraper's yrs_available guard).
    dts = [d for d, _ in upto]
    vals = [v for _, v in upto]
    six_m_anchor = _anchor_close(dts, vals, as_of_date - timedelta(days=183))
    point['sixMtd'] = round((last_close / six_m_anchor - 1) * 100, 2) if six_m_anchor else None
    avail_years = (last_date - upto[0][0]).days / 365.25
    if avail_years >= 3:
        point['threeYr'] = _ann_return(last_close, _anchor_close(dts, vals, as_of_date - timedelta(days=365 * 3)), 3)
    if avail_years >= 5:
        point['fiveYr'] = _ann_return(last_close, _anchor_close(dts, vals, as_of_date - timedelta(days=365 * 5)), 5)
    if avail_years >= 10:
        point['tenYr'] = _ann_return(last_close, _anchor_close(dts, vals, as_of_date - timedelta(days=365 * 10)), 10)

    point['close'] = round(last_close, 4)

    # Strip Nones — keep history file lean.
    return {k: v for k, v in point.items() if v is not None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--months', type=int, default=18,
                        help='How many months of history to reconstruct (default 18)')
    parser.add_argument('--daily', action='store_true',
                        help='Reconstruct daily (heavier); default is weekly cadence.')
    parser.add_argument('--out', type=str, default=None,
                        help='Override output path (default: data/history.json)')
    args = parser.parse_args()

    try:
        import yfinance as yf
    except ImportError:
        print('[backfill] yfinance not installed. Run: pip install -r scripts/requirements.txt',
              file=sys.stderr)
        return 1

    try:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate='chrome124')
    except ImportError:
        session = None

    repo_root = Path(__file__).resolve().parents[1]
    out_path = Path(args.out) if args.out else repo_root / 'data' / 'history.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)

    today = date.today()
    cutoff = today - timedelta(days=args.months * 31)
    cadence = timedelta(days=1) if args.daily else timedelta(days=7)

    series = {}

    for country, meta in MARKETS.items():
        ticker_sym = meta['etf']
        try:
            t = yf.Ticker(ticker_sym, session=session) if session else yf.Ticker(ticker_sym)
            hist = t.history(period='max', interval='1d', auto_adjust=True)
            if hist is None or len(hist) == 0:
                print(f'[backfill] {country:18s} {ticker_sym:6s}  no data')
                continue
            closes = hist['Close'].dropna().sort_index()
            if len(closes) < 30:
                print(f'[backfill] {country:18s} {ticker_sym:6s}  only {len(closes)} closes')
                continue

            # Materialise to plain Python lists for the inner loop — much faster
            # than slicing pandas Series for every probe date.
            idx_dates = [d.date() for d in closes.index]
            values = [float(v) for v in closes.values]

            # Walk from cutoff to today at the chosen cadence.
            points = []
            cursor = cutoff
            while cursor <= today:
                pt = reconstruct_point(idx_dates, values, cursor)
                if pt is not None:
                    points.append(pt)
                cursor = cursor + cadence

            # Cap to the most recent HISTORY_CAP points.
            if len(points) > HISTORY_CAP:
                points = points[-HISTORY_CAP:]

            if points:
                series[country] = points
                print(f'[backfill] {country:18s} {ticker_sym:6s}  {len(points)} points')
        except Exception as e:
            print(f'[backfill] {country:18s} {ticker_sym:6s}  ERROR: {type(e).__name__}: {e}',
                  file=sys.stderr)
        time.sleep(0.8)  # be gentle with Yahoo to avoid 429 rate-limiting

    output = {
        'schemaVersion': HISTORY_SCHEMA_VERSION,
        'lastUpdated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'series': series,
    }
    out_path.write_text(json.dumps(output, separators=(',', ':')))
    print(f'\n[backfill] wrote {out_path}')
    print(f'           {len(series)} markets, '
          f'{sum(len(v) for v in series.values())} total points')
    return 0


if __name__ == '__main__':
    sys.exit(main())
