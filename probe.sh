#!/usr/bin/env bash
# probe.sh — live validation against Movebank + the running MCP server.
# Movebank rate limit: 1 concurrent request per IP. Do NOT parallelize these.
set -euo pipefail

MB="https://www.movebank.org/movebank/service"
STUDY="${STUDY:-2911040}"          # Galapagos Albatrosses — fully public (CC_0)
IND="${IND:-4262-84830876}"
MCP="${MCP:-http://127.0.0.1:8765/mcp}"

echo "== 1. Public JSON, daily-reduced track for one albatross =="
curl -s "$MB/public/json?study_id=$STUDY&individual_local_identifiers=$IND&sensor_type=gps&event_reduction_profile=EURING_01" \
  | head -c 600; echo; echo

echo "== 2. Long-distance (migration) reduction, EURING_02 =="
curl -s "$MB/public/json?study_id=$STUDY&individual_local_identifiers=$IND&sensor_type=gps&event_reduction_profile=EURING_02" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); n=sum(len(i["locations"]) for i in d["individuals"]); print(f"individuals={len(d[\"individuals\"])}, points={n}")'
echo

echo "== 3. Study metadata (direct-read CSV) =="
curl -s "$MB/direct-read?entity_type=study&study_id=$STUDY&attributes=id,name,license_type,number_of_deployed_locations,taxon_ids" ; echo; echo

# --- Authenticated examples (uncomment + export MBUSER/MBPASS for private studies) ---
# echo "== token auth =="
# TOKEN=$(curl -s --user "$MBUSER:$MBPASS" "$MB/direct-read?service=request-token")
# curl -s "$MB/direct-read?entity_type=event&study_id=$STUDY&sensor_type_id=653&api-token=$TOKEN" | head -c 400; echo
#
# echo "== license-md5 handshake (for studies requiring term acceptance) =="
# curl -s -u "$MBUSER:$MBPASS" -c cookies.txt -o terms.html \
#   "$MB/direct-read?entity_type=event&study_id=<ID>"
# MD5=$(md5sum terms.html | cut -d' ' -f1)   # macOS: md5 -r terms.html
# curl -s -u "$MBUSER:$MBPASS" -b cookies.txt \
#   "$MB/direct-read?entity_type=event&study_id=<ID>&license-md5=$MD5" | head -c 400; echo

echo "== 4. MCP server: list tools (Streamable HTTP, stateless) =="
curl -s -X POST "$MCP" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | head -c 1200; echo; echo

echo "== 5. MCP server: call get_tracks on the public study =="
curl -s -X POST "$MCP" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"get_tracks\",\"arguments\":{\"study_id\":$STUDY,\"reduction_profile\":\"EURING_02\"}}}" | head -c 1200; echo
