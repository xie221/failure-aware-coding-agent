#!/usr/bin/env python3
"""Launch the offline test suite in a clean, socket-denied subprocess."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT.parent
MINI = PROJECTS / ".conda-envs/miniswe/bin/python"
MINI_SOURCE = PROJECTS / "mini-swe-agent/src"
STAGE6F = PROJECTS / "stage6f-agent-adapter-integration"
CONFIG_DIR = ROOT / "runtime/empty-global-config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
if any(CONFIG_DIR.iterdir()):
    raise RuntimeError("offline LiteLLM config directory must remain empty")

env = {
    "PATH": f"{MINI.parent}:/usr/bin:/bin",
    "HOME": str(ROOT / "runtime/test-home"),
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
    "PYTHONPATH": os.pathsep.join((str(ROOT / "tests"), str(ROOT / "src"),
                                    str(MINI_SOURCE), str(STAGE6F))),
    "PYTHONDONTWRITEBYTECODE": "1",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "MSWEA_GLOBAL_CONFIG_DIR": str(CONFIG_DIR),
    "MSWEA_SILENT_STARTUP": "1",
    "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT": "1",
}
Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
command = [str(MINI), "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
           "-p", "test_*.py", "-v"]
result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
(ROOT / "offline_test_output.txt").write_text(result.stdout, encoding="utf-8")
tests = []
pending = None
for line in result.stdout.splitlines():
    if line.startswith("test_") and " (" in line:
        if pending is not None:
            tests.append({"test": pending, "status": "FAIL_OR_INCOMPLETE"})
        pending = line.split(" (")[0]
        if line.rstrip().endswith(" ... ok"):
            tests.append({"test": pending, "status": "PASS"})
            pending = None
    elif pending is not None and line.strip() == "ok":
        tests.append({"test": pending, "status": "PASS"})
        pending = None
    elif pending is not None and (line.strip() == "FAIL" or line.strip() == "ERROR"):
        tests.append({"test": pending, "status": line.strip()})
        pending = None
if pending is not None:
    tests.append({"test": pending, "status": "FAIL_OR_INCOMPLETE"})
summary = {
    "suite": "failure_aware_coding_agent_offline",
    "exit_code": result.returncode,
    "tests_run": len(tests),
    "passed": sum(item["status"] == "PASS" for item in tests),
    "all_passed": result.returncode == 0 and bool(tests) and all(x["status"] == "PASS" for x in tests),
    "network_mode": "httpx.MockTransport plus pre-import socket/DNS deny",
    "blocked_socket_or_dns_attempts": 0 if result.returncode == 0 else "see test failure",
    "real_model_requests": 0,
    "cases": tests,
}
(ROOT / "offline_validation.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(result.stdout, end="")
print(f"OFFLINE_TESTS={summary['passed']}/{summary['tests_run']} EXIT={result.returncode}")
raise SystemExit(result.returncode)
