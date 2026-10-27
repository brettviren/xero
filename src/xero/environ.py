# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Compute the environment of a (topic, env) pair.

One function, :func:`compute`, serves ``xero env``, ``xero run``, the generated
``run/.envrc`` and the builders, so they can never disagree.

Layering, later layers taking precedence (path-like variables are prepended):

  1. the caller's environment
  2. the env profile: nothing for "view"; the captured ``spack build-env``
     dump for "spack-build-env" (with the real compilers instead of Spack's
     compiler wrapper unless ``compiler_wrapper = true``)
  3. the Spack view   (bin, lib, pkgconfig, CMAKE_PREFIX_PATH)
  4. the topic+env install prefix
  5. [envs.<e>.prepend|set] from xero.toml, then [env.<e>.prepend|set] from
     topic.toml
  6. the topic venv   (run only, so its tools win as with an activated venv)
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from . import view as topic_view
from .area import Area
from .topic import Topic
from .util import die, say, which

# Ambient compiler search-path vars a Spack-activated shell exports; if cmake's
# configure sees them it records $view/include as an implicit include dir and
# STRIPS it from imported targets.  find_package() supplies every needed path
# via CMAKE_PREFIX_PATH, so unsetting these for a "view" build loses nothing.
INCLUDE_VARS = ("CPATH", "CPLUS_INCLUDE_PATH", "C_INCLUDE_PATH", "OBJC_INCLUDE_PATH")

# Variables in a captured build-env dump that describe the capturing shell,
# not the Spack environment.
DUMP_DROP = {"PWD", "OLDPWD", "SHLVL", "_", "HOME", "USER", "LOGNAME", "TERM", "SHELL"}


def is_pathlike(name: str) -> bool:
    return name.endswith("PATH") and not name.startswith("SPACK_")


def prepend(env: dict[str, str], name: str, entries: list[str]) -> None:
    """Prepend *entries* to the ':'-list *name*, dropping duplicates."""
    entries = [str(e) for e in entries if e]
    old = [p for p in env.get(name, "").split(":") if p]
    new = []
    for p in entries + old:
        if p not in new:
            new.append(p)
    env[name] = ":".join(new)


def load_dump(path: Path, keep_prefix: str | None = None) -> dict[str, str]:
    """Evaluate a ``spack build-env --dump`` file in a clean bash, return its vars.

    With *keep_prefix*, keep only variables that reference it or are SPACK_*
    (filters session noise out of a dump captured from a busy shell).
    """
    if not path.is_file():
        die(f"no captured build environment {path}; run 'xero extern capture'")
    script = f"set -a; source {shlex.quote(str(path))} >/dev/null 2>&1; env -0"
    cp = subprocess.run(
        ["env", "-i", "HOME=" + os.environ.get("HOME", "/"), "bash", "--noprofile", "--norc", "-c", script],
        capture_output=True,
    )
    if cp.returncode != 0:
        die(f"failed to evaluate {path}: {cp.stderr.decode(errors='replace')}")
    out = {}
    for item in cp.stdout.split(b"\0"):
        if b"=" not in item:
            continue
        k, v = item.decode(errors="replace").split("=", 1)
        if k in DUMP_DROP or k.startswith(("XERO_", "XEROSERE_")):
            continue
        if keep_prefix and not (k.startswith("SPACK_") or keep_prefix in v):
            continue
        out[k] = v
    return out


def layer(env: dict[str, str], extra: dict[str, str]) -> None:
    """Apply a dump: path-like variables prepend, others replace."""
    for k, v in extra.items():
        if is_pathlike(k):
            prepend(env, k, v.split(":"))
        else:
            env[k] = v


COMPILER_VARS = {"CC": "SPACK_CC", "CXX": "SPACK_CXX", "FC": "SPACK_FC", "F77": "SPACK_F77"}


def unwrap_compilers(env: dict[str, str]) -> None:
    """Use the real compilers behind Spack's compiler wrapper.

    The wrapper injects -I/-L/rpath for every dependency of the captured
    spec.  Those then win over a topic's own install of the same package (eg
    a WCT source built in the topic is linked against the Spack store's WCT),
    so by default builds use the wrapped compilers and let CMake resolve
    everything through CMAKE_PREFIX_PATH (install first, then view).
    """
    for var, spack_var in COMPILER_VARS.items():
        if env.get(spack_var):
            env[var] = env[spack_var]
    path = [p for p in env.get("PATH", "").split(":") if p and "/libexec/spack" not in p]
    env["PATH"] = ":".join(path)


def templates(topic: Topic, env_name: str | None) -> dict[str, str]:
    area = topic.area
    vals = {
        "area": str(area.root),
        "topic": str(topic.root),
        "source": str(topic.source_dir),
        "refs": str(topic.refs_dir),
        "venv": str(topic.venv_dir),
        "extern": str(area.extern_real),
        "env": env_name or "",
    }
    if env_name:
        vals.update(
            build=str(topic.build_dir(env_name)),
            install=str(topic.install_dir(env_name)),
            run=str(topic.run_dir(env_name)),
            view=str(topic_view.path(topic, env_name)),
        )
    return vals


