#!/usr/bin/env python3
"""Prepare, preflight, and run the frozen smoke/control/enhanced protocol."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT.parent
MINI = PROJECTS / ".conda-envs/miniswe/bin/python"
MINI_SOURCE = PROJECTS / "mini-swe-agent/src"
BENCHMARK = PROJECTS / "agent-benchmark-v1"
MODEL_NAME = "Qwen2.5-Coder-7B-Instruct"
FORMAL_BATCH_CLAIM = ROOT / "runs/formal_batch_claim_v2.json"
sys.path.insert(0, str(ROOT / "src"))
from failure_aware_agent.persistence import atomic_write_json, atomic_write_text
from failure_aware_agent.prepare import code_fingerprint


def api_preflight() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["curl", "--noproxy", "*", "-sS", "--connect-timeout", "3", "--max-time", "8",
             "-w", "\\nHTTP_CODE=%{http_code}\\n", "http://127.0.0.1:8000/v1/models"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"API preflight failed: {type(exc).__name__}"
    body = result.stdout
    okay = result.returncode == 0 and "HTTP_CODE=200" in body and MODEL_NAME in body
    safe = f"curl_exit={result.returncode}; http_200={'yes' if 'HTTP_CODE=200' in body else 'no'}; expected_model={'yes' if MODEL_NAME in body else 'no'}"
    return okay, safe


def verify_prepared_record(record: dict) -> bool:
    run_dir = Path(record["workspace"]).parent
    task = json.loads((run_dir / "task.json").read_text(encoding="utf-8"))
    wrapper = Path(task["wrapper"])
    if not wrapper.is_file() or hashlib.sha256(wrapper.read_bytes()).hexdigest() != task["wrapper_sha256"]:
        return False
    workspace = Path(task["workspace"])
    expected = set(task["public_files_only"])
    found = set()
    for path in workspace.rglob("*"):
        if path.is_symlink():
            return False
        if path.is_file():
            found.add(path.relative_to(workspace).as_posix())
    if found != expected:
        return False
    for relative, sha in task["workspace_hashes"].items():
        if hashlib.sha256((workspace / relative).read_bytes()).hexdigest() != sha:
            return False
    source = Path(task["source_dir"])
    for relative, sha in task["source_hashes"].items():
        candidate = source / relative
        if candidate.is_symlink() or not candidate.is_file() or hashlib.sha256(candidate.read_bytes()).hexdigest() != sha:
            return False
    if (Path(task["audit_file"]).read_text(encoding="utf-8") != ""):
        return False
    return True


def smoke_gate_passes(root: Path, evidence_dir: Path | None = None) -> tuple[bool, str]:
    """Gate on the validity of the pilot execution/evaluation chain, not its quality."""
    run_dir = (evidence_dir or (root / "smoke/A01")).resolve()
    required = ("score.json", "task_result.json", "launcher_status.json", "task.json",
                "trajectory.json", "http_attempts.json")
    missing = [name for name in required if not (run_dir / name).is_file()]
    if missing:
        return False, f"A01 pilot evidence missing: {', '.join(missing)}"
    try:
        score = json.loads((run_dir / "score.json").read_text(encoding="utf-8"))
        result = json.loads((run_dir / "task_result.json").read_text(encoding="utf-8"))
        launcher = json.loads((run_dir / "launcher_status.json").read_text(encoding="utf-8"))
        task = json.loads((run_dir / "task.json").read_text(encoding="utf-8"))
        trajectory = json.loads((run_dir / "trajectory.json").read_text(encoding="utf-8"))
        events = json.loads((run_dir / "http_attempts.json").read_text(encoding="utf-8"))
        pilot_manifest = json.loads((run_dir.parents[1] / "prepared_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"smoke evidence is unreadable or incomplete ({type(exc).__name__})"

    benchmark_root = Path(pilot_manifest.get("benchmark_root", BENCHMARK)).resolve(strict=True)
    current_manifest = hashlib.sha256((benchmark_root / "benchmark_manifest.json").read_bytes()).hexdigest()
    if pilot_manifest.get("benchmark_manifest_sha256") != current_manifest:
        return False, "smoke run was prepared from a different Benchmark manifest"
    pilot_record = next((row for row in pilot_manifest.get("runs", [])
                         if row.get("split") == "smoke"), None)
    pilot_task_id = pilot_record.get("task_id") if pilot_record else None
    if (pilot_record is None or task.get("task_id") != pilot_task_id
            or result.get("task_id") != pilot_task_id or score.get("task_id") != pilot_task_id
            or task.get("source_hashes") != pilot_record.get("files")
            or task.get("wrapper_sha256") != pilot_record.get("wrapper_sha256")):
        return False, "A01 pilot task inputs do not match its prepared public manifest record"
    if not (run_dir.parents[1] / "experiment_config.yaml").is_file():
        return False, "A01 pilot frozen configuration snapshot is missing"

    if not isinstance(events, list) or any(
            not isinstance(event, dict)
            or event.get("event") not in {"http_attempt", "budget_rejected_before_send"}
            for event in events):
        return False, "A01 HTTP-attempt ledger contains malformed or unknown records"
    attempt_events = [event for event in events if event.get("event") == "http_attempt"]
    attempt_ids = [event.get("attempt_id") for event in attempt_events]
    attempt_count = len(attempt_events)
    if (not attempt_count or attempt_count > 12 or attempt_count != result.get("http_attempts")
            or len(set(attempt_ids)) != attempt_count or any(not value for value in attempt_ids)):
        return False, "A01 HTTP-attempt ledger is malformed, inconsistent, or over budget"
    if any(not isinstance(event.get("status_code"), int) for event in attempt_events):
        return False, "A01 HTTP ledger contains an attempt without a recorded HTTP outcome"

    actions = sum(len(message.get("extra", {}).get("actions", []))
                  for message in trajectory.get("messages", []) if message.get("role") == "assistant")
    wrapper = Path(task.get("wrapper", ""))
    audit = Path(task.get("audit_file", ""))
    workspace = Path(task.get("workspace", ""))
    if (not wrapper.is_file() or wrapper.is_symlink()
            or not wrapper.resolve().is_relative_to(run_dir)
            or hashlib.sha256(wrapper.read_bytes()).hexdigest() != task.get("wrapper_sha256")
            or not audit.is_file() or audit.is_symlink() or not audit.resolve().is_relative_to(run_dir)
            or not workspace.is_dir() or workspace.is_symlink()
            or not workspace.resolve().is_relative_to(run_dir)):
        return False, "A01 wrapper, audit, or workspace evidence is missing or mismatched"
    with audit.open(encoding="utf-8") as audit_stream:
        audit_lines = sum(1 for _ in audit_stream)
    if (actions <= 0 or actions != result.get("wrapper_invocations")
            or actions != audit_lines
            or actions != launcher.get("wrapper_invocations", actions)
            or launcher.get("action_wrapper_audit") != "matched"):
        return False, "A01 Shell Actions do not match path-pinned wrapper audit records"
    if launcher.get("cleanup_status") != "verified_no_process_group_members":
        return False, "A01 process-group cleanup was not verified"

    public_files = {"TASK.md", "buggy.py", "tests/test_buggy.py"}
    if set(task.get("public_files_only", [])) != public_files:
        return False, "A01 task public-file allowlist is unexpected"
    present_files = set()
    for path in workspace.rglob("*"):
        if path.is_symlink():
            return False, "A01 Agent workspace contains a symlink"
        if path.is_file():
            present_files.add(path.relative_to(workspace).as_posix())
    if not public_files.issubset(present_files):
        return False, "A01 Agent workspace is missing public task inputs"
    if any(name not in public_files and not name.startswith(".pytest_cache/")
           for name in present_files):
        return False, "A01 Agent workspace contains a file outside the public inputs/cache allowlist"
    if forbidden_workspace_artifacts(workspace):
        return False, "A01 Agent workspace contains a hidden-test/evaluator artifact"

    details = score.get("test_details", {})
    for group in ("public", "hidden"):
        if type(score.get(f"{group}_tests_passed")) is not bool:
            return False, f"A01 {group} independent score is not a recorded boolean result"
        detail = details.get(group, {})
        if detail.get("passed") is not score.get(f"{group}_tests_passed"):
            return False, f"A01 {group} summary and evaluator result disagree"
        log = Path(detail.get("log", ""))
        try:
            log.resolve().relative_to((run_dir / "evaluator_only" / group).resolve())
        except (ValueError, OSError):
            return False, f"A01 {group} evaluator log is not isolated under evaluator_only"
        if type(detail.get("passed")) is not bool or not isinstance(detail.get("exit_code"), int) or not log.is_file():
            return False, f"A01 {group} independent test result is not scoreable"
    if (run_dir / "evaluator_only/public/buggy.py").resolve() == (workspace / "buggy.py").resolve():
        return False, "A01 evaluator is not separated from the Agent workspace"

    return True, ("A01 pilot execution chain valid: API is checked by preflight; attempt ledger, "
                  "wrapper audit, independent scoring, Benchmark manifest, and cleanup verified; "
                  "patch correctness/protocol outcome is recorded but does not gate the batch")


def clean_env(task_dir: Path) -> dict[str, str]:
    runtime = task_dir / "runtime"
    for child in ("home", "tmp", "xdg-config", "xdg-cache", "empty-global-config"):
        (runtime / child).mkdir(parents=True, exist_ok=True)
    return {
        "PATH": f"{MINI.parent}:/usr/bin:/bin", "HOME": str(runtime / "home"),
        "TMPDIR": str(runtime / "tmp"), "XDG_CONFIG_HOME": str(runtime / "xdg-config"),
        "XDG_CACHE_HOME": str(runtime / "xdg-cache"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "TZ": "UTC", "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(MINI_SOURCE))),
        "PYTHONDONTWRITEBYTECODE": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "MSWEA_GLOBAL_CONFIG_DIR": str(runtime / "empty-global-config"),
        "MSWEA_SILENT_STARTUP": "1", "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT": "1",
    }


def remaining_processes(pgid: int) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().split()
            if int(fields[4]) == pgid:
                found.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return found


def action_wrapper_audit_matches(action_count: int, wrapper_count: int | None,
                                 guard_skips: int = 0) -> bool:
    """Every parsed Agent action is executed or guard-suppressed; zero is valid."""
    return (isinstance(action_count, int) and isinstance(wrapper_count, int)
            and isinstance(guard_skips, int) and action_count >= 0
            and wrapper_count >= 0 and guard_skips >= 0
            and wrapper_count + guard_skips == action_count)


def forbidden_workspace_artifacts(workspace: Path) -> list[str]:
    blocked_names = {"test_hidden.py", "reference.py", "reference_fix.py", "evaluator.py"}
    return [path.relative_to(workspace).as_posix() for path in workspace.rglob("*")
            if path.is_file() and path.name.lower() in blocked_names]


def finalize_hard_killed_task(run_dir: Path, elapsed_seconds: float) -> None:
    """Persist a minimal scoreable record if SIGKILL prevented the child finally block."""
    result_path = run_dir / "task_result.json"
    if result_path.exists():
        return
    task_path, trajectory_path = run_dir / "task.json", run_dir / "trajectory.json"
    if not task_path.exists():
        return
    task = json.loads(task_path.read_text(encoding="utf-8"))
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8")) if trajectory_path.exists() else {}
    hashes = {}
    changed = []
    for relative, original_hash in task["source_hashes"].items():
        path = Path(task["workspace"]) / relative
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            hashes[relative] = digest
            if digest != original_hash:
                changed.append(relative)
        else:
            changed.append(relative)
    attempts_path = run_dir / "http_attempts.json"
    events = json.loads(attempts_path.read_text(encoding="utf-8")) if attempts_path.exists() else []
    guard_path = run_dir / "retry_guard.json"
    guard_events = json.loads(guard_path.read_text(encoding="utf-8")) if guard_path.exists() else None
    prompt_tokens = completion_tokens = calls = 0
    for message in trajectory.get("messages", []):
        response = message.get("extra", {}).get("response", {})
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        if usage:
            calls += 1
            prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens += int(usage.get("completion_tokens", 0) or 0)
    try:
        with open(task["audit_file"], encoding="utf-8") as stream:
            wrappers = sum(1 for _ in stream)
    except OSError:
        wrappers = None
    result = {
        "task_id": task["task_id"], "condition": run_dir.parent.name,
        "exit_reason": "external_timeout", "error": {"class": "SIGKILL_or_finalization_interrupted"},
        "elapsed_seconds": round(elapsed_seconds, 3), "model_calls_with_usage": calls,
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "http_attempts": sum(event.get("event") == "http_attempt" for event in events),
        "budget_rejections": sum(event.get("event") == "budget_rejected_before_send" for event in events),
        "retry_guard_skips": (sum(event.get("event") == "retry_suppressed_unchanged_workspace"
                                   for event in guard_events) if guard_events is not None else None),
        "changed_files": changed, "final_hashes": hashes, "wrapper_invocations": wrappers,
        "trajectory_path": str(trajectory_path),
    }
    atomic_write_json(result_path, result)


def spawn_unattended_task(cmd: list[str], run_dir: Path, env: dict[str, str]):
    stdout_path, stderr_path = run_dir / "console.log", run_dir / "console.stderr.log"
    with stdout_path.open("w", encoding="utf-8") as stdout, \
            stderr_path.open("w", encoding="utf-8") as stderr:
        return subprocess.Popen(cmd, cwd=run_dir, env=env, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, start_new_session=True)


def run_one(task_record: dict, condition: str, timeout_seconds: int = 180) -> dict:
    run_dir = Path(task_record["workspace"]).parent
    task_json = run_dir / "task.json"
    cmd = [str(MINI), str(ROOT / "scripts/run_task.py"), str(task_json), condition, str(run_dir)]
    started = time.time()
    # Never inherit an SSH TTY: InteractiveAgent otherwise prompts to raise a
    # spent step limit and can wait until the outer 180s timeout.
    process = spawn_unattended_task(cmd, run_dir, clean_env(run_dir))
    termination, cleanup = "normal", "not_needed"
    try:
        exit_code = process.wait(timeout=timeout_seconds)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as wait_error:
        termination = "external_timeout" if isinstance(wait_error, subprocess.TimeoutExpired) else "operator_interrupt"
        cleanup = "term_sent"
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            cleanup = "kill_sent"
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        deadline = time.monotonic() + 2
        survivors = remaining_processes(process.pid)
        while survivors and time.monotonic() < deadline:
            time.sleep(.1)
            survivors = remaining_processes(process.pid)
        if survivors:
            cleanup = "kill_sent_to_residual_group"
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5
            while survivors and time.monotonic() < deadline:
                time.sleep(.1)
                survivors = remaining_processes(process.pid)
        cleanup = "verified_no_process_group_members" if not survivors else f"FAILED_residual_pids={survivors}"
        exit_code = process.returncode
        if not survivors:
            finalize_hard_killed_task(run_dir, time.time() - started)
    if termination == "normal":
        survivors = remaining_processes(process.pid)
        if survivors:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3
            while remaining_processes(process.pid) and time.monotonic() < deadline:
                time.sleep(.1)
            survivors = remaining_processes(process.pid)
            if survivors:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 3
                while remaining_processes(process.pid) and time.monotonic() < deadline:
                    time.sleep(.1)
                survivors = remaining_processes(process.pid)
            cleanup = "verified_no_process_group_members" if not survivors else f"FAILED_residual_pids={survivors}"
        else:
            cleanup = "verified_no_process_group_members"
    task_result_path = run_dir / "task_result.json"
    if termination in {"external_timeout", "operator_interrupt"} and task_result_path.is_file():
        task_result = json.loads(task_result_path.read_text(encoding="utf-8"))
        task_result["termination_category"] = termination
        task_result["exit_reason"] = termination
        atomic_write_json(task_result_path, task_result)
    result = {"command": cmd, "exit_code": exit_code, "termination": termination,
              "cleanup_status": cleanup, "wall_seconds": round(time.time() - started, 3),
              "stdout_path": str(run_dir / "console.log"),
              "stderr_path": str(run_dir / "console.stderr.log")}
    task_result_path = run_dir / "task_result.json"
    trajectory_path = run_dir / "trajectory.json"
    if task_result_path.exists() and trajectory_path.exists():
        task_result = json.loads(task_result_path.read_text(encoding="utf-8"))
        trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
        action_count = sum(len(message.get("extra", {}).get("actions", []))
                           for message in trajectory.get("messages", [])
                           if message.get("role") == "assistant")
        wrapper_count = task_result.get("wrapper_invocations")
        guard_skips = task_result.get("retry_guard_skips")
        if guard_skips is None:
            guard_skips = 0 if task_result.get("condition") == "control" else None
        if action_wrapper_audit_matches(action_count, wrapper_count, guard_skips):
            result["action_wrapper_audit"] = "matched"
        else:
            result["action_wrapper_audit"] = "FAILED_mismatch"
            result["cleanup_status"] = (
                f"FAILED_wrapper_audit_{wrapper_count}_plus_guard_skips_{guard_skips}"
                f"_vs_actions_{action_count}")
        blocked_files = forbidden_workspace_artifacts(Path(task_record["workspace"]))
        workspace = Path(task_record["workspace"])
        symlinks = [str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_symlink()]
        if blocked_files or symlinks:
            result["cleanup_status"] = "FAILED_possible_hidden_or_evaluator_artifact"
            result["workspace_integrity_findings"] = blocked_files + symlinks
    elif termination != "external_timeout":
        result["action_wrapper_audit"] = "FAILED_missing_result_or_trajectory"
        result["cleanup_status"] = "FAILED_missing_result_or_trajectory"
    atomic_write_json(run_dir / "launcher_status.json", result)
    if cleanup.startswith("FAILED"):
        raise RuntimeError(f"Process cleanup could not be confirmed for {task_record['task_id']}")
    return result


def append_jsonl(path: Path, row: dict):
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    if previous and not previous.endswith("\n"):
        raise RuntimeError("results.jsonl has a truncated prior record; refusing append")
    atomic_write_text(path, previous + json.dumps(row, sort_keys=True) + "\n")


def record_progress(experiment_root: Path, event: dict) -> None:
    progress_path = experiment_root / "batch_progress.json"
    try:
        state = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {
            "events": []
        }
    except (OSError, ValueError) as exc:
        raise RuntimeError("batch progress file is unreadable; stopping to preserve reliable records") from exc
    state["events"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **event})
    atomic_write_json(progress_path, state)


def score_finished_task(experiment_root: Path, run_dir: Path) -> tuple[int, dict | None]:
    command = [str(MINI), str(ROOT / "scripts/score_finished_runs.py"),
               str(experiment_root), "--run-dir", str(run_dir)]
    try:
        scored = subprocess.run(command, cwd=ROOT, env=clean_env(run_dir), stdin=subprocess.DEVNULL,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        atomic_write_text(run_dir / "task_scoring_launcher.log",
                          f"scoring launcher error: {type(exc).__name__}\n")
        return 2, None
    atomic_write_text(run_dir / "task_scoring_launcher.log", scored.stdout)
    score_path = run_dir / "score.json"
    if not score_path.is_file():
        return 2, None
    try:
        score = json.loads(score_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 2, None
    return (2 if score.get("status") == "unavailable" else scored.returncode), score


def integrity_stop_reason(launch: dict, score: dict | None, terminations: list[dict]) -> str | None:
    if launch.get("termination") == "operator_interrupt":
        return "operator interrupted the batch; stopped after cleanup and scoring current task"
    if launch.get("cleanup_status", "").startswith("FAILED"):
        return "process cleanup or workspace integrity failure"
    if launch.get("action_wrapper_audit") != "matched":
        return "Shell Action wrapper audit mismatch"
    if score is None or score.get("status") == "unavailable":
        return "independent evaluator could not produce a score"
    if any(item.get("category") == "model_transport_error" for item in terminations):
        return "model transport/API failure"
    return None


def http_ledger_integrity_error(run_dir: Path, result: dict) -> str | None:
    try:
        events = json.loads((run_dir / "http_attempts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "HTTP-attempt ledger is missing or unreadable"
    if not isinstance(events, list):
        return "HTTP-attempt ledger is not a list"
    if any(not isinstance(event, dict) or event.get("event") not in {
            "http_attempt", "budget_rejected_before_send"} for event in events):
        return "HTTP-attempt ledger contains malformed event types"
    attempts = [event for event in events if event["event"] == "http_attempt"]
    rejected = [event for event in events if event["event"] == "budget_rejected_before_send"]
    ids = [event.get("attempt_id") for event in attempts]
    if len(attempts) > 12 or len(attempts) != result.get("http_attempts"):
        return "HTTP-attempt ledger count is inconsistent or exceeds the 12-attempt cap"
    if any(not isinstance(value, str) or not value for value in ids):
        return "HTTP-attempt identifiers are missing or malformed"
    if len(set(ids)) != len(ids):
        return "HTTP-attempt identifiers are missing or duplicated"
    if len(rejected) != result.get("budget_rejections"):
        return "HTTP budget-rejection count does not match the ledger"
    for event in attempts:
        if (not isinstance(event.get("status_code"), int)
                and event.get("result") != "transport_error_delivery_unknown"):
            return "HTTP-attempt outcome is missing"
    return None


def should_stop_after_task(launch: dict, score: dict | None,
                           terminations: list[dict]) -> bool:
    """Agent-quality failures continue; execution/evaluation integrity failures stop."""
    return integrity_stop_reason(launch, score, terminations) is not None


def run_serial_tasks(records: list[dict], execute_one) -> int:
    """Continue after task-level failures; stop only when executor reports infra failure."""
    status = 0
    for record in records:
        outcome = execute_one(record)
        if outcome == "infrastructure_stop":
            return 2
        if outcome == "task_failed":
            status = 1
        elif outcome not in {"task_ok", "infrastructure_stop"}:
            return 2
    return status


def persist_and_score_task(experiment_root: Path, record: dict, launch: dict,
                           score_fn=score_finished_task) -> tuple[dict, dict | None, str | None]:
    task_run_dir = Path(record["workspace"]).parent
    append_jsonl(experiment_root / "results.jsonl",
                 {"task_id": record["task_id"], "split": record["split"], **launch})
    score_status, score = score_fn(experiment_root, task_run_dir)
    record_progress(experiment_root, {"event": "task_scored", "task_id": record["task_id"],
                                      "split": record["split"],
                                      "score_status": score.get("status", "scored") if score else "missing"})
    terminations_path = task_run_dir / "model_terminations.json"
    terminations = json.loads(terminations_path.read_text(encoding="utf-8")) if terminations_path.exists() else []
    reason = integrity_stop_reason(launch, score, terminations)
    try:
        task_result = json.loads((task_run_dir / "task_result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        task_result = {}
        reason = reason or "task result is missing or unreadable"
    if task_result.get("exit_reason") in {"environment_cleanup_failed", "trajectory_save_failed"}:
        reason = reason or "task environment cleanup or trajectory persistence failed"
    if task_result.get("termination_category") == "assembly_failed":
        reason = reason or "mini-SWE task assembly failed before Agent execution"
    reason = reason or http_ledger_integrity_error(task_run_dir, task_result)
    if score_status != 0 and reason is None:
        reason = "independent scorer failed"
    return launch, score, reason


def batch_claim_path(prepared: dict | None = None) -> Path:
    if prepared and prepared.get("experiment_protocol_version") == 2:
        manifest_hash = prepared.get("benchmark_manifest_sha256", "")
        if len(manifest_hash) != 64:
            raise RuntimeError("v2 prepared manifest has no valid benchmark hash")
        return ROOT / "runs" / "formal_claims" / f"{manifest_hash}.json"
    return FORMAL_BATCH_CLAIM


def claim_formal_batch(experiment_root: Path, prepared: dict | None = None) -> bool:
    """Atomically make the formal 20x2 run one-shot across newly named roots."""
    payload = {
        "experiment_root": str(experiment_root.resolve()),
        "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pid": os.getpid(),
        "policy": "one formal batch claim; do not rerun selectively or relaunch under another root",
    }
    claim_path = batch_claim_path(prepared)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    payload["claim_path"] = str(claim_path)
    try:
        fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True


def resume_unfinished_records(experiment_root: Path, records: list[dict], score_fn=None):
    """Recover scoring only and return never-started tasks; never relaunch a task result."""
    score_fn = score_fn or score_finished_task
    pending, recovered = [], []
    execution_artifacts = ("console.log", "console.stderr.log", "trajectory.json",
                           "http_attempts.json", "model_terminations.json", "launcher_status.json")
    for record in records:
        run_dir = Path(record["workspace"]).parent
        result_path, score_path = run_dir / "task_result.json", run_dir / "score.json"
        if score_path.is_file():
            try:
                current_score = json.loads(score_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return [], recovered, f"existing score is unreadable for {record['task_id']}"
            if current_score.get("status") == "unavailable":
                if not result_path.is_file():
                    return [], recovered, f"unavailable score has no task result for {record['task_id']}"
                if (run_dir / "evaluator_only").exists():
                    return [], recovered, f"partial evaluator output prevents safe rescoring for {record['task_id']}"
            else:
                if not result_path.is_file():
                    return [], recovered, f"scored task result is missing for {record['task_id']}"
                continue
        if result_path.is_file():
            try:
                launcher = json.loads((run_dir / "launcher_status.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return [], recovered, f"completed task lacks readable launcher status: {record['task_id']}"
            if (launcher.get("cleanup_status") != "verified_no_process_group_members"
                    or launcher.get("action_wrapper_audit") != "matched"):
                return [], recovered, f"task integrity/cleanup is not verified: {record['task_id']}"
            score_status, score = score_fn(experiment_root, run_dir)
            if score_status != 0 or not score or score.get("status") == "unavailable":
                return [], recovered, f"independent evaluator still cannot score {record['task_id']}"
            append_jsonl(experiment_root / "results.jsonl", {
                "task_id": record["task_id"], "split": record["split"],
                "status": "score_recovered_without_rerunning_agent",
                "patch_correct": score.get("patch_correct"),
            })
            recovered.append(record["task_id"])
            continue
        if any((run_dir / name).exists() for name in execution_artifacts):
            return [], recovered, f"task has execution artifacts but no result; refusing rerun: {record['task_id']}"
        pending.append(record)
    return pending, recovered, None


def main() -> int:
    def interrupt_as_cleanup(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupt_as_cleanup)
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "smoke", "batch", "preflight"))
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--smoke-evidence", type=Path,
                        help="completed, independently scored A01 pilot directory (for a fresh batch root)")
    parser.add_argument("--resume-claimed-batch", action="store_true",
                        help="continue only never-started tasks in the same previously claimed root")
    parser.add_argument("--config", type=Path, default=ROOT / "config/experiment.yaml")
    parser.add_argument("--benchmark-root", type=Path, default=BENCHMARK)
    parser.add_argument("--conditions", nargs="+", choices=("control", "enhanced", "enhanced_v2"),
                        default=("control", "enhanced"))
    parser.add_argument("--no-smoke", action="store_true",
                        help="prepare a v2 three-arm batch without a pilot workspace")
    args = parser.parse_args()
    if args.mode == "preflight":
        okay, detail = api_preflight()
        print(json.dumps({"model_api_ready": okay, "detail": detail}, indent=2))
        return 0 if okay else 2
    default_root = ROOT / "runs" / "failure-aware-20261023"
    experiment_root = (args.experiment_root or default_root).resolve()
    if args.mode == "prepare":
        sys.path.insert(0, str(ROOT / "src"))
        from failure_aware_agent.prepare import prepare
        import yaml
        try:
            config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
            benchmark_root = args.benchmark_root
            configured_path = config.get("runner", {}).get("benchmark_path")
            if configured_path and benchmark_root.resolve() == BENCHMARK.resolve():
                benchmark_root = (ROOT / configured_path).resolve()
            if args.no_smoke and config.get("runner", {}).get("protocol_version") != 2:
                parser.error("--no-smoke requires protocol_version 2")
            print(json.dumps(prepare(
                experiment_root, benchmark_root=benchmark_root, config_path=args.config,
                conditions=tuple(args.conditions), include_smoke=not args.no_smoke), indent=2))
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            parser.error(f"preparation failed: {type(exc).__name__}: {exc}")
        return 0

    ready, detail = api_preflight()
    preflight = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "detail": detail,
                 "ready": ready, "mode": args.mode}
    if not ready:
        print(json.dumps(preflight, indent=2))
        return 2
    if not experiment_root.is_dir():
        print("prepared experiment root does not exist; run prepare first", file=sys.stderr)
        return 2
    prepared = json.loads((experiment_root / "prepared_manifest.json").read_text(encoding="utf-8"))
    if prepared.get("code_version_sha256") and code_fingerprint() != prepared["code_version_sha256"]:
        print("experiment code changed since preparation; refusing to run", file=sys.stderr)
        return 2
    benchmark_root = Path(prepared.get("benchmark_root", BENCHMARK)).resolve(strict=True)
    manifest_path = benchmark_root / "benchmark_manifest.json"
    if prepared["benchmark_manifest_sha256"] != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
        print("Benchmark manifest changed since preparation; refusing to run", file=sys.stderr)
        return 2
    config_snapshot = experiment_root / "experiment_config.yaml"
    expected_config_hash = prepared.get("experiment_config_sha256")
    if args.mode in {"batch", "smoke"} and (not config_snapshot.is_file() or not expected_config_hash
            or hashlib.sha256(config_snapshot.read_bytes()).hexdigest() != expected_config_hash):
        print("frozen experiment configuration is missing or changed; refusing to run", file=sys.stderr)
        return 2
    if args.mode == "batch":
        expected_conditions = tuple(prepared.get("conditions", ("control", "enhanced")))
        smoke_exists = any(row.get("split") == "smoke" for row in prepared.get("runs", []))
        if smoke_exists:
            gate, reason = smoke_gate_passes(experiment_root, args.smoke_evidence)
            if not gate:
                print(json.dumps({"batch_start": "refused", "reason": reason}, indent=2))
                return 2
        elif not (prepared.get("experiment_protocol_version") == 2
                  and set(expected_conditions) == {"control", "enhanced", "enhanced_v2"}):
            print("batch without a smoke requires a complete v2 three-arm preparation", file=sys.stderr)
            return 2
        counts = {name: sum(row.get("split") == name for row in prepared.get("runs", []))
                  for name in expected_conditions}
        if any(count != prepared.get("task_count") for count in counts.values()):
            print(f"prepared condition counts are incomplete: {counts}", file=sys.stderr)
            return 2
        if any(not verify_prepared_record(row) for row in prepared.get("runs", [])):
            print("prepared workspace/wrapper integrity check failed", file=sys.stderr)
            return 2
        if args.resume_claimed_batch:
            try:
                claim = json.loads(batch_claim_path(prepared).read_text(encoding="utf-8"))
                old_status = int((experiment_root / "batch_exit_status.txt").read_text().strip())
            except (OSError, ValueError, TypeError):
                print("cannot verify prior claim/exit status; refusing resume", file=sys.stderr)
                return 2
            if claim.get("experiment_root") != str(experiment_root) or old_status == 0:
                print("resume requires the same interrupted, nonzero-claim batch root", file=sys.stderr)
                return 2
        elif not claim_formal_batch(experiment_root, prepared):
            print(f"formal batch already claimed at {batch_claim_path(prepared)}; refusing a repeat", file=sys.stderr)
            return 2
    selected = [r for r in prepared["runs"] if r["split"] == "smoke"] if args.mode == "smoke" else [
        r for r in prepared["runs"] if r["split"] in set(prepared.get("conditions", ("control", "enhanced")))]
    recovered = []
    if args.mode == "batch" and args.resume_claimed_batch:
        selected, recovered, resume_error = resume_unfinished_records(experiment_root, selected)
        if resume_error:
            print(json.dumps({"resume": "refused", "reason": resume_error}, indent=2), file=sys.stderr)
            return 2
        for task_id in recovered:
            record_progress(experiment_root, {"event": "task_score_recovered_without_rerun",
                                              "task_id": task_id})
    atomic_write_json(experiment_root / "preflight.json", preflight)
    status = 0
    record_progress(experiment_root, {"event": "batch_started", "mode": args.mode,
                                      "selected_tasks": len(selected)})
    def execute_record(record: dict) -> str:
        if not verify_prepared_record(record):
            row = {
                "task_id": record["task_id"], "split": record["split"],
                "status": "stopped_before_launch_integrity_check_failed",
            }
            append_jsonl(experiment_root / "results.jsonl", row)
            record_progress(experiment_root, {"event": "infrastructure_stop", **row})
            return "infrastructure_stop"
        ready_now, detail_now = api_preflight()
        if not ready_now:
            row = {
                "task_id": record["task_id"], "split": record["split"],
                "status": "stopped_before_launch_api_unhealthy", "detail": detail_now,
            }
            append_jsonl(experiment_root / "results.jsonl", row)
            record_progress(experiment_root, {"event": "infrastructure_stop", **row})
            return "infrastructure_stop"
        condition = "control" if record["split"] == "smoke" else record["split"]
        task_run_dir = Path(record["workspace"]).parent
        print(f"[batch] START {record['split']} {record['task_id']}", flush=True)
        record_progress(experiment_root, {"event": "task_started", "task_id": record["task_id"],
                                          "split": record["split"]})
        try:
            launch = run_one(record, condition)
        except BaseException as exc:
            row = {"task_id": record["task_id"], "split": record["split"],
                   "status": "infrastructure_stop_during_task", "error_class": type(exc).__name__}
            append_jsonl(experiment_root / "results.jsonl", row)
            record_progress(experiment_root, {"event": "infrastructure_stop", **row})
            return "infrastructure_stop"
        try:
            _, score, stop_reason = persist_and_score_task(experiment_root, record, launch)
        except BaseException as exc:
            row = {"task_id": record["task_id"], "split": record["split"],
                   "status": "infrastructure_stop_scoring_exception", "error_class": type(exc).__name__}
            append_jsonl(experiment_root / "results.jsonl", row)
            record_progress(experiment_root, {"event": "infrastructure_stop", **row})
            return "infrastructure_stop"
        print(f"[batch] SCORED {record['split']} {record['task_id']} "
              f"patch_correct={score.get('patch_correct', 'unavailable') if score else 'unavailable'} "
              f"termination={json.loads((task_run_dir / 'task_result.json').read_text()).get('termination_category', 'unknown')}",
              flush=True)
        if stop_reason:
            append_jsonl(experiment_root / "results.jsonl", {
                "task_id": record["task_id"], "split": record["split"],
                "status": "infrastructure_stop", "detail": stop_reason,
            })
            record_progress(experiment_root, {"event": "infrastructure_stop",
                                              "task_id": record["task_id"], "detail": stop_reason})
            return "infrastructure_stop"
        return "task_failed" if launch["exit_code"] != 0 else "task_ok"

    try:
        status = run_serial_tasks(selected, execute_record)
    except KeyboardInterrupt:
        record_progress(experiment_root, {"event": "operator_interrupt_outside_task",
                                          "message": "batch stopped; task-level process cleanup was not active"})
        status = 2
    try:
        summary_script = (ROOT / "scripts/summarize_v2_results.py"
                          if prepared.get("experiment_protocol_version") == 2
                          and set(prepared.get("conditions", [])) == {"control", "enhanced", "enhanced_v2"}
                          else ROOT / "scripts/summarize_results.py")
        summary = subprocess.run([str(MINI), str(summary_script),
                                  str(experiment_root)], cwd=ROOT, env=clean_env(experiment_root),
                                 stdin=subprocess.DEVNULL, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=180, check=False)
        atomic_write_text(experiment_root / "summary_launcher.log", summary.stdout)
        if summary.returncode != 0:
            status = 2
    except (OSError, subprocess.TimeoutExpired):
        status = 2
    record_progress(experiment_root, {"event": "batch_finished", "status": status})
    atomic_write_text(experiment_root / "batch_exit_status.txt", f"{status}\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
