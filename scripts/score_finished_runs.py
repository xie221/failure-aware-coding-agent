#!/usr/bin/env python3
"""Score completed workspaces in evaluator-only copies; never runs beside an Agent."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

sys_path = str(Path(__file__).resolve().parents[1] / "src")
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)
from failure_aware_agent.persistence import atomic_write_json, atomic_write_text

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT.parent
BENCHMARK = PROJECTS / "agent-benchmark-v1"
PYTHON = PROJECTS / ".conda-envs/miniswe/bin/python"


def patch_is_correct(public_passed: bool, hidden_passed: bool) -> bool:
    """A valid patch must satisfy both supplied public and held-out tests."""
    return public_passed and hidden_passed


def score_one(run_dir: Path) -> dict:
    result_path = run_dir / "task_result.json"
    if not result_path.is_file():
        scored = {"task_id": run_dir.name, "status": "unavailable", "reason": "task result missing"}
        atomic_write_json(run_dir / "score.json", scored)
        return scored
    result = json.loads(result_path.read_text(encoding="utf-8"))
    task_id = result["task_id"]
    task_meta = json.loads((run_dir / "task.json").read_text(encoding="utf-8"))
    benchmark_root = Path(task_meta.get("benchmark_root", BENCHMARK)).resolve(strict=True)
    manifest_path = benchmark_root / "benchmark_manifest.json"
    expected_manifest_hash = task_meta.get("benchmark_manifest_sha256")
    if expected_manifest_hash:
        manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if expected_manifest_hash != manifest_hash:
            raise RuntimeError("Benchmark manifest changed after task preparation")
    source = run_dir / "evaluator_snapshot"
    final_buggy = run_dir / "workspace/buggy.py"
    hidden_candidate = Path(task_meta.get(
        "hidden_test_path", benchmark_root / "evaluation/hidden_tests" / task_id / "test_hidden.py"))
    if hidden_candidate.is_symlink():
        raise RuntimeError("hidden test must not be a symlink")
    hidden_root = (benchmark_root / "evaluation/hidden_tests").resolve(strict=True)
    hidden_source = hidden_candidate.resolve(strict=True)
    if hidden_root not in hidden_source.parents:
        raise RuntimeError("hidden test path escapes evaluator-only benchmark area")
    expected_hidden_hash = task_meta.get("hidden_test_sha256")
    if expected_hidden_hash and hashlib.sha256(hidden_source.read_bytes()).hexdigest() != expected_hidden_hash:
        raise RuntimeError("hidden test changed after task preparation")
    if not hidden_source.is_file():
        scored = {"task_id": task_id, "status": "unavailable", "reason": "scoring input missing"}
        atomic_write_json(run_dir / "score.json", scored)
        return scored
    candidate = final_buggy
    candidate_source = "agent_workspace"
    if not final_buggy.is_file() or final_buggy.is_symlink():
        # A deleted/missing patch is a valid no-change outcome, not a scoring
        # infrastructure failure. Score the immutable public starter copy.
        candidate = source / "buggy.py"
        expected = task_meta.get("source_hashes", {}).get("buggy.py")
        if (not candidate.is_file() or candidate.is_symlink() or not expected
                or hashlib.sha256(candidate.read_bytes()).hexdigest() != expected):
            scored = {"task_id": task_id, "status": "unavailable",
                      "reason": "verified original public source missing for deleted candidate"}
            atomic_write_json(run_dir / "score.json", scored)
            return scored
        candidate_source = "original_public_buggy_due_to_missing_agent_file"
    score_root = run_dir / "evaluator_only"
    if score_root.exists():
        raise FileExistsError(f"refusing to overwrite evaluator output: {score_root}")
    outcomes = {}
    for group in ("public", "hidden"):
        case_dir = score_root / group
        (case_dir / "tests").mkdir(parents=True)
        shutil.copy2(candidate, case_dir / "buggy.py")
        if group == "public":
            shutil.copy2(source / "tests/test_buggy.py", case_dir / "tests/test_buggy.py")
            test_file = case_dir / "tests/test_buggy.py"
        else:
            shutil.copy2(hidden_source, case_dir / "tests/test_hidden.py")
            test_file = case_dir / "tests/test_hidden.py"
        env = {
            "PATH": f"{PYTHON.parent}:/usr/bin:/bin", "HOME": str(case_dir / ".home"),
            "TMPDIR": str(case_dir / ".tmp"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "TZ": "UTC", "PYTHONPATH": str(case_dir), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        Path(env["HOME"]).mkdir(parents=True)
        Path(env["TMPDIR"]).mkdir(parents=True)
        started = time.monotonic()
        proc = subprocess.Popen([str(PYTHON), "-m", "pytest", "-q", str(test_file)],
                                cwd=case_dir, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=60)
            atomic_write_text(case_dir / "pytest.log", output)
            outcomes[group] = {"passed": proc.returncode == 0, "exit_code": proc.returncode,
                               "elapsed_seconds": round(time.monotonic() - started, 3),
                               "log": str(case_dir / "pytest.log")}
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                output, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                output, _ = proc.communicate(timeout=5)
            remaining = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    fields = (entry / "stat").read_text().split()
                    if int(fields[4]) == proc.pid:
                        remaining.append(int(entry.name))
                except (OSError, ValueError, IndexError):
                    continue
            if remaining:
                raise RuntimeError(f"scoring subprocess group cleanup failed: {remaining}")
            atomic_write_text(case_dir / "pytest.log", output)
            outcomes[group] = {"passed": False, "exit_code": None, "timeout": True,
                               "elapsed_seconds": round(time.monotonic() - started, 3),
                               "log": str(case_dir / "pytest.log")}
    scored = {"task_id": task_id, "category": task_meta.get("category"),
              "condition": result["condition"],
              "public_tests_passed": outcomes["public"]["passed"],
              "hidden_tests_passed": outcomes["hidden"]["passed"],
              "patch_correct": patch_is_correct(outcomes["public"]["passed"],
                                                 outcomes["hidden"]["passed"]),
              "protocol_completion": result["exit_reason"] == "Submitted",
              "exit_reason": result["exit_reason"], "http_attempts": result["http_attempts"],
              "candidate_source": candidate_source,
              "total_tokens": result["total_tokens"], "elapsed_seconds": result["elapsed_seconds"],
              "feedback_events": len(json.loads((run_dir / "feedback.json").read_text())
                                      if (run_dir / "feedback.json").exists() else []),
              "retry_guard_skips": result.get("retry_guard_skips"),
              "test_details": outcomes}
    atomic_write_json(run_dir / "score.json", scored)
    return scored


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment_root", type=Path)
    parser.add_argument("--split", choices=("smoke", "control", "enhanced", "enhanced_v2", "all"), default="all")
    parser.add_argument("--run-dir", type=Path,
                        help="score exactly one completed task immediately after it exits")
    args = parser.parse_args()
    root = args.experiment_root.resolve()
    if not root.is_dir():
        parser.error("experiment root is missing")
    if args.run_dir:
        run_dir = args.run_dir.resolve()
        try:
            run_dir.relative_to(root)
        except ValueError:
            parser.error("--run-dir must be inside experiment_root")
        score_one(run_dir)
    else:
        runs = []
        for split in ("smoke", "control", "enhanced", "enhanced_v2"):
            if args.split not in ("all", split):
                continue
            split_root = root / split
            if split_root.exists():
                runs.extend(sorted(p for p in split_root.iterdir() if (p / "task_result.json").exists()))
        for run_dir in runs:
            if (run_dir / "score.json").exists():
                continue
            score_one(run_dir)
    output = [json.loads(score_path.read_text(encoding="utf-8"))
              for split in ("smoke", "control", "enhanced", "enhanced_v2")
              for score_path in sorted((root / split).glob("*/score.json"))]
    atomic_write_json(root / "independent_scores.json", output)
    print(json.dumps({"scored": len(output), "patches_correct": sum(x.get("patch_correct", False) for x in output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
