"""
server.py — mcp-movebank

A FastMCP server that lets an LLM explore animal-migration data, annotate it with
weather, and emit an animated deck.gl timelapse map.

Transport matches the resilient pattern you've standardized on:
    stateless_http=True, json_response=True  (Streamable HTTP, no SSE session state)

Tool-context discipline (the thing that bites people building MCP over big datasets):
every tool returns a COMPACT summary into model context. Full vertex geometry is
written to OUTPUT_DIR and referenced by path. Point your viz / a static file server
at OUTPUT_DIR; or expose those files as MCP Resources if your host supports it.

Env:
    MOVEBANK_USERNAME / MOVEBANK_PASSWORD   (optional; only for private studies)
    MCP_OUTPUT_DIR                          (default ./mcp_output)
    MCP_HOST / MCP_PORT                     (default 127.0.0.1 / 8765)

Run:
    python server.py
After any restart, remember: clients must re-run tool discovery (tool_search) before
the catalog is callable again.
"""

from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

import transforms
import weather as wx
from movebank_client import MovebankClient, REDUCTION_PROFILES

OUTPUT_DIR = pathlib.Path(os.environ.get("MCP_OUTPUT_DIR", "./mcp_output")).resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATE = pathlib.Path(__file__).parent / "trips_map.html"

mcp = FastMCP("mcp-movebank", stateless_http=True, json_response=True)


def _client() -> MovebankClient:
    return MovebankClient(
        username=os.environ.get("MOVEBANK_USERNAME"),
        password=os.environ.get("MOVEBANK_PASSWORD"),
    )


def _write(name: str, obj: dict) -> str:
    path = OUTPUT_DIR / name
    path.write_text(json.dumps(obj))
    return str(path)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

@mcp.tool()
def list_studies(
    only_downloadable: Annotated[bool, Field(description="Restrict to studies you can actually download.")] = True,
) -> dict:
    """List Movebank studies with id, name, taxon, location count, and time extent.
    Use this first to find a study_id. Many great migration studies are fully public
    (license_type CC_0). Returns a trimmed table, not raw data."""
    c = _client()
    try:
        rows = c.list_studies(only_downloadable=only_downloadable)
        rows = [r for r in rows if (r.get("number_of_deployed_locations") or "0") not in ("", "0")]
        return {"n": len(rows), "studies": rows[:200]}
    finally:
        c.close()


@mcp.tool()
def get_study(study_id: Annotated[int, Field(description="Movebank study id.")]) -> dict:
    """Get one study's metadata: citation, license terms, taxa, sensors, time span,
    and whether you have download access."""
    c = _client()
    try:
        return c.get_study(study_id)
    finally:
        c.close()


@mcp.tool()
def list_individuals(study_id: Annotated[int, Field(description="Movebank study id.")]) -> dict:
    """List tagged animals in a study (individual_local_identifier is the stable key
    to pass to get_tracks), with per-animal event counts and date ranges."""
    c = _client()
    try:
        rows = c.list_individuals(study_id)
        keep = ["local_identifier", "individual_taxon_canonical_name",
                "number_of_events", "timestamp_start", "timestamp_end", "sex"]
        slim = [{k: r.get(k) for k in keep} for r in rows]
        return {"n": len(slim), "individuals": slim}
    finally:
        c.close()


def _profile_help() -> str:
    return "; ".join(f"{k}={v}" for k, v in REDUCTION_PROFILES.items())


# --------------------------------------------------------------------------
# Tracks
# --------------------------------------------------------------------------

@mcp.tool()
def get_tracks(
    study_id: Annotated[int, Field(description="Movebank study id.")],
    individuals: Annotated[list[str] | None, Field(description="individual_local_identifier values; omit for all.")] = None,
    reduction_profile: Annotated[str | None, Field(description=f"Server-side reduction. {_profile_help()}. For migration use EURING_02.")] = "EURING_02",
    start_iso: Annotated[str | None, Field(description="ISO date/time lower bound, e.g. 2008-06-01.")] = None,
    end_iso: Annotated[str | None, Field(description="ISO date/time upper bound.")] = None,
    use_auth_csv: Annotated[bool, Field(description="Use authenticated direct-read CSV (needed for private studies). Default uses the public JSON endpoint.")] = False,
    max_points_per_individual: Annotated[int, Field(description="Client-side safety cap after server reduction.")] = 2000,
) -> dict:
    """Fetch GPS tracks and build an animated-map payload.

    Returns ONLY a summary (per-animal point counts, track length in km, time
    extent, bounding box) plus `trips_file` — the path to the full geometry written
    to disk for render_migration_map. The raw vertex stream is intentionally NOT
    returned into context. Prefer EURING_02 for migration; it extracts long-distance
    moves server-side and keeps payloads small."""
    start = datetime.fromisoformat(start_iso) if start_iso else None
    end = datetime.fromisoformat(end_iso) if end_iso else None
    c = _client()
    try:
        if use_auth_csv:
            rows = c.get_tracks_csv(study_id, individuals, reduction_profile=reduction_profile,
                                    start=start, end=end)
            inds = transforms.normalize_csv(rows)
        else:
            mb = c.get_tracks_json_public(study_id, individuals, sensor_type="gps",
                                          reduction_profile=reduction_profile, start=start, end=end)
            inds = transforms.normalize_json(mb)
    finally:
        c.close()

    payload = transforms.build_trips(inds, max_points_per_individual=max_points_per_individual)
    if not payload["trips"]:
        return {"ok": False, "hint": "No locations. Study may be private (try use_auth_csv=True), "
                                     "non-public, or have no GPS for that window.", "meta": payload["meta"]}
    fname = f"trips_{study_id}_{datetime.now():%Y%m%d_%H%M%S}.json"
    trips_file = _write(fname, payload)
    summary = transforms.llm_summary(payload)
    summary.update({"ok": True, "trips_file": trips_file})
    return summary


