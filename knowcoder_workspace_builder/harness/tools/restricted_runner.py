"""Run model-written Python with a cross-platform filesystem audit hook."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.harness.write_boundary import (
    WriteBoundaryError,
    execution_write_root,
    require_workspace_write_path,
)

_SINGLE_PATH_WRITE_EVENTS = {
    "os.chmod",
    "os.chown",
    "os.mkdir",
    "os.remove",
    "os.removexattr",
    "os.rmdir",
    "os.setxattr",
    "os.truncate",
    "os.unlink",
    "os.utime",
}
_TWO_PATH_WRITE_EVENTS = {"os.link", "os.rename", "os.replace", "os.symlink"}
_PROCESS_EVENTS = {
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.startfile",
    "os.startfile/2",
    "os.system",
    "pty.spawn",
    "subprocess.Popen",
}


def _path_text(value: Any) -> str:
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return str(value)


def _require_real_write_path(value: Any, allowed_root: Path) -> None:
    if isinstance(value, int):
        return
    path = Path(_path_text(value)).expanduser()
    if path.is_absolute():
        resolved = path.resolve()
    else:
        # Model-written scripts frequently pass a relative batch/output path. Anchor it to
        # the execution write root (the session's intermediate dir) instead of rejecting, so
        # it lands inside the allowed boundary. Resolve the (absolute) base first, then append
        # the relative parts — resolving the joined path directly would use the process cwd and
        # silently drop the anchor.
        resolved = allowed_root.resolve().joinpath(*path.parts)
    if resolved == Path(os.devnull).resolve():
        return
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise PermissionError(
            f"write blocked outside {allowed_root}: {resolved}"
        ) from exc


def _open_requests_write(mode: Any, flags: Any) -> bool:
    if isinstance(mode, str) and any(char in mode for char in "wax+"):
        return True
    if not isinstance(flags, int):
        return False
    access_mode = flags & os.O_ACCMODE
    write_flags = os.O_APPEND | os.O_CREAT | os.O_TRUNC
    return access_mode in {os.O_WRONLY, os.O_RDWR} or bool(flags & write_flags)


def install_write_audit_hook(allowed_root: Path) -> None:
    allowed_root = allowed_root.resolve()

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "open" and args:
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            if _open_requests_write(mode, flags):
                _require_real_write_path(args[0], allowed_root)
            return
        if event in _SINGLE_PATH_WRITE_EVENTS and args:
            _require_real_write_path(args[0], allowed_root)
            return
        if event in _TWO_PATH_WRITE_EVENTS:
            for value in args[:2]:
                _require_real_write_path(value, allowed_root)
            return
        if event in _PROCESS_EVENTS:
            raise PermissionError("child process creation is disabled in execute_code")
        if event.startswith("ctypes."):
            raise PermissionError("ctypes is disabled in execute_code")

    sys.addaudithook(audit)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("restricted_runner requires a script path")
    script = require_workspace_write_path(sys.argv[1])
    if not script.is_file() or script.suffix != ".py":
        raise WriteBoundaryError(f"script must be an existing Python file: {script}")
    allowed_root = execution_write_root()
    sys.dont_write_bytecode = True
    install_write_audit_hook(allowed_root)
    sys.argv = [str(script), *sys.argv[2:]]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
