#!/usr/bin/env python3
"""Small local, deterministic B3 replay surface.

It exposes the benchmark world over HTTP but prescribes no agent architecture.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "v2"


def load_json(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def load_jsonl(name: str):
    return [json.loads(line) for line in (DATA / name).read_text(encoding="utf-8").splitlines() if line]


SCENARIOS = load_json("scenarios.json")["scenarios"]
STATES = load_json("initial_states.json")
CONTEXT = load_json("context_gs_labs.json")
ITEMS = {x["id"]: x for x in load_jsonl("timeline.jsonl")}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [x for x in parsed.path.split("/") if x]
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            return self.send_json({"status": "ok", "dataset": "v2.0.0", "scenarios": sorted(SCENARIOS)})
        if parsed.path == "/v2/context":
            return self.send_json(CONTEXT)
        if len(parts) < 3 or parts[0] != "v2" or parts[1] not in SCENARIOS:
            return self.send_json({"error": "Use /v2/{scenario}/initial-state, timeline, or surface/{name}."}, 404)
        scenario_id, endpoint = parts[1], parts[2]
        if endpoint == "initial-state":
            return self.send_json(STATES[scenario_id])
        ids = SCENARIOS[scenario_id]["item_ids"]
        mode = query.get("mode", ["hybrid"])[0]
        if mode not in {"fixed", "search", "hybrid"}:
            return self.send_json({"error": "mode must be fixed, search, or hybrid"}, 400)
        until = query.get("until", [None])[0]
        try:
            until_dt = datetime.fromisoformat(until) if until else None
        except ValueError:
            return self.send_json({"error": "until must be ISO-8601"}, 400)
        rows = []
        for item_id in ids:
            item = ITEMS[item_id]
            if mode != "hybrid" and mode not in item["discoverability"]:
                continue
            if until_dt and datetime.fromisoformat(item["available_at"]) > until_dt:
                continue
            rows.append(item)
        if endpoint == "timeline":
            visible = [{key: value for key, value in row.items() if key != "transport_payload"} for row in rows]
            return self.send_json({"scenario_id": scenario_id, "mode": mode, "items": visible})
        if endpoint == "surface" and len(parts) == 4:
            surface = parts[3]
            visible = [{"canonical_item_id": row["id"], "available_at": row["available_at"], "payload": row["transport_payload"]} for row in rows if row["surface"] == surface]
            return self.send_json({"scenario_id": scenario_id, "surface": surface, "items": visible})
        return self.send_json({"error": "unknown endpoint"}, 404)


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", default=8787, type=int)
args = parser.parse_args()
server = ThreadingHTTPServer((args.host, args.port), Handler)
print(f"B3 replay server: http://{args.host}:{args.port}")
server.serve_forever()
