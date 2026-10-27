"""End-to-end tests of xero against throwaway repos."""

from __future__ import annotations

import os
import shutil

import pytest

from conftest import git
from xero import builders, check, environ, stamp, sync
from xero.area import Area
from xero.discover import Package, ordered
from xero.topic import Topic
from xero.util import Die


def test_init(xero):
    invoke, area = xero
    assert area.extern.is_symlink()
    assert area.env_names() == ["fake"]
    assert area.superbuild_file.is_file()


def test_repo_add_url(xero, upstreams):
    invoke, area = xero
    invoke("repo", "add", "alpha", upstreams["alpha"])
    bare = area.repo_path("alpha")
    assert git("config", "--get-all", "remote.origin.fetch", cwd=bare) == "+refs/heads/*:refs/remotes/origin/*"
    assert git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=bare) == ""
    assert "refs/remotes/origin/feature" in git("for-each-ref", "--format=%(refname)", cwd=bare)
    assert area.repo_url("alpha") == str(upstreams["alpha"])


def test_repo_add_from_local_clone(xero, upstreams, tmp_path):
    invoke, area = xero
    local = tmp_path / "legacy" / "alpha"
    git("clone", "-q", str(upstreams["alpha"]), str(local), cwd=tmp_path)
    git("checkout", "-q", "-b", "wip", cwd=local)
    (local / "new.txt").write_text("x")
    git("add", "new.txt", cwd=local)
    git("commit", "-q", "-m", "wip", cwd=local)
    # a personal fork remote holding the pushed "wip" branch
    fork = tmp_path / "fork" / "alpha.git"
    git("init", "-q", "--bare", str(fork), cwd=tmp_path)
    git("remote", "add", "mine", str(fork), cwd=local)
    git("push", "-q", "-u", "mine", "wip", cwd=local)
    invoke("repo", "add", "--from", local, "alpha")
    bare = area.repo_path("alpha")
    heads = git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=bare).split()
    assert "wip" in heads and "main" in heads
    assert git("remote", "get-url", "origin", cwd=bare) == str(upstreams["alpha"])
    assert git("remote", "get-url", "mine", cwd=bare) == str(fork)
    assert area.repo_remotes("alpha") == {"origin": str(upstreams["alpha"]), "mine": str(fork)}
    assert git("config", "branch.main.merge", cwd=bare) == "refs/heads/main"
    assert git("config", "branch.wip.remote", cwd=bare) == "mine"
    refs = git("for-each-ref", "--format=%(refname)", cwd=bare)
    assert "refs/remotes/origin/main" in refs and "refs/remotes/mine/wip" in refs
    invoke("branch", "add", "alpha", "wip")
    [wt] = check.worktrees(area)
    assert wt.pushed and wt.head_ok


def _topic(invoke, area, name="t1", *extra):
    invoke("topic", "new", name, "-e", "fake", *extra)
    return Topic(area, name)


def test_topic_link_sources(xero, upstreams):
    invoke, area = xero
    for n in ("alpha", "beta"):
        invoke("repo", "add", n, upstreams[n])
    t = _topic(invoke, area)
    invoke("topic", "add", "-t", "t1", "alpha", "main")
    invoke("topic", "add", "-t", "t1", "beta", "feature")
    for name, ref in (("alpha", "main"), ("beta", "feature")):
        link = t.source_dir / name
        assert link.is_symlink() and not os.path.isabs(os.readlink(link))
        assert link.resolve() == area.branch_path(name, ref).resolve()
    assert (t.source_dir / "CMakeLists.txt").resolve() == area.superbuild_file.resolve()
    env = t.env_root("fake")
    assert (env / "source").resolve() == t.source_dir.resolve()
    assert (env / "view").resolve() == area.view("fake")
    assert "xero env --shell bash -t t1 -e fake" in (t.run_dir("fake") / ".envrc").read_text()
    assert sync.sync(Topic(area, "t1"), fix=False) == []


