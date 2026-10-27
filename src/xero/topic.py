# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""A xero *topic*: ``topics/<t>/`` described by its ``topic.toml``.

Layout::

    topics/<t>/
      topic.toml                 SOURCE OF TRUTH
      source/
        CMakeLists.txt           -> share/superbuild/CMakeLists.txt (or a real file)
        <name>                   -> branches/<repo>/<ref> | git clone | directory
      refs/<name>                same three forms, never built
      venv/                      topic Python venv
      envs/<e>/
        source -> ../../source
        view   -> extern/envs/<e>/view
        build/ install/ run/

``topic.toml``::

    description = "..."
    epic = "ddm-xyz"                 # optional beads epic

    [source.<name>]                  # <name> is the entry under source/
    repo = "<repo>"                  # default: <name>
    ref = "<branch|tag|sha>"
    mode = "link"                    # link (default) | clone | dir
    path = "..."                     # dir mode: link to this path instead of
                                     # a plain directory under source/
    kind = "cmake"                   # cmake | python | none (default: detect)
    envs = ["gcc15"]                 # default: all of the topic's envs
    submodules = false               # init submodules of a new checkout

    [refs.<name>]                    # same as source, minus kind/envs

    [env.<e>]                        # the topic's Spack envs
    builder = "superbuild"           # superbuild | cmake-each | script
    script = "build.sh"              # script builder; relative to envs/<e>/
    [env.<e>.cmake]                  # -D for this topic+env
    [env.<e>.prepend] / [env.<e>.set]

    [python]
    python = "3.12"                  # venv interpreter (uv-managed)
    requirements = ["numpy"]         # extra packages for the venv
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from .area import Area
from .util import die

TOPIC_FILE = "topic.toml"
MODES = ("link", "clone", "dir")
KINDS = ("cmake", "python", "none")
BUILDERS = ("superbuild", "cmake-each", "script")


@dataclass
class Entry:
    """One [source.<name>] or [refs.<name>] entry."""

    name: str
    section: str  # "source" or "refs"
    repo: str | None = None
    ref: str | None = None
    mode: str = "link"
    path: str | None = None
    kind: str | None = None
    envs: list[str] | None = None
    submodules: bool = False
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_table(cls, name: str, section: str, tab: dict) -> "Entry":
        tab = dict(tab)
        ent = cls(
            name=name,
            section=section,
            repo=tab.pop("repo", None),
            ref=tab.pop("ref", None),
            mode=tab.pop("mode", "link"),
            path=tab.pop("path", None),
            kind=tab.pop("kind", None),
            envs=list(tab.pop("envs")) if "envs" in tab else None,
            submodules=bool(tab.pop("submodules", False)),
            extra=tab,
        )
        if ent.mode not in MODES:
            die(f"[{section}.{name}] mode must be one of {MODES}, got {ent.mode!r}")
        if ent.mode in ("link", "clone"):
            ent.repo = ent.repo or name
            if not ent.ref:
                die(f"[{section}.{name}] needs a ref")
        if ent.kind is not None and ent.kind not in KINDS:
            die(f"[{section}.{name}] kind must be one of {KINDS}, got {ent.kind!r}")
        return ent


