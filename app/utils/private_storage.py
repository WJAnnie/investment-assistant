"""Conservative filesystem boundaries for opt-in private research state.

Paths and hashes are accident/corruption guards, not encryption or an external
attestation. The host and parent directories must remain trusted and private.
"""

import json
import os
from pathlib import Path
import stat
import subprocess


MAX_PRIVATE_BYTES = 2_000_000
_REPARSE_POINT = 0x400


def reject_links(path):
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("private path cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            raise ValueError("private path must not use links or reparse points")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ValueError("private file hardlinks are not allowed")


def require_private_execution():
    if "GITHUB_ACTIONS" in os.environ and (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_REPOSITORY_VISIBILITY") != "private"
    ):
        raise ValueError("private state requires verified private execution")


def private_path(path):
    require_private_execution()
    candidate = Path(path).absolute()
    reject_links(candidate)
    destination = candidate.resolve()
    root = Path(__file__).resolve().parents[2]
    message = "private state must be in untracked .private/ or outside Git worktrees"
    if destination.is_relative_to(root):
        if not destination.is_relative_to(root / ".private"):
            raise ValueError(message)
        # Check the whole private tree, not '.' (which always matches source).
        try:
            result = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", ".private"],
                cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("private Git exclusion cannot be verified") from exc
        if result.returncode == 0:
            raise ValueError("private path is tracked by git")
        if result.returncode != 1:
            raise ValueError("private Git exclusion cannot be verified")
    for ancestor in (destination, *destination.parents):
        if os.path.lexists(ancestor / ".git") and ancestor != root:
            raise ValueError(message)
    return destination


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("private JSON contains duplicate keys")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("private JSON contains a nonfinite value")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid private JSON") from exc


def read_private_bytes(path, max_bytes=MAX_PRIVATE_BYTES):
    destination = private_path(path)
    with destination.open("rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("private file must be regular and not hardlinked")
        if info.st_size > max_bytes:
            raise ValueError("private file exceeds size cap")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("private file exceeds size cap")
    return data


def write_private_exclusive(path, data, max_bytes=MAX_PRIVATE_BYTES):
    if len(data) > max_bytes:
        raise ValueError("private file exceeds size cap")
    destination = private_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_path(destination)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