def test_shared_branch_and_set_ref(xero, upstreams):
    invoke, area = xero
    invoke("repo", "add", "alpha", upstreams["alpha"])
    _topic(invoke, area, "t1")
    _topic(invoke, area, "t2")
    invoke("topic", "add", "-t", "t1", "alpha", "main")
    invoke("topic", "add", "-t", "t2", "alpha", "main")
    wts = check.worktrees(area)
    assert len(wts) == 1 and sorted(wts[0].users) == ["t1:source.alpha", "t2:source.alpha"]
    invoke("topic", "set-ref", "-t", "t2", "alpha", "v1")
    t2 = Topic(area, "t2")
    assert (t2.source_dir / "alpha").resolve() == area.branch_path("alpha", "v1").resolve()
    assert len(check.worktrees(area)) == 2


def test_drift_and_strays(xero, upstreams):
    invoke, area = xero
    invoke("repo", "add", "alpha", upstreams["alpha"])
    t = _topic(invoke, area)
    invoke("topic", "add", "-t", "t1", "alpha", "main")
    wt = area.branch_path("alpha", "main")
    git("checkout", "-q", "feature", cwd=wt)
    (t.source_dir / "junk").mkdir()
    msgs = [n.message for n in sync.sync(Topic(area, "t1"), fix=False)]
    assert any("want main" in m for m in msgs)
    assert any("not described" in m for m in msgs)
    res = invoke("check", ok=False)
    assert res.exit_code == 1 and "HEAD is feature" in res.output


def test_gc(xero, upstreams):
    invoke, area = xero
    invoke("repo", "add", "alpha", upstreams["alpha"])
    invoke("branch", "add", "alpha", "main")
    invoke("branch", "add", "alpha", "feature")
    wt = area.branch_path("alpha", "feature")
    (wt / "f.txt").write_text("x")
    git("add", "f.txt", cwd=wt)
    git("commit", "-q", "-m", "local", cwd=wt)  # unpushed
    res = invoke("gc")
    assert "alpha/main" in res.output and "feature" not in res.output
    invoke("gc", "--remove")
    assert not area.branch_path("alpha", "main").exists()
    assert area.branch_path("alpha", "feature").exists()


def test_clone_and_dir_modes(xero, upstreams, tmp_path):
    invoke, area = xero
    invoke("repo", "add", "alpha", upstreams["alpha"])
    t = _topic(invoke, area)
    invoke("topic", "add", "-t", "t1", "--mode", "clone", "alpha", "feature")
    path = t.source_dir / "alpha"
    assert not path.is_symlink() and (path / ".git").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=path) == "feature"
    assert git("remote", "get-url", "origin", cwd=path) == str(upstreams["alpha"])
    other = tmp_path / "notes"
    other.mkdir()
    invoke("topic", "add", "-t", "t1", "--ref-only", "--mode", "dir", "--path", other, "notes")
    assert (t.refs_dir / "notes").resolve() == other.resolve()


def test_discover_order(tmp_path):
    pkgs = [
        Package("c", tmp_path, "gamma", ["beta", "Boost"]),
        Package("b", tmp_path, "beta", ["alpha"]),
        Package("a", tmp_path, "alpha", []),
    ]
    assert [p.project for p in ordered(pkgs)] == ["alpha", "beta", "gamma"]
    with pytest.raises(Die):
        ordered([Package("a", tmp_path, "a", ["b"]), Package("b", tmp_path, "b", ["a"])])


def test_environment_layers(xero, upstreams, monkeypatch):
    invoke, area = xero
    area.doc["envs"]["fake"] = {"prepend": {"FOO_PATH": ["{install}/foo"]}, "set": {"BAR": "{env}"}}
    area.save()
    t = _topic(invoke, area)
    t.venv_dir.mkdir()
    monkeypatch.setenv("CPATH", "/nowhere")
    run = environ.compute(t, "fake", "run", base={"PATH": "/usr/bin", "CPATH": "/nowhere"})
    path = run["PATH"].split(":")
    assert path[0] == str(t.venv_dir / "bin")
    assert path.index(str(t.install_dir("fake") / "bin")) < path.index(str(area.view("fake") / "bin"))
    assert run["FOO_PATH"] == str(t.install_dir("fake") / "foo")
    assert run["BAR"] == "fake"
    assert "LD_LIBRARY_PATH" in run and run["CPATH"] == "/nowhere"
    bld = environ.compute(t, "fake", "build", base={"PATH": "/usr/bin", "CPATH": "/nowhere"})
    assert "LD_LIBRARY_PATH" not in bld and "CPATH" not in bld