class Topic:
    def __init__(self, area: Area, name: str) -> None:
        self.area = area
        self.name = name
        self.root = area.topics_dir / name
        self.file = self.root / TOPIC_FILE
        if not self.file.is_file():
            known = ", ".join(area.topic_names()) or "none"
            die(f"no topic '{name}' (no {self.file}); known: {known}")
        self.doc = tomlkit.parse(self.file.read_text())

    def save(self) -> None:
        self.file.write_text(tomlkit.dumps(self.doc))

    # --- toml views ---------------------------------------------------------

    @property
    def description(self) -> str:
        return str(self.doc.get("description", ""))

    @property
    def epic(self) -> str | None:
        return self.doc.get("epic")

    def entries(self, section: str) -> dict[str, Entry]:
        tab = self.doc.get(section) or {}
        return {k: Entry.from_table(k, section, v) for k, v in tab.items()}

    @property
    def sources(self) -> dict[str, Entry]:
        return self.entries("source")

    @property
    def refs(self) -> dict[str, Entry]:
        return self.entries("refs")

    @property
    def envs(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in (self.doc.get("env") or {}).items()}

    def env_config(self, env: str) -> dict:
        envs = self.envs
        if env not in envs:
            die(f"topic '{self.name}' has no env '{env}' (has: {', '.join(envs) or 'none'})")
        return envs[env]

    def builder_name(self, env: str) -> str:
        name = self.env_config(env).get("builder", "superbuild")
        if name not in BUILDERS:
            die(f"topic '{self.name}' env '{env}': unknown builder {name!r} (want one of {BUILDERS})")
        return name

    @property
    def python(self) -> dict:
        return dict(self.doc.get("python") or {})

    # --- paths --------------------------------------------------------------

    @property
    def source_dir(self) -> Path:
        return self.root / "source"

    @property
    def refs_dir(self) -> Path:
        return self.root / "refs"

    @property
    def venv_dir(self) -> Path:
        return self.root / "venv"

    def env_root(self, env: str) -> Path:
        return self.root / "envs" / env

    def build_dir(self, env: str) -> Path:
        return self.env_root(env) / "build"

    def install_dir(self, env: str) -> Path:
        return self.env_root(env) / "install"

    def run_dir(self, env: str) -> Path:
        return self.env_root(env) / "run"

    def entry_path(self, ent: Entry) -> Path:
        base = self.source_dir if ent.section == "source" else self.refs_dir
        return base / ent.name

    def link_target(self, ent: Entry) -> Path | None:
        """Where the entry's symlink must point, or None for real directories."""
        if ent.mode == "link":
            return self.area.branch_path(ent.repo, ent.ref)
        if ent.mode == "dir" and ent.path:
            p = Path(os.path.expanduser(ent.path))
            return p if p.is_absolute() else self.root / p
        return None

    # --- source classification ------------------------------------------------

    def kind_of(self, ent: Entry) -> str:
        if ent.kind:
            return ent.kind
        path = self.entry_path(ent)
        if (path / "CMakeLists.txt").is_file():
            return "cmake"
        if (path / "pyproject.toml").is_file() or (path / "setup.py").is_file():
            return "python"
        return "none"

    def in_env(self, ent: Entry, env: str) -> bool:
        return ent.envs is None or env in ent.envs

    def cmake_sources(self, env: str) -> list[Entry]:
        return [e for e in self.sources.values() if self.in_env(e, env) and self.kind_of(e) == "cmake"]

    def python_sources(self) -> list[Entry]:
        return [e for e in self.sources.values() if self.kind_of(e) == "python"]

    # --- editing --------------------------------------------------------------

    def set_entry(self, section: str, name: str, values: dict) -> None:
        if section not in self.doc:
            self.doc[section] = tomlkit.table(is_super_table=True)
        tab = tomlkit.table()
        for k, v in values.items():
            if v is not None:
                tab[k] = v
        self.doc[section][name] = tab
        self.save()

    def remove_entry(self, section: str, name: str) -> None:
        tab = self.doc.get(section) or {}
        if name not in tab:
            die(f"topic '{self.name}' has no [{section}.{name}]")
        del self.doc[section][name]
        self.save()


TOPIC_TEMPLATE = """\
# xero topic.  Entries:
#   [source.<name>]  repo ref mode=link|clone|dir path kind=cmake|python|none
#                    envs=[...] submodules=false
#   [refs.<name>]    read-only references, same fields minus kind/envs
#   [env.<e>]        builder=superbuild|cmake-each|script script cxx_standard
#                    build_type cc cxx; [env.<e>.cmake|prepend|set]
#   [python]         python="3.12" requirements=[...]
description = {description}
"""


def create(area: Area, name: str, description: str, envs: list[str], builder: str) -> Topic:
    root = area.topics_dir / name
    if (root / TOPIC_FILE).exists():
        die(f"topic '{name}' already exists")
    if builder not in BUILDERS:
        die(f"unknown builder {builder!r} (want one of {BUILDERS})")
    for env in envs:
        area.require_env(env)
    root.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.parse(TOPIC_TEMPLATE.format(description=tomlkit.string(description).as_string()))
    if envs:
        doc["env"] = tomlkit.table(is_super_table=True)
    for env in envs:
        tab = tomlkit.table()
        tab["builder"] = builder
        doc["env"][env] = tab
    (root / TOPIC_FILE).write_text(tomlkit.dumps(doc))
    return Topic(area, name)


def logical_cwd() -> Path:
    """The cwd as the shell sees it ($PWD), keeping symlinked path components.

    os.getcwd() resolves symlinks, so from topics/<t>/source/<pkg> it would
    report branches/<repo>/<ref> and lose the topic.
    """
    pwd = os.environ.get("PWD")
    if pwd:
        try:
            if Path(pwd).resolve() == Path.cwd().resolve():
                return Path(pwd)
        except OSError:
            pass
    return Path.cwd()


def topic_from_cwd(area: Area, cwd: Path | None = None) -> tuple[str | None, str | None]:
    """(topic, env) implied by the current directory, if inside topics/<t>[/envs/<e>]."""
    if cwd is None:
        cwd = logical_cwd()
    cwd = cwd.absolute()
    try:
        rel = cwd.relative_to(area.topics_dir)
    except ValueError:
        return None, None
    parts = rel.parts
    topic = parts[0] if parts else None
    env = parts[2] if len(parts) >= 3 and parts[1] == "envs" else None
    return topic, env
