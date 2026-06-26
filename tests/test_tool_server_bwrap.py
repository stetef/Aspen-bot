"""Characterization tests for the bwrap sandbox command the tool server builds.

These import ``tool_server`` directly (it is not part of the ``aspen`` package, so
the ``sut`` facade does not cover it). We set the required env and stub dotenv
before import so the real .env can't leak in, and stub the interpreter-prefix
discovery so the assertions don't depend on the host's Python layout.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

_PY_BINDS = ("/opt/analysis-venv", "/opt/pybase")


@pytest.fixture(scope="module")
def ts(tmp_path_factory):
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: None
    ws = tmp_path_factory.mktemp("ws")
    projects = tmp_path_factory.mktemp("projects")
    os.environ.update({
        "AGENT_INTERNAL_SECRET": "test-secret",
        "PROJECTS_ROOT": str(projects),
        "WORKSPACE_ROOT": str(ws),
        "ANALYSIS_PYTHON": "/opt/analysis-venv/bin/python",
        "ANALYSIS_AS_LIMIT_BYTES": "2147483648",
        "ANALYSIS_CPU_LIMIT_SECONDS": "90",
        "ANALYSIS_FSIZE_LIMIT_BYTES": "536870912",
    })
    sys.modules.pop("tool_server", None)
    mod = importlib.import_module("tool_server")
    # Make interpreter-prefix discovery deterministic (don't shell out).
    mod._python_bind_paths = lambda: _PY_BINDS
    return mod


def _adjacent(cmd, *seq):
    """True if the values in ``seq`` appear consecutively in ``cmd``."""
    seq = list(seq)
    return any(cmd[i:i + len(seq)] == seq for i in range(len(cmd)))


def _build(ts, project="thermolysin"):
    return ts.build_sandbox_cmd(
        Path("/ws/generated/abc.py"), project, Path(f"/projects/{project}")
    )


def test_prlimit_wraps_with_all_caps(ts):
    cmd = _build(ts)
    assert cmd[0] == ts.PRLIMIT_BIN
    assert "--as=2147483648" in cmd
    assert "--cpu=90" in cmd
    assert "--fsize=536870912" in cmd
    # bwrap starts immediately after the '--' separator
    assert cmd[cmd.index("--") + 1] == ts.BWRAP_BIN


def test_no_prlimit_when_all_caps_disabled(ts, monkeypatch):
    monkeypatch.setattr(ts, "ANALYSIS_AS_LIMIT_BYTES", 0)
    monkeypatch.setattr(ts, "ANALYSIS_CPU_LIMIT_SECONDS", 0)
    monkeypatch.setattr(ts, "ANALYSIS_FSIZE_LIMIT_BYTES", 0)
    cmd = _build(ts)
    assert cmd[0] == ts.BWRAP_BIN
    assert "--" not in cmd
    assert ts.PRLIMIT_BIN not in cmd


def test_network_and_namespace_isolation(ts):
    cmd = _build(ts)
    assert "--unshare-all" in cmd        # unshares the network namespace -> no network
    assert "--share-net" not in cmd
    assert "--die-with-parent" in cmd
    assert "--new-session" in cmd


def test_project_mounted_read_only(ts):
    cmd = _build(ts, "myproj")
    assert _adjacent(cmd, "--ro-bind", "/projects/myproj", "/projects/myproj")
    # the project must never get a writable bind
    assert not _adjacent(cmd, "--bind", "/projects/myproj", "/projects/myproj")


def test_only_workspace_outputs_are_writable(ts):
    cmd = _build(ts)
    assert _adjacent(cmd, "--bind", str(ts.FIGURES_DIR), "/aspen_workspace/figures")
    assert _adjacent(cmd, "--bind", str(ts.CACHE_DIR), "/aspen_workspace/cache")


def test_script_bound_read_only_and_is_entrypoint(ts):
    cmd = _build(ts)
    assert _adjacent(cmd, "--ro-bind", "/ws/generated/abc.py", "/aspen_script.py")
    assert cmd[-2:] == [ts.ANALYSIS_PYTHON, "/aspen_script.py"]


def test_interpreter_prefixes_bound_read_only(ts):
    cmd = _build(ts)
    for p in _PY_BINDS:
        assert _adjacent(cmd, "--ro-bind", p, p)


def test_system_paths_bound_read_only_not_writable(ts):
    cmd = _build(ts)
    assert _adjacent(cmd, "--ro-bind-try", "/usr", "/usr")
    # nothing under the system roots is given a writable bind
    assert not _adjacent(cmd, "--bind", "/usr", "/usr")


def test_sandbox_env_is_scrubbed(ts):
    assert "AGENT_INTERNAL_SECRET" not in ts.SANDBOX_ENV
    assert "ANTHROPIC_API_KEY" not in ts.SANDBOX_ENV
    assert "SLACK_BOT_TOKEN" not in ts.SANDBOX_ENV
    assert ts.SANDBOX_ENV["MPLBACKEND"] == "Agg"
    assert ts.SANDBOX_ENV["PATH"] == "/usr/bin:/bin"


def test_mplconfigdir_is_in_hook_writable_area(ts):
    # matplotlib's cache must land where the import hook permits writes
    # (figures/ or cache/), else its font cache write is denied at import.
    assert ts.SANDBOX_ENV["MPLCONFIGDIR"].startswith(
        ("/aspen_workspace/figures/", "/aspen_workspace/cache/")
    )


def test_fontconfig_dir_bound_read_only(ts):
    cmd = _build(ts)
    assert _adjacent(cmd, "--ro-bind-try", "/etc/fonts", "/etc/fonts")


def test_seccomp_flag_present_when_fd_given(ts):
    cmd = ts.build_sandbox_cmd(
        Path("/ws/generated/abc.py"), "p", Path("/projects/p"), seccomp_fd=7
    )
    assert _adjacent(cmd, "--seccomp", "7")


def test_no_seccomp_flag_without_fd(ts):
    # The default build (no fd) must not emit --seccomp.
    assert "--seccomp" not in _build(ts)


def test_seccomp_denylist_contract(ts):
    deny = ts._SECCOMP_DENY
    # The escalation primitives we must always block...
    for s in ("unshare", "setns", "keyctl", "add_key", "ptrace", "bpf",
              "init_module", "io_uring_setup", "userfaultfd", "perf_event_open", "clone3"):
        assert s in deny, f"{s} must be in the seccomp denylist"
    # ...and the syscalls numeric Python needs, which must never be blocked.
    for s in ("clone", "futex", "mmap", "mprotect", "read", "write", "openat"):
        assert s not in deny, f"{s} must NOT be blocked"
    # clone3/io_uring fall back via ENOSYS; the rest deny with EPERM.
    assert deny["clone3"] == "ENOSYS"
    assert deny["unshare"] == "EPERM"


def test_seccomp_bpf_is_bytes_or_none(ts):
    # Built once at import: bytes when pyseccomp is available, else None (no filter).
    assert ts._SECCOMP_BPF is None or (
        isinstance(ts._SECCOMP_BPF, bytes) and len(ts._SECCOMP_BPF) > 0
    )
