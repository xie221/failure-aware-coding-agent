#!/usr/bin/env python3
"""Run exactly one prepared task; environment is supplied by the clean launcher."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from failure_aware_agent.runtime import assemble_task  # noqa: E402
from failure_aware_agent.persistence import atomic_write_json  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify_termination(agent_info: dict, adapter=None, error: BaseException | None = None) -> str:
    terminations = getattr(adapter, "terminations", []) if adapter is not None else []
    if any(item.get("category") == "budget_exhausted" for item in terminations):
        return "http_attempt_budget_exhausted"
    if isinstance(error, EOFError):
        return "noninteractive_eof"
    status = agent_info.get("exit_status")
    if status == "LimitsExceeded":
        return "step_or_cost_limit_exceeded"
    if status:
        return str(status)
    return type(error).__name__ if error is not None else "unknown"


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: run_task.py TASK_JSON CONDITION RUN_DIR", file=sys.stderr)
        return 64
    task_json, condition, run_dir = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    if condition not in {"control", "enhanced", "enhanced_v2"}:
        print("unsupported condition", file=sys.stderr)
        return 64
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    task = json.loads(task_json.read_text(encoding="utf-8"))
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = Path(task["workspace"]).resolve(strict=True)
    wrapper = Path(task["wrapper"]).resolve(strict=True)
    experiment_root = run_dir.parents[1]
    frozen_config = experiment_root / "experiment_config.yaml"
    config_path = frozen_config if frozen_config.is_file() else ROOT / "config/experiment.yaml"
    import yaml
    if frozen_config.is_file():
        prepared = json.loads((experiment_root / "prepared_manifest.json").read_text(encoding="utf-8"))
        actual_config_hash = hashlib.sha256(frozen_config.read_bytes()).hexdigest()
        if prepared.get("experiment_config_sha256") != actual_config_hash:
            raise RuntimeError("frozen experiment configuration hash mismatch")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    runner_config = config.get("runner", {})
    protocol_version = runner_config.get("protocol_version", 1)
    protocol_guard = runner_config.get("verified_submission_gate", False)
    if protocol_version == 2 and not protocol_guard:
        raise RuntimeError("protocol v2 config must enable the verified-submission gate")
    if condition == "enhanced_v2" and protocol_version != 2:
        raise RuntimeError("enhanced_v2 requires a frozen protocol v2 config")
    config["agent"]["output_path"] = str(run_dir / "trajectory.json")
    config["agent"]["step_limit"] = 12
    config["agent"]["wall_time_limit_seconds"] = 150
    config["environment"]["timeout"] = 30

    bundle = None
    start = time.monotonic()
    exit_reason = "not_started"
    termination_category = "not_started"
    agent_info = {}
    error = None
    try:
        bundle = assemble_task(
            config, workspace, wrapper, condition=condition,
            protocol_guard=protocol_guard,
            initial_hashes=task.get("workspace_hashes"),
            public_test_command=task.get("public_test_command"),
        )
        task_text = (workspace / "TASK.md").read_text(encoding="utf-8")
        agent_info = bundle["agent"].run(task_text)
        termination_category = classify_termination(agent_info, bundle["adapter"])
        exit_reason = str(agent_info.get("exit_status", termination_category))
        if termination_category == "http_attempt_budget_exhausted":
            exit_reason = termination_category
    except BaseException as exc:
        termination_category = ("assembly_failed" if bundle is None else
                                classify_termination(agent_info, bundle["adapter"], exc))
        exit_reason = termination_category
        error = {"class": type(exc).__name__, "message": str(exc)[:1000]}
        traceback.print_exc(file=sys.stderr)
    finally:
        elapsed = round(time.monotonic() - start, 3)
        if bundle is not None:
            try:
                trajectory = bundle["agent"].save(None)
                atomic_write_json(run_dir / "trajectory.json", trajectory)
            except BaseException as exc:
                trajectory = {}
                error = error or {"class": type(exc).__name__, "message": "trajectory save failed"}
                exit_reason = "trajectory_save_failed"
            atomic_write_json(run_dir / "http_attempts.json", bundle["budget"].safe_events())
            if bundle["feedback"] is not None:
                atomic_write_json(run_dir / "feedback.json", bundle["feedback"].records)
            if bundle["retry_guard"] is not None:
                atomic_write_json(run_dir / "retry_guard.json", bundle["retry_guard"].records)
            if bundle.get("submission_gate") is not None:
                atomic_write_json(run_dir / "submission_gate.json", bundle["submission_gate"].records)
            atomic_write_json(run_dir / "model_terminations.json", bundle["adapter"].terminations)
            try:
                bundle["agent"].env.cleanup()
            except BaseException as exc:
                error = error or {"class": type(exc).__name__, "message": "environment cleanup failed"}
                exit_reason = "environment_cleanup_failed"
            finally:
                bundle["adapter"].restore()
                bundle["http_client"].close()
        else:
            trajectory = {}
            atomic_write_json(run_dir / "trajectory.json", trajectory)
            atomic_write_json(run_dir / "http_attempts.json", [])
            atomic_write_json(run_dir / "model_terminations.json", [])
            atomic_write_json(run_dir / "retry_guard.json", [])

        actual_hashes = {}
        missing_files = []
        for relative in task["public_files_only"]:
            candidate = workspace / relative
            if candidate.is_file() and not candidate.is_symlink():
                actual_hashes[relative] = digest(candidate)
            else:
                missing_files.append(relative)
        changed = sorted(path for path, sha in actual_hashes.items()
                         if task["source_hashes"].get(path) != sha)
        changed.extend(missing_files)
        calls, prompt_tokens, completion_tokens = 0, 0, 0
        for message in trajectory.get("messages", []):
            response = message.get("extra", {}).get("response", {})
            usage = response.get("usage", {}) if isinstance(response, dict) else {}
            if usage:
                calls += 1
                prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
                completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        attempt_path = run_dir / "http_attempts.json"
        attempt_events = json.loads(attempt_path.read_text(encoding="utf-8")) if attempt_path.exists() else []
        try:
            with open(task["audit_file"], encoding="utf-8") as audit_stream:
                wrapper_invocations = sum(1 for _ in audit_stream)
        except OSError:
            wrapper_invocations = None
        result = {
            "task_id": task["task_id"], "condition": condition,
            "exit_reason": exit_reason, "agent_info": agent_info,
            "termination_category": termination_category,
            "error": error, "elapsed_seconds": elapsed,
            "limit_kind": agent_info.get("limit_kind"),
            "model_calls_with_usage": calls, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "http_attempts": sum(event.get("event") == "http_attempt" for event in attempt_events),
            "budget_rejections": sum(event.get("event") == "budget_rejected_before_send"
                                      for event in attempt_events),
            "changed_files": changed, "final_hashes": actual_hashes,
            "wrapper_invocations": wrapper_invocations,
            "retry_guard_skips": sum(
                event.get("event") == "retry_suppressed_unchanged_workspace"
                for event in (bundle["retry_guard"].records if bundle is not None
                              and bundle["retry_guard"] is not None else [])),
            "submission_gate_blocks": sum(
                event.get("event") == "submission_blocked"
                for event in (bundle["submission_gate"].records if bundle is not None
                              and bundle.get("submission_gate") is not None else [])),
            "trajectory_path": str(run_dir / "trajectory.json"),
        }
        atomic_write_json(run_dir / "task_result.json", result)
    return 0 if exit_reason in {
        "Submitted", "LimitsExceeded", "TimeExceeded", "FormatError", "RepeatedFormatError",
        "http_attempt_budget_exhausted", "step_or_cost_limit_exceeded", "noninteractive_eof",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
