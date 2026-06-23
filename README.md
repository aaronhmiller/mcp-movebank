# mcp-movebank — animal migration over MCP, with weather overlay + animated maps

An LLM-callable FastMCP server that explores [Movebank](https://www.movebank.org)
animal-tracking data, annotates it with ERA5 weather, and emits an animated
deck.gl timelapse map.

## Pipeline

```
list_studies → get_study → list_individuals → get_tracks ─┬─→ render_migration_map → .html
                                                          └─→ annotate_weather ──┘ (overlay)
```

`get_tracks` returns a **summary** to the model (counts, km, bbox, time extent) and
writes full geometry to `MCP_OUTPUT_DIR`. The vertex stream never enters context —
this is the token-discipline that keeps big migration datasets workable, the same
problem you hit with mcp-vampi. `render_migration_map` inlines the geometry into a
portable HTML file.

## Files

| file | role |
|---|---|
| `movebank_client.py` | REST client: `public/json`, `direct-read` CSV, token auth, license-md5 handshake |
| `transforms.py` | Movebank → deck.gl TripsLayer payload; global-clock timestamps; decimation |
| `weather.py` | Open-Meteo ERA5 join via 0.25° daily gridding |
| `server.py` | FastMCP server (stateless HTTP), 6 tools |
| `trips_map.html` | deck.gl TripsLayer animation template (injection targets) |
| `demo_map.html` | **open this first** — renders with baked synthetic data, no network |
| `probe.sh` | curl harness for live validation |
| `selftest.py` | offline unit checks on the pure logic |

## Run

```bash
pip install "mcp[cli]" httpx
python selftest.py          # offline sanity check
python server.py            # serves http://127.0.0.1:8765/mcp  (stateless streamable-http)
STUDY=2911040 ./probe.sh    # live: hits Movebank's public Galapagos study + your MCP server
```

Register the server in your MCP client, then drive it in natural language:
*"List public bird studies, pull the long-distance tracks for the Galapagos
albatrosses, annotate with weather, and render the map."*

Worked example study: **2911040** (Galapagos Albatrosses, CC_0, fully public — no
credentials needed). For private studies set `MOVEBANK_USERNAME` / `MOVEBANK_PASSWORD`
and pass `use_auth_csv=True` to `get_tracks`.

## Two API gotchas the docs confirm

1. **Rate limit: 1 concurrent request per IP.** The client is synchronous on purpose.
   Don't thread-pool it.
2. **License acceptance.** Some studies require accepting terms before first download.
   The client handles the md5 handshake automatically on the CSV path; the public
   JSON path only works for fully-public studies. Token auth (`request-token`) is the
   cleanest option for repeat programmatic use.

## Wiring decision you'll want to make

`get_tracks` returns a filesystem **path**. That assumes the thing rendering the map
can read `MCP_OUTPUT_DIR`. Two clean options:
- Serve `MCP_OUTPUT_DIR` over a static file server and open the `.html` in a browser.
- Or expose the trips/weather/html files as **MCP Resources** so the host fetches
  them directly. (FastMCP supports `@mcp.resource`; not wired here to keep the
  surface small.)

## Epistemics — read before claiming weather "causes" anything

The weather overlay is a **hypothesis generator**, not evidence. The join is a coarse
nearest-cell/nearest-day lookup, and more importantly the causal story is hard:
photoperiod usually dominates migration *timing*, green-up/NDVI drives *staging*, and
wind drives *daily* flight decisions — precipitation is frequently a confounded proxy.
A pretty animation of a track bending around a dry region proves nothing on its own.

If you want to make a real claim:
- Use Movebank's **Env-DATA** service for rigorous spatiotemporal annotation
  (interpolated to each fix, many more variables incl. NDVI and wind components).
- Model it with a **step-selection function** (e.g. `amt` / `survival` in R) rather
  than eyeballing overlays.

## Good public studies to try

Browse with `list_studies`, but `2911040` (albatrosses) is the reliable no-auth
starting point. Filter the study list by `taxon_ids` / `name` for storks, vultures,
geese, etc. — many of the classic long-distance migrants are public.

## What the map can look like
![map image](mcp_output/mcp-movebank-galapagos-albatross-foraging-tracks.gif)
