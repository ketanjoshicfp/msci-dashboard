"""
MSCI World Markets Scraper (v4)
================================

Strategy:
  1. PRIMARY: Try multiple MSCI page URLs with aggressive interaction
     (click Country tab, submit Search button) to get country-level data.
  2. SECONDARY: yfinance with curl_cffi as the always-on fallback.
  3. VALIDATOR: When both sources have data, cross-check overlap.

Debug artifacts written to debug/ on every run:
  - {name}_before.png  : screenshot before interactions
  - {name}_after.png   : screenshot after clicking Country + Search
  - {name}_dom.html    : full rendered HTML
  - {name}_tables.json : extracted DOM tables (every row)
  - all_captures.json  : every msci.com response body the script saw

The workflow uploads debug/ as an artifact so we can see exactly
what the page is rendering and iterate from concrete evidence.
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
    # ---- DEVELOPED (23) ----
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

    # ---- EMERGING (21) ----
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
# PRIMARY: Playwright scrape with debug artifacts
# =========================================================================
async def scrape_msci_playwright(debug_dir):
    from playwright.async_api import async_playwright

    print("[MSCI] Launching headless Chromium...")
    captured = []
    all_dom_tables = {}  # name -> tables

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
            # Capture json, xml, html, text — anything that might contain data
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
                    if 'json' in ct or 'xml' in ct:
                        print(f"[MSCI] {response.status} {ct[:30]:30s} {url[:90]} ({len(body)}b)")
            except Exception:
                pass

        page.on('response', handle_response)

        # ---- Try multiple URL strategies ----
        attempts = [
            # 1. Direct guess: country variant of the JSF page
            ('country_jsf', 'https://app2.msci.com/webapp/indexperf/pages/IEIPerformanceCountry.jsf'),
            # 2. Main data-search page (best chance with proper interaction)
            ('data_search', 'https://app2.msci.com/products/index-data-search/'),
            # 3. The end-of-day country page
            ('eod_country', 'https://www.msci.com/end-of-day-data-country'),
        ]

        for name, url in attempts:
            print(f"\n[MSCI] === Attempt: {name} === {url}")
            try:
                await page.goto(url, wait_until='networkidle', timeout=60000)
                await page.wait_for_timeout(5000)
            except Exception as e:
                print(f"[MSCI] navigation error: {e}")
                continue

            # Before-screenshot
            try:
                await page.screenshot(path=str(debug_dir / f'{name}_before.png'), full_page=True)
            except Exception:
                pass

            # --- Click "Country" tab ---
            country_tab_clicked = False
            for sel in [
                'a[href*="tabs-2"]',
                'a[href="#tabs-2"]',
                'li.ui-tabs-tab[aria-controls*="tab"] a:has-text("Country")',
                'a:has-text("Country")',
                'li:has-text("Country") > a',
                '[role="tab"]:has-text("Country")',
            ]:
                try:
                    elem = page.locator(sel).first
                    if await elem.count() > 0:
                        try:
                            await elem.scroll_into_view_if_needed(timeout=2000)
                        except Exception:
                            pass
                        await elem.click(timeout=3000)
                        await page.wait_for_timeout(3000)
                        country_tab_clicked = True
                        print(f"[MSCI] clicked Country tab: {sel}")
                        break
                except Exception:
                    pass

            # --- Click Search button (inside the active tab if possible) ---
            for sel in [
                '#tabs-2 button:has-text("Search")',
                '#tabs-2 input[type=submit][value*="Search" i]',
                'div[aria-labelledby*="tab"][aria-hidden="false"] button:has-text("Search")',
                'button:has-text("Search")',
                'input[type=submit][value*="Search" i]',
                'button:has-text("Update")',
                'a:has-text("Search")',
            ]:
                try:
                    btns = page.locator(sel)
                    cnt = await btns.count()
                    if cnt == 0:
                        continue
                    for i in range(min(cnt, 5)):
                        try:
                            btn = btns.nth(i)
                            if await btn.is_visible():
                                await btn.scroll_into_view_if_needed(timeout=2000)
                                await btn.click(timeout=3000)
                                print(f"[MSCI] clicked search: {sel} #{i}")
                                await page.wait_for_timeout(7000)
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            # After-screenshot
            try:
                await page.screenshot(path=str(debug_dir / f'{name}_after.png'), full_page=True)
            except Exception:
                pass

            # Save full DOM
            try:
                html = await page.content()
                (debug_dir / f'{name}_dom.html').write_text(html, encoding='utf-8')
            except Exception:
                pass

            # Extract all DOM tables
            try:
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
                all_dom_tables[name] = tables
                (debug_dir / f'{name}_tables.json').write_text(
                    json.dumps(tables, indent=2, ensure_ascii=False), encoding='utf-8'
                )
                # Quick country count for this attempt
                country_count = count_countries_in_tables(tables)
                print(f"[MSCI] {name}: {len(tables)} tables, {country_count} country rows recognised")
            except Exception as e:
                print(f"[MSCI] DOM extract failed: {e}")

        await browser.close()

    # Save all captured network responses
    try:
        (debug_dir / 'all_captures.json').write_text(
            json.dumps(captured, indent=2, ensure_ascii=False, default=str),
            encoding='utf-8'
        )
        print(f"\n[MSCI] saved {len(captured)} response bodies to debug/all_captures.json")
    except Exception as e:
        print(f"[MSCI] capture save failed: {e}")

    # Parse data from whichever attempt yielded countries
    print(f"\n[MSCI] === PARSING ===")
    best_results = {}
    for name, tables in all_dom_tables.items():
        results = parse_msci_tables(tables)
        print(f"[MSCI] {name} parser yielded {len(results)} countries")
        if len(results) > len(best_results):
            best_results = results

    # Also try parsing the JSON captures
    json_results = parse_msci_json(captured)
    print(f"[MSCI] JSON capture parser yielded {len(json_results)} countries")
    for k, v in json_results.items():
        if k not in best_results:
            best_results[k] = v

    return best_results


def count_countries_in_tables(tables):
    aliases = build_country_aliases()
    found = set()
    for tbl in tables:
        for row in tbl['rows']:
            for cell in row[:3]:
                key = re.sub(r'^MSCI\s+', '', str(cell).strip().upper())
                key = re.sub(r'\s+INDEX$', '', key).strip()
                if key in aliases:
                    found.add(aliases[key])
                    break
    return len(found)


def parse_msci_tables(tables):
    results = {}
    aliases = build_country_aliases()

    for tbl in tables:
        for row in tbl['rows']:
            if len(row) < 5:
                continue
            canonical = None
            for cell in row[:3]:
                key = re.sub(r'^MSCI\s+', '', str(cell).strip().upper())
                key = re.sub(r'\s+INDEX$', '', key).strip()
                if key in aliases:
                    canonical = aliases[key]
                    break
            if not canonical or canonical in results:
                continue

            nums = []
            for cell in row:
                cleaned = str(cell).replace(',', '').replace('%', '').replace('+', '').strip()
                if re.match(r'^-?\d+\.?\d*$', cleaned):
                    try:
                        nums.append(float(cleaned))
                    except ValueError:
                        pass

            if len(nums) >= 5:
                # If first number > 50, it's probably the "Last" price column
                if abs(nums[0]) > 50:
                    nums = nums[1:]

                # Map: [Day, MTD, 3MTD, YTD, 1Yr, ...]  — skip 3MTD
                if len(nums) >= 5:
                    results[canonical] = {
                        'day':   nums[0],
                        'mtd':   nums[1],
                        'ytd':   nums[3],
                        'oneYr': nums[4],
                    }
                elif len(nums) >= 4:
                    results[canonical] = {
                        'day':   nums[0],
                        'mtd':   nums[1],
                        'ytd':   nums[2],
                        'oneYr': nums[3],
                    }

    return results


def parse_msci_json(captured):
    aliases = build_country_aliases()
    results = {}

    for cap in captured:
        if 'json' not in cap['content_type']:
            continue
        try:
            data = json.loads(cap['body'])
        except Exception:
            continue

        def walk(node):
            if isinstance(node, dict):
                name_field = None
                for key in ('country', 'name', 'indexName', 'label', 'displayName', 'index'):
                    if key in node and isinstance(node[key], str):
                        name_field = node[key]
                        break
                if name_field:
                    key = re.sub(r'^MSCI\s+', '', name_field.strip().upper())
                    key = re.sub(r'\s+INDEX$', '', key).strip()
                    canonical = aliases.get(key)
                    if canonical and canonical not in results:
                        day = mtd = ytd = oneyr = None
                        for k, v in node.items():
                            if not isinstance(v, (int, float)):
                                continue
                            lk = k.lower()
                            if '1d' in lk or 'day' in lk: day = float(v)
                            elif 'mtd' in lk and '3' not in lk: mtd = float(v)
                            elif 'ytd' in lk: ytd = float(v)
                            elif '1y' in lk or '1yr' in lk or '12m' in lk or 'oneyear' in lk:
                                oneyr = float(v)
                        if any(x is not None for x in (day, mtd, ytd, oneyr)):
                            results[canonical] = {'day': day, 'mtd': mtd, 'ytd': ytd, 'oneYr': oneyr}
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)

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
# SECONDARY: yfinance via curl_cffi
# =========================================================================
def fetch_etf_returns():
    import yfinance as yf
    try:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate='chrome124')
        print("\n[ETF] Using curl_cffi session")
    except ImportError:
        session = None
        print("\n[ETF] curl_cffi unavailable, using default session", file=sys.stderr)

    print("[ETF] Fetching country ETF prices from Yahoo Finance...")
    results = {}

    for country, meta in MARKETS.items():
        ticker_sym = meta['etf']
        try:
            t = yf.Ticker(ticker_sym, session=session) if session else yf.Ticker(ticker_sym)
            hist = t.history(period='14mo', interval='1d', auto_adjust=True)
            if hist is None or len(hist) == 0:
                print(f"[ETF] {country:18s} {ticker_sym:6s}  EMPTY", file=sys.stderr)
                continue

            closes = hist['Close'].dropna().sort_index()
            if len(closes) < 30:
                print(f"[ETF] {country:18s} {ticker_sym:6s}  only {len(closes)} closes — skip", file=sys.stderr)
                continue

            last_date = closes.index[-1].date()
            last_close = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])

            this_month_start = last_date.replace(day=1)
            prev_month_closes = closes[closes.index.date < this_month_start]
            month_anchor = float(prev_month_closes.iloc[-1]) if len(prev_month_closes) else float(closes.iloc[0])

            this_year_start = last_date.replace(month=1, day=1)
            prev_year_closes = closes[closes.index.date < this_year_start]
            year_anchor = float(prev_year_closes.iloc[-1]) if len(prev_year_closes) else float(closes.iloc[0])

            target = last_date - timedelta(days=365)
            one_yr_anchor = min(
                [(abs((d.date() - target).days), float(closes.loc[d])) for d in closes.index],
                key=lambda x: x[0]
            )[1]

            results[country] = {
                'day':   round((last_close / prev_close - 1) * 100, 2),
                'mtd':   round((last_close / month_anchor - 1) * 100, 2),
                'ytd':   round((last_close / year_anchor - 1) * 100, 2),
                'oneYr': round((last_close / one_yr_anchor - 1) * 100, 2),
            }
            r = results[country]
            print(f"[ETF] {country:18s} {ticker_sym:6s} 1D={r['day']:+6.2f}  "
                  f"MTD={r['mtd']:+6.2f}  YTD={r['ytd']:+7.2f}  1Y={r['oneYr']:+7.2f}")

        except Exception as e:
            print(f"[ETF] {country:18s} {ticker_sym:6s}  ERROR: {type(e).__name__}: {e}", file=sys.stderr)

    return results


# =========================================================================
# VALIDATOR
# =========================================================================
def validate_sources(msci_data, etf_data, threshold=2.0):
    print(f"\n[VALIDATE] Comparing MSCI vs ETF proxy (threshold: {threshold}%)...")
    discrepancies = []
    compared = 0
    metric_labels = {'day': '1D', 'mtd': 'MTD', 'ytd': 'YTD', 'oneYr': '1Y'}

    for country in MARKETS:
        if country not in msci_data or country not in etf_data:
            continue
        compared += 1
        m = msci_data[country]
        e = etf_data[country]
        for metric in ('day', 'mtd', 'ytd', 'oneYr'):
            mv = m.get(metric)
            ev = e.get(metric)
            if mv is None or ev is None:
                continue
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
def build_output(market_data, source, validation=None):
    markets = []
    for country, meta in MARKETS.items():
        if country in market_data:
            d = market_data[country]
            markets.append({
                'country': country,
                'day':   d.get('day'),
                'mtd':   d.get('mtd'),
                'ytd':   d.get('ytd'),
                'oneYr': d.get('oneYr'),
                'region': meta['region'],
                'type':   meta['type'],
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
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


async def main():
    repo_root = Path(__file__).resolve().parents[1]
    out_path = repo_root / 'data' / 'msci-data.json'
    debug_dir = repo_root / 'debug'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)
    # Clear previous debug artifacts so each run is fresh
    for f in debug_dir.glob('*'):
        try:
            f.unlink()
        except Exception:
            pass

    previous = load_previous(out_path)

    msci_data = {}
    etf_data = {}

    try:
        msci_data = await scrape_msci_playwright(debug_dir)
    except Exception as e:
        print(f"[MSCI] scrape exception: {type(e).__name__}: {e}", file=sys.stderr)

    try:
        etf_data = fetch_etf_returns()
    except Exception as e:
        print(f"[ETF] fetch exception: {type(e).__name__}: {e}", file=sys.stderr)

    validation = None
    if len(msci_data) >= 5 and len(etf_data) >= 5:
        validation = validate_sources(msci_data, etf_data)

    if len(msci_data) >= 30:
        print(f"\n[OK] MSCI captured {len(msci_data)}/{len(MARKETS)} — using MSCI as primary source")
        output = build_output(msci_data, source='MSCI', validation=validation)
    elif len(etf_data) >= 30:
        print(f"\n[OK] yfinance captured {len(etf_data)}/{len(MARKETS)} — using ETF proxy as source")
        output = build_output(etf_data, source='ETF_PROXY', validation=validation)
    else:
        print(f"\n[ERR] Neither source ≥30 markets (MSCI={len(msci_data)}, ETF={len(etf_data)})", file=sys.stderr)
        if previous and previous.get('markets'):
            print("[OK] keeping previous data file")
            return 0
        if etf_data:
            output = build_output(etf_data, source='ETF_PROXY', validation=validation)
        elif msci_data:
            output = build_output(msci_data, source='MSCI', validation=validation)
        else:
            output = build_output({}, source='FAILED')

    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n[OK] wrote {out_path}")
    print(f"      {output['marketsCount']} markets, source={output['source']}")
    if validation:
        print(f"      validation: {validation['discrepancyCount']} discrepancies of {validation['compared']} compared")
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
