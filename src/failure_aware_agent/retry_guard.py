"""Suppress exact failed Shell retries only while the task workspace is unchanged."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
from pathlib import Path
import stat
from typing import Any


def normalize_command(command: str) -> str:
    # Avoid collapsing whitespace inside quoted shell strings: false-positive
    # suppression is worse than missing a cosmetically different retry.
    return command.replace("\r\n", "\n").strip()


def workspace_fingerprint(workspace: Path) -> str | None:
    """Hash workspace paths, file bytes, and permission bits; fail open on links."""
    try:
        workspace = workspace.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    digest = hashlib.sha256()
    try:
        for path in sorted(workspace.rglob("*")):
            relative = path.relative_to(workspace)
            if any(part in {".pytest_cache", "__pycache__", ".mypy_cache"}
                   for part in relative.parts):
                continue
            if path.is_symlink():
                return None
            if path.is_file() and path.suffix in {".pyc", ".pyo"}:
                continue
            metadata = path.stat(follow_symlinks=False)
            if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                return None
            digest.update(relative.as_posix().encode("utf-8", "replace"))
            digest.update(b"\0D" if stat.S_ISDIR(metadata.st_mode) else b"\0F")
            digest.update(oct(stat.S_IMODE(metadata.st_mode)).encode("ascii"))
            digest.update(b"\0")
            if stat.S_ISREG(metadata.st_mode):
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            digest.update(b"\0")
    except (OSError, RuntimeError):
        return None
    return digest.hexdigest()


class RepeatFailureGuard:
    """Remember recent nonzero commands and skip exact retries against same bytes."""

    def __init__(self, workspace: Path, *, capacity: int = 16):
        self.workspace = Path(workspace)
        self.capacity = capacity
        self._failed: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self.records: list[dict[str, Any]] = []

    def should_suppress(self, command: str) -> bool:
        normalized = normalize_command(command)
        command_hash = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()
        fingerprint = workspace_fingerprint(self.workspace)
        previous = self._failed.get(command_hash)
        if fingerprint is None:
            return False
        if previous is None:
            return False
        if previous[0] != fingerprint:
            self._failed.pop(command_hash, None)
            self.records.append({"event": "retry_allowed_workspace_changed",
                                 "command_fingerprint": command_hash,
                                 "workspace_fingerprint": fingerprint})
            return False
        self.records.append({"event": "retry_suppressed_unchanged_workspace",
                             "command_fingerprint": command_hash,
                             "workspace_fingerprint": fingerprint})
        return True

    def observe(self, command: str, result: dict) -> None:
        normalized = normalize_command(command)
        command_hash = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()
        fingerprint = workspace_fingerprint(self.workspace)
        returncode = result.get("returncode")
        if fingerprint is None or not isinstance(returncode, int) or returncode == 0:
            self._failed.pop(command_hash, None)
            return
        self._failed[command_hash] = (fingerprint, type(returncode).__name__)
        self._failed.move_to_end(command_hash)
        while len(self._failed) > self.capacity:
            self._failed.popitem(last=False)
        self.records.append({"event": "failure_observed", "command_fingerprint": command_hash,
                             "workspace_fingerprint": fingerprint, "returncode": returncode})

    @property
    def suppressed_count(self) -> int:
        return sum(row.get("event") == "retry_suppressed_unchanged_workspace" for row in self.records)
