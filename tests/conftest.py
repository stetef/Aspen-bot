"""
Shared test fixtures for the Aspen characterization suite.

THE TEST SEAM. The Phase 1 suite was written against the flat ``aspen-bot.py``
module. After the Phase 2 refactor the code lives in the ``aspen.*`` package, so
this file is the *only* thing that changed: it exposes a ``sut`` facade that maps
the old flat names onto the new modules, proxying both reads and writes (so
``monkeypatch.setattr(sut, ...)`` reaches the module the code actually reads from).
The test bodies are unchanged — that is what "the refactor preserves the tests" means.
"""

import importlib
import os
import threading

import pytest

# Legacy flat name -> module that now owns it (attribute name is identical).
_MODMAP = {
    # config
    "MODEL": "aspen.config",
    "MAX_FILE_BYTES": "aspen.config",
    "AGENT_INTERNAL_SECRET": "aspen.config",
    "TOOL_SERVER_SOCKET": "aspen.config",
    "CALCULATIONS_ROOT": "aspen.config",
    "FIGURE_ARCHIVE_DIR": "aspen.config",
    "WORKSPACE_ROOT": "aspen.config",
    "MAX_ATTACHMENT_BYTES": "aspen.config",
    "MAX_CONCURRENT": "aspen.config",
    "CONTEXT_EXPIRY": "aspen.config",
    "RATE_LIMIT_REQUESTS": "aspen.config",
    "RATE_LIMIT_WINDOW": "aspen.config",
    "ALLOWED_USER_IDS": "aspen.config",
    "ADMIN_USER_ID": "aspen.config",
    "USERS_FILE": "aspen.config",
    "WORKFLOWS_ROOT": "aspen.config",
    "STATE_DIR": "aspen.config",
    "MAX_WORKFLOW_BYTES": "aspen.config",
    "METADATA_ROOT": "aspen.config",
    "METADATA_HISTORY_ROOT": "aspen.config",
    "SHARED_CALC_ROOTS": "aspen.config",
    "SEARCH_MAX_FILES_ALL": "aspen.config",
    "BOOTSTRAP_USER_IDS": "aspen.config",
    "ADMIN_OVERRIDE": "aspen.config",
    # Slurm jobs (spec §19)
    "JOBS_SUBMIT_ENABLED": "aspen.config",
    "JOBS_LEDGER": "aspen.config",
    "JOBS_STAGING_ROOT": "aspen.config",
    "JOBS_PIPELINE_BIN": "aspen.config",
    "JOBS_PIPELINE_PATH_DIR": "aspen.config",
    "JOBS_SCHEDULER": "aspen.config",
    "JOBS_MAX_STRUCTURES": "aspen.config",
    "JOBS_MAX_ACTIVE_PER_USER": "aspen.config",
    "JOBS_MAX_ACTIVE_TOTAL": "aspen.config",
    "JOBS_MAX_SUBMITS_PER_DAY": "aspen.config",
    "JOBS_CONFIRM_TTL": "aspen.config",
    "JOBS_SUBMIT_TIMEOUT": "aspen.config",
    "JOBS_SLURM_TIMEOUT": "aspen.config",
    "JOBS_SBATCH_EXPORT": "aspen.config",
    "JOBS_ENV_PASSTHROUGH": "aspen.config",
    # prompts
    "SYSTEM_PROMPT": "aspen.prompts",
    # tools
    "_safe_path": "aspen.tools",
    "_list_directory": "aspen.tools",
    "_read_file": "aspen.tools",
    "_search_files": "aspen.tools",
    "_write_metadata": "aspen.tools",
    "_call_tool_server": "aspen.tools",
    "_tool_server_post": "aspen.tools",
    "_attach_file": "aspen.tools",
    "TOOL_FNS": "aspen.tools",
    "TOOL_SPECS": "aspen.tools",
    "dispatch": "aspen.tools",
    # sessions
    "_thread_key": "aspen.sessions",
    "MANAGER": "aspen.sessions",
    # rate limiting
    "_check_rate_limit": "aspen.ratelimit",
    "_release_user": "aspen.ratelimit",
    # global state
    "_rate_data": "aspen.state",
    "_user_active": "aspen.state",
    "_global_sem": "aspen.state",
    # attachments
    "_upload_attachments": "aspen.attachments",
    "_under": "aspen.attachments",
    # calculations roots, metadata sidecar, setup + request queue
    "REQUESTS_FILE": "aspen.config",
    "REQUEST_NOTIFY_COOLDOWN_HOURS": "aspen.config",
    "_read_metadata": "aspen.tools",
    "_scoped": "aspen.tools",
    # whole modules, reached as sut.<name> so tests can call them directly
    "registry": "aspen",
    "workflows": "aspen",
    "roots": "aspen",
    "metadata": "aspen",
    "pending": "aspen",
    "setup": "aspen",
    "demo": "aspen",
    "jobs": "aspen",
    "staging": "aspen",
    "main": "aspen",
    "DEMO_ENABLED": "aspen.config",
    "DEMO_ROOT": "aspen.config",
    "DEMO_MAX_TURNS": "aspen.config",
    "DEMO_MAX_STARTS_PER_DAY": "aspen.config",
    "DEMO_SESSION_TTL": "aspen.config",
    "tools": "aspen",
    # startup guards
    "_check_state_locations": "aspen.main",
    "_check_calculations_roots": "aspen.main",
    "SANDBOX_WRITE_PATHS": "aspen.config",
    "SEARCH_MAX_MATCHES": "aspen.config",
    "SEARCH_MAX_FILES": "aspen.config",
    # turn telemetry
    "telemetry": "aspen",
    "TELEMETRY_ENABLED": "aspen.config",
    "TELEMETRY_DIR": "aspen.config",
    "TELEMETRY_STATE_FILE": "aspen.config",
    "TELEMETRY_MAX_TEXT": "aspen.config",
    # slack front-end
    "_handle_event": "aspen.slack_app",
    "handle_message": "aspen.slack_app",
    "handle_mention": "aspen.slack_app",
    "_start_typing_status": "aspen.slack_app",
    "_STATUS_TEXT": "aspen.slack_app",
    "_is_group_dm": "aspen.slack_app",
    "_unauthorized_group_members": "aspen.slack_app",
    "_bot_user_id": "aspen.slack_app",
    "_bot_uid_cache": "aspen.slack_app",
    "_admin_mention": "aspen.slack_app",
    "_request_access": "aspen.slack_app",
}


