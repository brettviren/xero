# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""A topic's view of a Spack env, minus the packages the topic overrides.

A Spack view merges every package into one prefix, so when a topic builds its
own copy of a package that is also in the view (eg wire-cell-toolkit), the
view's copy leaks back in: every view package's include/ and lib/ are on the
compiler and linker search paths, and a duplicated -I cannot beat -isystem.

So when a topic source overrides a view package, ``topics/<t>/envs/<e>/view``
is a real directory: a copy of the view's symlinks with the overridden
packages' entries left out.  Otherwise it is a plain symlink to the env view.

A view entry belongs to Spack package <name> when its symlink target lies in a
store prefix named ``<name>-<version>-<hash>``.  A source's Spack package name
is its ``spack = "..."`` key, else its repo name.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .topic import Entry, Topic
from .util import say, sha256_file

META = ".xero-view.json"
_HASH = re.compile(r"[a-z0-9]{32}")


def spack_name(ent: Entry) -> str:
    return str(ent.extra.get("spack") or ent.repo or ent.name)


def _prefix_package(component: str, names: set[str]) -> str | None:
    """The package name if *component* is a store prefix <name>-<ver>-<hash>."""
    head, _, h = component.rpartition("-")
    if not _HASH.fullmatch(h):
        return None
    for name in names:
        if head.startswith(name + "-"):
            version = head[len(name) + 1:]
            if version and (version[0].isdigit() or version in ("develop", "main", "master")):
                return name
    return None


def _owner(target: str, names: set[str]) -> str | None:
    for comp in target.split(os.sep):
        pkg = _prefix_package(comp, names)
        if pkg:
            return pkg
    return None


def overridden(topic: Topic, env: str, view: Path) -> list[str]:
    """Spack package names of the topic's sources (in *env*) present in *view*."""
    names = {spack_name(e) for e in topic.sources.values()
             if topic.in_env(e, env) and (e.mode != "dir" or "spack" in e.extra)}
    if not names or not view.is_dir():
        return []
    found = set()
    for root, dirs, files in os.walk(view):
        for n in dirs + files:
            p = os.path.join(root, n)
            if os.path.islink(p):
                pkg = _owner(os.readlink(p), names)
                if pkg:
                    found.add(pkg)
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        if found == names:
            break
    return sorted(found)


def drops(target: str, names: set[str]) -> bool:
    return _owner(target, names) is not None


def build(src: Path, dst: Path, names: set[str]) -> int:
    """Copy view *src* as symlinks into *dst*, leaving out *names*' entries."""
    skipped = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out = dst if rel == "." else dst / rel
        out.mkdir(parents=True, exist_ok=True)
        keep_dirs = []
        for n in dirs + files:
            p = os.path.join(root, n)
            if os.path.islink(p):
                target = os.readlink(p)
                if drops(target, names):
                    skipped += 1
                    continue
                os.symlink(target, out / n)
            elif n in dirs:
                keep_dirs.append(n)
            else:
                os.symlink(p, out / n)
        dirs[:] = keep_dirs
    return skipped


def read_meta(topic: Topic, env: str) -> dict:
    f = topic.env_root(env) / "view" / META
    if f.is_file():
        try:
            return json.loads(f.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def path(topic: Topic, env: str) -> Path:
    """The view a topic+env builds and runs against."""
    local = topic.env_root(env) / "view"
    if local.is_dir() and not local.is_symlink():
        return local
    return topic.area.view(env)


def wanted(topic: Topic, env: str) -> dict:
    """The meta a filtered view should have now ({} if none is needed)."""
    src = topic.area.view(env)
    names = overridden(topic, env, src)
    if not names:
        return {}
    return {
        "source": str(src),
        "overridden": names,
        "lock": sha256_file(topic.area.env_dir(env) / "spack.lock"),
    }


def ensure(topic: Topic, env: str, fix: bool) -> str | None:
    """Make envs/<e>/view right; return a description of what was (or is) wrong."""
    local = topic.env_root(env) / "view"
    want = wanted(topic, env)
    if not want:
        if local.is_dir() and not local.is_symlink():
            if fix:
                shutil.rmtree(local)
            return "filtered view no longer needed"
        return None  # the plain symlink is handled by sync
    if local.is_dir() and not local.is_symlink() and read_meta(topic, env) == want:
        return None
    msg = f"view must leave out {', '.join(want['overridden'])} (overridden by topic sources)"
    if not fix:
        return msg
    if local.is_symlink():
        local.unlink()
    elif local.exists():
        shutil.rmtree(local)
    skipped = build(Path(want["source"]), local, set(want["overridden"]))
    (local / META).write_text(json.dumps(want, indent=1, sort_keys=True))
    say(f"{local}: filtered view without {', '.join(want['overridden'])} ({skipped} entries left out)")
    return msg
