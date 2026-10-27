# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""xero command-line interface (Click)."""

from __future__ import annotations

import os
import shutil
import sys
from importlib import resources
from pathlib import Path

import click

from . import builders, check, environ, gitops, spack, sync, venv
from .area import AREA_FILE, AREA_TEMPLATE, Area
from .topic import BUILDERS, KINDS, MODES, Topic, create, topic_from_cwd
from .util import Die, die, run, say

AREA_GITIGNORE = """\
# xero: only xero.toml and topics/*/topic.toml (plus your own notes) are content.
/extern
/repos/
/branches/
/cache/
/topics/*/source/
/topics/*/refs/
/topics/*/venv/
/topics/*/envs/
"""


# --- resolution helpers -------------------------------------------------------


def _area(ctx: click.Context) -> Area:
    if ctx.obj is None:
        ctx.obj = Area.find()
    return ctx.obj


def _topic(area: Area, name: str | None) -> Topic:
    if not name:
        name, _ = topic_from_cwd(area)
    if not name:
        name = os.environ.get("XERO_TOPIC")
    if not name:
        die("no topic: give -t/--topic, run inside topics/<t>/, or set XERO_TOPIC")
    return Topic(area, name)


def _env(area: Area, topic: Topic, name: str | None, required: bool = True) -> str | None:
    envs = list(topic.envs)
    if not name:
        t, e = topic_from_cwd(area)
        if t == topic.name and e:
            name = e
    if not name and os.environ.get("XERO_TOPIC") == topic.name and os.environ.get("XERO_ENV") in envs:
        name = os.environ["XERO_ENV"]
    if not name and len(envs) == 1:
        name = envs[0]
    if not name:
        if required:
            die(f"topic '{topic.name}' has envs {envs or 'none'}; give -e/--env")
        return None
    if name not in envs:
        die(f"topic '{topic.name}' has no env '{name}' (has: {', '.join(envs) or 'none'})")
    return name


topic_opt = click.option("-t", "--topic", "topic_name", help="topic name (default: from cwd or XERO_TOPIC)")
env_opt = click.option("-e", "--env", "env_name", help="Spack env name (default: from cwd, XERO_ENV or the topic's only env)")


def _print_notes(notes) -> None:
    for n in notes:
        click.echo(f"  {n}")


# --- top level ----------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Manage topic-oriented development areas."""


@cli.command()
@click.option("--extern", "extern", type=click.Path(path_type=Path),
              help="make extern/ a symlink to this directory (eg another area's extern/)")
@click.argument("directory", type=click.Path(path_type=Path), default=".")
def init(extern, directory) -> None:
    """Initialize (or complete) a xero area in DIRECTORY."""
    root = directory.absolute()
    root.mkdir(parents=True, exist_ok=True)
    if not (root / AREA_FILE).exists():
        (root / AREA_FILE).write_text(AREA_TEMPLATE)
        say(f"wrote {root / AREA_FILE}")
    if not (root / ".gitignore").exists():
        (root / ".gitignore").write_text(AREA_GITIGNORE)
    area = Area(root)
    for d in (area.repos_dir, area.branches_dir, area.topics_dir, area.cache_dir):
        d.mkdir(parents=True, exist_ok=True)
    if not area.superbuild_file.exists():
        area.superbuild_file.parent.mkdir(parents=True, exist_ok=True)
        text = resources.files("xero").joinpath("data/superbuild/CMakeLists.txt").read_text()
        area.superbuild_file.write_text(text)
        say(f"wrote {area.superbuild_file}")
    if extern:
        target = extern.expanduser().absolute()
        if not (target / "envs").is_dir():
            die(f"{target} does not look like an extern directory (no envs/)")
        link = area.extern
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            die(f"{link} exists and is not a symlink")
        link.symlink_to(target)
        say(f"linked {link} -> {target}")
    envs = area.env_names()
    say(f"area {root} ready; Spack envs: {', '.join(envs) or 'none'}")


# --- repos --------------------------------------------------------------------


@cli.group()
def repo() -> None:
    """Bare repos under repos/."""


@repo.command("add")
@click.option("--from", "from_", type=click.Path(exists=True, path_type=Path),
              help="make the bare repo from this existing local clone (keeps its local branches)")
@click.option("--fetch/--no-fetch", default=None,
              help="fetch from upstream now (default: yes for a url, no with --from)")
