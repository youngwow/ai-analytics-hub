#!/usr/bin/env python3
"""Run any external AI implementation on frozen B2 input and score it."""

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
parser.add_argument("--version", default="v1")
parser.add_argument("--split", default="validation", choices=["all", "development", "validation", "holdout", "rat"])
parser.add_argument("--output-dir", required=True, type=Path)
parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --; use {input}/{output} or B2_INPUT/B2_OUTPUT")
args = parser.parse_args()
command = args.command[1:] if args.command and args.command[0] == "--" else args.command
if not command:
    parser.error("external system command is required after --")

args.output_dir.mkdir(parents=True, exist_ok=True)
input_path = (args.output_dir / "benchmark_input.json").resolve()
prediction_path = (args.output_dir / "prediction.json").resolve()
report_path = (args.output_dir / "deterministic_report.json").resolve()
judge_path = (args.output_dir / "judge_packet.json").resolve()
execution_path = (args.output_dir / "execution.json").resolve()
subprocess.run([sys.executable, str(ROOT / "prepare_input.py"), "--version", args.version, "--split", args.split, "--output", str(input_path)], check=True)

launch_cwd = Path.cwd()
resolved = []
for part in command:
    replaced = part.replace("{input}", str(input_path)).replace("{output}", str(prediction_path))
    candidate = launch_cwd / replaced
    # Keep virtualenv executable symlinks intact. Path.resolve() dereferences
    # `.venv/bin/python` to the system interpreter and loses the venv context.
    resolved.append(
        str(candidate.absolute())
        if not Path(replaced).is_absolute() and candidate.exists()
        else replaced
    )
env = os.environ.copy()
env.update({"B2_INPUT": str(input_path), "B2_OUTPUT": str(prediction_path), "B2_SPLIT": args.split, "B2_WORKDIR": str(args.output_dir.resolve())})
started = time.perf_counter()
completed = subprocess.run(resolved, cwd=args.output_dir.resolve(), env=env, text=True, capture_output=True)
elapsed = time.perf_counter() - started
execution_path.write_text(json.dumps({"command": resolved, "returncode": completed.returncode, "wall_seconds": elapsed, "stdout": completed.stdout, "stderr": completed.stderr}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
if completed.returncode != 0:
    raise SystemExit(f"system command failed ({completed.returncode}); see {execution_path}")
if not prediction_path.exists():
    raise SystemExit(f"system did not write {prediction_path}; use {{output}} or B2_OUTPUT")
subprocess.run([sys.executable, str(ROOT / "evaluate.py"), str(prediction_path), "--version", args.version, "--report", str(report_path), "--judge-packet", str(judge_path)], check=True)
print(json.dumps({"status": "completed", "wall_seconds": elapsed, "input": str(input_path), "prediction": str(prediction_path), "report": str(report_path), "judge_packet": str(judge_path)}, ensure_ascii=False, indent=2))
