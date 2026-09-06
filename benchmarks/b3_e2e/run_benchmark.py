#!/usr/bin/env python3
"""Run any external product/agent against one B3 scenario and score its output."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--scenario", required=True)
parser.add_argument("--mode", default="hybrid", choices=["fixed", "search", "hybrid"])
parser.add_argument("--view", default="normalized", choices=["normalized", "transport"])
parser.add_argument("--output-dir", required=True, type=Path)
parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --; use {input} and {output} placeholders or B3_INPUT/B3_OUTPUT env vars")
args = parser.parse_args()
command = args.command[1:] if args.command and args.command[0] == "--" else args.command
if not command:
    parser.error("external system command is required after --")

args.output_dir.mkdir(parents=True, exist_ok=True)
input_path = (args.output_dir / "agent_input.json").resolve()
prediction_path = (args.output_dir / "prediction.json").resolve()
report_path = (args.output_dir / "deterministic_report.json").resolve()
judge_path = (args.output_dir / "judge_packet.json").resolve()
log_path = (args.output_dir / "execution.json").resolve()

subprocess.run([
    sys.executable, str(ROOT / "prepare_input.py"), "--version", "v2", "--scenario", args.scenario,
    "--mode", args.mode, "--view", args.view, "--output", str(input_path),
], check=True)

launch_cwd = Path.cwd()
resolved = []
for part in command:
    replaced = part.replace("{input}", str(input_path)).replace("{output}", str(prediction_path))
    candidate = launch_cwd / replaced
    # Preserve virtualenv executable symlinks; resolving them selects the
    # system interpreter and drops the environment's installed dependencies.
    resolved.append(
        str(candidate.absolute())
        if not Path(replaced).is_absolute() and candidate.exists()
        else replaced
    )
env = os.environ.copy()
env.update({"B3_INPUT": str(input_path), "B3_OUTPUT": str(prediction_path), "B3_SCENARIO": args.scenario, "B3_MODE": args.mode, "B3_WORKDIR": str(args.output_dir.resolve())})
started = time.perf_counter()
completed = subprocess.run(resolved, env=env, cwd=args.output_dir.resolve(), text=True, capture_output=True)
elapsed = time.perf_counter() - started
log_path.write_text(json.dumps({"command": resolved, "cwd": str(args.output_dir.resolve()), "returncode": completed.returncode, "wall_seconds": elapsed, "stdout": completed.stdout, "stderr": completed.stderr}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
if completed.returncode != 0:
    raise SystemExit(f"system command failed ({completed.returncode}); see {log_path}")
if not prediction_path.exists():
    raise SystemExit(f"system did not write {prediction_path}; use {{output}} or B3_OUTPUT")

subprocess.run([
    sys.executable, str(ROOT / "evaluate.py"), str(prediction_path), "--version", "v2",
    "--report", str(report_path), "--judge-packet", str(judge_path),
], check=True)
print(json.dumps({"status": "completed", "wall_seconds": elapsed, "input": str(input_path), "prediction": str(prediction_path), "report": str(report_path), "judge_packet": str(judge_path)}, ensure_ascii=False, indent=2))