def test_staleness(xero, upstreams):
    invoke, area = xero
    invoke("repo", "add", "alpha", upstreams["alpha"])
    t = _topic(invoke, area)
    invoke("topic", "add", "-t", "t1", "alpha", "main")
    b = builders.get(Topic(area, "t1"), "fake")
    assert b.status()["state"] == "unbuilt"
    stamp.save(b.ctx.build, b.fingerprint(), b.ctx.package_states(), merge=False)
    assert b.status()["state"] == "ok"
    (area.branch_path("alpha", "main") / "data.txt").write_text("changed\n")
    rep = b.status()
    assert rep["state"] == "stale" and rep["stale"] == ["alpha"]
    (area.env_dir("fake") / "spack.lock").write_text('{"new": 1}\n')
    assert b.status()["state"] == "wipe"


@pytest.mark.skipif(not shutil.which("cmake"), reason="needs cmake")
@pytest.mark.parametrize("builder", ["superbuild", "cmake-each"])
def test_build(xero, upstreams, builder):
    invoke, area = xero
    for n in ("alpha", "beta"):
        invoke("repo", "add", n, upstreams[n])
    t = _topic(invoke, area, "t1", "--builder", builder)
    invoke("topic", "add", "-t", "t1", "beta", "main")
    invoke("topic", "add", "-t", "t1", "alpha", "main")
    invoke("build", "-t", "t1")
    inst = t.install_dir("fake")
    assert (inst / "share" / "alpha" / "data.txt").is_file()
    assert (inst / "share" / "beta" / "data.txt").is_file()
    res = invoke("status", "-t", "t1")
    assert "env fake: ok" in res.output
    data = area.branch_path("beta", "main") / "data.txt"
    data.write_text("b2\n")
    st = data.stat()  # cmake install skips files whose mtime did not change
    os.utime(data, (st.st_atime + 10, st.st_mtime + 10))
    res = invoke("status", "-t", "t1")
    assert "env fake: stale" in res.output and "beta: uncommitted changes" in res.output
    invoke("build", "-t", "t1", "--stale-only")
    assert (inst / "share" / "beta" / "data.txt").read_text() == "b2\n"
    assert "env fake: ok" in invoke("status", "-t", "t1").output


def test_env_add_and_new_branch(xero, upstreams):
    invoke, area = xero
    invoke("repo", "add", "alpha", upstreams["alpha"])
    invoke("topic", "new", "t1")
    t = Topic(area, "t1")
    assert t.envs == {}
    invoke("topic", "env-add", "-t", "t1", "fake", "--builder", "cmake-each")
    t = Topic(area, "t1")
    assert t.envs == {"fake": {"builder": "cmake-each"}}
    assert (t.env_root("fake") / "view").is_symlink()
    invoke("topic", "add", "-t", "t1", "alpha", "main")
    invoke("topic", "set-ref", "-t", "t1", "--new", "alpha", "my/work")
    wt = area.branch_path("alpha", "my/work")
    assert (t.source_dir / "alpha").resolve() == wt.resolve()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "my/work"
    assert git("rev-parse", "HEAD", cwd=wt) == git("rev-parse", "main", cwd=area.repo_path("alpha"))
    with pytest.raises(Die, match="already exists"):
        invoke("topic", "set-ref", "-t", "t1", "--new", "alpha", "my/work")
    invoke("branch", "add", "--new", "v1", "alpha", "other")
    assert area.branch_path("alpha", "other").is_dir()
    invoke("topic", "env-rm", "-t", "t1", "fake")
    assert Topic(area, "t1").envs == {}


