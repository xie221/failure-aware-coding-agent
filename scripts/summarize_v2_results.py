#!/usr/bin/env python3
"""Summarize a frozen three-arm run: Control, legacy Enhanced, Enhanced v2."""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from summarize_results import summarize_condition  # noqa: E402
from failure_aware_agent.persistence import atomic_write_json, atomic_write_text  # noqa: E402

CONDITIONS = ("control", "enhanced", "enhanced_v2")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment_root", type=Path)
    args = parser.parse_args()
    root = args.experiment_root.resolve()
    outputs = (root / "comparison_v2_summary.json", root / "comparison_v2_per_task.csv")
    if any(path.exists() for path in outputs):
        parser.error("refusing to overwrite an existing v2 summary artifact")
    prepared_path = root / "prepared_manifest.json"
    if not prepared_path.is_file():
        parser.error("prepared_manifest.json is required")
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    manifest_hash = prepared.get("benchmark_manifest_sha256")
    if not isinstance(manifest_hash, str) or len(manifest_hash) != 64:
        parser.error("prepared manifest has no valid benchmark SHA-256")

    conditions, details = {}, {}
    for name in CONDITIONS:
        conditions[name], details[name] = summarize_condition(root, name)
    maps = {
        name: {row["task_id"]: row for row in details[name] if "patch_correct" in row}
        for name in CONDITIONS
    }
    common_ids = set.intersection(*(set(item) for item in maps.values()))
    paired = []
    for task_id in sorted(common_ids):
        rows = {name: maps[name][task_id] for name in CONDITIONS}
        paired.append({"task_id": task_id,
                       "category": rows["control"].get("category"),
                       **{f"{name}_patch_correct": rows[name].get("patch_correct")
                          for name in CONDITIONS},
                       **{f"{name}_public": rows[name].get("public_tests_passed")
                          for name in CONDITIONS},
                       **{f"{name}_hidden": rows[name].get("hidden_tests_passed")
                          for name in CONDITIONS}})
    summary = {
        "protocol_version": 2,
        "benchmark_manifest_sha256": manifest_hash,
        "conditions": conditions,
        "paired_task_count": len(paired),
        "paired": paired,
        "limitations": "one run per task/condition; do not infer general efficacy from a small paired sample",
    }
    atomic_write_json(outputs[0], summary)
    fields = ["condition", "task_id", "category", "patch_correct", "public_tests_passed",
              "hidden_tests_passed", "protocol_completion", "exit_reason", "http_attempts",
              "total_tokens", "elapsed_seconds", "repeated_failed_commands", "feedback_events",
              "retry_guard_skips"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for name in CONDITIONS:
        for row in details[name]:
            writer.writerow({"condition": name, **row})
    atomic_write_text(outputs[1], stream.getvalue())
    print(json.dumps({"paired_task_count": len(paired), "conditions": conditions}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
