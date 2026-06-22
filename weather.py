"""
weather.py — DIY environmental annotation via Open-Meteo's ERA5 archive (keyless).

Strategy (and its limits): we snap each fix to a 0.25-degree grid cell + calendar
day, dedupe, and batch-query Open-Meteo's historical archive for those cells. 0.25
deg is intentional — it's ERA5's native resolution AND matches Movebank's EURING_04
profile, so the join is honest about its own coarseness.

LIMITS — read before you draw conclusions:
  * This is a nearest-cell / nearest-day join, NOT spatiotemporal interpolation.
    Movebank's Env-DATA service does the rigorous version (interpolated to each
    fix). Use this for visualization & hypothesis generation, not causal claims.
  * Open-Meteo has its own rate limits. We dedupe hard and chunk requests.
  * archive-api.open-meteo.com lags real-time by ~5 days.

Variables chosen for migration relevance:
  precipitation_sum, temperature_2m_mean, et0_fao_evapotranspiration (dryness proxy),
  wind_speed_10m_max, wind_direction_10m_dominant.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
DAILY_VARS = [
    "precipitation_sum",
    "temperature_2m_mean",
    "et0_fao_evapotranspiration",
    "wind_speed_10m_max",
    "wind_direction_10m_dominant",
]


def _snap(v: float, grid: float) -> float:
    return round(round(v / grid) * grid, 2)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def grid_cells(individuals: list[dict], grid_deg: float = 0.25) -> dict[tuple[float, float], tuple[str, str]]:
    """
    Collapse all fixes to the unique (cell_lat, cell_lon) -> (min_day, max_day)
    needed to cover the data. One Open-Meteo call per cell covers its whole date span.
    """
    span: dict[tuple[float, float], list[str]] = defaultdict(list)
    for rec in individuals:
        for p in rec["locations"]:
            cell = (_snap(p["lat"], grid_deg), _snap(p["lon"], grid_deg))
            span[cell].append(_day(p["ms"]))
    return {cell: (min(days), max(days)) for cell, days in span.items()}


def fetch_weather(
    individuals: list[dict],
    grid_deg: float = 0.25,
    max_cells: int = 300,
    polite_delay_s: float = 0.3,
    timeout: float = 60.0,
) -> dict:
    """Return {'grid_deg', 'cells':[{lat,lon,date,<vars>}], 'n_cells', 'n_days'}."""
    cells = grid_cells(individuals, grid_deg)
    if len(cells) > max_cells:
        raise ValueError(
            f"{len(cells)} grid cells exceeds max_cells={max_cells}. "
            f"Reduce the track first (EURING_02 reduction profile or a tighter time window)."
        )

    out_rows: list[dict] = []
    with httpx.Client(timeout=timeout) as client:
        for (lat, lon), (d0, d1) in cells.items():
            params = {
                "latitude": lat, "longitude": lon,
                "start_date": d0, "end_date": d1,
                "daily": ",".join(DAILY_VARS),
                "timezone": "UTC",
            }
            r = client.get(ARCHIVE, params=params)
            r.raise_for_status()
            daily = r.json().get("daily", {})
            dates = daily.get("time", [])
            for i, date in enumerate(dates):
                row = {"lat": lat, "lon": lon, "date": date}
                for v in DAILY_VARS:
                    arr = daily.get(v) or []
                    row[v] = arr[i] if i < len(arr) else None
                out_rows.append(row)
            time.sleep(polite_delay_s)  # be a good citizen

    days = {r["date"] for r in out_rows}
    return {
        "grid_deg": grid_deg,
        "n_cells": len(cells),
        "n_days": len(days),
        "cells": out_rows,
    }


def annotate_and_summarize(individuals: list[dict], weather: dict, grid_deg: float = 0.25) -> dict:
    """
    Per-individual exposure summary: average precip / temp / wind the animal
    actually experienced along its path. This is the number you might *correlate*
    with timing — caveats in the module docstring still apply.
    """
    lut = {(c["lat"], c["lon"], c["date"]): c for c in weather["cells"]}
    summaries = []
    for rec in individuals:
        vals = defaultdict(list)
        for p in rec["locations"]:
            key = (_snap(p["lat"], grid_deg), _snap(p["lon"], grid_deg), _day(p["ms"]))
            cell = lut.get(key)
            if not cell:
                continue
            for v in DAILY_VARS:
                if cell.get(v) is not None:
                    vals[v].append(cell[v])
        summaries.append({
            "individual": rec["individual"],
            "mean_precip_mm": _avg(vals["precipitation_sum"]),
            "mean_temp_c": _avg(vals["temperature_2m_mean"]),
            "mean_et0_mm": _avg(vals["et0_fao_evapotranspiration"]),
            "mean_wind_max_kmh": _avg(vals["wind_speed_10m_max"]),
            "n_annotated_fixes": len(vals["precipitation_sum"]),
        })
    return {"by_individual": summaries}


def _avg(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None
