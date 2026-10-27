# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Build fingerprints and staleness.

After a successful build a builder writes ``envs/<e>/build/.xero-stamp.json``:

    {"config": {...},                 env-level inputs (builder, -D flags,
                                      package set, spack.lock, profile)
     "packages": {name: {...}}}       per-package source state

Comparing a fresh fingerprint with the stamp says what is stale and why.  A
changed "config" means reconfigure; a changed spack.lock or profile means the
build dir must be wiped (sub-build caches pin paths into the old view).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import gitops
from .util import sha256_file, sha256_text

STAMP = ".xero-stamp.json"


def _dirty_hash(path: Path) -> str | None:
    """Hash of uncommitted state: tracked diff plus untracked file contents."""
    status = gitops.dirty_lines(path)
    if not status:
        return None
    diff = subprocess.run(
        ["git", "diff", "HEAD", "--no-ext-diff", "--submodule=short"],
        cwd=path, capture_output=True,
    ).stdout
    h = [sha256_text(diff)]
    for line in status:
        if line.startswith("?? "):
            f = path / line[3:]
            h.append(f"{line[3:]}:{sha256_file(f)}")
    return sha256_text("\n".join(h))


def _tree_hash(path: Path) -> str:
    """For non-git directories: hash of (relative path, size, mtime) of all files."""
    items = []
    for f in sorted(path.rglob("*")):
        if f.is_file():
            st = f.stat()
            items.append(f"{f.relative_to(path)}:{st.st_size}:{st.st_mtime_ns}")
    return sha256_text("\n".join(items))


def source_state(path: Path) -> dict:
    """Fingerprint of one source directory."""
    real = path.resolve()
    if gitops.is_git(real):
        return {"path": str(real), "head": gitops.head_sha(real), "dirty": _dirty_hash(real)}
    return {"path": str(real), "tree": _tree_hash(real)}


def describe_source_change(old: dict, new: dict) -> str:
    if old.get("path") != new.get("path"):
        return f"source moved {old.get('path')} -> {new.get('path')}"
    if old.get("head") != new.get("head"):
        return f"HEAD {str(old.get('head'))[:12]} -> {str(new.get('head'))[:12]}"
    if old.get("dirty") != new.get("dirty"):
        if new.get("dirty") is None:
            return "uncommitted changes went away"
        return "uncommitted changes"
    return "files changed"


def load(build_dir: Path) -> dict | None:
    f = build_dir / STAMP
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return None


def save(build_dir: Path, config: dict, packages: dict, merge: bool) -> None:
    old = load(build_dir) if merge else None
    pk = dict(old.get("packages", {})) if old else {}
    pk.update(packages)
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / STAMP).write_text(json.dumps({"config": config, "packages": pk}, indent=1, sort_keys=True))


def compare(stamp: dict | None, config: dict, packages: dict) -> dict:
    """Staleness report: {"state": "unbuilt"|"wipe"|"reconfigure"|"stale"|"ok",
    "reasons": [...], "stale": [package names], "moved": [package names]}.

    A package whose source path changed (eg a ref switch) is "moved": its
    build dir is tied to the old source path, so it needs a fresh configure.
    """
    if stamp is None:
        return {"state": "unbuilt", "reasons": ["never built"], "stale": sorted(packages), "moved": []}
    old_cfg = stamp.get("config", {})
    reasons = []
    state = "ok"
    for key in ("lock", "profile"):
        if old_cfg.get(key) != config.get(key):
            reasons.append(f"Spack env {key} changed")
            state = "wipe"
    if state != "wipe":
        for key in sorted(set(old_cfg) | set(config)):
            if key in ("lock", "profile"):
                continue
            if old_cfg.get(key) != config.get(key):
                reasons.append(f"{key} changed")
                state = "reconfigure"
    stale = []
    moved = []
    old_pk = stamp.get("packages", {})
    for name, st in sorted(packages.items()):
        if name not in old_pk:
            stale.append(name)
            reasons.append(f"{name}: never built")
        elif old_pk[name] != st:
            stale.append(name)
            reasons.append(f"{name}: {describe_source_change(old_pk[name], st)}")
            if old_pk[name].get("path") != st.get("path"):
                moved.append(name)
    if moved and state in ("ok", "stale"):
        state = "reconfigure"
    if stale and state == "ok":
        state = "stale"
    return {"state": state, "reasons": reasons, "stale": stale, "moved": moved}
