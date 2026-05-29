# MSCI World Markets Dashboard — Implementation Plan

> Hand-off plan for Claude Code. Work top-to-bottom: **Phase 0 (bug fixes)** first,
> then **Phase 1 (history foundation)** because most later features depend on it,
> then the remaining phases in any order. Each task lists the files to touch,
> concrete implementation notes tied to the existing code, and acceptance criteria.

---

## 0. Orientation (read first)

**Architecture.** A single static `index.html` (~3,550 lines: embedded CSS in `<style>`,
embedded JS in `<script>`) reads `data/msci-data.json` from `raw.githubusercontent.com`.
A GitHub Action (`.github/workflows/scrape-msci.yml`) runs `scripts/scrape_msci.py`
every weekday 23:00 UTC, scrapes MSCI via Playwright (ETF proxy fallback via yfinance),
and commits a fresh `data/msci-data.json`.

**Key JS landmarks in `index.html`:**
- `CONFIG` / `DATA_URL` — data source config (~line 1887).
- `COUNTRY_META`, `COUNTRY_FLAGS`, `METRIC_LABELS`, `METRIC_SHORT` — lookup tables (~1904–1985).
- `state` — single source of truth: `data`, `filtered`, `filters`, `metric`, `sort`, chart handles (~1991).
- `fetchMarketData()` (~2006), `loadCache()`/`saveCache()` (~2039), cache key `msci_dashboard_cache_v3`.
- `applyFilters()` (~2237) → `renderAll()` (~2253) is the central render orchestrator.
- Renderers: `renderInsightStrip`, `renderSummary`, `renderMovers`, `updateMapColors`,
  `updateLegend`, `renderTable`, `renderStatsStrip`, `renderBarChart`, `renderHistogram`, `renderTreemap`.
- `buildColorScale()` (~2177) — percentile-clamped 5-stop diverging scale, reads CSS vars.
- Map: `initMap()` (~3022), `updateMapColors()` (~3072), `handleMapHover()` (~3124).
- Interactivity: `focusCountry()` (~2839), `pulseMapCountry()` (~2853).
- `wireEvents()` (~3349), theme system (~3455), `init()` IIFE (~3519).

**Conventions to follow:**
- No build step, no framework, no npm. Vanilla JS + D3 v7 + Chart.js v4 + topojson, all via CDN.
- All colours come from CSS custom properties so light/dark themes work — **never hardcode a hex in JS**; read with `cssVar('name')` / `cssVarAlpha('name', a)`.
- Metric keys are: `day, mtd, threeMtd, ytd, oneYr, threeYr, fiveYr, tenYr`.
- Percentages format via `fmtPct(v)`; sign-class via `pctClass(v)`.
- Keep the "Folio" visual language: `--font-display` (Fraunces) for headings, `--font-mono` (IBM Plex Mono) for numbers.
- Test by opening `index.html` directly in a browser (it fetches live JSON from GitHub). For local data work, you can temporarily point `CONFIG` at a local file.

---

## Phase 0 — Bug fixes (quick wins, do first)

### 0.1 Theme toggle doesn't re-colour charts
**File:** `index.html` — `applyTheme()` (~3490) and `applyPaper()` (~3502).
**Problem:** Guards on `window.state`, but `state` is a top-level `const` and classic scripts do
*not* attach `const`/`let` to `window`. So `window.state` is `undefined` and `renderAll()` never
fires on theme change — Chart.js canvases keep stale axis/grid/tooltip colours until the next
filter/metric change.
**Fix:** Replace `window.state` with `state` in both functions:
```js
if (state.data && state.data.length) renderAll();
```
**Accept:** Toggle light/dark with the Compare tab open → bar chart, histogram axis ticks,
gridlines and tooltips immediately repaint to the new palette.

### 0.2 `minPerf` filter leaks null-metric rows
**File:** `index.html` — `applyFilters()` (~2237).
**Problem:** `if (typeof d[state.metric] === 'number' && d[state.metric] < minPerf) return false;`
means rows where the active metric is `null` skip the filter entirely.
**Fix:** Decide product behaviour — recommended: when `minPerf > -100`, exclude rows with no value
for the active metric:
```js
const v = d[state.metric];
if (state.filters.minPerf > -100 && !Number.isFinite(v)) return false;
if (Number.isFinite(v) && v < state.filters.minPerf) return false;
```
**Accept:** Raising the slider above −100 never shows a "—" row for the active metric.

