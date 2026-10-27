"""Fixtures: throwaway upstream git repos and a xero area with a fake extern."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from xero.area import Area
from xero.cli import cli

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def git(*args, cwd):
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


ALPHA = """\
cmake_minimum_required(VERSION 3.21)
project(alpha LANGUAGES NONE)
file(WRITE ${CMAKE_BINARY_DIR}/alphaConfig.cmake "set(alpha_FOUND TRUE)\\n")
install(FILES ${CMAKE_BINARY_DIR}/alphaConfig.cmake DESTINATION lib/cmake/alpha)
install(FILES data.txt DESTINATION share/alpha)
"""

BETA = """\
cmake_minimum_required(VERSION 3.21)
project(beta LANGUAGES NONE)
find_package(alpha REQUIRED)
install(FILES data.txt DESTINATION share/beta)
"""

PYPKG = """\
[project]
name = "pyx"
version = "0.1"
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
"""


def make_upstream(root: Path, name: str, files: dict[str, str]) -> Path:
    """A non-bare upstream repo with main, feature and tag v1."""
    repo = root / "upstream" / name
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("tag", "v1", cwd=repo)
    git("branch", "feature", cwd=repo)
    return repo


@pytest.fixture
def upstreams(tmp_path):
    return {
        "alpha": make_upstream(tmp_path, "alpha", {"CMakeLists.txt": ALPHA, "data.txt": "a\n"}),
        "beta": make_upstream(tmp_path, "beta", {"CMakeLists.txt": BETA, "data.txt": "b\n"}),
        "pyx": make_upstream(tmp_path, "pyx", {"pyproject.toml": PYPKG, "pyx/__init__.py": ""}),
    }


@pytest.fixture
def xero(tmp_path, monkeypatch):
    """Invoke the CLI inside the area; returns (runner function, Area)."""
    extern = tmp_path / "otherarea" / "extern"
    (extern / "envs" / "fake" / "view" / "bin").mkdir(parents=True)
    (extern / "envs" / "fake" / "spack.yaml").write_text("spack: {}\n")
    (extern / "envs" / "fake" / "spack.lock").write_text("{}\n")
    root = tmp_path / "area"
    runner = CliRunner()
    res = runner.invoke(cli, ["init", "--extern", str(extern), str(root)], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    monkeypatch.chdir(root)
    monkeypatch.setenv("PWD", str(root))
    monkeypatch.delenv("XERO_TOPIC", raising=False)
    monkeypatch.delenv("XERO_ENV", raising=False)
    monkeypatch.delenv("XERO_ROOT", raising=False)
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)

    def invoke(*args, ok=True):
        res = runner.invoke(cli, [str(a) for a in args], catch_exceptions=False)
        if ok:
            assert res.exit_code == 0, res.output
        return res

    return invoke, FreshArea(root)


class FreshArea:
    """An Area that re-reads xero.toml on every access (the CLI rewrites it)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._area = None

    def __getattr__(self, name):
        if name in ("doc", "save"):  # keep one doc between edit and save
            if self._area is None:
                self._area = Area(self._root)
            return getattr(self._area, name)
        self._area = None
        return getattr(Area(self._root), name)
