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
    # slack front-end
    "_handle_event": "aspen.slack_app",
    "_start_typing_status": "aspen.slack_app",
    "_STATUS_TEXT": "aspen.slack_app",
}


class _Facade:
    """Proxies legacy flat attribute access onto the refactored ``aspen.*`` modules."""

    def __getattr__(self, name):
        if name == "requests":
            return importlib.import_module("requests")
        modname = _MODMAP.get(name)
        if modname is None:
            raise AttributeError(name)
        return getattr(importlib.import_module(modname), name)

    def __setattr__(self, name, value):
        modname = _MODMAP.get(name)
        if modname is None:
            raise AttributeError(name)
        setattr(importlib.import_module(modname), name, value)


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
    os.environ.update(
        {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_APP_TOKEN": "xapp-test",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "CALCULATIONS_ROOT": str(calc_root),
            "ASPEN_ALLOWED_SLACK_USER_IDS": "U1,U2,U3,U4,U5",
            "WORKSPACE_ROOT": str(workspace_root),
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
    sut.MANAGER.clear()
    yield


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