def _apply_config(env: dict[str, str], cfg: dict, vals: dict[str, str], where: str) -> None:
    def fill(s: str) -> str:
        try:
            return str(s).format(**vals)
        except KeyError as err:
            die(f"{where}: unknown template {{{err.args[0]}}} in {s!r}")

    for name, entries in (cfg.get("prepend") or {}).items():
        if isinstance(entries, str):
            entries = [entries]
        prepend(env, name, [fill(e) for e in entries])
    for name, value in (cfg.get("set") or {}).items():
        env[name] = fill(value)


def lib_dirs(prefix: Path) -> list[Path]:
    return [prefix / "lib", prefix / "lib64"]


def compute(
    topic: Topic,
    env_name: str | None,
    purpose: str = "run",
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """The full environment for *topic* in Spack env *env_name* (may be None).

    *purpose* is "run" (interactive use / ``xero run``) or "build" (builders).
    A build environment does not set LD_LIBRARY_PATH from the view or install
    (installed RPATHs make that unnecessary), drops ambient include-path
    variables for "view" profiles, and adds the ccache settings.
    """
    area = topic.area
    env = dict(os.environ if base is None else base)
    vals = templates(topic, env_name)

    if env_name:
        area.require_env(env_name)
        profile = area.env_profile(env_name)
        kind = profile.get("profile", "view")
        view = topic_view.path(topic, env_name)
        install = topic.install_dir(env_name)

        if kind == "spack-build-env":
            dump = load_dump(area.build_env_dump(env_name))
            drop = set(topic_view.read_meta(topic, env_name).get("overridden", []))
            if drop:
                # The dump points into the Spack store prefixes of packages the
                # topic overrides; keep those out like the filtered view does.
                dump = {k: ":".join(p for p in v.split(":") if not topic_view.drops(p, drop))
                        if is_pathlike(k) else v for k, v in dump.items()}
            layer(env, dump)
            if not profile.get("compiler_wrapper", False):
                unwrap_compilers(env)
        elif kind != "view":
            die(f"xero.toml [envs.{env_name}] unknown profile {kind!r} (want view|spack-build-env)")

        if purpose == "build" and kind == "view":
            for var in INCLUDE_VARS:
                env.pop(var, None)

        prepend(env, "PATH", [install / "bin", view / "bin"])
        prepend(env, "CMAKE_PREFIX_PATH", [install, view])
        prepend(env, "PKG_CONFIG_PATH", [d / "pkgconfig" for d in lib_dirs(install) + lib_dirs(view)])
        if purpose == "run":
            prepend(env, "LD_LIBRARY_PATH", lib_dirs(install) + lib_dirs(view))

        _apply_config(env, profile, vals, f"xero.toml [envs.{env_name}]")
        _apply_config(env, topic.envs.get(env_name, {}), vals, f"topic.toml [env.{env_name}]")

        env["XERO_ENV"] = env_name
        env["XERO_VIEW"] = str(view)
        env["XERO_INSTALL"] = str(install)
        env["XERO_BUILD"] = str(topic.build_dir(env_name))

        if purpose == "build":
            if area.settings.get("ccache", True) and which("ccache", env.get("PATH")):
                env.pop("CCACHE_DISABLE", None)
                # Build dirs differ per topic; rewrite paths under the area so
                # topics sharing a worktree share cache hits.
                env.setdefault("CCACHE_BASEDIR", str(area.root.resolve()))
                env["XERO_CCACHE"] = which("ccache", env.get("PATH"))

    # The venv is for running; a CMake build must find the Spack env's Python
    # (find_package(Python3)), not the Spack-independent venv's.
    if purpose == "run" and topic.venv_dir.is_dir():
        env["VIRTUAL_ENV"] = str(topic.venv_dir)
        prepend(env, "PATH", [topic.venv_dir / "bin"])
        env.pop("PYTHONHOME", None)

    env["XERO_ROOT"] = str(area.root)
    env["XERO_TOPIC"] = topic.name
    env["XERO_TOPIC_DIR"] = str(topic.root)
    env["XERO_SOURCE"] = str(topic.source_dir)
    return env


def diff(new: dict[str, str], old: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """(changed-or-added variables, removed variable names)."""
    changed = {k: v for k, v in new.items() if old.get(k) != v}
    removed = [k for k in old if k not in new]
    return changed, removed


def render(changed: dict[str, str], removed: list[str], shell: str) -> str:
    lines = []
    if shell == "bash":
        for k in removed:
            lines.append(f"unset {k}")
        for k, v in sorted(changed.items()):
            lines.append(f"export {k}={shlex.quote(v)}")
    elif shell == "fish":
        for k in removed:
            lines.append(f"set -e {k}")
        for k, v in sorted(changed.items()):
            if k == "PATH":
                parts = " ".join(shlex.quote(p) for p in v.split(":") if p)
                lines.append(f"set -gx PATH {parts}")
            else:
                lines.append(f"set -gx {k} {shlex.quote(v)}")
    else:
        die(f"unknown shell {shell!r} (want bash or fish)")
    return "\n".join(lines) + ("\n" if lines else "")


def describe_missing_venv(topic: Topic) -> None:
    if topic.python_sources() and not topic.venv_dir.is_dir():
        say(f"topic '{topic.name}' has python sources but no venv yet; run 'xero build'")
