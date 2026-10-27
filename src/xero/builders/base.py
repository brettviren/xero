# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Shared builder machinery: context, CMake settings, fingerprints."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .. import discover, environ, stamp, view
from ..topic import Topic
from ..util import die, run, say, sha256_file, sha256_text, which

INIT_CACHE = "xero-init.cmake"

_CACHE_ENTRY = re.compile(r"^([A-Za-z0-9_]+)_(LIBRARY|DIR):(FILEPATH|PATH)=(.*)$")


def shadowed_entries(cache: Path, view: Path, install: Path) -> list[str]:
    """CMake cache entries pinned to the view that the install now shadows.

    A cached *_LIBRARY / *_DIR hit is never re-searched, so when a package was
    configured before a sibling providing the same artifact was installed (eg
    a WCT source added to a topic after larwirecell was first built), the
    package keeps using the view's copy.  *_INCLUDE_DIR entries are skipped:
    every prefix has an include/, so they would always look shadowed.
    """
    if not cache.is_file():
        return []
    view_s = str(view) + os.sep
    hits = []
    for line in cache.read_text(errors="replace").splitlines():
        m = _CACHE_ENTRY.match(line)
        if not m or m.group(1).endswith("_INCLUDE"):
            continue
        val = m.group(4)
        if not val.startswith(view_s):
            continue
        cand = install / val[len(view_s):]
        # A generic directory (include/, lib/) exists in every prefix; only a
        # file or a CMake package-config directory is a real shadow.
        if cand.is_file() or (cand.is_dir() and _is_config_dir(cand)):
            hits.append(f"{m.group(1)}_{m.group(2)}={val}")
    return hits


def _is_config_dir(path: Path) -> bool:
    return any(path.glob("*Config.cmake")) or any(path.glob("*-config.cmake"))


class Context:
    """Everything a builder needs for one (topic, env)."""

    def __init__(self, topic: Topic, env: str) -> None:
        self.topic = topic
        self.area = topic.area
        self.env = env
        self.area.require_env(env)
        self.profile = self.area.env_profile(env)
        self.config = topic.env_config(env)
        self.source = topic.source_dir
        self.build = topic.build_dir(env)
        self.install = topic.install_dir(env)
        self.view = view.path(topic, env)
        self._environ: dict[str, str] | None = None

    @property
    def environ(self) -> dict[str, str]:
        if self._environ is None:
            self._environ = environ.compute(self.topic, self.env, purpose="build")
        return self._environ

    def setting(self, key: str, default=None):
        """A per-env setting: topic [env.<e>] wins over xero.toml [envs.<e>]."""
        if key in self.config:
            return self.config[key]
        return self.profile.get(key, default)

    def tool(self, name: str) -> str:
        found = which(name, self.environ.get("PATH"))
        if not found:
            die(f"'{name}' not found on the build PATH of env '{self.env}'")
        return found

    def packages(self) -> list[discover.Package]:
        """The env's CMake packages in dependency order."""
        pkgs = []
        for ent in self.topic.cmake_sources(self.env):
            path = self.topic.entry_path(ent)
            if not path.exists():
                die(f"source '{ent.name}' missing; run 'xero sync -t {self.topic.name}'")
            pkg = discover.scan(ent.name, path)
            if pkg is None:
                die(f"source '{ent.name}' has no CMakeLists.txt with a project()")
            pkgs.append(pkg)
        return discover.ordered(pkgs)

    def excluded(self) -> list[str]:
        """Entries under source/ that are not CMake packages of this env."""
        keep = {e.name for e in self.topic.cmake_sources(self.env)}
        if not self.source.is_dir():
            return []
        return sorted(p.name for p in self.source.iterdir() if p.is_dir() and p.name not in keep)

    # --- CMake settings ---------------------------------------------------------

    def compilers(self) -> dict[str, str]:
        out = {}
        for lang, key, default in (("C", "cc", "gcc"), ("CXX", "cxx", "g++")):
            name = self.setting(key)
            if name:
                out[f"CMAKE_{lang}_COMPILER"] = name if Path(name).is_absolute() else self.tool(name)
            elif (self.view / "bin" / default).is_file():
                out[f"CMAKE_{lang}_COMPILER"] = str(self.view / "bin" / default)
            elif self.environ.get("CC" if lang == "C" else "CXX"):
                out[f"CMAKE_{lang}_COMPILER"] = self.environ["CC" if lang == "C" else "CXX"]
        return out

    def cmake_settings(self) -> dict[str, object]:
        """Every setting given to each CMake package (via the init cache)."""
        s: dict[str, object] = {
            "CMAKE_INSTALL_PREFIX": str(self.install),
            "CMAKE_PREFIX_PATH": [str(self.install), str(self.view)],
            "CMAKE_BUILD_TYPE": self.setting("build_type", "Release"),
            # CMake >=3.30 removed the bundled FindBoost (CMP0167); use Boost's
            # own config from the view.
            "CMAKE_POLICY_DEFAULT_CMP0167": "NEW",
            # Installed binaries must find siblings in the install prefix and
            # the external view libs at run time.
            "CMAKE_INSTALL_RPATH_USE_LINK_PATH": True,
            "CMAKE_INSTALL_RPATH": [str(self.install / "lib"), str(self.install / "lib64")],
            "CMAKE_EXPORT_COMPILE_COMMANDS": True,
        }
        std = self.setting("cxx_standard")
        if std:
            s["CMAKE_CXX_STANDARD"] = str(std)
        s.update(self.compilers())
        ccache = self.environ.get("XERO_CCACHE")
        if ccache:
            s["CMAKE_C_COMPILER_LAUNCHER"] = ccache
            s["CMAKE_CXX_COMPILER_LAUNCHER"] = ccache
        for table in (self.profile.get("cmake") or {}, self.config.get("cmake") or {}):
            for k, v in table.items():
                s[k] = v
        return s

    def init_cache_text(self) -> str:
        lines = ["# Generated by xero -- every setting for this topic+env's CMake packages."]
        for k, v in self.cmake_settings().items():
            if isinstance(v, bool):
                lines.append(f'set({k} {"ON" if v else "OFF"} CACHE BOOL "" FORCE)')
            else:
                if isinstance(v, (list, tuple)):
                    v = ";".join(str(x) for x in v)
                v = str(v).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'set({k} "{v}" CACHE STRING "" FORCE)')
        return "\n".join(lines) + "\n"

    def write_init_cache(self) -> Path:
        path = self.build / INIT_CACHE
        self.build.mkdir(parents=True, exist_ok=True)
        text = self.init_cache_text()
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
        return path

    def generator(self) -> list[str]:
        return ["-G", "Ninja"] if which("ninja", self.environ.get("PATH")) else []

    # --- fingerprints -------------------------------------------------------------

    def config_fingerprint(self, builder: str, extra: dict | None = None) -> dict:
        prof = dict(self.profile)
        if prof.get("profile") == "spack-build-env":
            prof["_dump"] = sha256_file(self.area.build_env_dump(self.env))
        fp = {
            "builder": builder,
            "settings": sha256_text(self.init_cache_text()),
            "packages": sorted(e.name for e in self.topic.cmake_sources(self.env)),
            "lock": sha256_file(self.area.env_dir(self.env) / "spack.lock"),
            "compilers": sha256_text(repr(sorted(self.compilers().items()))),
            "view": sha256_text(f"{self.view}:{view.read_meta(self.topic, self.env).get('overridden', [])}"),
            "profile": sha256_text(repr(sorted((k, repr(v)) for k, v in prof.items()))),
        }
        fp.update(extra or {})
        return fp

    def package_states(self, names: list[str] | None = None) -> dict:
        out = {}
        for ent in self.topic.cmake_sources(self.env):
            if names is None or ent.name in names:
                path = self.topic.entry_path(ent)
                if path.exists():
                    out[ent.name] = stamp.source_state(path)
        return out

    def run(self, cmd: list, **kw) -> None:
        run(cmd, env=self.environ, **kw)