class _Facade:
    """Proxies legacy flat attribute access onto the refactored ``aspen.*`` modules."""

    def __getattr__(self, name):
        if name == "requests":
            return importlib.import_module("requests")
        modname = _MODMAP.get(name)
        if modname is None:
            raise AttributeError(name)
        module = importlib.import_module(modname)
        try:
            return getattr(module, name)
        except AttributeError:
            # A whole-module entry (``"jobs": "aspen"``) resolves only once the
            # submodule has been imported by something — which, run in isolation, a
            # test file may be the first to need. Import it explicitly rather than
            # depending on another test having pulled it in first.
            return importlib.import_module(f"{modname}.{name}")

    def __setattr__(self, name, value):
        modname = _MODMAP.get(name)
        if modname is None:
            raise AttributeError(name)
        setattr(importlib.import_module(modname), name, value)


# Names that ``aspen.config`` serves through its PEP 562 module ``__getattr__``
# rather than holding in its ``__dict__`` (they resolve through the registry on
# every read, which is what makes ``aspen-users remove`` take effect on the next
# message). See ``config._REGISTRY_BACKED``.
_HOOK_BACKED = ("ALLOWED_USER_IDS", "ADMIN_USER_ID")


def _unshadow_hook_backed() -> None:
    """Undo the damage ``monkeypatch`` does to a ``__getattr__``-backed name.

    ``monkeypatch.setattr`` reads the current value so it can restore it later —
    but a module ``__getattr__`` only fires for names *absent* from ``__dict__``,
    so the restoring ``setattr`` in ``undo()`` writes a **real attribute** and the
    hook is shadowed from then on. The allowlist is then silently frozen at
    whatever it happened to be when that test ran, for every test that follows.

    This stayed invisible for a long time because every early use snapshotted the
    same bootstrap value. It surfaced the moment a test patched the allowlist
    while its own registry fixture was installed: ten unrelated tests began seeing
    that fixture's two-user allowlist and failing as "not authorized".

    Deleting the key restores the hook. Called autouse, before and after each
    test — the "after" runs once ``monkeypatch`` has done its own teardown, since
    autouse fixtures are set up first and therefore finalize last.
    """
    config = importlib.import_module("aspen.config")
    for name in _HOOK_BACKED:
        config.__dict__.pop(name, None)


