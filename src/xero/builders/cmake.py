# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""The CMake builders: ``superbuild`` and ``cmake-each``.

Both hand every package the same initial-cache script (``build/xero-init.cmake``)
so they build identically; they differ only in orchestration.
"""

from __future__ import annotations

from .base import Builder
from ..util import die, sha256_text


class SuperBuild(Builder):
    """One CMake super-build over source/ (source/CMakeLists.txt)."""

    name = "superbuild"

    def configured(self) -> bool:
        return (self.ctx.build / "CMakeCache.txt").is_file()

    def configure(self) -> None:
        ctx = self.ctx
        if not (ctx.source / "CMakeLists.txt").exists():
            die(f"no {ctx.source}/CMakeLists.txt; run 'xero sync -t {ctx.topic.name}'")
        init = ctx.write_init_cache()
        ctx.run([
            ctx.tool("cmake"),
            "-S", ctx.topic.env_root(ctx.env) / "source",
            "-B", ctx.build,
            *ctx.generator(),
            f"-DXERO_INIT_CACHE={init}",
            f"-DXERO_INIT_HASH={sha256_text(init.read_text())[:16]}",
            f"-DXERO_EXCLUDE={';'.join(ctx.excluded())}",
        ])

    def build(self, packages: list[str] | None) -> None:
        ctx = self.ctx
        cmd = [ctx.tool("cmake"), "--build", ctx.build]
        if packages:
            by_dir = {p.dirname: p.project for p in ctx.packages()}
            for name in packages:
                cmd += ["--target", by_dir[name]]
        ctx.run(cmd)


class CMakeEach(Builder):
    """Configure, build and install each package in dependency order."""

    name = "cmake-each"
    _reconfigure = False

    def configured(self) -> bool:
        return all(
            (self.ctx.build / p.dirname / "CMakeCache.txt").is_file() for p in self.ctx.packages()
        )

    def configure(self) -> None:
        ctx = self.ctx
        ctx.write_init_cache()
        # Each package is (re)configured just before it is built: a package's
        # find_package() of a sibling needs that sibling installed first.
        self._reconfigure = True

    def _configure_one(self, pkg) -> None:
        ctx = self.ctx
        init = ctx.build / "xero-init.cmake"
        bdir = ctx.build / pkg.dirname
        if bdir.joinpath("CMakeCache.txt").is_file() and not self._reconfigure:
            return
        ctx.run([
            ctx.tool("cmake"), "-C", init, "-S", pkg.path, "-B", bdir,
            *([] if bdir.joinpath("CMakeCache.txt").is_file() else ctx.generator()),
        ])

    def build(self, packages: list[str] | None) -> None:
        ctx = self.ctx
        if not (ctx.build / "xero-init.cmake").is_file():
            ctx.write_init_cache()
        pkgs = ctx.packages()
        if packages:
            pkgs = [p for p in pkgs if p.dirname in packages]
        for pkg in pkgs:
            self._configure_one(pkg)
            bdir = ctx.build / pkg.dirname
            ctx.run([ctx.tool("cmake"), "--build", bdir])
            ctx.run([ctx.tool("cmake"), "--install", bdir])
