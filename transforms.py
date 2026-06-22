"""
transforms.py — pure functions turning Movebank event data into a compact
deck.gl TripsLayer payload, plus the summary stats the LLM actually needs in context.

Key principle for the MCP layer: the LLM should receive SUMMARIES (counts, extent,
per-individual stats), never the raw vertex stream. Full geometry goes to a file
that the visualization reads directly. These functions produce both halves.

TripsLayer wants, per path:
    path:        [[lng, lat], ...]
    timestamps:  [t, ...]   (one per vertex; we use seconds since track t0)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

# Distinct, colorblind-friendlier palette (RGB 0-255).
_PALETTE = [
    [31, 119, 180], [255, 127, 14], [44, 160, 44], [214, 39, 40],
    [148, 103, 189], [140, 86, 75], [227, 119, 194], [127, 127, 127],
    [188, 189, 34], [23, 190, 207],
]


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def normalize_json(mb_json: dict) -> list[dict]:
    """Movebank public/json nested shape -> list of per-individual location dicts."""
    out = []
    for ind in mb_json.get("individuals", []):
        locs = [
            {
                "ms": int(p["timestamp"]),
                "lon": float(p["location_long"]),
                "lat": float(p["location_lat"]),
            }
            for p in ind.get("locations", [])
            if p.get("location_long") is not None and p.get("location_lat") is not None
        ]
        locs.sort(key=lambda x: x["ms"])
        out.append({
            "individual": ind.get("individual_local_identifier") or str(ind.get("individual_id")),
            "taxon": ind.get("individual_taxon_canonical_name", ""),
            "locations": locs,
        })
    return out


def normalize_csv(rows: list[dict]) -> list[dict]:
    """direct-read CSV rows -> same per-individual structure."""
    by_ind: dict[str, dict] = {}
    for row in rows:
        if row.get("visible", "true").lower() == "false":
            continue
        lon, lat = row.get("location_long"), row.get("location_lat")
        if not lon or not lat:
            continue
        ind = row.get("individual_local_identifier") or row.get("individual_id") or "unknown"
        ts = row.get("timestamp", "")
        try:
            # "2008-05-31 13:30:02.001"
            dt = datetime.strptime(ts.split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            ms = int(dt.timestamp() * 1000)
        except Exception:
            continue
        rec = by_ind.setdefault(ind, {
            "individual": ind,
            "taxon": row.get("individual_taxon_canonical_name", ""),
            "locations": [],
        })
        rec["locations"].append({"ms": ms, "lon": float(lon), "lat": float(lat)})
    for rec in by_ind.values():
        rec["locations"].sort(key=lambda x: x["ms"])
    return list(by_ind.values())


def decimate(locations: list[dict], max_points: int) -> list[dict]:
    """Even decimation that always keeps first & last and returns AT MOST max_points.
    Cheap safety net; prefer server-side EURING reduction profiles for the real work."""
    n = len(locations)
    if n <= max_points or max_points < 2:
        return locations
    # Evenly spaced indices across [0, n-1], endpoints included, exactly max_points of them.
    idx = sorted({round(k * (n - 1) / (max_points - 1)) for k in range(max_points)})
    return [locations[i] for i in idx]


def build_trips(individuals: list[dict], max_points_per_individual: int = 2000) -> dict:
    """Produce the full TripsLayer payload + a compact summary for the LLM.

    All vertex timestamps are seconds since a single GLOBAL t0 (the earliest fix
    across every animal), so the animation clock is shared: an animal that moves
    later in the season genuinely animates later."""
    # First pass: decimate and find the global earliest timestamp.
    decimated = []
    for rec in individuals:
        locs = decimate(rec["locations"], max_points_per_individual)
        if len(locs) >= 2:
            decimated.append((rec, locs))
    if not decimated:
        return {"meta": {"individuals": []}, "trips": []}
    global_t0 = min(locs[0]["ms"] for _, locs in decimated)

    trips = []
    summary_inds = []
    all_lon, all_lat, all_ms = [], [], []

    for i, (rec, locs) in enumerate(decimated):
        path = [[round(p["lon"], 5), round(p["lat"], 5)] for p in locs]
        timestamps = [round((p["ms"] - global_t0) / 1000.0, 1) for p in locs]  # seconds since GLOBAL t0
        t0 = global_t0

        dist = sum(_haversine_km((path[k][0], path[k][1]), (path[k + 1][0], path[k + 1][1]))
                   for k in range(len(path) - 1))
        color = _PALETTE[i % len(_PALETTE)]
        trips.append({
            "individual": rec["individual"],
            "taxon": rec["taxon"],
            "color": color,
            "t0_ms": t0,
            "path": path,
            "timestamps": timestamps,
        })
        summary_inds.append({
            "individual": rec["individual"],
            "taxon": rec["taxon"],
            "n_points": len(locs),
            "track_km": round(dist, 1),
            "start": _iso(locs[0]["ms"]),
            "end": _iso(locs[-1]["ms"]),
            "color_rgb": color,
        })
        all_lon += [p[0] for p in path]
        all_lat += [p[1] for p in path]
        all_ms += [locs[0]["ms"], locs[-1]["ms"]]

    if not trips:
        return {"meta": {"individuals": []}, "trips": []}

    bbox = [min(all_lon), min(all_lat), max(all_lon), max(all_lat)]
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_individuals": len(trips),
        "bbox": [round(v, 4) for v in bbox],
        "time_start": _iso(min(all_ms)),
        "time_end": _iso(max(all_ms)),
        "t0_ms": global_t0,
        "duration_s": round((max(all_ms) - global_t0) / 1000.0, 1),
        "individuals": summary_inds,
    }
    return {"meta": meta, "trips": trips}


def llm_summary(payload: dict) -> dict:
    """The small dict safe to return into model context (no vertex streams)."""
    return payload.get("meta", {})
