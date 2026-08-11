"""
Tests for the tool server's own root resolution.

The tool server is a separate process with no Slack or model configuration, so it
reads the registry file directly rather than importing the ``aspen`` package. The
property under test is the one that makes that safe: **a request carries a user
and an owner NAME; the mapping name -> directory is the server's own.** Nothing
the agent sends can point the sandbox at an arbitrary path.
"""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def ts(tmp_path, monkeypatch):
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: None

    default_root = tmp_path / "projects"
    arun_root = tmp_path / "arun-calcs"
    shared_root = tmp_path / "group-calcs"
    state = tmp_path / "state"
    for d in (default_root, arun_root, shared_root, state):
        d.mkdir()
    (default_root / "thermolysin").mkdir()
    (arun_root / "dft").mkdir()

    users = {"version": 1, "users": [
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "status": "active"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "role": "member", "status": "active", "calc_root": str(arun_root)},
    ]}
    (state / "users.json").write_text(json.dumps(users))

    os.environ.update({
        "AGENT_INTERNAL_SECRET": "test-secret",
        "PROJECTS_ROOT": str(default_root),
        "WORKSPACE_ROOT": str(tmp_path / "ws"),
        "ASPEN_STATE_DIR": str(state),
        "ASPEN_SHARED_CALC_ROOTS": f"smb={shared_root}",
    })
    sys.modules.pop("tool_server", None)
    mod = importlib.import_module("tool_server")
    yield mod, {"default": default_root, "arun": arun_root, "shared": shared_root}
    sys.modules.pop("tool_server", None)
    for key in ("ASPEN_STATE_DIR", "ASPEN_SHARED_CALC_ROOTS"):
        os.environ.pop(key, None)


def test_a_caller_gets_their_own_root(ts):
    mod, roots = ts
    root, scope = mod.resolve_scope("U01ARUN")
    assert root == roots["arun"]
    assert scope == "arun__U01ARUN"


def test_a_caller_without_a_root_gets_the_default(ts):
    mod, roots = ts
    root, scope = mod.resolve_scope("U0SAM")
    assert root == roots["default"]
    assert scope == "sam__U0SAM"


def test_an_owner_name_selects_someone_elses_root(ts):
    mod, roots = ts
    root, _scope = mod.resolve_scope("U0SAM", "arun")
    assert root == roots["arun"]


def test_a_shared_root_resolves_by_name(ts):
    mod, roots = ts
    root, scope = mod.resolve_scope("U0SAM", "smb")
    assert root == roots["shared"]
    assert scope == "_shared__smb"


def test_an_unknown_caller_falls_back_to_the_default(ts):
    """Single-root behavior is the floor — an unknown user is never an error."""
    mod, roots = ts
    root, scope = mod.resolve_scope("U0NOBODY")
    assert root == roots["default"] and scope == ""


def test_a_path_passed_as_an_owner_resolves_to_nothing_useful(ts):
    """The wire carries names. A directory sent as 'owner' is simply not a name."""
    mod, roots = ts
    root, _scope = mod.resolve_scope("U0SAM", "/etc")
    assert root == roots["default"]


def test_the_project_fence_follows_the_resolved_root(ts):
    mod, roots = ts
    root, _ = mod.resolve_scope("U01ARUN")
    assert mod._safe_project_path("dft", root) == roots["arun"] / "dft"
    with pytest.raises(Exception):                 # HTTPException(400)
        mod._safe_project_path("../projects/thermolysin", root)


def test_a_project_in_another_root_is_not_reachable(ts):
    mod, roots = ts
    root, _ = mod.resolve_scope("U01ARUN")
    with pytest.raises(Exception):
        mod._safe_project_path("thermolysin", root)   # that one is Sam's


def test_the_registry_is_re_read_when_it_changes(ts, tmp_path):
    """Hot-reload: setting a root must not need a tool-server restart."""
    mod, roots = ts
    assert mod.resolve_scope("U0SAM")[0] == roots["default"]
    users = json.loads(mod.USERS_FILE.read_text())
    new_root = tmp_path / "sam-calcs"
    new_root.mkdir()
    users["users"][0]["calc_root"] = str(new_root)
    mod.USERS_FILE.write_text(json.dumps(users))
    assert mod.resolve_scope("U0SAM")[0] == new_root


# --------------------------------------------------------------------------- #
# Sidecar metadata
# --------------------------------------------------------------------------- #
def test_sidecar_metadata_is_preferred_over_the_tree(ts):
    mod, roots = ts
    project = roots["default"] / "thermolysin"
    (project / "metadata.md").write_text(
        "## Python libraries available for analysis\n- numpy\n")
    sidecar = mod.METADATA_ROOT / "sam__U0SAM" / "thermolysin"
    sidecar.mkdir(parents=True)
    (sidecar / "metadata.md").write_text(
        "## Python libraries available for analysis\n- pandas\n")

    meta = mod.load_metadata(project, "sam__U0SAM")
    assert "pandas" in meta["allowed_libraries"]
    assert "numpy" not in meta["allowed_libraries"]


def test_in_tree_metadata_still_works_before_migration(ts):
    mod, roots = ts
    project = roots["default"] / "thermolysin"
    (project / "metadata.md").write_text(
        "## Python libraries available for analysis\n- numpy\n")
    meta = mod.load_metadata(project, "sam__U0SAM")
    assert "numpy" in meta["allowed_libraries"]


def test_no_metadata_anywhere_still_asks_for_one(ts):
    mod, roots = ts
    with pytest.raises(Exception):                 # HTTPException(422) + template
        mod.load_metadata(roots["default"] / "thermolysin", "sam__U0SAM")


def test_the_sidecar_lookup_cannot_traverse_out(ts):
    mod, _roots = ts
    assert mod._sidecar_metadata("../../etc", "passwd") is None
