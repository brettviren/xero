# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Area-wide checks: topic drift and staleness, worktree drift, GC candidates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import builders, gitops, sync, venv
from .area import Area
from .topic import Topic


def topic_report(topic: Topic) -> dict:
    """{"drift": [Note], "envs": {env: report}, "venv": report}."""
    out = {"drift": sync.sync(topic, fix=False), "envs": {}, "venv": venv.status(topic)}
    for env in topic.envs:
        try:
            out["envs"][env] = builders.get(topic, env).status()
        except Exception as err:  # a broken env must not hide the others
            out["envs"][env] = {"state": "error", "reasons": [str(err)], "stale": []}
    return out


@dataclass
class Worktree:
    repo: str
    path: Path
    ref: str  # relative path under branches/<repo>/
    users: list[str]  # "topic:section.name"
    head_ok: bool
    head: str
    dirty: int
    pushed: bool

    @property
    def collectable(self) -> bool:
        return not self.users and self.head_ok and not self.dirty and self.pushed


def users_by_worktree(area: Area) -> dict[Path, list[str]]:
    out: dict[Path, list[str]] = {}
    for name in area.topic_names():
        topic = Topic(area, name)
        for section in ("source", "refs"):
            for ent in topic.entries(section).values():
                if ent.mode == "link":
                    path = area.branch_path(ent.repo, ent.ref).resolve()
                    out.setdefault(path, []).append(f"{name}:{section}.{ent.name}")
    return out


def worktrees(area: Area) -> list[Worktree]:
    users = users_by_worktree(area)
    out = []
    if not area.repos_dir.is_dir():
        return out
    for bare in sorted(area.repos_dir.glob("*.git")):
        repo = bare.name[: -len(".git")]
        base = area.branches_dir.resolve() / repo
        for wt in gitops.worktree_list(bare):
            wt = wt.resolve()
            try:
                ref = str(wt.relative_to(base))
            except ValueError:
                continue  # a worktree outside branches/ (eg a topic clone) is not ours
            ok, actual = gitops.head_matches(wt, ref) if wt.exists() else (False, "missing")
            out.append(Worktree(
                repo=repo, path=wt, ref=ref, users=users.get(wt, []),
                head_ok=ok, head=actual,
                dirty=len(gitops.dirty_lines(wt)) if wt.exists() else 0,
                pushed=gitops.is_pushed(wt) if wt.exists() else False,
            ))
    return out