@click.argument("name")
@click.argument("url", required=False)
@click.pass_context
def repo_add(ctx, from_, fetch, name, url) -> None:
    """Add repo NAME as a bare clone of URL (or of a local clone via --from)."""
    area = _area(ctx)
    bare = area.repo_path(name)
    if from_:
        rems = gitops.clone_bare_from(bare, from_, url, fetch=bool(fetch))
    else:
        if not url:
            die("give a URL or --from")
        gitops.init_bare(bare, url, fetch=fetch is not False)
        rems = {"origin": url}
    area.set_repo(name, rems["origin"], rems)
    for r, u in sorted(rems.items()):
        say(f"repo {name}: remote {r} {u}")


@repo.command("remote")
@click.option("--fetch/--no-fetch", default=True, help="fetch the remote now")
@click.argument("name")
@click.argument("remote")
@click.argument("url")
@click.pass_context
def repo_remote(ctx, fetch, name, remote, url) -> None:
    """Add (or change) REMOTE at URL for repo NAME, eg a personal fork."""
    area = _area(ctx)
    bare = area.require_repo(name)
    gitops.configure_remote(bare, remote, url)
    rems = area.repo_remotes(name)
    rems[remote] = url
    area.set_repo(name, rems["origin"], rems)
    if fetch:
        run(["git", "fetch", "--tags", remote], cwd=bare)


@repo.command("list")
@click.pass_context
def repo_list(ctx) -> None:
    """List registered repos."""
    area = _area(ctx)
    for name, tab in sorted(area.repos().items()):
        bare = area.repo_path(name)
        n = len(gitops.worktree_list(bare)) if bare.is_dir() else 0
        flag = "" if bare.is_dir() else "  (MISSING bare repo)"
        click.echo(f"{name:30s} {n:3d} worktrees  {tab.get('url', '')}{flag}")


@repo.command("fetch")
@click.argument("names", nargs=-1)
@click.pass_context
def repo_fetch(ctx, names) -> None:
    """Fetch all remotes of NAMES (default: all repos)."""
    area = _area(ctx)
    for name in names or sorted(area.repos()):
        run(["git", "fetch", "--all", "--tags", "--prune"], cwd=area.require_repo(name))


# --- branches (worktrees) -------------------------------------------------------


@cli.group()
def branch() -> None:
    """Worktrees under branches/<repo>/<ref>/."""


@branch.command("add")
@click.option("--submodules", is_flag=True, help="init submodules in the new worktree")
@click.option("--new", "base", metavar="BASE", help="create REF as a new branch starting at BASE")
@click.argument("repo_name")
@click.argument("ref")
@click.pass_context
def branch_add(ctx, submodules, base, repo_name, ref) -> None:
    """Make a worktree of REPO_NAME at REF (branch, tag or sha)."""
    area = _area(ctx)
    path = area.branch_path(repo_name, ref)
    if path.exists():
        die(f"already exists: {path}")
    repo = area.require_repo(repo_name)
    if base:
        gitops.create_branch(repo, ref, base)
    gitops.add_worktree(repo, path, ref, submodules=submodules)


@branch.command("list")
@click.argument("repo_name", required=False)
@click.pass_context
def branch_list(ctx, repo_name) -> None:
    """List worktrees with their users and state."""
    area = _area(ctx)
    for wt in check.worktrees(area):
        if repo_name and wt.repo != repo_name:
            continue
        flags = []
        if not wt.head_ok:
            flags.append(f"HEAD={wt.head}")
        if wt.dirty:
            flags.append(f"dirty({wt.dirty})")
        if not wt.pushed:
            flags.append("unpushed")
        users = ", ".join(wt.users) or "-"
        click.echo(f"{wt.repo}/{wt.ref}  [{users}]  {' '.join(flags)}")


# --- topics ---------------------------------------------------------------------


@cli.group()
def topic() -> None:
    """Topics under topics/<t>/."""


@topic.command("new")
@click.option("-e", "--env", "envs", multiple=True, help="Spack env for this topic (repeatable)")
@click.option("--builder", type=click.Choice(BUILDERS), default="superbuild", show_default=True)
@click.option("-d", "--description", default="")
@click.argument("name")
@click.pass_context
def topic_new(ctx, envs, builder, description, name) -> None:
    """Create topic NAME."""
    area = _area(ctx)
    t = create(area, name, description, list(envs), builder)
    _print_notes(sync.sync(t))
    say(f"topic {name}: {t.file}")


@topic.command("list")
@click.pass_context
def topic_list(ctx) -> None:
    """List topics."""
    area = _area(ctx)
    for name in area.topic_names():
        t = Topic(area, name)
        envs = ",".join(t.envs) or "-"
        click.echo(f"{name:24s} envs={envs:20s} {t.description}")


