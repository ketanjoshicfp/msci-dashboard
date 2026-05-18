"""
MSCI World Markets Scraper (v2)
================================

Strategy:
  1. PRIMARY: Use Playwright to load MSCI's end-of-day data page,
     intercept the XHR responses + scrape the rendered DOM table.
  2. FALLBACK: If MSCI returns nothing usable, fetch country-ETF prices
     from Yahoo Finance via the `yfinance` package and compute returns.

Why yfinance and not Stooq:
  Stooq blocks GitHub Actions IP ranges. Yahoo Finance does not.
  yfinance is the de-facto standard library for free EOD data and
  works reliably from cloud runners.

Output: data/msci-data.json
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
# etf: iShares MSCI country ETF ticker on Yahoo Finance. These ETFs are
# explicitly designed to track the corresponding MSCI country index
# (correlation 0.98+).
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
# PRIMARY: Playwright scrape of MSCI's end-of-day data search page
# =========================================================================
async def scrape_msci_playwright():
    from playwright.async_api import async_playwright

    print("[MSCI] Launching headless Chromium...")
    captured_responses = []
    captured_json = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            locale='en-GB',
            timezone_id='Europe/London',
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
                elif any(k in url.lower() for k in ['indexperf', 'index-data', 'performance', 'webapp']):
                    body = await response.text()
                    if body and len(body) > 50:
                        captured_responses.append({'url': url, 'body': body[:200000]})
            except Exception:
                pass

        page.on('response', handle_response)

        try:
            print("[MSCI] Navigating to index-data-search...")
            await page.goto('https://app2.msci.com/products/index-data-search/',
                            wait_until='networkidle', timeout=60000)
            await page.wait_for_timeout(8000)
        except Exception as e:
            print(f"[MSCI] navigation timeout: {e}", file=sys.stderr)

        # Try to interact with the country tab + search
        for selector in ['text=Country', 'a:has-text("Country")', 'button:has-text("Search")',
                         'input[type=submit][value*="Search" i]', 'button:has-text("Update")']:
            try:
                elem = page.locator(selector).first
                if await elem.count() > 0:
                    await elem.click(timeout=3000)
                    await page.wait_for_timeout(3000)
                    print(f"[MSCI] clicked: {selector}")
            except Exception:
                pass

        # Also try the legacy regional page
        try:
            await page.goto('https://app2.msci.com/webapp/indexperf/pages/IEIPerformanceRegional.jsf',
                            wait_until='networkidle', timeout=45000)
            await page.wait_for_timeout(8000)
        except Exception:
            pass

        # Extract all DOM tables with their full row structure for debugging
        dom_tables = await page.evaluate('''() => {
            const out = [];
            document.querySelectorAll('table').forEach((table, ti) => {
                const rows = [];
                table.querySelectorAll('tr').forEach((tr, ri) => {
                    const cells = Array.from(tr.querySelectorAll('th,td')).map(c => c.innerText.trim());
                    if (cells.length > 0) rows.push(cells);
                });
                if (rows.length > 0) out.push({ tableIndex: ti, rows });
            });
            return out;
        }''')

        await browser.close()

    # ---- Debug logging: show what we actually got ----
    print(f"\n[MSCI] === DEBUG: DOM TABLE STRUCTURE ===")
    print(f"[MSCI] Found {len(dom_tables)} tables")
    for i, t in enumerate(dom_tables):
        print(f"[MSCI] Table {i}: {len(t['rows'])} rows")
        for j, row in enumerate(t['rows'][:3]):
            row_str = ' | '.join(str(c)[:25] for c in row[:8])
            print(f"[MSCI]   row {j}: {row_str}")
        if len(t['rows']) > 3:
            print(f"[MSCI]   ... + {len(t['rows']) - 3} more rows")

    print(f"\n[MSCI] === DEBUG: JSON RESPONSES ({len(captured_json)}) ===")
    for jr in captured_json[:5]:
        snippet = jr['body'][:300].replace('\n', ' ')
        print(f"[MSCI] {jr['url'][:80]}: {snippet}")

    return parse_msci(dom_tables, captured_json, captured_responses)


def parse_msci(dom_tables, captured_json, captured_responses):
    """Try multiple parsing strategies in order of reliability."""
    results = {}
    country_aliases = build_country_aliases()

    # ---- Strategy 1: DOM tables (flexible name search across first 3 cells) ----
    for tbl in dom_tables:
        for row in tbl['rows']:
            if len(row) < 5:
                continue
            canonical = None
            for cell in row[:3]:
                key = cell.strip().upper()
                key = re.sub(r'^MSCI\s+', '', key)
                key = re.sub(r'\s+INDEX$', '', key)
                key = key.strip()
                if key in country_aliases:
                    canonical = country_aliases[key]
                    break
            if not canonical or canonical in results:
                continue

            nums = []
            for cell in row:
                cleaned = cell.replace(',', '').replace('%', '').replace('+', '').strip()
                if re.match(r'^-?\d+\.?\d*$', cleaned):
                    try:
                        nums.append(float(cleaned))
                    except ValueError:
                        pass

            if len(nums) >= 4:
                results[canonical] = {
                    'day':   nums[0],
                    'mtd':   nums[1],
                    'ytd':   nums[2] if len(nums) >= 3 else None,
                    'oneYr': nums[3] if len(nums) >= 4 else None,
                }

    if results:
        print(f"[MSCI] DOM strategy yielded {len(results)} markets")

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
    """Walk a JSON structure looking for country name + return fields."""
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
                        elif 'mtd' in lk: mtd = float(v)
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
    return a


# =========================================================================
# FALLBACK: yfinance country-ETF proxies
# =========================================================================
def fetch_etf_returns():
    """Fetch ETF history via yfinance and compute returns."""
    import yfinance as yf

    print("\n[ETF] Fetching country ETF prices from Yahoo Finance...")
    tickers = [m['etf'] for m in MARKETS.values()]
    ticker_to_country = {m['etf']: c for c, m in MARKETS.items()}

    try:
        df = yf.download(tickers, period='14mo', interval='1d',
                         progress=False, group_by='ticker', auto_adjust=True,
                         threads=True)
    except Exception as e:
        print(f"[ETF] yfinance batch download failed: {e}", file=sys.stderr)
        return {}

    results = {}

    for ticker in tickers:
        country = ticker_to_country[ticker]
        try:
            if len(tickers) == 1:
                sub = df
            else:
                sub = df[ticker]

            closes = sub['Close'].dropna()
            if len(closes) < 30:
                print(f"[ETF] {country:18s} {ticker:6s} only {len(closes)} closes — skip", file=sys.stderr)
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
            print(f"[ETF] {country:18s} {ticker:6s} 1D={r['day']:+6.2f}  "
                  f"MTD={r['mtd']:+6.2f}  YTD={r['ytd']:+7.2f}  1Y={r['oneYr']:+7.2f}")

        except Exception as e:
            print(f"[ETF] {country:18s} {ticker:6s}  ERROR: {e}", file=sys.stderr)

    return results


# =========================================================================
# ORCHESTRATION
# =========================================================================
def build_output(market_data, source, as_of=None):
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
    return {
        'lastUpdated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'source': source,
        'asOf': as_of or datetime.now(timezone.utc).date().isoformat(),
        'marketsCount': len(markets),
        'expectedCount': len(MARKETS),
        'markets': markets,
    }


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

    msci_data = {}
    try:
        msci_data = await scrape_msci_playwright()
    except Exception as e:
        print(f"[MSCI] scrape exception: {e}", file=sys.stderr)

    if len(msci_data) >= 30:
        print(f"\n[OK] MSCI captured {len(msci_data)}/{len(MARKETS)} — using MSCI as source")
        output = build_output(msci_data, source='MSCI')
    else:
        print(f"\n[WARN] MSCI only captured {len(msci_data)} markets — falling back to yfinance")
        try:
            etf_data = fetch_etf_returns()
            if len(etf_data) >= 30:
                print(f"\n[OK] yfinance captured {len(etf_data)}/{len(MARKETS)} markets")
                output = build_output(etf_data, source='ETF_PROXY')
            else:
                print(f"\n[ERR] yfinance only got {len(etf_data)} markets", file=sys.stderr)
                if previous and previous.get('markets'):
                    print("[OK] keeping previous data file")
                    return 0
                output = build_output(etf_data, source='ETF_PROXY' if etf_data else 'FAILED')
        except Exception as e:
            print(f"[ERR] yfinance exception: {e}", file=sys.stderr)
            if previous and previous.get('markets'):
                return 0
            output = build_output({}, source='FAILED')

    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n[OK] wrote {out_path}")
    print(f"      {output['marketsCount']} markets, source={output['source']}")
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
