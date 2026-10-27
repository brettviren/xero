# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Small shared helpers: echoed subprocess execution, fatal errors, symlinks."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path


class Die(Exception):
    """A fatal, user-facing error; carries the message and exit status."""

    def __init__(self, message: str, status: int = 1) -> None:
        super().__init__(message)
        self.status = status


def die(message: str, status: int = 1):
    raise Die(message, status)


def say(message: str) -> None:
    print(f"xero: {message}", file=sys.stderr, flush=True)


def run(
    cmd: list,
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    quiet: bool = False,
) -> subprocess.CompletedProcess:
    """Echo *cmd* (like ``set -x``) to stderr, then run it.

    With *capture* the child's stdout/stderr are captured (as text) on the
    returned :class:`~subprocess.CompletedProcess` instead of inherited.
    """
    if not quiet:
        printable = " ".join(shlex.quote(str(c)) for c in cmd)
        print(f"+ {printable}", file=sys.stderr, flush=True)
    try:
        return subprocess.run(
            [str(c) for c in cmd],
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            check=check,
            capture_output=capture,
            text=True if capture else None,
        )
    except subprocess.CalledProcessError as err:
        detail = ""
        if capture and err.stderr:
            detail = ": " + err.stderr.strip()
        die(f"command failed ({err.returncode}): {shlex.join(str(c) for c in cmd)}{detail}")


def relsymlink(link: Path, target: Path) -> str:
    """Make *link* a relative symlink to *target*, idempotently.

    Returns "ok" (already correct), "made" or "fixed" (was a symlink to
    elsewhere).  A non-symlink at *link* is never touched: :func:`die`.
    """
    rel = os.path.relpath(target, link.parent)
    if link.is_symlink():
        if os.readlink(link) == rel:
            return "ok"
        link.unlink()
        link.symlink_to(rel)
        return "fixed"
    if link.exists():
        die(f"refusing to replace non-symlink {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(rel)
    return "made"


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode()
    return hashlib.sha256(text).hexdigest()


def which(name: str, path: str | None) -> str | None:
    """shutil.which against an explicit PATH string."""
    import shutil

    return shutil.which(name, path=path)
