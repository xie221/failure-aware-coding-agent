"""Create fresh, public-only workspaces and path-pinned action wrappers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import yaml

from .action_wrapper import render_action_wrapper
from .persistence import atomic_write_json


PROJECTS = Path(__file__).resolve().parents[3]
BENCHMARK = PROJECTS / "agent-benchmark-v1"
MINI_ENV = PROJECTS / ".conda-envs/miniswe"
MINI_SOURCE = PROJECTS / "mini-swe-agent/src"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config/experiment.yaml"
PUBLIC_FILES = ("TASK.md", "buggy.py", "tests/test_buggy.py")
MANIFEST_SHA256 = "672a075e89522d6fd329c47c113863c21cdc178c95fa904f4bce386564c10d7e"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_fingerprint() -> str:
    """Deterministic source version id when this workspace has no Git metadata."""
    files = []
    for folder, suffixes in ((PROJECT_ROOT / "src", {".py"}),
                             (PROJECT_ROOT / "scripts", {".py", ".sh"}),
                             (PROJECT_ROOT / "tests", {".py"}),
                             (PROJECT_ROOT / "config", {".yaml", ".yml"})):
        if folder.exists():
            files.extend(path for path in folder.rglob("*")
                         if path.is_file() and not path.is_symlink() and path.suffix in suffixes
                         and "__pycache__" not in path.parts)
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(PROJECT_ROOT).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + bytes.fromhex(_sha(path)))
    return digest.hexdigest()


def prepare(experiment_root: Path, *, benchmark_root: Path = BENCHMARK,
            config_path: Path = CONFIG_PATH, conditions: tuple[str, ...] = ("control", "enhanced"),
            include_smoke: bool = True) -> dict:
    benchmark_root = Path(benchmark_root).resolve(strict=True)
    config_path = Path(config_path).resolve(strict=True)
    manifest_path = benchmark_root / "benchmark_manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if benchmark_root == BENCHMARK.resolve() and manifest_hash != MANIFEST_SHA256:
        raise RuntimeError(f"Benchmark manifest hash mismatch: {manifest_hash}")
    manifest = json.loads(manifest_bytes)
    tasks = manifest["tasks"]
    ids = [task["task_id"] for task in tasks]
    declared_count = manifest.get("task_count", len(tasks))
    if len(ids) != declared_count or len(set(ids)) != len(ids) or not ids:
        raise RuntimeError("Manifest task count is empty, inconsistent, or contains duplicate IDs")
    if (not conditions or len(set(conditions)) != len(conditions)
            or any(name not in {"control", "enhanced", "enhanced_v2"} for name in conditions)):
        raise RuntimeError("conditions must be a nonempty subset of control/enhanced/enhanced_v2")
    if experiment_root.exists():
        raise FileExistsError(f"Refusing to overwrite experiment directory: {experiment_root}")
    if not MINI_ENV.is_dir() or not MINI_SOURCE.is_dir():
        raise RuntimeError("mini-SWE runtime paths are missing")
    if not config_path.is_file():
        raise RuntimeError("frozen experiment configuration is missing")

    experiment_root.mkdir(parents=True)
    config_snapshot = experiment_root / "experiment_config.yaml"
    shutil.copy2(config_path, config_snapshot)
    config_hash = _sha(config_snapshot)
    frozen_config = yaml.safe_load(config_snapshot.read_text(encoding="utf-8"))
    runner_config = frozen_config.get("runner", {})
    protocol_version = runner_config.get("protocol_version", 1)
    configured_order = runner_config.get("condition_order")
    if configured_order is not None and list(conditions) != list(configured_order):
        raise RuntimeError("requested conditions do not match the frozen condition_order")
    records = []
    splits = (("smoke",) if include_smoke else ()) + tuple(conditions)
    for split in splits:
        split_tasks = [ids[0]] if split == "smoke" else ids
        for task_id in split_tasks:
            task = next(row for row in tasks if row["task_id"] == task_id)
            source = (benchmark_root / task["task_path"]).resolve()
            if benchmark_root not in source.parents:
                raise RuntimeError(f"Task path escapes benchmark: {task_id}")
            public_files = tuple(task.get("public_file_hashes", {}).keys()) or PUBLIC_FILES
            if set(public_files) != set(PUBLIC_FILES):
                raise RuntimeError(f"Unexpected public-file allowlist for {task_id}: {public_files}")
            source_files = {}
            for rel in public_files:
                path = source / rel
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(f"Missing/symlink public file for {task_id}: {rel}")
                source_files[rel] = _sha(path)
            if task.get("public_file_hashes") and source_files != task["public_file_hashes"]:
                raise RuntimeError(f"Public source hash differs from manifest: {task_id}")
            extra = [p for p in source.rglob("*") if p.is_symlink()]
            if extra:
                raise RuntimeError(f"Task contains symlink: {task_id}")

            run_dir = experiment_root / split / task_id
            workspace = run_dir / "workspace"
            snapshot = run_dir / "evaluator_snapshot"
            wrapper_dir = run_dir / "runtime"
            workspace.mkdir(parents=True)
            snapshot.mkdir()
            wrapper_dir.mkdir()
            for rel in public_files:
                (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / rel, workspace / rel)
                (snapshot / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / rel, snapshot / rel)
            copied = {rel: _sha(workspace / rel) for rel in PUBLIC_FILES}
            if source_files != copied:
                raise RuntimeError(f"Public copy hash mismatch: {task_id}/{split}")
            audit = wrapper_dir / "wrapper_invocations.count"
            audit.touch(mode=0o600)
            wrapper = wrapper_dir / "action-wrapper.py"
            wrapper.write_text(render_action_wrapper(workspace, audit, MINI_ENV, MINI_SOURCE), encoding="utf-8")
            wrapper.chmod(0o700)
            wrapper_hash = _sha(wrapper)
            atomic_write_json(run_dir / "task.json", {
                "task_id": task_id, "category": task["category"], "difficulty": task["difficulty"],
                "description": task["description"], "public_test_command": task["test_command"],
                "source_dir": str(source),
                "benchmark_root": str(benchmark_root),
                "hidden_test_path": str(benchmark_root / "evaluation/hidden_tests" / task_id / "test_hidden.py"),
                "hidden_test_sha256": task.get("hidden_test_sha256"),
                "benchmark_manifest_sha256": manifest_hash,
                "workspace": str(workspace), "wrapper": str(wrapper), "audit_file": str(audit),
                "wrapper_sha256": wrapper_hash,
                "source_hashes": source_files, "workspace_hashes": copied,
                "public_files_only": list(public_files),
            })
            records.append({"split": split, "task_id": task_id, "workspace": str(workspace),
                            "source_dir": str(source),
                            "files": source_files, "wrapper": str(wrapper),
                            "wrapper_sha256": wrapper_hash})
    frozen = {"benchmark_manifest_sha256": manifest_hash, "benchmark_root": str(benchmark_root),
              "benchmark_id": manifest.get("benchmark_id", "legacy-v1"),
              "experiment_protocol_version": protocol_version,
              "conditions": list(conditions), "smoke_included": include_smoke,
              "condition_order": list(conditions), "task_order": ids,
              "task_order_policy": runner_config.get("task_order_policy", "manifest_order"),
              "task_count": len(ids),
              "experiment_config_sha256": config_hash,
              "code_version_sha256": code_fingerprint(),
              "source_control_revision": None,
              "public_files": list(PUBLIC_FILES), "runs": records}
    atomic_write_json(experiment_root / "prepared_manifest.json", frozen)
    (experiment_root / "results.jsonl").touch()
    return frozen