class Builder:
    """Builder interface.  Subclasses implement configure() and build()."""

    name = "base"
    _reconfigure = False

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def fingerprint(self) -> dict:
        return self.ctx.config_fingerprint(self.name)

    def configured(self) -> bool:
        raise NotImplementedError

    def configure(self) -> None:
        raise NotImplementedError

    def build(self, packages: list[str] | None) -> None:
        raise NotImplementedError

    def package_build_dir(self, pkg) -> Path:
        """The build dir of one package (a discover.Package)."""
        return self.ctx.build / pkg.dirname

    def package_cache(self, pkg) -> Path:
        """The CMakeCache.txt of one package's build."""
        return self.package_build_dir(pkg) / "CMakeCache.txt"

    def forget(self, names: list[str], why: str) -> None:
        """Remove the build dirs of the named packages so they reconfigure."""
        for pkg in self.ctx.packages():
            if pkg.dirname in names:
                bdir = self.package_build_dir(pkg)
                if bdir.exists():
                    say(f"removing {bdir} ({why})")
                    shutil.rmtree(bdir)

    def shadowed(self) -> dict[str, list[str]]:
        """{package: shadowed cache entries} over the env's packages."""
        ctx = self.ctx
        out = {}
        for pkg in ctx.packages():
            hits = shadowed_entries(self.package_cache(pkg), ctx.view, ctx.install)
            if hits:
                out[pkg.dirname] = hits
        return out

    def status(self) -> dict:
        report = stamp.compare(stamp.load(self.ctx.build), self.fingerprint(), self.ctx.package_states())
        shadowed = self.shadowed()
        report["shadowed"] = sorted(shadowed)
        for name, hits in sorted(shadowed.items()):
            more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
            report["reasons"].append(f"{name}: CMake cache uses the view's {hits[0]}{more}, now shadowed by the install")
        if shadowed and report["state"] in ("ok", "stale"):
            report["state"] = "reconfigure"
        return report

    def go(self, packages: list[str] | None = None, fresh: bool = False) -> None:
        """Configure if needed, build, and stamp."""
        ctx = self.ctx
        report = self.status()
        if fresh or report["state"] == "wipe":
            why = "requested" if fresh else "; ".join(
                r for r in report["reasons"] if r.startswith(("Spack env", "compilers", "view"))
            )
            if ctx.build.exists():
                say(f"wiping {ctx.build} ({why})")
                shutil.rmtree(ctx.build)
            report = {"state": "unbuilt", "moved": [], "shadowed": []}
        if packages:
            known = {e.name for e in ctx.topic.cmake_sources(ctx.env)}
            unknown = [p for p in packages if p not in known]
            if unknown:
                die(f"not a CMake source of env '{ctx.env}': {', '.join(unknown)}")
        if report.get("moved"):
            self.forget(report["moved"], "its source moved")
            self._reconfigure = True
        if report.get("shadowed"):
            self.forget(report["shadowed"], "its CMake cache pins view artifacts the install now provides")
            self._reconfigure = True
        if not self.configured() or report["state"] in ("unbuilt", "reconfigure"):
            self.configure()
        self.build(packages)
        stamp.save(ctx.build, self.fingerprint(), ctx.package_states(packages), merge=bool(packages))