### 0.3 Expose the "neutral paper" variant
**File:** `index.html` — header controls markup (~1646) + `wireEvents()` (~3349).
**Problem:** `applyPaper('neutral')` + full stylesheet exist (~1460) but only reachable via
localStorage; no UI.
**Fix:** Add a small paper-toggle (e.g. a second icon button in `.header-actions-row`, only shown
in light mode) that flips `PAPER_STORAGE_KEY` between unset and `'neutral'` and calls `applyPaper()`.
**Accept:** In light mode a control switches cream ↔ neutral off-white; preference persists across reload.

### 0.4 City-state / tiny geometry markers on the map
**File:** `index.html` — `initMap()` (~3022), `updateMapColors()` (~3072).
**Problem:** world-atlas `countries-110m` has no usable polygon for Singapore, Hong Kong, Qatar, UAE
(and barely for others), so they never colour.
**Fix (choose one):**
- (a) Switch the topojson source to `countries-50m.json` (heavier but more complete), **or**
- (b) Keep 110m and overlay small circle markers for a hardcoded list of city-states/small markets,
  positioned via `state.projection([lon, lat])`, coloured by `state.colorScale.fn()`, with the same
  hover tooltip. Add a `SMALL_MARKET_COORDS` lookup `{ 'Singapore': [103.8, 1.35], ... }`.
**Accept:** Singapore, Hong Kong, Qatar, UAE show a coloured mark with a working tooltip.

---

## Phase 1 — History foundation (unlocks Phases 2–3)

> This is the dependency for sparklines, trends, the real Compare tab, and overnight deltas.
> Build it before those.

### 1.1 Append dated snapshots in the scraper
**File:** `scripts/scrape_msci.py` — `main()` (~536), new helper alongside `build_output()`.
**Design:** Keep writing `data/msci-data.json` (current snapshot, unchanged). Additionally maintain a
compact rolling history file `data/history.json`:
```json
{
  "schemaVersion": 1,
  "series": {
    "USA":   [{ "d": "2026-05-28", "oneYr": 26.54, "ytd": 9.62, "day": -0.01, "mtd": 4.27, "threeMtd": 9.37, "close": 123.45 }, ...],
    "Japan": [ ... ]
  }
}
```
**Notes:**
- Append one point per country per successful run, keyed by `asOf` date. **De-dupe** by date
  (overwrite same-day re-runs). Cap each series to the most recent **~400 points** to keep the file small.
- Only append on a *good* run (the `len >= 30` branches). Skip on FAILED.
- Store at least `day, mtd, threeMtd, ytd, oneYr` plus the raw ETF `close` when available
  (close enables future re-derivation). Annualised 3/5/10Y change slowly — daily storage optional.
- Write history with `json.dumps(..., separators=(',',':'))` to minimise size.
**Action file:** `.github/workflows/scrape-msci.yml` — `git add data/msci-data.json data/history.json`
in the commit step (~bottom).
**Accept:** After two runs on different days, `data/history.json` has two dated points per market;
re-running same day does not duplicate.

### 1.2 Backfill initial history (one-off)
**File:** `scripts/backfill_history.py` (new).
**Design:** Use the ETF path's `period='max'` daily closes (already fetched in `fetch_etf_returns`,
~401) to reconstruct a daily/weekly history of `ytd`/`oneYr`/etc. for the last ~12–18 months so charts
aren't empty on day one. Write the same `data/history.json` schema. Document running it once locally.
**Accept:** `history.json` ships with ≥6 months of points for the ETF-backed markets.

### 1.3 Client loads history lazily
**File:** `index.html` — near `fetchMarketData()` (~2006).
**Design:** Add `fetchHistory()` that fetches `data/history.json` (same `raw.githubusercontent.com`
base) **only when first needed** (opening a country drawer or the Compare tab), cache in
`state.history` and in localStorage under a new key with TTL. Don't block initial paint on it.
**Accept:** History loads on demand; initial dashboard load time unchanged.

---

## Phase 2 — History-powered features

