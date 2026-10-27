#!/usr/bin/env python3
"""Create reproducible condition and paired summaries from completed score files."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT.parent
BENCHMARK = PROJECTS / "agent-benchmark-v1"
sys.path.insert(0, str(ROOT / "src"))
from failure_aware_agent.persistence import atomic_write_json, atomic_write_text


def repeated_actions(run_dir: Path) -> tuple[int, int]:
    path = run_dir / "trajectory.json"
    if not path.exists():
        return 0, 0
    trajectory = json.loads(path.read_text(encoding="utf-8"))
    commands, observations = [], []
    for message in trajectory.get("messages", []):
        extra = message.get("extra", {})
        if message.get("role") == "assistant":
            commands.extend(a.get("command", "") for a in extra.get("actions", []))
        if "raw_output" in extra and "returncode" in extra:
            observations.append((int(extra.get("returncode", -1)), str(extra.get("raw_output", ""))))
    seen, failed = set(), set()
    repeat_count = failed_repeat_count = 0
    for index, command in enumerate(commands):
        norm = re.sub(r"\s+", " ", command.strip())
        rc = observations[index][0] if index < len(observations) else -1
        if norm in seen:
            repeat_count += 1
            if norm in failed:
                failed_repeat_count += 1
        seen.add(norm)
        if rc != 0:
            failed.add(norm)
    return repeat_count, failed_repeat_count


def summarize_condition(root: Path, split: str) -> tuple[dict, list[dict]]:
    scored = []
    details = []
    split_root = root / split
    if not split_root.exists():
        return {"condition": split, "expected": 20 if split != "smoke" else 1, "started": 0}, []
    for run_dir in sorted(p for p in split_root.iterdir() if p.is_dir()):
        score_path, result_path = run_dir / "score.json", run_dir / "task_result.json"
        if not result_path.exists():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not score_path.exists():
            details.append({"task_id": result.get("task_id", run_dir.name),
                            "condition": split, "status": "unscored",
                            "exit_reason": result.get("exit_reason", "unknown")})
            continue
        score = json.loads(score_path.read_text(encoding="utf-8"))
        if score.get("status") == "unavailable":
            details.append({"task_id": result.get("task_id", run_dir.name),
                            "condition": split, "status": "score_unavailable",
                            "exit_reason": result.get("exit_reason", "unknown")})
            continue
        repeat, failed_repeat = repeated_actions(run_dir)
        row = {**score, "repeated_commands": repeat, "repeated_failed_commands": failed_repeat,
               "budget_exhausted": result.get("budget_rejections", 0) > 0,
               "http_budget_rejections": result.get("budget_rejections", 0),
               "wrapper_invocations": result.get("wrapper_invocations"),
               "changed_files": result.get("changed_files", [])}
        scored.append(row)
        details.append(row)
    n = len(scored)
    def count(key):
        return sum(bool(row.get(key)) for row in scored)
    def mean(key):
        values = [float(row[key]) for row in scored if isinstance(row.get(key), (float, int))]
        return sum(values) / len(values) if values else None
    summary = {
        "condition": split, "expected": 20 if split != "smoke" else 1,
        "started": sum((split_root / task_id).joinpath("task_result.json").is_file()
                       for task_id in (p.name for p in split_root.iterdir() if p.is_dir())),
        "scored": n,
        "unscored_or_unavailable": sum(row.get("status") in {"unscored", "score_unavailable"}
                                        for row in details),
        "public_pass": count("public_tests_passed"), "hidden_pass": count("hidden_tests_passed"),
        "patch_correct": count("patch_correct"), "protocol_completion": count("protocol_completion"),
        "budget_exhausted": sum(bool(row["budget_exhausted"]) for row in scored),
        "external_timeouts": sum((run_dir / "launcher_status.json").exists() and
                                  json.loads((run_dir / "launcher_status.json").read_text()).get("termination") == "external_timeout"
                                  for run_dir in (split_root.iterdir() if split_root.exists() else [])),
        "mean_http_attempts": mean("http_attempts"), "mean_tokens": mean("total_tokens"),
        "mean_runtime_seconds": mean("elapsed_seconds"),
        "repeated_commands": sum(row["repeated_commands"] for row in scored),
        "repeated_failed_commands": sum(row["repeated_failed_commands"] for row in scored),
        "feedback_triggers": sum(int(row.get("feedback_events", 0)) for row in scored),
        "unchanged_workspace_retry_skips": sum(
            int(row["retry_guard_skips"]) for row in scored
            if isinstance(row.get("retry_guard_skips"), int)),
        "retry_guard_skip_records_unavailable": sum(
            row.get("retry_guard_skips") is None for row in scored),
        "by_category": {},
    }
    for category in sorted({row.get("category") or "unknown" for row in scored}):
        subset = [row for row in scored if (row.get("category") or "unknown") == category]
        summary["by_category"][category] = {
            "n": len(subset), "patch_correct": sum(bool(row.get("patch_correct")) for row in subset),
            "patch_correct_rate": sum(bool(row.get("patch_correct")) for row in subset) / len(subset),
            "public_pass": sum(bool(row.get("public_tests_passed")) for row in subset),
        }
    for key in ("public_pass", "hidden_pass", "patch_correct", "protocol_completion"):
        summary[key + "_rate"] = summary[key] / n if n else None
    return summary, details


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment_root", type=Path)
    args = parser.parse_args()
    root = args.experiment_root.resolve()
    conditions, details = {}, {}
    for split in ("smoke", "control", "enhanced"):
        conditions[split], details[split] = summarize_condition(root, split)
    manifest_hash = hashlib.sha256((BENCHMARK / "benchmark_manifest.json").read_bytes()).hexdigest()
    output = {"benchmark_manifest_sha256_after": manifest_hash, "conditions": conditions,
              "paired": [], "limitations": "single run per task; model nondeterminism; n=20 per condition"}
    control = {row["task_id"]: row for row in details["control"] if "patch_correct" in row}
    enhanced = {row["task_id"]: row for row in details["enhanced"] if "patch_correct" in row}
    for task_id in sorted(control.keys() & enhanced.keys()):
        output["paired"].append({"task_id": task_id,
                                 "category": control[task_id].get("category"),
                                 "control_patch_correct": control[task_id]["patch_correct"],
                                 "enhanced_patch_correct": enhanced[task_id]["patch_correct"],
                                 "control_public": control[task_id]["public_tests_passed"],
                                 "enhanced_public": enhanced[task_id]["public_tests_passed"],
                                 "control_attempts": control[task_id]["http_attempts"],
                                 "enhanced_attempts": enhanced[task_id]["http_attempts"]})
    atomic_write_json(root / "baseline_vs_enhanced_summary.json", output)
    stream = io.StringIO(newline="")
    fields = ["condition", "task_id", "status", "public_tests_passed", "hidden_tests_passed", "patch_correct",
                  "protocol_completion", "exit_reason", "http_attempts", "total_tokens", "elapsed_seconds",
                  "budget_exhausted", "repeated_commands", "repeated_failed_commands", "feedback_events",
                  "retry_guard_skips"]
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for split in ("smoke", "control", "enhanced"):
        for row in details[split]:
            writer.writerow({"condition": split, **row})
    atomic_write_text(root / "per_task_results.csv", stream.getvalue())
    print(json.dumps(output["conditions"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