@topic.command("add")
@topic_opt
@click.option("--repo", "repo_name", help="repo name (default: NAME)")
@click.option("--mode", type=click.Choice(MODES), default="link", show_default=True)
@click.option("--kind", type=click.Choice(KINDS), help="override source kind detection")
@click.option("--env", "envs", multiple=True, help="restrict to these envs (repeatable)")
@click.option("--path", "path", help="dir mode: link to this path")
@click.option("--submodules", is_flag=True, help="init submodules of a new checkout")
@click.option("--ref-only", "ref_only", is_flag=True, help="add under refs/ (never built)")
@click.argument("name")
@click.argument("ref", required=False)
@click.pass_context
def topic_add(ctx, topic_name, repo_name, mode, kind, envs, path, submodules, ref_only, name, ref) -> None:
    """Add source (or --ref-only reference) NAME at REF to a topic."""
    area = _area(ctx)
    t = _topic(area, topic_name)
    section = "refs" if ref_only else "source"
    if mode != "dir" and not ref:
        die("a REF is needed unless --mode dir")
    values = {
        "repo": repo_name if repo_name and repo_name != name else None,
        "ref": ref,
        "mode": mode if mode != "link" else None,
        "path": path,
        "kind": kind,
        "envs": list(envs) if envs else None,
        "submodules": True if submodules else None,
    }
    if ref_only:
        values.pop("kind")
        values.pop("envs")
    t.set_entry(section, name, values)
    _print_notes(sync.sync(Topic(area, t.name)))


@topic.command("set-ref")
@topic_opt
@click.option("--ref-only", "ref_only", is_flag=True, help="change a refs/ entry")
@click.option("--new", "new", is_flag=True, help="create REF as a new branch (see --base)")
@click.option("--base", help="with --new: start the branch here (default: the current ref)")
@click.argument("name")
@click.argument("ref")
@click.pass_context
def topic_set_ref(ctx, topic_name, ref_only, new, base, name, ref) -> None:
    """Point a topic's NAME at REF (makes the worktree if needed)."""
    area = _area(ctx)
    t = _topic(area, topic_name)
    section = "refs" if ref_only else "source"
    tab = (t.doc.get(section) or {}).get(name)
    if tab is None:
        die(f"topic '{t.name}' has no [{section}.{name}]")
    ent = t.entries(section)[name]
    if new:
        if ent.mode == "dir":
            die(f"[{section}.{name}] is a dir entry; --new needs a repo")
        gitops.create_branch(area.require_repo(ent.repo), ref, base or ent.ref)
    elif base:
        die("--base needs --new")
    tab["ref"] = ref
    t.save()
    _print_notes(sync.sync(Topic(area, t.name)))


@topic.command("rm")
@topic_opt
@click.option("--ref-only", "ref_only", is_flag=True, help="remove a refs/ entry")
@click.argument("name")
@click.pass_context
def topic_rm(ctx, topic_name, ref_only, name) -> None:
    """Remove NAME from a topic (a symlink is removed; real directories are left)."""
    area = _area(ctx)
    t = _topic(area, topic_name)
    section = "refs" if ref_only else "source"
    ent = t.entries(section).get(name)
    if ent is None:
        die(f"topic '{t.name}' has no [{section}.{name}]")
    path = t.entry_path(ent)
    t.remove_entry(section, name)
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        say(f"left {path} in place (not a symlink); remove it yourself if wanted")


@topic.command("env-add")
@topic_opt
@click.option("--builder", type=click.Choice(BUILDERS), default="superbuild", show_default=True)
@click.argument("env_name")
@click.pass_context
def topic_env_add(ctx, topic_name, builder, env_name) -> None:
    """Add Spack env ENV_NAME to a topic (or change its builder)."""
    area = _area(ctx)
    t = _topic(area, topic_name)
    t.set_env(env_name, builder)
    _print_notes(sync.sync(Topic(area, t.name)))


@topic.command("env-rm")
@topic_opt
@click.argument("env_name")
@click.pass_context
def topic_env_rm(ctx, topic_name, env_name) -> None:
    """Remove Spack env ENV_NAME from a topic (envs/<env>/ is left in place)."""
    area = _area(ctx)
    t = _topic(area, topic_name)
    t.remove_env(env_name)
    if t.env_root(env_name).exists():
        say(f"left {t.env_root(env_name)} in place; remove it yourself if wanted")


# --- sync / status / check / gc --------------------------------------------------------


def _topics(area: Area, name: str | None, all_: bool) -> list[Topic]:
    if all_:
        return [Topic(area, n) for n in area.topic_names()]
    return [_topic(area, name)]


