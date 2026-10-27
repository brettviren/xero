# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Git operations on bare repos and their worktrees.

A bare repo here is made with ``git init --bare`` + an ``origin`` remote whose
fetch refspec maps to ``refs/remotes/origin/*``.  (A plain ``git clone --bare``
maps remote heads straight onto local ``refs/heads/*``, so a later fetch would
clobber local branches.)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .util import die, run, say

def git(*args, cwd: Path | None = None, check: bool = True, quiet: bool = True) -> str:
    """Run git and return stripped stdout."""
    cp = run(["git", *args], cwd=cwd, capture=True, check=check, quiet=quiet)
    return cp.stdout.strip() if cp.returncode == 0 or not check else ""


def git_ok(*args, cwd: Path | None = None) -> bool:
    cp = subprocess.run(
        ["git", *[str(a) for a in args]],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    return cp.returncode == 0


def git_out(*args, cwd: Path | None = None) -> str | None:
    """stdout of a git command, or None if it fails."""
    cp = subprocess.run(
        ["git", *[str(a) for a in args]],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    return cp.stdout.strip() if cp.returncode == 0 else None


# --- bare repos -----------------------------------------------------------


def refspec(remote: str) -> str:
    return f"+refs/heads/*:refs/remotes/{remote}/*"


def configure_remote(bare: Path, remote: str, url: str) -> None:
    """Add or update *remote* in *bare* with a remote-tracking fetch refspec."""
    if git_out("remote", "get-url", remote, cwd=bare) is None:
        git("remote", "add", remote, url, cwd=bare)
    else:
        git("remote", "set-url", remote, url, cwd=bare)
    git("config", "--replace-all", f"remote.{remote}.fetch", refspec(remote), cwd=bare)


def remotes(repo: Path) -> dict[str, str]:
    """{remote name: url} of a repo."""
    out = {}
    for name in (git_out("remote", cwd=repo) or "").split():
        url = git_out("remote", "get-url", name, cwd=repo)
        if url:
            out[name] = url
    return out


def init_bare(bare: Path, url: str, fetch: bool = True) -> None:
    """Make a new bare repo at *bare* tracking *url* as origin."""
    if bare.exists():
        die(f"already exists: {bare}")
    bare.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "--bare", "-q", bare])
    configure_remote(bare, "origin", url)
    if fetch:
        run(["git", "fetch", "--tags", "origin"], cwd=bare)


def clone_bare_from(bare: Path, src: Path, url: str | None, fetch: bool = False) -> dict[str, str]:
    """Make a bare repo at *bare* from an existing local clone *src*.

    Carries over every local branch (including unpushed work), every remote of
    *src* with its remote-tracking refs, the tags and the branches' upstream
    settings.  *url*, if given, replaces the origin url.  Objects are
    hard-linked when on the same filesystem.  Returns {remote: url}.
    """
    if bare.exists():
        die(f"already exists: {bare}")
    src = src.resolve()
    if git_out("rev-parse", "--git-dir", cwd=src) is None:
        die(f"not a git repo: {src}")
    rems = remotes(src)
    if url:
        rems["origin"] = url
    if "origin" not in rems:
        die(f"{src} has no origin remote; give the upstream url explicitly")
    nstash = len((git_out("stash", "list", cwd=src) or "").splitlines())
    if nstash:
        say(f"WARNING: {src} has {nstash} stash entr{'y' if nstash == 1 else 'ies'} which are NOT carried over")

    bare.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--bare", "-q", src, bare])
    # A bare clone's origin points at src; replace it with src's remotes.
    for name, rurl in rems.items():
        configure_remote(bare, name, rurl)
        # Bring src's view of each remote along without touching the network.
        git("fetch", "-q", str(src), f"+refs/remotes/{name}/*:refs/remotes/{name}/*", cwd=bare)
    git("fetch", "-q", "--tags", str(src), cwd=bare)
    # Point HEAD at no branch so no branch counts as checked out in the bare repo.
    git("symbolic-ref", "HEAD", "refs/heads/__xero_bare__", cwd=bare)
    for line in (git_out("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=bare) or "").splitlines():
        remote = git_out("config", f"branch.{line}.remote", cwd=src)
        merge = git_out("config", f"branch.{line}.merge", cwd=src)
        if remote in rems and merge:
            git("config", f"branch.{line}.remote", remote, cwd=bare)
            git("config", f"branch.{line}.merge", merge, cwd=bare)
    if fetch:
        run(["git", "fetch", "--all", "--tags"], cwd=bare)
    return rems


# --- refs -----------------------------------------------------------------


def resolve_ref(repo: Path, ref: str) -> tuple[str, str]:
    """Classify *ref* in *repo*: ("branch"|"remote"|"tag"|"commit", full name or sha)."""
    if git_ok("show-ref", "--verify", "--quiet", f"refs/heads/{ref}", cwd=repo):
        return "branch", f"refs/heads/{ref}"
    for remote in ["origin"] + sorted(set(remotes(repo)) - {"origin"}):
        if git_ok("show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{ref}", cwd=repo):
            return "remote", f"refs/remotes/{remote}/{ref}"
    if git_ok("show-ref", "--verify", "--quiet", f"refs/tags/{ref}", cwd=repo):
        return "tag", f"refs/tags/{ref}"
    sha = git_out("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=repo)
    if sha:
        return "commit", sha
    die(f"unknown ref '{ref}' in {repo} (try 'xero repo fetch')")


def ensure_local_branch(repo: Path, ref: str) -> str:
    """Make sure *ref* exists as a local branch if it names a remote branch.

    Returns the kind from :func:`resolve_ref` after any branch creation.
    """
    kind, full = resolve_ref(repo, ref)
    if kind == "remote":
        git("branch", "--track", ref, full[len("refs/remotes/"):], cwd=repo)
        kind = "branch"
    return kind


def add_worktree(repo: Path, path: Path, ref: str, submodules: bool = False) -> None:
    """Add a worktree of *ref* at *path* (branch checked out; tag/sha detached)."""
    kind = ensure_local_branch(repo, ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "branch":
        run(["git", "worktree", "add", "-q", path, ref], cwd=repo)
    else:
        run(["git", "worktree", "add", "-q", "--detach", path, ref], cwd=repo)
    if submodules:
        init_submodules(path)


def init_submodules(path: Path) -> None:
    if (path / ".gitmodules").is_file():
        run(["git", "submodule", "update", "--init", "--recursive"], cwd=path)


def worktree_list(repo: Path) -> list[Path]:
    """Linked worktree paths of a bare repo (that still exist on disk)."""
    out = git_out("worktree", "list", "--porcelain", cwd=repo) or ""
    paths = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            p = Path(line[len("worktree "):])
            if p.resolve() != repo.resolve():
                paths.append(p)
    return paths


# --- worktree / clone state -------------------------------------------------


def head_matches(path: Path, ref: str) -> tuple[bool, str]:
    """Does the checkout at *path* have *ref* checked out?  Returns (ok, actual)."""
    branch = git_out("symbolic-ref", "-q", "--short", "HEAD", cwd=path)
    if branch is not None:
        return branch == ref, branch
    head = git_out("rev-parse", "HEAD", cwd=path) or "?"
    want = git_out("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=path)
    return head == want, f"detached@{head[:12]}"


def head_sha(path: Path) -> str | None:
    return git_out("rev-parse", "HEAD", cwd=path)


def is_git(path: Path) -> bool:
    return git_out("rev-parse", "--is-inside-work-tree", cwd=path) == "true"


def dirty_lines(path: Path) -> list[str]:
    out = git_out("status", "--porcelain=v1", "--untracked-files=all", cwd=path) or ""
    return [line for line in out.splitlines() if line]


def is_pushed(path: Path) -> bool:
    """True if HEAD is reachable from some remote-tracking ref or tag."""
    out = git_out(
        "for-each-ref", "--contains", "HEAD", "--count=1", "--format=%(refname)",
        "refs/remotes", "refs/tags", cwd=path,
    )
    return bool(out)