# --------------------------------------------------------------------------
# Weather annotation
# --------------------------------------------------------------------------

@mcp.tool()
def annotate_weather(
    trips_file: Annotated[str, Field(description="Path returned by get_tracks.")],
    grid_deg: Annotated[float, Field(description="Spatial grid for the join (0.25 = ERA5 native).")] = 0.25,
    max_cells: Annotated[int, Field(description="Abort if the join would exceed this many Open-Meteo calls.")] = 300,
) -> dict:
    """Annotate a track with ERA5 weather (precip, temp, ET0 dryness proxy, wind) via
    Open-Meteo, and summarize what each animal experienced along its path.

    NOTE — this is a coarse nearest-cell/day join for visualization and hypothesis
    generation, not spatiotemporal interpolation. For rigorous environmental
    annotation use Movebank's Env-DATA service. Writes a weather_file for the map and
    returns per-individual exposure means only."""
    payload = json.loads(pathlib.Path(trips_file).read_text())
    # Reconstruct per-individual location lists (ms/lon/lat) from the trips payload.
    inds = []
    for t in payload["trips"]:
        t0 = t["t0_ms"]
        locs = [{"ms": int(t0 + s * 1000), "lon": p[0], "lat": p[1]}
                for p, s in zip(t["path"], t["timestamps"])]
        inds.append({"individual": t["individual"], "taxon": t.get("taxon", ""), "locations": locs})

    weather = wx.fetch_weather(inds, grid_deg=grid_deg, max_cells=max_cells)
    exposure = wx.annotate_and_summarize(inds, weather, grid_deg=grid_deg)
    wfile = _write(pathlib.Path(trips_file).stem + "_weather.json", weather)
    return {
        "ok": True,
        "weather_file": wfile,
        "n_cells": weather["n_cells"],
        "n_days": weather["n_days"],
        "exposure": exposure["by_individual"],
        "caveat": "Coarse join; do not infer causation. Photoperiod/NDVI usually dominate "
                  "migration timing. For causal work use a step-selection function + Env-DATA.",
    }


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------

@mcp.tool()
def render_migration_map(
    trips_file: Annotated[str, Field(description="Path returned by get_tracks.")],
    weather_file: Annotated[str | None, Field(description="Optional path from annotate_weather.")] = None,
    title: Annotated[str, Field(description="Map title.")] = "Animal Migration Timelapse",
) -> dict:
    """Produce a self-contained animated HTML map (deck.gl TripsLayer) from a trips
    file, optionally with a daily precipitation overlay. Returns the html_file path;
    open it in a browser. Data is inlined so the file is portable."""
    if not TEMPLATE.exists():
        return {"ok": False, "error": f"template missing at {TEMPLATE}"}
    trips = json.loads(pathlib.Path(trips_file).read_text())
    weather = json.loads(pathlib.Path(weather_file).read_text()) if weather_file else {"cells": []}
    html = TEMPLATE.read_text()
    html = (html
            .replace("/*__TRIPS__*/null", json.dumps(trips))
            .replace("/*__WEATHER__*/null", json.dumps(weather))
            .replace("__TITLE__", title.replace("<", "")))
    out = OUTPUT_DIR / (pathlib.Path(trips_file).stem + "_map.html")
    out.write_text(html)
    return {"ok": True, "html_file": str(out), "n_individuals": trips["meta"]["n_individuals"],
            "time_start": trips["meta"]["time_start"], "time_end": trips["meta"]["time_end"]}


if __name__ == "__main__":
    mcp.settings.host = os.environ.get("MCP_HOST", "127.0.0.1")
    mcp.settings.port = int(os.environ.get("MCP_PORT", "8765"))
    mcp.run(transport="streamable-http")