def _neutralize_import_side_effects():
    """Stop import-time work from reading real config or hitting the network."""
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: None

    import slack_bolt

    class _DummyApp:
        def __init__(self, *args, **kwargs):
            pass

        def event(self, *args, **kwargs):
            def _decorator(fn):
                return fn

            return _decorator

    slack_bolt.App = _DummyApp


@pytest.fixture(scope="session")
def sut(tmp_path_factory):
    """Facade over the refactored ``aspen`` package (system under test)."""
    calc_root = tmp_path_factory.mktemp("calculations")
    workspace_root = tmp_path_factory.mktemp("workspace")
    state_dir = tmp_path_factory.mktemp("state")
    os.environ.update(
        {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_APP_TOKEN": "xapp-test",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "CALCULATIONS_ROOT": str(calc_root),
            # No registry file is created here, so the suite runs in bootstrap
            # mode: the allowlist comes from this env var exactly as it used to,
            # which is what keeps the pre-registry admission tests unchanged.
            "ASPEN_ALLOWED_SLACK_USER_IDS": "U1,U2,U3,U4,U5",
            "WORKSPACE_ROOT": str(workspace_root),
            # Keep the registry and workflow tree inside tmp — never ~/.aspen.
            "ASPEN_STATE_DIR": str(state_dir),
            "AGENT_INTERNAL_SECRET": "test-secret",
        }
    )
    _neutralize_import_side_effects()
    # Importing the Slack front-end pulls in the whole package (config, tools, ...).
    importlib.import_module("aspen.slack_app")
    return _Facade()


@pytest.fixture(autouse=True)
def _reset_state(sut):
    """Reset all in-memory module state before each test for isolation."""
    sut._rate_data.clear()
    sut._user_active.clear()
    sut._global_sem = threading.Semaphore(sut.MAX_CONCURRENT)
    sut._bot_uid_cache = None  # re-resolve the bot's own ID per test client
    sut.MANAGER.clear()
    sut.registry.invalidate()  # the registry is mtime-cached; don't leak across tests
    sut.demo.clear()           # demo sessions are process-global too
    _unshadow_hook_backed()
    yield
    # After monkeypatch.undo(), which is what leaves the shadow behind.
    _unshadow_hook_backed()
    sut.registry.invalidate()


@pytest.fixture(autouse=True)
def _isolate_telemetry(sut, tmp_path, monkeypatch):
    """Point the turn log and its switch at a per-test directory.

    Autouse because ``_handle_event`` records every turn now, so *any* test that
    drives one would otherwise append into a location shared by the whole session.
    """
    monkeypatch.setattr(sut, "TELEMETRY_DIR", tmp_path / "telemetry")
    monkeypatch.setattr(sut, "TELEMETRY_STATE_FILE", tmp_path / "telemetry.json")
    sut.telemetry.invalidate()
    yield
    sut.telemetry.invalidate()


