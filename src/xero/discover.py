# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Discover CMake packages and their in-topic dependency order.

Mirrors the super-build's discovery: a package's name is its first
``project(<name>`` and its dependencies are its ``find_package(<name>`` calls
that name another package of the same set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import die

_PROJECT = re.compile(r"^[ \t]*project[ \t]*\([ \t]*([A-Za-z0-9_]+)", re.MULTILINE | re.IGNORECASE)
_FIND = re.compile(r"^[ \t]*find_package[ \t]*\([ \t]*([A-Za-z0-9_]+)", re.MULTILINE | re.IGNORECASE)


@dataclass
class Package:
    dirname: str  # entry name under source/
    path: Path  # resolved source directory
    project: str
    requires: list[str] = field(default_factory=list)


def scan(dirname: str, path: Path) -> Package | None:
    cml = path / "CMakeLists.txt"
    if not cml.is_file():
        return None
    text = cml.read_text(errors="replace")
    m = _PROJECT.search(text)
    if not m:
        return None
    return Package(dirname, path.resolve(), m.group(1), _FIND.findall(text))


def ordered(pkgs: list[Package]) -> list[Package]:
    """Topologically sort *pkgs* so each comes after the siblings it requires."""
    by_name = {p.project: p for p in pkgs}
    done: list[Package] = []
    state: dict[str, int] = {}  # 1 visiting, 2 done

    def visit(p: Package, chain: list[str]) -> None:
        s = state.get(p.project)
        if s == 2:
            return
        if s == 1:
            die("dependency cycle: " + " -> ".join(chain + [p.project]))
        state[p.project] = 1
        for req in p.requires:
            dep = by_name.get(req)
            if dep is not None and dep is not p:
                visit(dep, chain + [p.project])
        state[p.project] = 2
        done.append(p)

    for p in sorted(pkgs, key=lambda p: p.dirname):
        visit(p, [])
    return done


def internal_deps(pkg: Package, pkgs: list[Package]) -> list[str]:
    names = {p.project for p in pkgs}
    return [r for r in pkg.requires if r in names and r != pkg.project]
