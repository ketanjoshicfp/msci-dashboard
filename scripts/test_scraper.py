"""
Minimal pytest for scrape_msci helpers — runs in CI without network access.

Covers:
  * parse_pct: number-coercion edge cases.
  * build_country_aliases: canonical-name lookup.
  * update_history: same-day de-dupe and HISTORY_CAP enforcement.
  * validate_output: catches malformed payloads.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from scrape_msci import (
    parse_pct,
    build_country_aliases,
    update_history,
    validate_output,
    HISTORY_CAP,
    HISTORY_SCHEMA_VERSION,
    MARKETS,
)


# ------------------------------------------------------------------ parse_pct

@pytest.mark.parametrize('raw,expected', [
    ('12.34', 12.34),
    ('-3.5', -3.5),
    ('+7', 7.0),
    ('1,234.5', 1234.5),
    ('17.30%', 17.30),
    ('', None),
    ('—', None),
    ('-', None),
    ('N/A', None),
    (None, None),
    ('abc', None),
])
def test_parse_pct(raw, expected):
    assert parse_pct(raw) == expected


# ----------------------------------------------------------- country aliases

def test_aliases_canonicalise_known_variants():
    a = build_country_aliases()
    assert a['UNITED STATES'] == 'USA'
    assert a['US'] == 'USA'
    # Every canonical market name maps to itself.
    for canon in MARKETS:
        assert a[canon.upper()] == canon


# ----------------------------------------------------------- update_history

def test_update_history_dedupes_same_day(tmp_path):
    p = tmp_path / 'history.json'
    today = date(2026, 5, 28)

    primary = {'USA': {'day': 0.5, 'mtd': 1.2, 'threeMtd': 3.0, 'ytd': 8.0, 'oneYr': 18.0}}
    etf = {'USA': {'_close': 100.0}}
    update_history(p, primary, etf, today)
    update_history(p, primary, etf, today)  # same day re-run

    h = json.loads(p.read_text())
    assert h['schemaVersion'] == HISTORY_SCHEMA_VERSION
    assert len(h['series']['USA']) == 1
    assert h['series']['USA'][0]['d'] == '2026-05-28'
    assert h['series']['USA'][0]['close'] == 100.0


def test_update_history_caps_to_history_cap(tmp_path):
    p = tmp_path / 'history.json'
    primary = {'USA': {'day': 0.1, 'mtd': 1.0, 'threeMtd': 2.0, 'ytd': 5.0, 'oneYr': 10.0}}
    etf = {'USA': {}}
    # Inject HISTORY_CAP + 5 distinct dates.
    for i in range(HISTORY_CAP + 5):
        d = date(2024, 1, 1) + timedelta(days=i)
        update_history(p, primary, etf, d)
    h = json.loads(p.read_text())
    series = h['series']['USA']
    assert len(series) == HISTORY_CAP
    # Most recent point survives.
    assert series[-1]['d'] == (date(2024, 1, 1) + timedelta(days=HISTORY_CAP + 4)).isoformat()


# ----------------------------------------------------------- validate_output

def _good_output(n=44):
    """Return a synthetic but well-formed output dict for round-trip checks."""
    countries = list(MARKETS.keys())[:n]
    return {
        'lastUpdated': '2026-05-28T23:00:00+00:00',
        'source': 'MSCI',
        'asOf': '2026-05-28',
        'marketsCount': n,
        'expectedCount': len(MARKETS),
        'markets': [
            {
                'country': c, 'region': MARKETS[c]['region'], 'type': MARKETS[c]['type'],
                'day': 0.1, 'mtd': 1.0, 'threeMtd': 2.0, 'ytd': 5.0,
                'oneYr': 10.0, 'threeYr': 8.0, 'fiveYr': 7.0, 'tenYr': 6.0,
            } for c in countries
        ],
    }


def test_validate_output_accepts_good_payload():
    assert validate_output(_good_output()) == []


def test_validate_output_rejects_missing_required_field():
    out = _good_output()
    del out['markets'][0]['oneYr']
    probs = validate_output(out)
    assert any('missing field oneYr' in p for p in probs)


def test_validate_output_rejects_insane_value():
    out = _good_output()
    out['markets'][0]['day'] = 5000  # >>> sane bound
    probs = validate_output(out)
    assert any('out of sane bounds' in p for p in probs)


def test_validate_output_rejects_too_few_markets():
    out = _good_output(n=5)
    probs = validate_output(out, min_markets=25)
    assert any('too few markets' in p for p in probs)
