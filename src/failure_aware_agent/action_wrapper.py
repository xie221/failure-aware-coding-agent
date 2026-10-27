"""Workspace-pinned action wrapper template, rendered once for each task copy."""
from __future__ import annotations

import json
from pathlib import Path


def render_action_wrapper(workspace: Path, audit_file: Path, mini_env: Path,
                          mini_source: Path) -> str:
    constants = {
        "WORKSPACE": str(workspace.resolve()),
        "AUDIT_FILE": str(audit_file.resolve()),
        "MINI_ENV": str(mini_env.resolve()),
        "MINI_SOURCE": str(mini_source.resolve()),
    }
    const_text = "\n".join(f"{k} = {json.dumps(v)}" for k, v in constants.items())
    return '''#!/usr/bin/env python3
import os
import sys
from pathlib import Path
''' + const_text + '''

SAFE_ENV = {"LANG", "LC_ALL", "TZ"}
INTERACTIVE_EDITOR_RE = r"(?im)(?:^|[;&|()\\n]|\\$\\()\\s*(?:(?:sudo(?:\\s+-\\S+)*\\s+|command\\s+|env(?:\\s+[A-Za-z_]\\w*=\\S+)*\\s+|timeout\\s+\\S+\\s+))*(?:/[^\\s;&|()]*/)?(?:nano|vim|vi)(?=\\s|$)"

def is_interactive_editor_command(command):
    # Intentionally small command-boundary check, not a general shell parser.
    import re
    return re.search(INTERACTIVE_EDITOR_RE, command) is not None

def fail(message):
    print("failure-aware-wrapper: " + message, file=sys.stderr)
    raise SystemExit(125)

def add_empty_parent_dirs(argv, target, created):
    path = Path(target)
    if not path.is_absolute():
        fail("runtime bind path must be absolute")
    for parent in reversed(path.parents):
        directory = str(parent)
        if directory == "/" or directory in created:
            continue
        argv.extend(["--dir", directory])
        created.add(directory)

def main():
    args = sys.argv[1:]
    if len(args) < 7 or args[:3] != ["--bind", WORKSPACE, WORKSPACE]:
        fail("unexpected workspace bind protocol")
    if args[3:5] != ["--chdir", WORKSPACE]:
        fail("unexpected workspace cwd")
    i, safe_env = 5, {}
    while i < len(args) and args[i] == "--setenv":
        if i + 2 >= len(args) or args[i + 1] not in SAFE_ENV:
            fail("environment key not allowed")
        safe_env[args[i + 1]] = args[i + 2]
        i += 3
    if i + 3 != len(args) or args[i:i + 2] != ["bash", "-c"]:
        fail("unexpected action argv")
    command = args[i + 2]
    try:
        fd = os.open(AUDIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write("1\\n")
    except OSError:
        fail("audit counter unavailable")
    if is_interactive_editor_command(command):
        print("failure-aware-wrapper: interactive terminal editors (nano/vim/vi) are unavailable; "
              "use Python file operations, sed, or shell redirection instead", file=sys.stderr)
        raise SystemExit(125)

    bwrap = [
        "/usr/bin/bwrap", "--die-with-parent", "--unshare-pid", "--tmpfs", "/",
        "--dir", "/usr", "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--dir", "/tmp/home", "--dir", "/tmp/home/.config",
        "--dir", "/tmp/cache",
    ]
    created_dirs = {"/", "/usr", "/tmp"}
    for runtime_path in (MINI_ENV, MINI_SOURCE):
        add_empty_parent_dirs(bwrap, runtime_path, created_dirs)
        bwrap.extend(["--ro-bind", runtime_path, runtime_path])
    bwrap.extend([
        "--dir", "/workspace", "--bind", WORKSPACE, "/workspace",
        "--chdir", "/workspace", "--clearenv",
        "--setenv", "PATH", MINI_ENV + "/bin:/usr/bin:/bin",
        "--setenv", "HOME", "/tmp/home", "--setenv", "TMPDIR", "/tmp",
        "--setenv", "XDG_CONFIG_HOME", "/tmp/home/.config",
        "--setenv", "XDG_CACHE_HOME", "/tmp/cache",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        # The `pytest` console-script sets sys.path[0] to the environment's bin
        # directory. Expose only this task's root so public imports such as
        # `from buggy import ...` work inside the sandbox (the evaluator does
        # the same explicitly).
        "--setenv", "PYTHONPATH", "/workspace",
        "--setenv", "LANG", safe_env.get("LANG", "C.UTF-8"),
        "--setenv", "LC_ALL", safe_env.get("LC_ALL", "C.UTF-8"),
        "--setenv", "TZ", safe_env.get("TZ", "UTC"),
        "bash", "-c", command,
    ])
    argv = ["/usr/bin/unshare", "--user", "--map-root-user", "--net", "--", *bwrap]
    os.execv(argv[0], argv)

if __name__ == "__main__":
    main()
'''