@pytest.mark.skipif(not shutil.which("cmake"), reason="needs cmake")
@pytest.mark.parametrize("builder", ["superbuild", "cmake-each"])
def test_build_after_ref_switch(xero, upstreams, builder):
    invoke, area = xero
    for n in ("alpha", "beta"):
        invoke("repo", "add", n, upstreams[n])
    t = _topic(invoke, area, "t1", "--builder", builder)
    invoke("topic", "add", "-t", "t1", "alpha", "main")
    invoke("topic", "add", "-t", "t1", "beta", "main")
    invoke("build", "-t", "t1")
    invoke("topic", "set-ref", "-t", "t1", "--new", "beta", "work")
    wt = area.branch_path("beta", "work")
    (wt / "data.txt").write_text("from work\n")
    st = (wt / "data.txt").stat()  # cmake install skips files whose mtime did not change
    os.utime(wt / "data.txt", (st.st_atime + 10, st.st_mtime + 10))
    git("commit", "-q", "-am", "work", cwd=wt)
    res = invoke("status", "-t", "t1")
    assert "env fake: reconfigure" in res.output and "source moved" in res.output
    invoke("build", "-t", "t1")
    assert (t.install_dir("fake") / "share" / "beta" / "data.txt").read_text() == "from work\n"
    assert "env fake: ok" in invoke("status", "-t", "t1").output


def test_bare_invocation_is_help(xero, upstreams, monkeypatch):
    invoke, area = xero
    for args in ([], ["init"], ["repo"], ["repo", "fetch"], ["topic", "add"], ["status"], ["build"], ["env"]):
        res = invoke(*args, ok=False)
        assert "Usage:" in res.output, args
    assert "Usage:" not in invoke("topic", "list").output
    assert "Usage:" not in invoke("gc").output
    invoke("topic", "new", "t1", "-e", "fake")
    t = Topic(area, "t1")
    monkeypatch.chdir(t.env_root("fake"))
    monkeypatch.setenv("PWD", str(t.env_root("fake")))
    res = invoke("status")
    assert "Usage:" not in res.output and "topic t1" in res.output


@pytest.mark.skipif(not shutil.which("cmake"), reason="needs cmake")
@pytest.mark.parametrize("builder", ["superbuild", "cmake-each"])
def test_view_shadowed_by_added_source(xero, upstreams, builder):
    """A package first configured against the view's alpha must switch to the
    topic's alpha once alpha is added as a source (stale CMake cache)."""
    invoke, area = xero
    view_cfg = area.view("fake") / "lib" / "cmake" / "alpha"
    view_cfg.mkdir(parents=True)
    (view_cfg / "alphaConfig.cmake").write_text("set(alpha_FOUND TRUE)\n")
    for n in ("alpha", "beta"):
        invoke("repo", "add", n, upstreams[n])
    t = _topic(invoke, area, "t1", "--builder", builder)
    invoke("topic", "add", "-t", "t1", "beta", "main")
    invoke("build", "-t", "t1")
    b = builders.get(Topic(area, "t1"), "fake")
    [pkg] = b.ctx.packages()
    assert f"alpha_DIR:PATH={view_cfg}" in b.package_cache(pkg).read_text()

    invoke("topic", "add", "-t", "t1", "alpha", "main")
    invoke("build", "-t", "t1")
    b = builders.get(Topic(area, "t1"), "fake")
    beta = [p for p in b.ctx.packages() if p.dirname == "beta"][0]
    want = t.install_dir("fake") / "lib" / "cmake" / "alpha"
    assert f"alpha_DIR:PATH={want}" in b.package_cache(beta).read_text()
    assert "env fake: ok" in invoke("status", "-t", "t1").output


def test_unwrap_spack_compilers(xero, tmp_path):
    invoke, area = xero
    wrapper = tmp_path / "spack" / "libexec" / "spack" / "gcc"
    dump = area.build_env_dump("fake")
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_text(
        f"export PATH={wrapper}:/usr/bin\n"
        f"export CC={wrapper}/gcc\nexport CXX={wrapper}/g++\n"
        "export SPACK_CC=/usr/bin/gcc\nexport SPACK_CXX=/usr/bin/g++\n"
    )
    area.doc["envs"]["fake"] = {"profile": "spack-build-env"}
    area.save()
    t = _topic(invoke, area)
    env = environ.compute(t, "fake", "build", base={"PATH": "/bin"})
    assert env["CC"] == "/usr/bin/gcc" and env["CXX"] == "/usr/bin/g++"
    assert "libexec/spack" not in env["PATH"]
    b = builders.get(Topic(area, "t1"), "fake")
    assert b.ctx.compilers() == {"CMAKE_C_COMPILER": "/usr/bin/gcc", "CMAKE_CXX_COMPILER": "/usr/bin/g++"}
    area.doc["envs"]["fake"]["compiler_wrapper"] = True
    area.save()
    env = environ.compute(Topic(area, "t1"), "fake", "build", base={"PATH": "/bin"})
    assert env["CC"] == f"{wrapper}/gcc" and str(wrapper) in env["PATH"]


