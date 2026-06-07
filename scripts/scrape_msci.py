"""
MSCI World Markets Scraper (v5)
================================

Fixes from v4:
  - Position-based column parsing (was: flat number list, broken for rows
    where Index Code + Last price are both numeric).
    Column structure: [Name, Code, Last, Day, MTD, 3MTD, YTD, 1Yr, ...]
  - Change Market dropdown to "All Country (DM+EM)" before clicking Search
    so we get all 44 countries in one pass instead of only the 23 DM defaults.
"""

import asyncio
import json
import sys
import re
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

# =========================================================================
# COUNTRY METADATA
# =========================================================================
MARKETS = {
    'USA':           {'etf': 'EUSA', 'region': 'Americas',     'type': 'Developed'},
    'Canada':        {'etf': 'EWC',  'region': 'Americas',     'type': 'Developed'},
    'Australia':     {'etf': 'EWA',  'region': 'Asia-Pacific', 'type': 'Developed'},
    'Hong Kong':     {'etf': 'EWH',  'region': 'Asia-Pacific', 'type': 'Developed'},
    'Japan':         {'etf': 'EWJ',  'region': 'Asia-Pacific', 'type': 'Developed'},
    'New Zealand':   {'etf': 'ENZL', 'region': 'Asia-Pacific', 'type': 'Developed'},
    'Singapore':     {'etf': 'EWS',  'region': 'Asia-Pacific', 'type': 'Developed'},
    'Israel':        {'etf': 'EIS',  'region': 'EMEA',         'type': 'Developed'},
    'Austria':       {'etf': 'EWO',  'region': 'EMEA',         'type': 'Developed'},
    'Belgium':       {'etf': 'EWK',  'region': 'EMEA',         'type': 'Developed'},
    'Denmark':       {'etf': 'EDEN', 'region': 'EMEA',         'type': 'Developed'},
    'Finland':       {'etf': 'EFNL', 'region': 'EMEA',         'type': 'Developed'},
    'France':        {'etf': 'EWQ',  'region': 'EMEA',         'type': 'Developed'},
    'Germany':       {'etf': 'EWG',  'region': 'EMEA',         'type': 'Developed'},
    'Ireland':       {'etf': 'EIRL', 'region': 'EMEA',         'type': 'Developed'},
    'Italy':         {'etf': 'EWI',  'region': 'EMEA',         'type': 'Developed'},
    'Netherlands':   {'etf': 'EWN',  'region': 'EMEA',         'type': 'Developed'},
    'Norway':        {'etf': 'ENOR', 'region': 'EMEA',         'type': 'Developed'},
    'Portugal':      {'etf': 'PGAL', 'region': 'EMEA',         'type': 'Developed'},
    'Spain':         {'etf': 'EWP',  'region': 'EMEA',         'type': 'Developed'},
    'Sweden':        {'etf': 'EWD',  'region': 'EMEA',         'type': 'Developed'},
    'Switzerland':   {'etf': 'EWL',  'region': 'EMEA',         'type': 'Developed'},
    'United Kingdom':{'etf': 'EWU',  'region': 'EMEA',         'type': 'Developed'},
    'Brazil':        {'etf': 'EWZ',  'region': 'Americas',     'type': 'Emerging'},
    'Chile':         {'etf': 'ECH',  'region': 'Americas',     'type': 'Emerging'},
    'Colombia':      {'etf': 'GXG',  'region': 'Americas',     'type': 'Emerging'},
    'Peru':          {'etf': 'EPU',  'region': 'Americas',     'type': 'Emerging'},
    'Mexico':        {'etf': 'EWW',  'region': 'Americas',     'type': 'Emerging'},
    'China':         {'etf': 'MCHI', 'region': 'Asia-Pacific', 'type': 'Emerging'},
    'India':         {'etf': 'INDA', 'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Indonesia':     {'etf': 'EIDO', 'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Korea':         {'etf': 'EWY',  'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Malaysia':      {'etf': 'EWM',  'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Philippines':   {'etf': 'EPHE', 'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Taiwan':        {'etf': 'EWT',  'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Thailand':      {'etf': 'THD',  'region': 'Asia-Pacific', 'type': 'Emerging'},
    'South Africa':  {'etf': 'EZA',  'region': 'EMEA',         'type': 'Emerging'},
    'Egypt':         {'etf': 'EGPT', 'region': 'EMEA',         'type': 'Emerging'},
    'Saudi Arabia':  {'etf': 'KSA',  'region': 'EMEA',         'type': 'Emerging'},
    'UAE':           {'etf': 'UAE',  'region': 'EMEA',         'type': 'Emerging'},
    'Qatar':         {'etf': 'QAT',  'region': 'EMEA',         'type': 'Emerging'},
    'Turkey':        {'etf': 'TUR',  'region': 'EMEA',         'type': 'Emerging'},
    'Poland':        {'etf': 'EPOL', 'region': 'EMEA',         'type': 'Emerging'},
    'Greece':        {'etf': 'GREK', 'region': 'EMEA',         'type': 'Emerging'},
}


# =========================================================================
# PRIMARY: Playwright scrape of MSCI's data search page
# =========================================================================
async def scrape_msci_playwright(debug_dir):
    from playwright.async_api import async_playwright

    print("[MSCI] Launching headless Chromium...")
    captured = []
    all_results = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            locale='en-GB',
            timezone_id='Europe/London',
            viewport={'width': 1600, 'height': 1000},
        )
        page = await context.new_page()

        async def handle_response(response):
            url = response.url
            if 'msci' not in url.lower():
                return
            ct = response.headers.get('content-type', '').lower()
            if not any(t in ct for t in ['json', 'xml', 'html', 'javascript', 'text/plain']):
                return
            try:
                body = await response.text()
                if 50 < len(body) < 500000:
                    captured.append({
                        'url': url,
                        'status': response.status,
                        'content_type': ct,
                        'body': body,
                        'length': len(body),
                    })
            except Exception:
                pass

        page.on('response', handle_response)

        # Strategy: navigate to data-search, click Country tab, set Market to
        # "All Country (DM+EM)", click Search, scrape the resulting table.
        # If that yields <30 countries, do the DM+EM split as a fallback.
        url = 'https://app2.msci.com/products/index-data-search/'
        print(f"[MSCI] Navigating to {url}")
        try:
            await page.goto(url, wait_until='networkidle', timeout=60000)
            await page.wait_for_timeout(5000)
        except Exception as e:
            print(f"[MSCI] navigation timeout: {e}")

        await page.screenshot(path=str(debug_dir / 'main_initial.png'), full_page=True)

        # Click "Country" tab
        country_clicked = False
        for sel in ['a[href*="tabs-2"]', 'a:has-text("Country")']:
            try:
                elem = page.locator(sel).first
                if await elem.count() > 0:
                    await elem.click(timeout=3000)
                    await page.wait_for_timeout(3000)
                    country_clicked = True
                    print(f"[MSCI] clicked Country tab: {sel}")
                    break
            except Exception:
                pass

        # Pass 1: try "All Country (DM+EM)"
        all_results.update(await search_with_market(
            page, debug_dir, market_label='All Country (DM+EM)', name='all_country'))

        # Pass 2: if we didn't get enough, fall back to DM + EM separately
        if len(all_results) < 30:
            print(f"\n[MSCI] Only {len(all_results)} from All Country — trying DM + EM separately")
            dm = await search_with_market(
                page, debug_dir, market_label='Developed Markets (DM)', name='dm')
            em = await search_with_market(
                page, debug_dir, market_label='Emerging Markets (EM)', name='em')
            for k, v in dm.items():
                if k not in all_results: all_results[k] = v
            for k, v in em.items():
                if k not in all_results: all_results[k] = v

        await browser.close()

    # Save debug captures
    try:
        (debug_dir / 'all_captures.json').write_text(
            json.dumps(captured, indent=2, ensure_ascii=False, default=str),
            encoding='utf-8'
        )
    except Exception:
        pass

    print(f"\n[MSCI] === FINAL RESULTS: {len(all_results)} countries ===")
    for c in sorted(all_results.keys()):
        r = all_results[c]
        def f(v): return f"{v:+6.2f}" if isinstance(v, (int, float)) else "  --  "
        print(f"[MSCI]   {c:18s} "
              f"1D={f(r.get('day'))}  MTD={f(r.get('mtd'))}  3MTD={f(r.get('threeMtd'))}  "
              f"YTD={f(r.get('ytd'))}  1Y={f(r.get('oneYr'))}  3Y={f(r.get('threeYr'))}  "
              f"5Y={f(r.get('fiveYr'))}  10Y={f(r.get('tenYr'))}")

    return all_results


async def search_with_market(page, debug_dir, market_label, name):
    """Set the Market dropdown to a given label, click Search, scrape the table."""
    print(f"\n[MSCI] === Market: {market_label} ({name}) ===")

    # Find the Market select dropdown and change its value
    market_set = False
    try:
        selects = page.locator('select')
        cnt = await selects.count()
        for i in range(cnt):
            try:
                sel = selects.nth(i)
                if not await sel.is_visible():
                    continue
                options = await sel.locator('option').all_inner_texts()
                if any('All Country' in o or 'Developed Markets' in o or 'Emerging Markets' in o
                       for o in options):
                    # Try to select by label
                    await sel.select_option(label=market_label, timeout=3000)
                    print(f"[MSCI] set Market dropdown #{i} to '{market_label}'")
                    market_set = True
                    await page.wait_for_timeout(2000)
                    break
            except Exception as e:
                continue
    except Exception as e:
        print(f"[MSCI] market dropdown error: {e}")

    if not market_set:
        print(f"[MSCI] could not set Market to '{market_label}' — skipping")
        return {}

    # Click Search button
    search_clicked = False
    for sel in [
        '#tabs-2 button:has-text("Search")',
        'button:has-text("Search")',
        'input[type=submit][value*="Search" i]',
    ]:
        try:
            btns = page.locator(sel)
            cnt = await btns.count()
            for i in range(min(cnt, 5)):
                try:
                    btn = btns.nth(i)
                    if await btn.is_visible():
                        await btn.scroll_into_view_if_needed(timeout=2000)
                        await btn.click(timeout=3000)
                        search_clicked = True
                        print(f"[MSCI] clicked search button: {sel} #{i}")
                        await page.wait_for_timeout(8000)
                        break
                except Exception:
                    continue
            if search_clicked: break
        except Exception:
            pass

    # Screenshot + DOM dump after Search
    try:
        await page.screenshot(path=str(debug_dir / f'{name}_after_search.png'), full_page=True)
        html = await page.content()
        (debug_dir / f'{name}_dom.html').write_text(html, encoding='utf-8')
    except Exception:
        pass

    # Extract tables
    tables = await page.evaluate('''() => {
        const out = [];
        document.querySelectorAll('table').forEach((table, ti) => {
            const rows = [];
            table.querySelectorAll('tr').forEach((tr) => {
                const cells = Array.from(tr.querySelectorAll('th,td'))
                    .map(c => c.innerText.trim());
                if (cells.length > 0) rows.push(cells);
            });
            if (rows.length > 0) out.push({ tableIndex: ti, rows });
        });
        return out;
    }''')

    (debug_dir / f'{name}_tables.json').write_text(
        json.dumps(tables, indent=2, ensure_ascii=False), encoding='utf-8'
    )
    print(f"[MSCI] {name}: {len(tables)} tables")
    for i, t in enumerate(tables):
        print(f"[MSCI]   table {i}: {len(t['rows'])} rows")

    results = parse_msci_tables(tables)
    print(f"[MSCI] {name}: parsed {len(results)} countries")
    return results


def parse_pct(s):
    """Parse a percentage-like string into a float, returning None for empty/'-'.

    Module-level so pytest can import it. Used by parse_msci_tables.
    """
    if s is None: return None
    s = str(s).replace(',', '').replace('%', '').replace('+', '').strip()
    if s in ('', '-', '—', 'N/A', 'NA'): return None
    if re.match(r'^-?\d+\.?\d*$', s):
        try: return float(s)
        except ValueError: pass
    return None


def parse_msci_tables(tables):
    """Parse country performance from MSCI's table using fixed column positions:
       row[0] = MSCI Index name
       row[1] = Index Code (numeric)
       row[2] = Last price (numeric, large)
       row[3] = Day %
       row[4] = MTD %
       row[5] = 3MTD %
       row[6] = YTD %
       row[7] = 1 Yr %
       row[8] = 3 Yr % (annualised)
       row[9] = 5 Yr % (annualised)
       row[10] = 10 Yr % (annualised)
    """
    results = {}
    aliases = build_country_aliases()

    for tbl in tables:
        for row in tbl['rows']:
            if len(row) < 8:
                continue

            # Identify country from cell 0
            name_raw = str(row[0]).strip()
            name_norm = re.sub(r'^MSCI\s+', '', name_raw.upper())
            name_norm = re.sub(r'\s+INDEX$', '', name_norm).strip()
            canonical = aliases.get(name_norm)
            if not canonical or canonical in results:
                continue

            # Sanity check: cell 1 should be a 6-digit index code, cell 2 a price >10
            code = parse_pct(row[1])
            last = parse_pct(row[2])
            if code is None or last is None or last < 10:
                continue  # not a data row in expected format

            # Parse the metric cells by position
            day      = parse_pct(row[3])
            mtd      = parse_pct(row[4])
            threeMtd = parse_pct(row[5])
            ytd      = parse_pct(row[6])
            oneYr    = parse_pct(row[7])
            threeYr  = parse_pct(row[8])  if len(row) > 8  else None
            fiveYr   = parse_pct(row[9])  if len(row) > 9  else None
            tenYr    = parse_pct(row[10]) if len(row) > 10 else None

            # Sanity check: day return should be small in absolute terms
            if day is None or abs(day) > 25:
                continue

            results[canonical] = {
                'day':      day,
                'mtd':      mtd,
                'threeMtd': threeMtd,
                'ytd':      ytd,
                'oneYr':    oneYr,
                'threeYr':  threeYr,
                'fiveYr':   fiveYr,
                'tenYr':    tenYr,
            }

    return results


def build_country_aliases():
    a = {}
    for canon in MARKETS:
        a[canon.upper()] = canon
    a['UNITED STATES'] = 'USA'
    a['US'] = 'USA'
    a['UNITED ARAB EMIRATES'] = 'UAE'
    a['KOREA, REPUBLIC OF'] = 'Korea'
    a['SOUTH KOREA'] = 'Korea'
    a['REPUBLIC OF KOREA'] = 'Korea'
    a['CHINESE TAIPEI'] = 'Taiwan'
    a['UK'] = 'United Kingdom'
    a['GREAT BRITAIN'] = 'United Kingdom'
    a['HONGKONG'] = 'Hong Kong'
    a['NEWZEALAND'] = 'New Zealand'
    a['SOUTHAFRICA'] = 'South Africa'
    a['SAUDIARABIA'] = 'Saudi Arabia'
    return a


# =========================================================================
# SECONDARY: yfinance via curl_cffi (unchanged)
# =========================================================================
def fetch_etf_returns():
    import yfinance as yf
    try:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate='chrome124')
        print("\n[ETF] Using curl_cffi session")
    except ImportError:
        session = None
        print("\n[ETF] curl_cffi unavailable", file=sys.stderr)

    print("[ETF] Fetching country ETF prices from Yahoo Finance...")
    results = {}

    def anchor_close(closes, target_date):
        """Most recent close on or before target_date — the correct return anchor.

        `closes` is sorted ascending. The previous version picked the close with
        the smallest *absolute* calendar distance, which could select a close that
        falls *after* the target (e.g. when the target lands on a weekend/holiday),
        biasing trailing returns. We now take the last close at or before the
        target, falling back to the earliest available close when the ETF's
        history starts after the target date (mirrors backfill_history.py)."""
        if len(closes) == 0:
            return None
        prior = [float(closes.loc[d]) for d in closes.index if d.date() <= target_date]
        if prior:
            return prior[-1]
        return float(closes.iloc[0])

    def ann_return(last_close, anchor, years):
        """Annualised return given last price, anchor price, and years between."""
        if anchor is None or anchor <= 0 or years <= 0:
            return None
        return round(((last_close / anchor) ** (1.0 / years) - 1) * 100, 2)

    for country, meta in MARKETS.items():
        ticker_sym = meta['etf']
        try:
            t = yf.Ticker(ticker_sym, session=session) if session else yf.Ticker(ticker_sym)
            # Pull max available history so 10Y is computable
            hist = t.history(period='max', interval='1d', auto_adjust=True)
            if hist is None or len(hist) == 0:
                continue
            closes = hist['Close'].dropna().sort_index()
            if len(closes) < 30:
                continue

            last_date = closes.index[-1].date()
            last_close = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])

            this_month_start = last_date.replace(day=1)
            prev_month_closes = closes[closes.index.date < this_month_start]
            month_anchor = float(prev_month_closes.iloc[-1]) if len(prev_month_closes) else float(closes.iloc[0])

            # 3MTD = trailing 3 calendar months from last_date
            target_3m = last_date - timedelta(days=91)
            three_m_anchor = anchor_close(closes, target_3m)

            this_year_start = last_date.replace(month=1, day=1)
            prev_year_closes = closes[closes.index.date < this_year_start]
            year_anchor = float(prev_year_closes.iloc[-1]) if len(prev_year_closes) else float(closes.iloc[0])

            one_yr_anchor   = anchor_close(closes, last_date - timedelta(days=365))
            three_yr_anchor = anchor_close(closes, last_date - timedelta(days=365 * 3))
            five_yr_anchor  = anchor_close(closes, last_date - timedelta(days=365 * 5))
            ten_yr_anchor   = anchor_close(closes, last_date - timedelta(days=365 * 10))

            # Verify the anchor dates are actually old enough — if the ETF is newer
            # than e.g. 5 years, the 5Y anchor will just be the earliest available close.
            oldest_avail = closes.index[0].date()
            yrs_available = (last_date - oldest_avail).days / 365.25

            # ---------- Phase 4.1: extra ETF-derived metrics ----------
            # 6M return from the trailing-6-months closest close.
            six_m_anchor = anchor_close(closes, last_date - timedelta(days=183))
            six_m_return = (round((last_close / six_m_anchor - 1) * 100, 2)
                            if six_m_anchor else None)

            # Realised volatility — annualised stdev of daily log returns, ~1Y window.
            vol_window = closes.iloc[-252:] if len(closes) >= 252 else closes
            vol = None
            max_dd = None
            if len(vol_window) >= 30:
                # Daily simple returns.
                pct = vol_window.pct_change().dropna()
                if len(pct):
                    # Annualise daily stdev with sqrt(252) trading days.
                    daily_std = float(pct.std())
                    vol = round(daily_std * (252 ** 0.5) * 100, 2)

                # Max drawdown across the 1Y window — worst peak-to-trough %.
                running_peak = vol_window.cummax()
                dd_series = (vol_window / running_peak - 1) * 100
                worst = float(dd_series.min())
                # Drawdowns are negative-valued; round to 2dp.
                max_dd = round(worst, 2) if worst < 0 else 0.0

            results[country] = {
                'day':      round((last_close / prev_close   - 1) * 100, 2),
                'mtd':      round((last_close / month_anchor - 1) * 100, 2),
                'threeMtd': round((last_close / three_m_anchor - 1) * 100, 2) if three_m_anchor else None,
                'sixMtd':   six_m_return,
                'ytd':      round((last_close / year_anchor  - 1) * 100, 2),
                'oneYr':    round((last_close / one_yr_anchor - 1) * 100, 2) if one_yr_anchor else None,
                'threeYr':  ann_return(last_close, three_yr_anchor, 3) if yrs_available >= 3 else None,
                'fiveYr':   ann_return(last_close, five_yr_anchor,  5) if yrs_available >= 5 else None,
                'tenYr':    ann_return(last_close, ten_yr_anchor,  10) if yrs_available >= 10 else None,
                'vol1Y':    vol,
                'maxDd1Y':  max_dd,
                # _close is kept for history.json so future rebuilds can
                # re-derive returns; build_output() drops it before write.
                '_close':   round(last_close, 4),
            }
        except Exception as e:
            print(f"[ETF] {country:18s} {ticker_sym:6s}  ERROR: {type(e).__name__}: {e}", file=sys.stderr)

    print(f"[ETF] captured {len(results)} markets")
    return results


# =========================================================================
# VALIDATOR
# =========================================================================
def validate_sources(msci_data, etf_data, threshold=2.0):
    print(f"\n[VALIDATE] Comparing MSCI vs ETF (threshold: {threshold}%)...")
    discrepancies = []
    compared = 0
    metric_labels = {
        'day': '1D', 'mtd': 'MTD', 'threeMtd': '3MTD', 'ytd': 'YTD',
        'oneYr': '1Y', 'threeYr': '3Y', 'fiveYr': '5Y', 'tenYr': '10Y',
    }

    for country in MARKETS:
        if country not in msci_data or country not in etf_data:
            continue
        compared += 1
        m = msci_data[country]
        e = etf_data[country]
        for metric in metric_labels:
            mv = m.get(metric); ev = e.get(metric)
            if mv is None or ev is None: continue
            diff = round(mv - ev, 2)
            if abs(diff) >= threshold:
                discrepancies.append({
                    'country': country, 'metric': metric_labels[metric],
                    'msci': mv, 'etf': ev, 'diff': diff,
                })

    if discrepancies:
        print(f"[VALIDATE] {len(discrepancies)} discrepancies of ≥{threshold}% (out of {compared} compared):")
        for d in discrepancies[:15]:
            print(f"[VALIDATE]   {d['country']:18s} {d['metric']:4s}  "
                  f"MSCI={d['msci']:+7.2f}  ETF={d['etf']:+7.2f}  diff={d['diff']:+6.2f}")
    else:
        print(f"[VALIDATE] No discrepancies of ≥{threshold}% across {compared} countries")

    return {
        'compared': compared,
        'threshold': threshold,
        'discrepancyCount': len(discrepancies),
        'discrepancies': discrepancies[:50],
    }


# =========================================================================
# ORCHESTRATION
# =========================================================================
def build_output(market_data, source, validation=None, etf_data=None):
    etf_data = etf_data or {}
    markets = []
    for country, meta in MARKETS.items():
        if country in market_data:
            d = market_data[country]
            # The MSCI scrape path doesn't compute 6M / realised-vol / max-drawdown.
            # When MSCI is the primary source, backfill those three from the ETF
            # data (already fetched this run) so they aren't permanently null in
            # the dashboard's 6M / Vol 1Y / Max DD 1Y views.
            e = etf_data.get(country, {})
            markets.append({
                'country':  country,
                'day':      d.get('day'),
                'mtd':      d.get('mtd'),
                'threeMtd': d.get('threeMtd'),
                'sixMtd':   d.get('sixMtd')   if d.get('sixMtd')   is not None else e.get('sixMtd'),
                'ytd':      d.get('ytd'),
                'oneYr':    d.get('oneYr'),
                'threeYr':  d.get('threeYr'),
                'fiveYr':   d.get('fiveYr'),
                'tenYr':    d.get('tenYr'),
                'vol1Y':    d.get('vol1Y')    if d.get('vol1Y')    is not None else e.get('vol1Y'),
                'maxDd1Y':  d.get('maxDd1Y')  if d.get('maxDd1Y')  is not None else e.get('maxDd1Y'),
                'region':   meta['region'],
                'type':     meta['type'],
            })
    out = {
        'lastUpdated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'source': source,
        'asOf': datetime.now(timezone.utc).date().isoformat(),
        'marketsCount': len(markets),
        'expectedCount': len(MARKETS),
        'markets': markets,
    }
    if validation:
        out['validation'] = validation
    return out


def load_previous(path):
    if path.exists():
        try: return json.loads(path.read_text())
        except Exception: return None
    return None


# Schema validation (Phase 4.2). Run at the end of main() and abort the commit
# if the output is obviously malformed so the GitHub Action surfaces it instead
# of silently committing junk over a known-good file.
REQUIRED_FIELDS = (
    'country', 'region', 'type', 'day', 'mtd', 'threeMtd', 'ytd',
    'oneYr', 'threeYr', 'fiveYr', 'tenYr',
)
SANE_PCT_BOUNDS = (-99.5, 500.0)


def validate_output(output, min_markets=25):
    """Return a list of validation problems (empty list = OK)."""
    problems = []
    if not isinstance(output, dict):
        problems.append('output is not a dict')
        return problems
    markets = output.get('markets')
    if not isinstance(markets, list):
        problems.append('output.markets is not a list')
        return problems
    if len(markets) < min_markets:
        problems.append(f'too few markets: {len(markets)} < {min_markets}')

    seen = set()
    for i, m in enumerate(markets):
        if not isinstance(m, dict):
            problems.append(f'row {i} is not a dict'); continue
        for f in REQUIRED_FIELDS:
            if f not in m:
                problems.append(f'row {i} ({m.get("country", "?")}) missing field {f}')
        country = m.get('country')
        if country in seen:
            problems.append(f'duplicate country: {country}')
        seen.add(country)
        for k in ('day', 'mtd', 'threeMtd', 'sixMtd', 'ytd', 'oneYr', 'threeYr', 'fiveYr', 'tenYr'):
            v = m.get(k)
            if v is None:
                continue
            if not isinstance(v, (int, float)):
                problems.append(f'{country} {k} is not numeric: {v!r}')
                continue
            lo, hi = SANE_PCT_BOUNDS
            if not (lo <= v <= hi):
                problems.append(f'{country} {k} out of sane bounds: {v}')
    return problems


# History-file rolling cap. ~400 trading days ≈ 18 months — small enough that the
# JSON stays trivial to fetch, long enough that the Compare-tab 1Y window has
# headroom for non-trading-day backfill.
HISTORY_CAP = 400
HISTORY_SCHEMA_VERSION = 1
HISTORY_METRIC_KEYS = ('day', 'mtd', 'threeMtd', 'ytd', 'oneYr')


def update_history(history_path, primary_data, etf_data, as_of_date):
    """Append today's snapshot to data/history.json.

    De-dupe: if a series already has a point with this date, overwrite it
    (handles same-day re-runs). Cap each series to HISTORY_CAP points.

    Pulls metric values from primary_data (MSCI or ETF, whichever won) and
    augments with the raw ETF close when available — close enables future
    re-derivation if metric definitions change.
    """
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text())
        except Exception:
            history = {'schemaVersion': HISTORY_SCHEMA_VERSION, 'series': {}}
    else:
        history = {'schemaVersion': HISTORY_SCHEMA_VERSION, 'series': {}}

    series = history.setdefault('series', {})
    date_str = as_of_date.isoformat() if hasattr(as_of_date, 'isoformat') else str(as_of_date)

    appended = 0
    for country in MARKETS:
        row = primary_data.get(country) or {}
        etf_row = etf_data.get(country) or {}
        if not row and not etf_row:
            continue

        point = {'d': date_str}
        for key in HISTORY_METRIC_KEYS:
            v = row.get(key) if row.get(key) is not None else etf_row.get(key)
            if v is not None:
                point[key] = v

        close = etf_row.get('_close')
        if close is not None:
            point['close'] = close

        if len(point) == 1:
            # Just the date — nothing worth storing.
            continue

        existing = series.get(country, [])
        # Drop any existing same-day entry, append, sort, cap.
        existing = [p for p in existing if p.get('d') != date_str]
        existing.append(point)
        existing.sort(key=lambda p: p.get('d', ''))
        if len(existing) > HISTORY_CAP:
            existing = existing[-HISTORY_CAP:]
        series[country] = existing
        appended += 1

    history['schemaVersion'] = HISTORY_SCHEMA_VERSION
    history['lastUpdated'] = datetime.now(timezone.utc).isoformat(timespec='seconds')

    # Compact JSON — the file is fetched by every client on demand.
    history_path.write_text(json.dumps(history, separators=(',', ':')))
    print(f"[HISTORY] appended {appended} country points → {history_path.name}")


async def main():
    repo_root = Path(__file__).resolve().parents[1]
    out_path = repo_root / 'data' / 'msci-data.json'
    debug_dir = repo_root / 'debug'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)
    for f in debug_dir.glob('*'):
        try: f.unlink()
        except Exception: pass

    previous = load_previous(out_path)

    msci_data = {}
    etf_data = {}

    try:
        msci_data = await scrape_msci_playwright(debug_dir)
    except Exception as e:
        print(f"[MSCI] scrape exception: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback; traceback.print_exc()

    try:
        etf_data = fetch_etf_returns()
    except Exception as e:
        print(f"[ETF] fetch exception: {type(e).__name__}: {e}", file=sys.stderr)

    validation = None
    if len(msci_data) >= 5 and len(etf_data) >= 5:
        validation = validate_sources(msci_data, etf_data)

    history_path = repo_root / 'data' / 'history.json'
    primary_for_history = None

    if len(msci_data) >= 30:
        print(f"\n[OK] MSCI captured {len(msci_data)}/{len(MARKETS)} — using MSCI as PRIMARY source")
        output = build_output(msci_data, source='MSCI', validation=validation, etf_data=etf_data)
        primary_for_history = msci_data
    elif len(etf_data) >= 30:
        print(f"\n[OK] yfinance captured {len(etf_data)}/{len(MARKETS)} — using ETF proxy")
        output = build_output(etf_data, source='ETF_PROXY', validation=validation, etf_data=etf_data)
        primary_for_history = etf_data
    else:
        print(f"\n[ERR] Neither source ≥30 (MSCI={len(msci_data)}, ETF={len(etf_data)})", file=sys.stderr)
        if previous and previous.get('markets'):
            print("[OK] keeping previous data file")
            return 0
        output = build_output(etf_data or msci_data, source='ETF_PROXY' if etf_data else 'MSCI' if msci_data else 'FAILED', validation=validation, etf_data=etf_data)
        # Don't append history on a FAILED run — only good data goes into the timeline.

    # Schema validation gate (Phase 4.2). If the output is clearly malformed
    # *and* we already have a good previous file, prefer keeping the previous
    # one instead of committing junk.
    problems = validate_output(output)
    if problems:
        print(f"\n[VALIDATE_SCHEMA] {len(problems)} issue(s):", file=sys.stderr)
        for p in problems[:20]:
            print(f"[VALIDATE_SCHEMA]   - {p}", file=sys.stderr)
        if previous and previous.get('markets') and len(problems) > 5:
            print("[VALIDATE_SCHEMA] keeping previous file (too many issues)", file=sys.stderr)
            return 0

    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n[OK] wrote {out_path}")
    print(f"      {output['marketsCount']} markets, source={output['source']}")
    if validation:
        print(f"      validation: {validation['discrepancyCount']} discrepancies of {validation['compared']} compared")

    if primary_for_history is not None:
        try:
            update_history(history_path, primary_for_history, etf_data, datetime.now(timezone.utc).date())
        except Exception as e:
            print(f"[HISTORY] update failed: {type(e).__name__}: {e}", file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
