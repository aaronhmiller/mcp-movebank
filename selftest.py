"""selftest.py — exercises the pure logic with synthetic data (no network)."""
import json
import pathlib

import transforms
import weather as wx

# Two synthetic animals; one starts a month later (tests the global clock).
def make(ind, taxon, base_ms, dlat):
    locs = []
    for i in range(50):
        locs.append({"ms": base_ms + i * 86400_000, "lon": -89.0 + i * 0.3, "lat": -1.0 + i * dlat})
    return {"individual": ind, "taxon": taxon, "locations": locs}

JUN = 1212192000000  # 2008-05-31-ish
inds = [make("A", "Phoebastria irrorata", JUN, 0.20),
        make("B", "Phoebastria irrorata", JUN + 30 * 86400_000, -0.15)]

payload = transforms.build_trips(inds, max_points_per_individual=40)
m = payload["meta"]
assert m["n_individuals"] == 2, m
# Global clock: B's first vertex timestamp must be ~30 days, not 0.
b_first_ts = next(t["timestamps"][0] for t in payload["trips"] if t["individual"] == "B")
assert abs(b_first_ts - 30 * 86400) < 86400, b_first_ts
# Decimation cap honored.
assert all(len(t["path"]) <= 40 for t in payload["trips"])
# Timestamps monotonic and aligned with path length.
for t in payload["trips"]:
    assert len(t["path"]) == len(t["timestamps"])
    assert t["timestamps"] == sorted(t["timestamps"])
print(f"transforms OK  | individuals={m['n_individuals']} bbox={m['bbox']} "
      f"duration_days={m['duration_s']/86400:.0f} A_km={m['individuals'][0]['track_km']}")

# Weather gridding (pure part — no fetch). Confirm cell collapse + day spans.
cells = wx.grid_cells(inds, grid_deg=0.25)
assert len(cells) > 0
sample_cell = next(iter(cells.values()))
assert sample_cell[0] <= sample_cell[1]  # (min_day, max_day)
print(f"weather grid OK | unique_cells={len(cells)} (vs {sum(len(i['locations']) for i in inds)} raw fixes)")

# annotate_and_summarize against a fake weather payload.
fake_cells = [{"lat": c[0], "lon": c[1], "date": c[2][0] if isinstance(c[2], tuple) else c[2],
               "precipitation_sum": 5.0, "temperature_2m_mean": 24.0,
               "et0_fao_evapotranspiration": 4.0, "wind_speed_10m_max": 30.0,
               "wind_direction_10m_dominant": 180}
              for c in [(k[0], k[1], v) for k, v in cells.items()]]
fake_weather = {"grid_deg": 0.25, "n_cells": len(cells), "n_days": 1, "cells": fake_cells}
exp = wx.annotate_and_summarize(inds, fake_weather, grid_deg=0.25)
print(f"exposure OK     | {exp['by_individual'][0]}")

# HTML template substitution.
tpl = pathlib.Path("trips_map.html").read_text()
html = (tpl.replace("/*__TRIPS__*/null", json.dumps(payload))
           .replace("/*__WEATHER__*/null", json.dumps(fake_weather))
           .replace("__TITLE__", "Self Test"))
assert "/*__TRIPS__*/null" not in html and '"trips"' in html
assert "Self Test" in html
print(f"template OK     | html_bytes={len(html)}")
print("\nALL SELFTESTS PASSED")