@cli.command("sync")
@topic_opt
@click.option("--all", "all_", is_flag=True, help="all topics")
@click.option("-n", "--dry-run", is_flag=True, help="only report")
@click.pass_context
def sync_cmd(ctx, topic_name, all_, dry_run) -> None:
    """Make topic directories match their topic.toml."""
    area = _area(ctx)
    for t in _topics(area, topic_name, all_):
        notes = sync.sync(t, fix=not dry_run)
        click.echo(f"{t.name}: {'in sync' if not notes else ''}")
        _print_notes(notes)


def _print_report(t: Topic, rep: dict) -> int:
    """Print one topic's report; return the number of problems."""
    problems = 0
    drift = [n for n in rep["drift"]]
    click.echo(f"topic {t.name}" + (f"  (epic {t.epic})" if t.epic else ""))
    if drift:
        problems += len(drift)
        click.echo("  drift (run 'xero sync'):")
        for n in drift:
            click.echo(f"    {n.path}: {n.message}")
    for env, r in rep["envs"].items():
        click.echo(f"  env {env}: {r['state']}")
        if r["state"] != "ok":
            problems += 1
        for reason in r["reasons"]:
            click.echo(f"    {reason}")
    v = rep["venv"]
    if v["state"] != "none":
        click.echo(f"  venv: {v['state']}")
        if v["state"] != "ok":
            problems += 1
        for reason in v["reasons"]:
            click.echo(f"    {reason}")
    return problems


@cli.command()
@topic_opt
@click.option("--all", "all_", is_flag=True, help="all topics")
@click.pass_context
def status(ctx, topic_name, all_) -> None:
    """Report topic drift and what needs rebuilding."""
    area = _area(ctx)
    for t in _topics(area, topic_name, all_):
        _print_report(t, check.topic_report(t))


@cli.command("check")
@click.pass_context
def check_cmd(ctx) -> None:
    """Check every topic and worktree; exit 1 if anything needs attention."""
    area = _area(ctx)
    problems = 0
    for name in area.topic_names():
        t = Topic(area, name)
        problems += _print_report(t, check.topic_report(t))
    wts = check.worktrees(area)
    bad = [w for w in wts if not w.head_ok]
    unused = [w for w in wts if not w.users]
    if bad:
        problems += len(bad)
        click.echo("worktrees whose HEAD does not match their directory name:")
        for w in bad:
            click.echo(f"  {w.repo}/{w.ref}: HEAD is {w.head}")
    if unused:
        click.echo("worktrees no topic uses ('xero gc' removes the clean, pushed ones):")
        for w in unused:
            why = []
            if w.dirty:
                why.append(f"dirty({w.dirty})")
            if not w.pushed:
                why.append("unpushed")
            click.echo(f"  {w.repo}/{w.ref}  {' '.join(why) or 'collectable'}")
    sys.exit(1 if problems else 0)


@cli.command()
@click.option("--remove", is_flag=True, help="actually remove the collectable worktrees")
@click.pass_context
def gc(ctx, remove) -> None:
    """List (or --remove) worktrees no topic uses that are clean and pushed."""
    area = _area(ctx)
    for w in check.worktrees(area):
        if not w.collectable:
            continue
        if not remove:
            click.echo(f"would remove {w.repo}/{w.ref}")
            continue
        run(["git", "worktree", "remove", w.path], cwd=area.repo_path(w.repo))
        # prune now-empty parents of nested refs (eg features/foo)
        parent = w.path.parent
        stop = area.branches_dir.resolve() / w.repo
        while parent != stop and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


# --- environment ------------------------------------------------------------------------


@cli.command("env")
@topic_opt
@env_opt
@click.option("--shell", type=click.Choice(["bash", "fish"]), default="bash", show_default=True)
@click.option("--build", "for_build", is_flag=True, help="the build environment instead of the run environment")
@click.pass_context
def env_cmd(ctx, topic_name, env_name, shell, for_build) -> None:
    """Print shell code that enters a topic's environment.

    \b
    bash:  eval "$(xero env -t T -e E)"
    fish:  xero env -t T -e E --shell fish | source
    """
    area = _area(ctx)
    t = _topic(area, topic_name)
    e = _env(area, t, env_name, required=False)
    new = environ.compute(t, e, purpose="build" if for_build else "run")
    changed, removed = environ.diff(new, dict(os.environ))
    click.echo(environ.render(changed, removed, shell), nl=False)


