"""
MSCI World Markets Scraper
==========================

Strategy:
  1. PRIMARY: Use Playwright to load MSCI's end-of-day data page,
     intercept the XHR responses that populate the performance table,
     and extract 1D / MTD / YTD / 1Y returns for all 44 markets.
  2. FALLBACK: If MSCI scraping fails, fall back to country ETF proxies
     fetched from Stooq (free, no auth, returns close to MSCI indices
     because the ETFs are designed to track them).

Output: data/msci-data.json with this shape:
  {
    "lastUpdated": "2026-05-18T22:00:00Z",
    "source": "MSCI" | "ETF_PROXY",
    "asOf": "2026-05-16",
    "markets": [
      {"country": "USA", "day": 0.42, "mtd": 2.18, "ytd": 8.34,
       "oneYr": 18.62, "region": "Americas", "type": "Developed"},
      ...
    ]
  }
"""

import asyncio
import json
import sys
import re
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------- COUNTRY METADATA ----------
# MSCI download codes (from JeremyBowyer/MSCI-Indices public mapping)
# Format: "internalId,style,size" — these feed both the XHR scrape
# and the legacy XLS endpoint.
MARKETS = {
    # ---- DEVELOPED (23) ----
    'USA':           {'msci_code': '104,C,30',   'etf': 'spy.us',  'region': 'Americas',     'type': 'Developed'},
    'Canada':        {'msci_code': '64,C,30',    'etf': 'ewc.us',  'region': 'Americas',     'type': 'Developed'},
    'Australia':     {'msci_code': '60,C,30',    'etf': 'ewa.us',  'region': 'Asia-Pacific', 'type': 'Developed'},
    'Hong Kong':     {'msci_code': '75,C,30',    'etf': 'ewh.us',  'region': 'Asia-Pacific', 'type': 'Developed'},
    'Japan':         {'msci_code': '83,C,30',    'etf': 'ewj.us',  'region': 'Asia-Pacific', 'type': 'Developed'},
    'New Zealand':   {'msci_code': '90,C,30',    'etf': 'enzl.us', 'region': 'Asia-Pacific', 'type': 'Developed'},
    'Singapore':     {'msci_code': '181,C,30',   'etf': 'ews.us',  'region': 'Asia-Pacific', 'type': 'Developed'},
    'Israel':        {'msci_code': '2352,C,30',  'etf': 'eis.us',  'region': 'EMEA',         'type': 'Developed'},
    'Austria':       {'msci_code': '61,C,30',    'etf': 'ewo.us',  'region': 'EMEA',         'type': 'Developed'},
    'Belgium':       {'msci_code': '62,C,30',    'etf': 'ewk.us',  'region': 'EMEA',         'type': 'Developed'},
    'Denmark':       {'msci_code': '69,C,30',    'etf': 'edenx.us','region': 'EMEA',         'type': 'Developed'},
    'Finland':       {'msci_code': '70,C,30',    'etf': 'efnl.us', 'region': 'EMEA',         'type': 'Developed'},
    'France':        {'msci_code': '71,C,30',    'etf': 'ewq.us',  'region': 'EMEA',         'type': 'Developed'},
    'Germany':       {'msci_code': '73,C,30',    'etf': 'ewg.us',  'region': 'EMEA',         'type': 'Developed'},
    'Ireland':       {'msci_code': '79,C,30',    'etf': 'eirl.us', 'region': 'EMEA',         'type': 'Developed'},
    'Italy':         {'msci_code': '81,C,30',    'etf': 'ewi.us',  'region': 'EMEA',         'type': 'Developed'},
    'Netherlands':   {'msci_code': '89,C,30',    'etf': 'ewn.us',  'region': 'EMEA',         'type': 'Developed'},
    'Norway':        {'msci_code': '91,C,30',    'etf': 'enor.us', 'region': 'EMEA',         'type': 'Developed'},
    'Portugal':      {'msci_code': '1190,C,30',  'etf': 'pgal.us', 'region': 'EMEA',         'type': 'Developed'},
    'Spain':         {'msci_code': '98,C,30',    'etf': 'ewp.us',  'region': 'EMEA',         'type': 'Developed'},
    'Sweden':        {'msci_code': '99,C,30',    'etf': 'ewd.us',  'region': 'EMEA',         'type': 'Developed'},
    'Switzerland':   {'msci_code': '100,C,30',   'etf': 'ewl.us',  'region': 'EMEA',         'type': 'Developed'},
    'United Kingdom':{'msci_code': '103,C,30',   'etf': 'ewu.us',  'region': 'EMEA',         'type': 'Developed'},

    # ---- EMERGING (21) ----
    'Brazil':        {'msci_code': '63,C,30',    'etf': 'ewz.us',  'region': 'Americas',     'type': 'Emerging'},
    'Chile':         {'msci_code': '65,C,30',    'etf': 'ech.us',  'region': 'Americas',     'type': 'Emerging'},
    'Colombia':      {'msci_code': '1890,C,30',  'etf': 'gxg.us',  'region': 'Americas',     'type': 'Emerging'},
    'Peru':          {'msci_code': '2323,C,30',  'etf': 'epu.us',  'region': 'Americas',     'type': 'Emerging'},
    'Mexico':        {'msci_code': '2,C,30',     'etf': 'eww.us',  'region': 'Americas',     'type': 'Emerging'},
    'China':         {'msci_code': '2713,C,30',  'etf': 'mchi.us', 'region': 'Asia-Pacific', 'type': 'Emerging'},
    'India':         {'msci_code': '77,C,30',    'etf': 'inda.us', 'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Indonesia':     {'msci_code': '2879,C,30',  'etf': 'eido.us', 'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Korea':         {'msci_code': '85,C,30',    'etf': 'ewy.us',  'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Malaysia':      {'msci_code': '2880,C,30',  'etf': 'ewm.us',  'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Philippines':   {'msci_code': '4,C,30',     'etf': 'ephe.us', 'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Taiwan':        {'msci_code': '66,C,30',    'etf': 'ewt.us',  'region': 'Asia-Pacific', 'type': 'Emerging'},
    'Thailand':      {'msci_code': '2881,C,30',  'etf': 'thd.us',  'region': 'Asia-Pacific', 'type': 'Emerging'},
    'South Africa':  {'msci_code': '2428,C,30',  'etf': 'eza.us',  'region': 'EMEA',         'type': 'Emerging'},
    'Egypt':         {'msci_code': '2877,C,30',  'etf': 'egpt.us', 'region': 'EMEA',         'type': 'Emerging'},
    'Saudi Arabia':  {'msci_code': '136064,C,30','etf': 'ksa.us',  'region': 'EMEA',         'type': 'Emerging'},
    'UAE':           {'msci_code': '25560,C,30', 'etf': 'uae.us',  'region': 'EMEA',         'type': 'Emerging'},
    'Qatar':         {'msci_code': '25558,C,30', 'etf': 'qat.us',  'region': 'EMEA',         'type': 'Emerging'},
    'Turkey':        {'msci_code': '102,C,30',   'etf': 'tur.us',  'region': 'EMEA',         'type': 'Emerging'},
    'Poland':        {'msci_code': '95,C,30',    'etf': 'epol.us', 'region': 'EMEA',         'type': 'Emerging'},
    'Greece':        {'msci_code': '1146,C,30',  'etf': 'grek.us', 'region': 'EMEA',         'type': 'Emerging'},
}


# =========================================================================
# PRIMARY PATH: Playwright scrape of MSCI's end-of-day data search page
# =========================================================================
async def scrape_msci_playwright():
    """
    Loads the MSCI Index Data Search page in headless Chromium, intercepts the
    XHR responses that populate the country performance table, and parses out
    the 1D / MTD / YTD / 1Y returns.

    MSCI's site is JSF-based (JavaServer Faces) and the data lands via AJAX
    after the page configures itself. We listen on all responses and look
    for ones that contain the country names + performance numbers.
    """
    from playwright.async_api import async_playwright

    print("[MSCI] Launching headless Chromium...")
    captured_responses = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            locale='en-GB',
            timezone_id='Europe/London',
        )
        page = await context.new_page()

        # Intercept all XHR / fetch responses
        async def handle_response(response):
            url = response.url
            ct = response.headers.get('content-type', '').lower()
            if any(k in url.lower() for k in ['indexperf', 'index-data', 'performance', 'chart', 'webapp']):
                try:
                    body = await response.text()
                    if body and len(body) > 50:
                        captured_responses.append({
                            'url': url,
                            'content_type': ct,
                            'body': body[:200000],  # cap at 200KB per response
                        })
                        print(f"[MSCI] captured {response.status} {url[:80]} ({ct}, {len(body)}b)")
                except Exception as e:
                    pass

        page.on('response', handle_response)

        # Visit the MSCI Index Data Search page
        url = 'https://app2.msci.com/products/index-data-search/'
        print(f"[MSCI] Navigating to {url}")
        try:
            await page.goto(url, wait_until='networkidle', timeout=60000)
        except Exception as e:
            print(f"[MSCI] navigation timed out: {e}", file=sys.stderr)

        # Give JS a moment to fire follow-up requests
        await page.wait_for_timeout(8000)

        # Try to click the "Country" tab + Search button to force data load
        try:
            await page.click('text=Country', timeout=5000)
            await page.wait_for_timeout(3000)
        except Exception:
            pass

        try:
            search_btn = page.locator('button:has-text("Search"), input[type=submit][value*="Search" i]').first
            if await search_btn.count() > 0:
                await search_btn.click(timeout=5000)
                await page.wait_for_timeout(5000)
        except Exception:
            pass

        # Also try the regional chart URL directly
        try:
            await page.goto(
                'https://app2.msci.com/webapp/indexperf/pages/IEIPerformanceRegional.jsf',
                wait_until='networkidle', timeout=45000
            )
            await page.wait_for_timeout(8000)
        except Exception as e:
            print(f"[MSCI] regional page nav failed: {e}", file=sys.stderr)

        # Try to extract rendered table data from DOM as a last resort
        dom_table = None
        try:
            dom_table = await page.evaluate('''() => {
                const rows = [];
                document.querySelectorAll('table tr').forEach(tr => {
                    const cells = Array.from(tr.querySelectorAll('td, th')).map(c => c.innerText.trim());
                    if (cells.length >= 4) rows.push(cells);
                });
                return rows;
            }''')
            print(f"[MSCI] DOM table rows: {len(dom_table) if dom_table else 0}")
        except Exception as e:
            print(f"[MSCI] DOM extract failed: {e}", file=sys.stderr)

        await browser.close()

    # Parse captured responses
    print(f"[MSCI] Total captured responses: {len(captured_responses)}")
    parsed = parse_msci_responses(captured_responses, dom_table)
    return parsed


def parse_msci_responses(responses, dom_table=None):
    """
    Walk through captured response bodies looking for performance data.
    MSCI's responses are typically XML or partial-HTML (JSF Ajax responses).
    We look for country names + numeric values nearby.
    """
    results = {}

    # First try DOM-extracted table
    if dom_table:
        results.update(parse_dom_table(dom_table))
        if results:
            print(f"[MSCI] DOM table yielded {len(results)} markets")

    # Then walk XHR bodies for any additional/missing countries
    country_aliases = build_country_aliases()
    for resp in responses:
        body = resp['body']
        # Look for country-name patterns followed by numeric values
        for alias, canonical in country_aliases.items():
            if canonical in results:
                continue
            # Match: country name then up to 4 numeric values
            pattern = re.escape(alias) + r'[^\d\-\+]{0,40}([\-\+]?\d+\.\d+)[^\d\-\+]{1,40}([\-\+]?\d+\.\d+)[^\d\-\+]{1,40}([\-\+]?\d+\.\d+)[^\d\-\+]{1,40}([\-\+]?\d+\.\d+)'
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                try:
                    vals = [float(x) for x in m.groups()]
                    # Heuristic: 1D values should be small (-5 to 5), 1Y can be big
                    if abs(vals[0]) < 15:
                        results[canonical] = {
                            'day':   vals[0],
                            'mtd':   vals[1],
                            'ytd':   vals[2],
                            'oneYr': vals[3],
                        }
                except Exception:
                    pass

    return results


def parse_dom_table(rows):
    """Extract country -> returns from a rendered HTML table."""
    out = {}
    country_aliases = build_country_aliases()
    num_re = re.compile(r'^-?\d+\.?\d*$')

    for row in rows:
        if not row:
            continue
        first = row[0].strip()
        canonical = country_aliases.get(first.upper())
        if not canonical:
            # Try fuzzy match against country names
            for alias, c in country_aliases.items():
                if first.upper() == alias or first.upper().startswith(alias):
                    canonical = c
                    break
        if not canonical:
            continue

        # Extract numeric cells
        numerics = []
        for cell in row[1:]:
            cleaned = cell.replace(',', '').replace('%', '').strip()
            if num_re.match(cleaned):
                numerics.append(float(cleaned))

        if len(numerics) >= 4:
            out[canonical] = {
                'day':   numerics[0],
                'mtd':   numerics[1],
                'ytd':   numerics[2] if len(numerics) >= 3 else None,
                'oneYr': numerics[3] if len(numerics) >= 4 else None,
            }
    return out


def build_country_aliases():
    """Map various spellings/casings to our canonical country name."""
    aliases = {}
    for canon in MARKETS:
        aliases[canon.upper()] = canon
    # Common alternate spellings
    aliases['UNITED STATES'] = 'USA'
    aliases['US'] = 'USA'
    aliases['UNITED ARAB EMIRATES'] = 'UAE'
    aliases['KOREA, REPUBLIC OF'] = 'Korea'
    aliases['SOUTH KOREA'] = 'Korea'
    aliases['CZECH REPUBLIC'] = None  # not in our set
    return {k: v for k, v in aliases.items() if v}


# =========================================================================
# FALLBACK PATH: ETF proxies via Stooq
# =========================================================================
def fetch_etf_returns():
    """
    Fetch daily-resolution price history from Stooq for each country ETF and
    compute 1D / MTD / YTD / 1Y returns. Stooq is free, returns CSV, and is
    CORS-friendly (but we're running server-side anyway).
    """
    import requests
    print("[ETF] Fetching country ETF prices from Stooq...")

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=400)
    results = {}

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/121.0.0.0 Safari/537.36'
    })

    for country, meta in MARKETS.items():
        ticker = meta['etf']
        url = f"https://stooq.com/q/d/l/?s={ticker}&i=d&d1={start:%Y%m%d}&d2={today:%Y%m%d}"
        try:
            r = session.get(url, timeout=20)
            if r.status_code != 200 or 'No data' in r.text[:200]:
                print(f"[ETF] {country}: no data from {ticker}", file=sys.stderr)
                continue

            lines = [l for l in r.text.strip().splitlines() if l]
            if len(lines) < 5:
                continue

            # CSV: Date,Open,High,Low,Close,Volume
            rows = []
            for line in lines[1:]:  # skip header
                parts = line.split(',')
                if len(parts) >= 5:
                    try:
                        rows.append({
                            'date': datetime.strptime(parts[0], '%Y-%m-%d').date(),
                            'close': float(parts[4]),
                        })
                    except ValueError:
                        pass

            if len(rows) < 2:
                continue

            rows.sort(key=lambda x: x['date'])
            last = rows[-1]
            prev_day = rows[-2]

            # Find end-of-previous-month close
            this_month = last['date'].replace(day=1)
            prev_month_rows = [r for r in rows if r['date'] < this_month]
            month_anchor = prev_month_rows[-1] if prev_month_rows else rows[0]

            # Find end-of-previous-year close
            this_year_start = last['date'].replace(month=1, day=1)
            prev_year_rows = [r for r in rows if r['date'] < this_year_start]
            year_anchor = prev_year_rows[-1] if prev_year_rows else rows[0]

            # Find ~1Y ago close (closest to 365 days back)
            target = last['date'] - timedelta(days=365)
            one_yr_anchor = min(rows, key=lambda r: abs((r['date'] - target).days))

            results[country] = {
                'day':   round((last['close'] / prev_day['close'] - 1) * 100, 2),
                'mtd':   round((last['close'] / month_anchor['close'] - 1) * 100, 2),
                'ytd':   round((last['close'] / year_anchor['close'] - 1) * 100, 2),
                'oneYr': round((last['close'] / one_yr_anchor['close'] - 1) * 100, 2),
                '_as_of': last['date'].isoformat(),
            }
            print(f"[ETF] {country:18s} {ticker:9s} 1D={results[country]['day']:+6.2f}  "
                  f"MTD={results[country]['mtd']:+6.2f}  YTD={results[country]['ytd']:+7.2f}  "
                  f"1Y={results[country]['oneYr']:+7.2f}")

        except Exception as e:
            print(f"[ETF] {country}: {e}", file=sys.stderr)

    return results


# =========================================================================
# ORCHESTRATION
# =========================================================================
def build_output(market_data, source, as_of=None):
    """Build the final JSON payload."""
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

    # Require at least 30 of 44 markets from MSCI to consider it a success
    if len(msci_data) >= 30:
        print(f"[OK] MSCI scrape captured {len(msci_data)}/{len(MARKETS)} markets — using as source")
        output = build_output(msci_data, source='MSCI')
    else:
        print(f"[WARN] MSCI scrape only captured {len(msci_data)} markets — falling back to ETF proxies")
        try:
            etf_data = fetch_etf_returns()
            if len(etf_data) >= 30:
                output = build_output(etf_data, source='ETF_PROXY')
            else:
                print(f"[ERR] ETF fallback only got {len(etf_data)} — keeping previous data", file=sys.stderr)
                if previous:
                    print("[OK] previous data retained")
                    return 1
                else:
                    print("[ERR] no previous data either, writing empty file", file=sys.stderr)
                    output = build_output({}, source='FAILED')
        except Exception as e:
            print(f"[ERR] ETF fallback exception: {e}", file=sys.stderr)
            if previous:
                return 1
            output = build_output({}, source='FAILED')

    out_path.write_text(json.dumps(output, indent=2))
    print(f"[OK] wrote {out_path} ({output['marketsCount']} markets, source={output['source']})")
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
