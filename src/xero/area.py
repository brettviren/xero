# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""A xero *area*: the directory holding ``xero.toml``.

Layout::

    <area>/
      xero.toml                 repo registry, env profiles, area settings
      extern/                   Spack + envs (may be a symlink to another area)
      repos/<repo>.git/         bare clones
      branches/<repo>/<ref>/    worktrees, one per ref
      share/superbuild/         shared super-build CMakeLists.txt
      topics/<t>/               see topic.py
      cache/env/<e>.sh          captured Spack build environments
"""

from __future__ import annotations

import os
from pathlib import Path

import tomlkit

from .util import die

AREA_FILE = "xero.toml"

# Spack scope/cache isolation, relative to the (resolved) extern directory, so
# ~/.spack is never touched.
SPACK_ISOLATION = {
    "SPACK_USER_CACHE_PATH": "cache",
    "SPACK_USER_CONFIG_PATH": "scopes/user",
    "SPACK_SYSTEM_CONFIG_PATH": "scopes/system",
}


def find_root(start: Path | None = None) -> Path:
    """Return the area root: $XERO_ROOT, else the nearest parent with xero.toml."""
    env = os.environ.get("XERO_ROOT")
    if env:
        root = Path(env).absolute()
        if not (root / AREA_FILE).is_file():
            die(f"XERO_ROOT={env} holds no {AREA_FILE}")
        return root
    here = (start or Path.cwd()).absolute()
    for cand in (here, *here.parents):
        if (cand / AREA_FILE).is_file():
            return cand
    die(f"not inside a xero area (no {AREA_FILE} found above {here}); see 'xero init'")


class Area:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()
        self.file = self.root / AREA_FILE
        self.doc = tomlkit.parse(self.file.read_text())

    @classmethod
    def find(cls, start: Path | None = None) -> "Area":
        return cls(find_root(start))

    def save(self) -> None:
        self.file.write_text(tomlkit.dumps(self.doc))

    # --- top-level directories ---------------------------------------------

    @property
    def repos_dir(self) -> Path:
        return self.root / "repos"

    @property
    def branches_dir(self) -> Path:
        return self.root / "branches"

    @property
    def topics_dir(self) -> Path:
        return self.root / "topics"

    @property
    def superbuild_file(self) -> Path:
        return self.root / "share" / "superbuild" / "CMakeLists.txt"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    def _table(self, name: str) -> dict:
        return self.doc.get(name) or {}

    @property
    def settings(self) -> dict:
        return self._table("area")

    # --- extern -------------------------------------------------------------

    @property
    def extern(self) -> Path:
        """The extern directory as seen from the area (may be a symlink)."""
        return self.root / self.settings.get("extern", "extern")

    @property
    def extern_real(self) -> Path:
        """The resolved extern directory.

        Spack records absolute install prefixes, so Spack must always be driven
        through the real path, never through a symlink into another area.
        """
        return self.extern.resolve()

    @property
    def spack_exe(self) -> Path:
        return self.extern_real / "spack" / "bin" / "spack"

    def spack_environ(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)
        for var, rel in SPACK_ISOLATION.items():
            env[var] = str(self.extern_real / rel)
        return env

    def env_dir(self, env: str) -> Path:
        """The Spack environment directory, through the area's extern path."""
        return self.extern / "envs" / env

    def view(self, env: str) -> Path:
        """The resolved Spack view for *env*."""
        return (self.env_dir(env) / "view").resolve()

    def env_names(self) -> list[str]:
        envs = self.extern / "envs"
        if not envs.is_dir():
            return []
        return sorted(p.name for p in envs.iterdir() if (p / "spack.yaml").is_file())

    def require_env(self, env: str) -> None:
        if not (self.env_dir(env) / "spack.yaml").is_file():
            known = ", ".join(self.env_names()) or "none"
            die(f"no Spack env '{env}' under {self.extern}/envs (known: {known})")

    def env_profile(self, env: str) -> dict:
        """The [envs.<env>] table of xero.toml (empty if absent)."""
        return dict(self._table("envs").get(env) or {})

    def build_env_dump(self, env: str) -> Path:
        return self.cache_dir / "env" / f"{env}.sh"

    # --- repos and branches -------------------------------------------------

    def repos(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._table("repos").items()}

    def repo_url(self, name: str) -> str | None:
        return self.repos().get(name, {}).get("url")

    def repo_path(self, name: str) -> Path:
        return self.repos_dir / f"{name}.git"

    def require_repo(self, name: str) -> Path:
        path = self.repo_path(name)
        if not path.is_dir():
            die(f"no repo '{name}' (expected {path}); see 'xero repo add'")
        return path

    def branch_path(self, repo: str, ref: str) -> Path:
        return self.branches_dir / repo / ref

    def repo_remotes(self, name: str) -> dict[str, str]:
        """{remote: url} for a repo: origin from "url" plus its [remotes]."""
        tab = self.repos().get(name, {})
        out = dict(tab.get("remotes") or {})
        if tab.get("url"):
            out["origin"] = tab["url"]
        return out

    def set_repo(self, name: str, url: str, remotes: dict[str, str] | None = None) -> None:
        if "repos" not in self.doc:
            self.doc["repos"] = tomlkit.table(is_super_table=True)
        tab = tomlkit.table()
        tab["url"] = url
        extra = {k: v for k, v in (remotes or {}).items() if k != "origin"}
        if extra:
            rt = tomlkit.inline_table()
            rt.update(extra)
            tab["remotes"] = rt
        self.doc["repos"][name] = tab
        self.save()

    # --- topics -------------------------------------------------------------

    def topic_names(self) -> list[str]:
        if not self.topics_dir.is_dir():
            return []
        return sorted(p.name for p in self.topics_dir.iterdir() if (p / "topic.toml").is_file())


AREA_TEMPLATE = """\
# xero area configuration.

[area]
# Directory holding Spack and its envs (extern/spack, extern/envs/<env>).  It
# may be a symlink to another area's extern/ to reuse its Spack builds.
extern = "extern"
# Use ccache (if found) as the compiler launcher for all CMake builds.
ccache = true

# The repo registry: [repos.<name>] url = "<upstream (origin) git url>"
#   remotes = { mine = "<fork url>" }   optional extra remotes
[repos]

# Per-Spack-env profiles: [envs.<env>]
#   profile = "view"             environment from the env's view (default)
#   profile = "spack-build-env"  a captured `spack build-env <spec>` (see
#                                'xero extern capture'), for stacks that need it
#   spec = "<spec>"              the spec for spack-build-env capture
#   cc = "gcc", cxx = "g++"      compilers (names resolved on the build PATH, or
#                                absolute paths); default: view's gcc/g++ if any
#   cxx_standard = "20", build_type = "Release"
#   [envs.<env>.cmake]           extra -D for every topic in this env
#   [envs.<env>.prepend]         VAR = ["{install}/lib", ...] path prepends
#   [envs.<env>.set]             VAR = "value"
# Templates: {area} {topic} {env} {source} {build} {install} {run} {view}
#            {extern} {venv} {refs}
[envs]
"""