### 2.1 Per-country sparklines
**Files:** `index.html` — `renderTable()` (~2494), tooltips in `handleMapHover` (~3124) and
`handleTreemapHover` (~2991).
**Design:** Add a small inline SVG sparkline (last ~90 points of the active metric) in a new table
column and in tooltips. Pure inline SVG `<polyline>` (no new library); colour the last segment by
`pctClass`. Guard gracefully when `state.history` isn't loaded yet (render blank/skeleton).
**Accept:** Each market shows a mini-trend; switching the active metric redraws sparklines.

### 2.2 Real "Compare" tab
**Files:** `index.html` — Compare panel markup (~1848) and a new `renderComparison()` renderer; wire in
`renderAll()` (~2253) and `setActiveTab()` (~3160).
**Problem:** The tab is named *Compare* but only shows aggregate views (treemap/ranking/histogram).
**Design:** Add a multi-select (reuse `.chip` styling or a searchable picker) to choose 2–5 countries,
persisted in localStorage. Render a Chart.js **line chart** of each selected country's active-metric
history over a selectable window (3M / 6M / 1Y / Max). Keep the existing aggregate charts below, or move
them to a sub-section. Use distinct line colours (brass accent + a small categorical palette derived
from CSS vars).
**Accept:** Selecting USA + Japan + India plots three history lines; window switcher rescales; choices persist.

### 2.3 Country detail drawer
**Files:** `index.html` — new `.detail-drawer` markup + CSS, `openCountryDrawer(country)` function;
hook into existing click handlers in `focusCountry()` (~2839), map click (~3056), treemap click (~2942),
mover click (~3425).
**Design:** Slide-out right-hand panel showing the country flag/name, all 8 metrics with `pctClass`
colouring, a larger history sparkline/line, and its rank within its region and within its type
(developed/emerging). Close on Esc / backdrop click / X.
**Accept:** Clicking a country anywhere opens the drawer with correct metrics, history, and ranks.

### 2.4 Overnight / since-last-scrape deltas
**Files:** `index.html` — `renderSummary()` (~2386), `renderTable()` (~2494).
**Design:** Compare the latest history point to the previous one; show a small ▲/▼ delta beside the
active metric (and on summary cards). If only one history point exists, hide deltas.
**Accept:** After ≥2 scrapes, table/summary show day-over-day change indicators.

---

## Phase 3 — Standalone features (no history dependency)

### 3.1 Export (CSV + PNG)
**Files:** `index.html` — new buttons near the filter bar (~1671) or section headers; `exportCSV()` and
`exportPNG()` helpers.
**Design:**
- **CSV:** serialise `state.filtered` (respecting current sort) to CSV, trigger download via a Blob +
  `<a download>`. No library.
- **PNG:** Chart.js exposes `chart.toBase64Image()` for bar/histogram. For the map/treemap (SVG),
  serialise the SVG → draw to a `<canvas>` → `toDataURL('image/png')` (no external dep needed; if it
  gets fiddly, document `html-to-image` from CDN as an optional fallback).
- Keep the README's data-licensing note in mind — exports are for personal use; add a small caption
  stamp ("MSCI/ETF proxy · as of <date> · personal use") onto image exports.
**Accept:** CSV opens cleanly in Excel with correct headers/values; PNG downloads for each chart and the map.

### 3.2 Watchlist / favourites
**Files:** `index.html` — `renderTable()` (~2494) (add a star toggle), `applyFilters()` (~2237),
filter bar (~1671), new `state.favourites` (Set) persisted in localStorage.
**Design:** Star icon per row toggles favourite; add a "Favourites only" chip to the filter bar.
**Accept:** Starred markets persist across reload; the filter shows only favourites when active.

### 3.3 Deep-linkable state
**Files:** `index.html` — `applyFilters()`/`setMetric()`/`setActiveTab()` and `init()` (~3519).
**Design:** Mirror metric, tab, region, type, search, minPerf into the URL query string
(`history.replaceState`), and read them on boot (the theme system already does this for `?theme=`).
Precedence: URL > defaults.
**Accept:** Configuring filters updates the URL; pasting that URL into a new tab reproduces the view.

### 3.4 Scatter view (momentum)
**Files:** `index.html` — add a chart card to the Compare tab (~1848) + `renderScatter()`.
**Design:** Chart.js scatter, X = one metric (e.g. YTD), Y = another (e.g. 1Y), point colour by region
or type, tooltip = country. Axis-metric pickers (two small dropdowns). Reuse `rowHighlightPlugin` styling
language for consistency.
**Accept:** Scatter renders one point per visible market; changing axis metrics re-plots; tooltip names the country.

