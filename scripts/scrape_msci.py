"""
MSCI World Markets Scraper (v3)
================================

Strategy:
  1. PRIMARY: Playwright scrape of MSCI's end-of-day data search page
     (logs every data row found, parses country indices from the table).
  2. SECONDARY: yfinance with curl_cffi browser-TLS-impersonation session.
     Yahoo Finance blocks ordinary Python clients but accepts requests
     that look like Chrome — curl_cffi handles the impersonation.
  3. VALIDATOR: When BOTH sources succeed, compare overlapping countries
     and report any 1Y / YTD / MTD / 1D returns differing by > 2%.

Output: data/msci-data.json
  - source: "MSCI" (preferred) | "ETF_PROXY" | "FAILED"
  - validation: { compared: N, discrepancies: [{country, metric, msci, etf, diff}, ...] }
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
# PRIMARY: Playwright scrape of MSCI's data search page
# =========================================================================
async def scrape_msci_playwright():
    from playwright.async_api import async_playwright

    print("[MSCI] Launching headless Chromium...")
    captured_json = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            locale='en-GB',
            timezone_id='Europe/London',
            viewport={'width': 1600, 'height': 900},
        )
        page = await context.new_page()

        async def handle_response(response):
            url = response.url
            ct = response.headers.get('content-type', '').lower()
            try:
                if 'application/json' in ct and 'msci' in url.lower():
                    body = await response.text()
                    captured_json.append({'url': url, 'body': body})
                    print(f"[MSCI] JSON {response.status} {url[:120]} ({len(body)}b)")
            except Exception:
                pass

        page.on('response', handle_response)

        # Visit the main page
        try:
            print("[MSCI] Navigating to index-data-search...")
            await page.goto('https://app2.msci.com/products/index-data-search/',
                            wait_until='networkidle', timeout=60000)
            await page.wait_for_timeout(5000)
        except Exception as e:
            print(f"[MSCI] navigation timeout: {e}", file=sys.stderr)

        # Click "Country" tab if present, wait for re-render
        for selector in ['a:has-text("Country")', 'li:has-text("Country") a', 'text=Country']:
            try:
                elem = page.locator(selector).first
                if await elem.count() > 0:
                    await elem.click(timeout=3000)
                    await page.wait_for_timeout(4000)
                    print(f"[MSCI] clicked Country tab via: {selector}")
                    break
            except Exception:
                pass

        # Click Search/Update if available to force data population
        for selector in ['button:has-text("Search")', 'button:has-text("Update")',
                         'input[type=submit][value*="Search" i]',
                         'input[type=submit][value*="Update" i]']:
            try:
                elem = page.locator(selector).first
                if await elem.count() > 0:
                    await elem.click(timeout=3000)
                    await page.wait_for_timeout(5000)
                    print(f"[MSCI] clicked: {selector}")
            except Exception:
                pass

        # Also try the legacy regional page (might include countries)
        try:
            await page.goto('https://app2.msci.com/webapp/indexperf/pages/IEIPerformanceRegional.jsf',
                            wait_until='networkidle', timeout=45000)
            await page.wait_for_timeout(6000)
        except Exception:
            pass

        # Extract ALL DOM tables with full row structure
        dom_tables = await page.evaluate('''() => {
            const out = [];
            document.querySelectorAll('table').forEach((table, ti) => {
                const rows = [];
                table.querySelectorAll('tr').forEach((tr) => {
                    const cells = Array.from(tr.querySelectorAll('th,td')).map(c => c.innerText.trim());
                    if (cells.length > 0) rows.push(cells);
                });
                if (rows.length > 0) out.push({ tableIndex: ti, rows });
            });
            return out;
        }''')

        await browser.close()

    # ---- Verbose debug logging ----
    print(f"\n[MSCI] === DOM TABLES ({len(dom_tables)}) ===")
    for i, t in enumerate(dom_tables):
        print(f"[MSCI] Table {i}: {len(t['rows'])} rows")
        for j, row in enumerate(t['rows']):
            # Show first 4 rows always, plus any row containing a % sign (data rows)
            has_pct = any('%' in str(cell) for cell in row)
            if j < 4 or has_pct:
                preview = ' | '.join(str(c)[:25] for c in row[:9])
                print(f"[MSCI]   r{j:2}: {preview}")

    print(f"\n[MSCI] === JSON RESPONSES ({len(captured_json)}) ===")
    for jr in captured_json:
        snippet = jr['body'][:200].replace('\n', ' ')
        print(f"[MSCI] {jr['url'][:80]}\n        {snippet}")

    return parse_msci(dom_tables, captured_json)


def parse_msci(dom_tables, captured_json):
    """Parse country data from DOM tables and JSON responses."""
    results = {}
    country_aliases = build_country_aliases()

    # ---- Strategy 1: DOM tables ----
    for tbl in dom_tables:
        for row in tbl['rows']:
            if len(row) < 5:
                continue

            # Search first 3 cells for a recognized country name.
            canonical = None
            for cell in row[:3]:
                key = re.sub(r'^MSCI\s+', '', cell.strip().upper())
                key = re.sub(r'\s+INDEX$', '', key).strip()
                if key in country_aliases:
                    canonical = country_aliases[key]
                    break
            if not canonical or canonical in results:
                continue

            # Pull numeric values from remaining cells.
            nums = []
            for cell in row:
                cleaned = str(cell).replace(',', '').replace('%', '').replace('+', '').strip()
                if re.match(r'^-?\d+\.?\d*$', cleaned):
                    try:
                        nums.append(float(cleaned))
                    except ValueError:
                        pass

            # Expected MSCI columns: Last, Day%, MTD%, 3MTD%, YTD%, 1Yr%
            # The "Last" price is usually large (>10); skip it if present.
            # Then we want positions 0=Day, 1=MTD, [skip 3MTD], 2 or 3=YTD, last=1Yr
            if len(nums) >= 5:
                # Heuristic: if first number is > 50, it's the Last price — drop it.
                if abs(nums[0]) > 50:
                    nums = nums[1:]

                # Map: [Day, MTD, 3MTD, YTD, 1Yr]  → we want Day, MTD, YTD, 1Yr (skip 3MTD)
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

    if results:
        print(f"\n[MSCI] DOM strategy yielded {len(results)} countries:")
        for c in sorted(results.keys())[:10]:
            r = results[c]
            print(f"[MSCI]   {c:18s} 1D={r['day']:+6.2f} MTD={r['mtd']:+6.2f} YTD={r['ytd']:+7.2f} 1Y={r['oneYr']:+7.2f}")
        if len(results) > 10:
            print(f"[MSCI]   ... and {len(results) - 10} more")

    # ---- Strategy 2: JSON responses ----
    for jr in captured_json:
        try:
            data = json.loads(jr['body'])
            extracted = extract_from_json(data, country_aliases)
            for k, v in extracted.items():
                if k not in results:
                    results[k] = v
        except Exception:
            pass

    return results


def extract_from_json(obj, country_aliases, results=None):
    if results is None:
        results = {}

    def walk(node):
        if isinstance(node, dict):
            name_field = None
            for key in ('country', 'name', 'indexName', 'label', 'displayName'):
                if key in node and isinstance(node[key], str):
                    name_field = node[key]
                    break
            if name_field:
                key = re.sub(r'^MSCI\s+', '', name_field.strip().upper())
                key = re.sub(r'\s+INDEX$', '', key).strip()
                canonical = country_aliases.get(key)
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

    walk(obj)
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
# SECONDARY: yfinance with curl_cffi browser TLS impersonation
# =========================================================================
def fetch_etf_returns():
    """Fetch ETF history via yfinance using a curl_cffi session that
    impersonates Chrome's TLS fingerprint, bypassing Yahoo's bot detection."""
    import yfinance as yf

    # Set up the browser-impersonation session
    try:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate='chrome124')
        print("\n[ETF] Using curl_cffi session (Chrome 124 TLS impersonation)")
    except ImportError:
        print("\n[ETF] curl_cffi unavailable — falling back to default session", file=sys.stderr)
        session = None

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

            closes = hist['Close'].dropna()
            if len(closes) < 30:
                print(f"[ETF] {country:18s} {ticker_sym:6s}  only {len(closes)} closes — skip", file=sys.stderr)
                continue

            closes = closes.sort_index()
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
                '_as_of': last_date.isoformat(),
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
    """Compare overlapping countries between MSCI and ETF proxy.
    Returns a report dict suitable for embedding in the output JSON."""
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
                    'country': country,
                    'metric': metric_labels[metric],
                    'msci': mv,
                    'etf': ev,
                    'diff': diff,
                })

    if discrepancies:
        print(f"[VALIDATE] {len(discrepancies)} discrepancies of ≥{threshold}% (out of {compared} compared):")
        for d in discrepancies[:15]:
            print(f"[VALIDATE]   {d['country']:18s} {d['metric']:4s}  "
                  f"MSCI={d['msci']:+7.2f}  ETF={d['etf']:+7.2f}  diff={d['diff']:+6.2f}")
        if len(discrepancies) > 15:
            print(f"[VALIDATE]   ... + {len(discrepancies) - 15} more")
    else:
        print(f"[VALIDATE] No discrepancies of ≥{threshold}% across {compared} countries")

    return {
        'compared': compared,
        'threshold': threshold,
        'discrepancyCount': len(discrepancies),
        'discrepancies': discrepancies[:50],  # cap at 50 to keep JSON small
    }


# =========================================================================
# ORCHESTRATION
# =========================================================================
def build_output(market_data, source, validation=None, as_of=None):
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
        'asOf': as_of or datetime.now(timezone.utc).date().isoformat(),
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
    out_path = Path(__file__).resolve().parents[1] / 'data' / 'msci-data.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    previous = load_previous(out_path)

    # Always run BOTH sources so we can validate when both succeed.
    msci_data = {}
    etf_data = {}

    try:
        msci_data = await scrape_msci_playwright()
    except Exception as e:
        print(f"[MSCI] scrape exception: {type(e).__name__}: {e}", file=sys.stderr)

    try:
        etf_data = fetch_etf_returns()
    except Exception as e:
        print(f"[ETF] fetch exception: {type(e).__name__}: {e}", file=sys.stderr)

    # Validate when both have meaningful data
    validation = None
    if len(msci_data) >= 5 and len(etf_data) >= 5:
        validation = validate_sources(msci_data, etf_data)

    # Prefer MSCI when it gave us ≥30 of 44 markets
    if len(msci_data) >= 30:
        print(f"\n[OK] MSCI captured {len(msci_data)}/{len(MARKETS)} — using MSCI as primary source")
        output = build_output(msci_data, source='MSCI', validation=validation)
    elif len(etf_data) >= 30:
        print(f"\n[OK] yfinance captured {len(etf_data)}/{len(MARKETS)} — using ETF proxy as source")
        output = build_output(etf_data, source='ETF_PROXY', validation=validation)
    else:
        print(f"\n[ERR] Neither source produced ≥30 markets "
              f"(MSCI={len(msci_data)}, ETF={len(etf_data)})", file=sys.stderr)
        if previous and previous.get('markets'):
            print("[OK] keeping previous data file")
            return 0
        # Last resort: use whichever has any data
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