@cli.command("run", context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@topic_opt
@env_opt
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.pass_context
def run_cmd(ctx, topic_name, env_name, command) -> None:
    """Run COMMAND in a topic's environment, in the current directory."""
    area = _area(ctx)
    t = _topic(area, topic_name)
    e = _env(area, t, env_name, required=False)
    environ.describe_missing_venv(t)
    env = environ.compute(t, e, purpose="run")
    exe = shutil.which(command[0], path=env.get("PATH"))
    if not exe:
        die(f"command not found in topic environment: {command[0]}")
    os.execve(exe, list(command), env)


@cli.command("shell")
@topic_opt
@env_opt
@click.pass_context
def shell_cmd(ctx, topic_name, env_name) -> None:
    """Start $SHELL in a topic's environment (exit it to leave)."""
    area = _area(ctx)
    t = _topic(area, topic_name)
    e = _env(area, t, env_name, required=False)
    environ.describe_missing_venv(t)
    env = environ.compute(t, e, purpose="run")
    env["XERO_SHELL"] = f"{t.name}/{e}" if e else t.name
    sh = os.environ.get("SHELL", "/bin/sh")
    say(f"entering {env['XERO_SHELL']}; exit to leave")
    os.execve(sh, [sh], env)


# --- build --------------------------------------------------------------------------------


@cli.command()
@topic_opt
@env_opt
@click.option("--all", "all_", is_flag=True, help="all topics (and all their envs)")
@click.option("--stale-only", is_flag=True, help="skip what is already up to date")
@click.option("--fresh", is_flag=True, help="wipe build dirs (and recreate the venv) first")
@click.option("--no-venv", is_flag=True, help="skip the topic venv")
@click.argument("packages", nargs=-1)
@click.pass_context
def build(ctx, topic_name, env_name, all_, stale_only, fresh, no_venv, packages) -> None:
    """Build a topic in one or all of its envs (and its venv).

    PACKAGES (source names) limits the CMake build to those packages.
    """
    area = _area(ctx)
    for t in _topics(area, topic_name, all_):
        notes = sync.sync(t)
        if notes:
            click.echo(f"{t.name}: sync")
            _print_notes(notes)
        if any(n.level == "drift" for n in notes):
            say(f"WARNING: topic {t.name} has unresolved drift; building anyway")
        if not no_venv and not packages:
            vs = venv.status(t)
            if vs["state"] != "none" and (fresh or not stale_only or vs["state"] != "ok"):
                venv.build(t, fresh=fresh)
        e = _env(area, t, env_name, required=False)
        envs = [e] if e and not all_ else list(t.envs)
        for e in envs:
            b = builders.get(t, e)
            if stale_only and not fresh and b.status()["state"] == "ok":
                say(f"{t.name}/{e}: up to date")
                continue
            say(f"building topic {t.name} env {e} with {b.name}")
            b.go(list(packages) or None, fresh=fresh)


# --- extern (Spack) ---------------------------------------------------------------------------


@cli.group()
def extern() -> None:
    """Drive the Spack envs under extern/ (shared if extern/ is a symlink!)."""


@extern.command("spack", context_settings={"ignore_unknown_options": True})
@click.option("-e", "--env", "env_name", required=True)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def extern_spack(ctx, env_name, args) -> None:
    """Run 'spack -e <env> ARGS' with xero's Spack isolation."""
    spack.spack(_area(ctx), env_name, list(args))


@extern.command("concretize", context_settings={"ignore_unknown_options": True})
@click.option("-e", "--env", "env_name", required=True)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def extern_concretize(ctx, env_name, args) -> None:
    """spack concretize -f an env."""
    spack.concretize(_area(ctx), env_name, list(args))


@extern.command("install", context_settings={"ignore_unknown_options": True})
@click.option("-e", "--env", "env_name", required=True)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def extern_install(ctx, env_name, args) -> None:
    """spack install an env, then regenerate its view from scratch."""
    spack.install(_area(ctx), env_name, list(args))


@extern.command("capture")
@click.option("-e", "--env", "env_name", required=True)
@click.option("--spec", help="spec whose build environment to capture (default: xero.toml [envs.<e>] spec)")
@click.option("--from", "from_", type=click.Path(exists=True, path_type=Path),
              help="import an existing 'spack build-env --dump' file instead")
@click.pass_context
def extern_capture(ctx, env_name, spec, from_) -> None:
    """Capture an env's Spack build environment for profile "spack-build-env"."""
    spack.capture(_area(ctx), env_name, spec, from_)


def main() -> None:
    try:
        cli(standalone_mode=True)
    except Die as err:
        click.echo(f"xero: error: {err}", err=True)
        sys.exit(err.status)