class AspenEnv:
    """An isolated Aspen: its own state dir, workspace, and calculations trees.

    Six test modules had grown their own near-identical version of this, each
    patching a slightly different subset of the config paths and laying the
    directories out slightly differently. That inconsistency was not free: one of
    them put ``STATE_DIR`` *at* ``tmp_path``, so every calculations root created
    under it was (correctly) refused as overlapping the state directory — a real
    check firing on a fixture artefact rather than on the behavior under test.

    The layout here is the one that matches production, and the reason it matters
    is the same reason ``roots.validate`` enforces it::

        <tmp>/state/       registry, workflows, metadata, requests   (STATE_DIR)
        <tmp>/workspace/   figures, cache, generated code           (WORKSPACE_ROOT)
        <tmp>/calcs/       one subdirectory per user's root
    """

    def __init__(self, sut, tmp_path, monkeypatch):
        self._sut = sut
        self._monkeypatch = monkeypatch
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        self.workspace = tmp_path / "workspace"
        self.calcs = tmp_path / "calcs"
        for path in (self.state, self.workspace, self.calcs):
            path.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(sut, "STATE_DIR", self.state)
        monkeypatch.setattr(sut, "USERS_FILE", self.state / "users.json")
        monkeypatch.setattr(sut, "WORKFLOWS_ROOT", self.state / "workflows")
        monkeypatch.setattr(sut, "METADATA_ROOT", self.state / "metadata")
        monkeypatch.setattr(sut, "METADATA_HISTORY_ROOT", self.state / "metadata_history")
        monkeypatch.setattr(sut, "REQUESTS_FILE", self.state / "requests.json")
        monkeypatch.setattr(sut, "WORKSPACE_ROOT", self.workspace)
        monkeypatch.setattr(sut, "SHARED_CALC_ROOTS", {})
        # Slurm jobs: the ledger and staging live in STATE_DIR for the reason the
        # registry does — a sandbox-writable ledger is a forgeable cancel, and
        # sandbox-writable staging is a job body the model can plant.
        monkeypatch.setattr(sut, "JOBS_LEDGER", self.state / "jobs.sqlite")
        monkeypatch.setattr(sut, "JOBS_STAGING_ROOT", self.state / "jobs-staging")
        monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", True)
        (self.state / "workflows").mkdir(exist_ok=True)
        sut.registry.invalidate()

    # -- calculations -------------------------------------------------------- #
    def root(self, name: str, projects=()):
        """Create a calculations tree, optionally with empty project dirs."""
        path = self.calcs / name
        path.mkdir(parents=True, exist_ok=True)
        for project in projects:
            (path / project).mkdir(parents=True, exist_ok=True)
        return path

    def shared(self, name: str, projects=()):
        """Create a shared (owned-by-nobody) root and register it under ``name``."""
        path = self.root(f"shared-{name}", projects)
        current = dict(self._sut.SHARED_CALC_ROOTS)
        current[name] = str(path)
        self._monkeypatch.setattr(self._sut, "SHARED_CALC_ROOTS", current)
        return path

    # -- people -------------------------------------------------------------- #
    def register(self, *users):
        """Save a registry. Each entry may be a dict or ``(alias, uid, **kw)``."""
        entries = []
        for user in users:
            entry = dict(user)
            entry.setdefault("display_name", entry["alias"].title())
            entry.setdefault("role", "member")
            entry.setdefault("status", "active")
            entries.append(entry)
        self._sut.registry.invalidate()
        self._sut.registry.save(entries)
        return entries

    def default_group(self):
        """sam (admin), arun, priya — the cast most of the suite already uses."""
        return self.register(
            {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam", "role": "admin"},
            {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N."},
            {"slack_user_id": "U0PRIYA", "alias": "priya", "display_name": "Priya P."},
        )


@pytest.fixture
def env(sut, tmp_path, monkeypatch):
    """An isolated Aspen (see :class:`AspenEnv`). Register users yourself."""
    environment = AspenEnv(sut, tmp_path, monkeypatch)
    yield environment
    sut.registry.invalidate()


class SayRecorder:
    """Stand-in for Slack Bolt's ``say`` — records the text of every post."""

    def __init__(self):
        self.texts = []
        self.calls = []

    def __call__(self, text=None, thread_ts=None, **kwargs):
        self.texts.append(text)
        self.calls.append({"text": text, "thread_ts": thread_ts, **kwargs})


@pytest.fixture
def say():
    return SayRecorder()