### 3.5 Range filter: min AND max, any metric
**Files:** `index.html` — filter bar (~1671), `state.filters` (~1994), `applyFilters()` (~2237),
`wireEvents()` (~3382).
**Design:** Replace the single min slider with a dual-thumb min/max range (two `<input type=range>` or a
lightweight custom dual-slider) and an optional "filter metric" selector so you can filter on a metric
other than the active display metric.
**Accept:** Setting min/max narrows the set correctly; filtering on a non-active metric works.

### 3.6 Staleness warning
**Files:** `index.html` — `renderDataSourceBanner()` (~3243), `loadAndRender()` (~3262).
**Design:** If `meta.asOf` (or `lastUpdated`) is older than a threshold (e.g. >3 calendar days,
accounting for weekends), show a prominent amber notice in the info banner ("Data is N days old —
last successful scrape <date>").
**Accept:** Forcing an old `asOf` in local test data shows the warning; fresh data does not.

### 3.7 Surface the validation block
**Files:** `index.html` — `renderDataSourceBanner()` (~3243) or a new small badge near the banner.
**Design:** `data/msci-data.json` already carries `validation` (`compared`, `discrepancyCount`,
`discrepancies[]`). Show a confidence chip: e.g. "MSCI vs ETF: 0 discrepancies / 38 compared" with a
hover/expand listing the worst offenders. Green when clean, amber when discrepancies exist.
**Accept:** Banner reflects the JSON's validation numbers; expanding lists discrepant country/metric pairs.

### 3.8 Accessibility pass
**Files:** `index.html` — table (~1825), charts, map, filter controls.
**Design:** Keyboard-navigable table (roving tabindex on rows, Enter opens drawer), ARIA labels on
icon-only buttons (zoom, theme, star), `aria-sort` on sorted headers, focus-visible styles, and a
text-equivalent (visually-hidden table or `aria-label` summaries) for the map/charts. Reduced-motion is
already handled (~1385) — preserve it.
**Accept:** Full keyboard traversal of filters → table → drawer; Lighthouse a11y score ≥ 90.

---

## Phase 4 — Scraper enrichments (optional, low risk)

### 4.1 Extra metrics from existing ETF history
**File:** `scripts/scrape_msci.py` — `fetch_etf_returns()` (~370).
**Design:** You already pull `period='max'` daily closes. Cheaply add **6M return**, **realised
volatility** (annualised stdev of daily returns over ~1Y), and **max drawdown** (1Y). Emit new fields;
update `build_output()` (~498) and the JSON consumers/labels in `index.html`
(`METRIC_LABELS`/`METRIC_SHORT` ~1972, table headers ~1826, metric toggle ~1704).
**Note:** MSCI scrape path won't provide vol/drawdown — compute these from ETF closes regardless of which
source wins, or mark them ETF-derived.
**Accept:** New columns/metrics appear and populate for ETF-backed markets.

### 4.2 Light schema validation + tests
**Files:** `scripts/` — add a `validate_output(output)` check (expected count, field types, sane ranges)
called at the end of `main()`; optionally a tiny `pytest` for `parse_pct`, `parse_msci_tables`,
`build_country_aliases`, and history de-dupe/cap logic.
**Accept:** A malformed scrape fails loudly in the Action logs instead of committing junk.

---

## Suggested sequencing for Claude Code

1. **Phase 0** (all four — small, independent, immediately visible).
2. **Phase 1.1 + 1.3** (history write + lazy client load), then **1.2** backfill.
3. **Phase 2** in order (2.1 → 2.4) — each builds on `state.history`.
4. **Phase 3** items are independent; do in any order. 3.1 (export) and 3.7 (validation chip) are the
   fastest wins.
5. **Phase 4** last, optional.

## Global acceptance / regression checks
- Dashboard still loads and renders with **no history file present** (graceful degradation).
- Light/dark + neutral-paper all render correctly across every tab (manually toggle on each tab).
- No hardcoded colours introduced in JS — all via `cssVar`.
- `data/msci-data.json` schema unchanged for backward compatibility (history is additive).
- Mobile breakpoints (~1100px, ~720px) still hold for any new UI.
- Action still completes < 15 min and commits both data files.