def test_shadowed_entries_ignore_generic_dirs(tmp_path):
    from xero.builders.base import shadowed_entries

    view, install = tmp_path / "view", tmp_path / "install"
    for d in (view / "include", install / "include", install / "lib", install / "lib" / "cmake" / "Foo"):
        d.mkdir(parents=True, exist_ok=True)
    (install / "lib" / "libfoo.so").write_text("")
    (install / "lib" / "cmake" / "Foo" / "FooConfig.cmake").write_text("")
    cache = tmp_path / "CMakeCache.txt"
    cache.write_text(
        f"MPI_CXX_HEADER_DIR:PATH={view}/include\n"
        f"Foo_LIBRARY:FILEPATH={view}/lib/libfoo.so\n"
        f"Foo_DIR:PATH={view}/lib/cmake/Foo\n"
        f"Bar_LIBRARY:FILEPATH={view}/lib/libbar.so\n"
    )
    hits = shadowed_entries(cache, view, install)
    assert [h.split("=")[0] for h in hits] == ["Foo_LIBRARY", "Foo_DIR"]


@pytest.mark.skipif(not shutil.which("cmake"), reason="needs cmake")
@pytest.mark.parametrize("builder", ["superbuild", "cmake-each"])
def test_filtered_view_hides_overridden_package(xero, upstreams, tmp_path, builder):
    """A view package that a topic source overrides is left out of the topic's view."""
    from xero import view

    invoke, area = xero
    store = tmp_path / "store" / ("alpha-1.0-" + "a" * 32)
    (store / "lib" / "cmake" / "alpha").mkdir(parents=True)
    (store / "lib" / "cmake" / "alpha" / "alphaConfig.cmake").write_text("set(alpha_FOUND TRUE)\n")
    (store / "include").mkdir()
    (store / "include" / "alpha.h").write_text("// old\n")
    other = tmp_path / "store" / ("zeta-2.0-" + "b" * 32) / "include"
    other.mkdir(parents=True)
    (other / "zeta.h").write_text("")
    v = area.view("fake")
    (v / "lib" / "cmake").mkdir(parents=True)
    (v / "lib" / "cmake" / "alpha").symlink_to(store / "lib" / "cmake" / "alpha")
    (v / "include").mkdir()
    (v / "include" / "alpha.h").symlink_to(store / "include" / "alpha.h")
    (v / "include" / "zeta.h").symlink_to(other / "zeta.h")

    for n in ("alpha", "beta"):
        invoke("repo", "add", n, upstreams[n])
    t = _topic(invoke, area, "t1", "--builder", builder)
    invoke("topic", "add", "-t", "t1", "beta", "main")
    assert (t.env_root("fake") / "view").is_symlink()
    invoke("build", "-t", "t1")

    invoke("topic", "add", "-t", "t1", "alpha", "main")
    local = t.env_root("fake") / "view"
    assert local.is_dir() and not local.is_symlink()
    assert not (local / "include" / "alpha.h").exists()
    assert not (local / "lib" / "cmake" / "alpha").exists()
    assert (local / "include" / "zeta.h").resolve() == (other / "zeta.h").resolve()
    assert view.path(Topic(area, "t1"), "fake") == local
    assert "env fake: wipe" in invoke("status", "-t", "t1").output

    invoke("build", "-t", "t1")
    b = builders.get(Topic(area, "t1"), "fake")
    beta = [p for p in b.ctx.packages() if p.dirname == "beta"][0]
    want = t.install_dir("fake") / "lib" / "cmake" / "alpha"
    assert f"alpha_DIR:PATH={want}" in b.package_cache(beta).read_text()
    assert "env fake: ok" in invoke("status", "-t", "t1").output

    invoke("topic", "rm", "-t", "t1", "alpha")
    assert (t.env_root("fake") / "view").is_symlink()
