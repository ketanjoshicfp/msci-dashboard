# MSCI World Markets Dashboard

A self-updating performance dashboard for 44 MSCI country indices (23 developed + 21 emerging), built as a single static HTML page that reads from a JSON file refreshed daily by a GitHub Action.

## How it works

```
┌────────────────────────────┐         ┌──────────────────────────┐
│ GitHub Actions (daily cron)│ ──►     │ scrape_msci.py           │
│                            │         │  1. Playwright → MSCI    │
│  23:00 UTC, Mon–Fri        │         │  2. Stooq ETF fallback   │
└────────────────────────────┘         └──────────┬───────────────┘
                                                  │ writes
                                                  ▼
                                       ┌──────────────────────────┐
                                       │ data/msci-data.json      │
                                       │ (committed to main)      │
                                       └──────────┬───────────────┘
                                                  │ fetched by
                                                  ▼
                                       ┌──────────────────────────┐
                                       │ index.html               │
                                       │ (your dashboard)         │
                                       └──────────────────────────┘
```

The Python scraper tries MSCI's official end-of-day data page first using a headless Chromium browser. If MSCI is unavailable or returns fewer than 30 of the 44 markets, it falls back to country ETFs (iShares MSCI ETFs like EWZ, EWJ, EWU, INDA) fetched from Stooq, which track the corresponding MSCI indices very closely.

## One-time setup

### 1. Create a new GitHub repo

Create a public repo. The repo must be public for the dashboard to fetch JSON without authentication. Call it whatever you want — `msci-dashboard` is a reasonable name.

### 2. Upload these files to the repo

Preserve the directory structure exactly:

```
your-repo/
├── .github/
│   └── workflows/
│       └── scrape-msci.yml
├── scripts/
│   ├── scrape_msci.py
│   └── requirements.txt
├── data/
│   └── msci-data.json     ← placeholder, the Action will overwrite
├── index.html
└── README.md
```

### 3. Edit two lines in `index.html`

Open `index.html`, find the `CONFIG` block near the top of the script section, and set your GitHub username and repo name:

```js
const CONFIG = {
  githubUser: 'YOUR_GITHUB_USERNAME',   // ← change me
  githubRepo: 'msci-dashboard',          // ← change me if different
  branch:     'main',
  dataPath:   'data/msci-data.json'
};
```

Commit and push.

### 4. Trigger the Action manually for the first run

1. Go to your repo on github.com
2. Click the **Actions** tab
3. Select **Scrape MSCI daily** in the left sidebar
4. Click **Run workflow** → **Run workflow** (green button)
5. Wait ~3–5 minutes for it to finish

You should see a green tick. The Action will have:
- Run the Playwright scraper against MSCI
- Computed 1D / MTD / YTD / 1Y returns
- Committed an updated `data/msci-data.json` to your repo

### 5. Open the dashboard

Two options:

**Open locally:** Just double-click `index.html`. It will fetch the latest data from your repo via `raw.githubusercontent.com`.

**Host on GitHub Pages (recommended):** Settings → Pages → Source: Deploy from branch → main / root → Save. After a few minutes your dashboard will be live at `https://YOUR_USERNAME.github.io/msci-dashboard/`.

## Daily refresh

The Action runs automatically every weekday at 23:00 UTC (just after US market close). Each run commits a new `data/msci-data.json` with that day's numbers. Your dashboard picks up the new data the next time you (or anyone) loads or refreshes it.

You can also click the **Refresh Data** button in the dashboard to force-bypass the local cache and fetch the latest from GitHub.

## Data source indicator

The banner at the top of the dashboard tells you which source the current data came from:

- **Live MSCI end-of-day data** — Scraped directly from MSCI. This is the primary, preferred path.
- **Country-ETF proxy data** — MSCI scraping failed, so the Action used iShares country ETFs as a proxy. Returns will be very close to MSCI's (correlation 0.98+) but not identical, mainly because of NAV-vs-price discrepancies and currency hedging differences.
- **Placeholder data** — The Action hasn't run yet. Trigger it manually from the Actions tab.
- **Last scrape failed** — Check the Actions tab for the failure log.

## Troubleshooting

**Dashboard says "Could not fetch market data"**
Your `CONFIG.githubUser`/`githubRepo` is wrong or the repo isn't public. Open the dashboard in browser DevTools (F12), check the Network tab to see what URL it's trying to hit.

**Action runs but commits zero markets, source=FAILED**
Both MSCI and Stooq are unreachable in that run. Re-run manually in 30 minutes. Check the Action logs for the captured XHR responses — MSCI may have changed their page structure.

**Action runs but source=ETF_PROXY**
MSCI's site has likely changed its JS structure. The ETF data is still good for most purposes. To debug the MSCI path, download the Action's logs and look at the `[MSCI] captured ...` lines to see which endpoints are responding and which aren't.

**Saudi Arabia / Qatar / UAE numbers look stale**
These markets aren't in the older public MSCI download-code list, so the codes in `scrape_msci.py` are best-effort. If they consistently break, comment them out of the `MARKETS` dict and live with 41 markets instead of 44.

**I want different metrics (3Y, 5Y, since inception)**
Edit `parse_msci_responses` and `fetch_etf_returns` in `scripts/scrape_msci.py` to compute and emit additional fields, then update the dashboard's table/chart rendering in `index.html` to surface them.

## A word on data licensing

MSCI's terms state their index data is their property and shouldn't be redistributed without permission. Using a personal dashboard to inform your own market understanding is one thing; embedding the numbers in any client-facing material, marketing collateral, or anything redistributed externally is another. SJP already licenses MSCI properly through internal systems — for anything client-facing, use those channels.

## Cost

Free. GitHub Actions gives 2,000 minutes/month of compute on the free tier; this Action uses ~3 minutes per run, ~22 weekday runs/month = ~66 minutes/month. GitHub Pages and raw.githubusercontent.com are free for public repos.
