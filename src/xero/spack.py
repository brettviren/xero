# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Drive the Spack environments under extern/.

Ported from xerosere: always a *directory* env (``spack -e <env_dir>``), the
Spack scope/cache dirs isolated from ~/.spack, and ``install`` rebuilds the
view from scratch.  Spack is always driven through the resolved extern path.

NOTE: when extern/ is a symlink to another area, these commands change that
area's Spack envs too.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .area import Area
from .environ import load_dump
from .util import die, run, say


def spack_exe(area: Area) -> str:
    exe = area.spack_exe
    if exe.is_file() and os.access(exe, os.X_OK):
        return str(exe)
    found = shutil.which("spack")
    if found:
        return found
    die(f"spack not found at {exe} or on PATH")


def env_dir_real(area: Area, env: str) -> Path:
    area.require_env(env)
    return area.extern_real / "envs" / env


def spack(area: Area, env: str, args: list[str]) -> None:
    run([spack_exe(area), "-e", env_dir_real(area, env), *args], env=area.spack_environ())


def concretize(area: Area, env: str, extra: list[str]) -> None:
    spack(area, env, ["concretize", "-f", *extra])


def install(area: Area, env: str, extra: list[str]) -> None:
    spack(area, env, ["install", *extra])
    # Spack's view regeneration short-circuits on a content-hash marker and can
    # be a no-op over a stale/partial view; remove the view and regenerate.
    view = env_dir_real(area, env) / "view"
    if view.exists() or view.is_symlink():
        run(["rm", "-rf", view])
    spack(area, env, ["env", "view", "regenerate"])


def capture(area: Area, env: str, spec: str | None, from_file: Path | None) -> Path:
    """Capture the env's ``spack build-env`` for profile "spack-build-env".

    The dump is taken in a clean environment (so it holds only what Spack
    sets).  With *from_file*, import an existing dump instead, keeping only
    variables that reference the Spack install tree or are SPACK_*.
    """
    dest = area.build_env_dump(env)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if from_file:
        values = load_dump(from_file, keep_prefix=str(area.extern_real))
        say(f"importing {len(values)} variables from {from_file}")
    else:
        spec = spec or area.env_profile(env).get("spec")
        if not spec:
            die(f"no spec to capture: give --spec or set xero.toml [envs.{env}] spec")
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "dump.sh"
            clean = {"HOME": os.environ.get("HOME", "/"), "PATH": "/usr/bin:/bin"}
            run(
                [spack_exe(area), "-e", env_dir_real(area, env), "build-env", "--dump", raw, spec],
                env=area.spack_environ(clean),
            )
            values = load_dump(raw)
    lines = [f"# Captured Spack build environment for env '{env}' (xero extern capture)."]
    for k, v in sorted(values.items()):
        lines.append(f"export {k}={_quote(v)}")
    dest.write_text("\n".join(lines) + "\n")
    say(f"wrote {dest}")
    return dest


def _quote(v: str) -> str:
    import shlex

    return shlex.quote(v)
