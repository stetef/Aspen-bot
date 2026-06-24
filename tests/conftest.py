"""
Shared test fixtures for the Aspen bot characterization suite.

These tests lock in the *current* behavior of ``aspen-bot.py`` so the upcoming
refactor (package split + backend abstraction) can be proven behavior-preserving.

The module under test is loaded through an **import shim**: ``aspen-bot.py`` has a
hyphen (not importable as ``aspen_bot``) and, at import time, reads several required
environment variables and constructs an ``anthropic.Anthropic`` client and a
``slack_bolt.App``. The shim sets dummy env, neutralizes ``load_dotenv`` and the
Slack ``App`` (so importing touches no real config and makes no network call), then
loads the file under the stable name ``aspen_legacy``.

This indirection is deliberate: when the refactor lands, ONLY this file changes — it
will bind the same names from the new ``aspen.*`` modules — and the test bodies stay
identical. That is what "the refactor preserves the tests" means in practice.
"""

import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
BOT_FILE = PROJECT_DIR / "aspen-bot.py"


def _neutralize_import_side_effects():
    """Stop the module's import-time work from reading real config or hitting the network."""
    # load_dotenv() at import would read the real .env from the project dir.
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: None

    # App(token=...) is constructed at import and decorators (@app.event) run at import.
    # Replace it with a no-op that still supports the .event(...) decorator usage.
    import slack_bolt

    class _DummyApp:
        def __init__(self, *args, **kwargs):
            pass

        def event(self, *args, **kwargs):
            def _decorator(fn):
                return fn

            return _decorator

    slack_bolt.App = _DummyApp


def _load_sut(calc_root: Path, workspace_root: Path):
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

    spec = importlib.util.spec_from_file_location("aspen_legacy", BOT_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["aspen_legacy"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def sut(tmp_path_factory):
    """The loaded ``aspen-bot.py`` module (system under test)."""
    calc_root = tmp_path_factory.mktemp("calculations")
    workspace_root = tmp_path_factory.mktemp("workspace")
    return _load_sut(calc_root, workspace_root)


@pytest.fixture(autouse=True)
def _reset_state(sut):
    """Reset all in-memory module state before each test for isolation."""
    sut._histories.clear()
    sut._rate_data.clear()
    sut._user_active.clear()
    # Re-create the semaphore so concurrency tests start from a full count and never
    # leak an acquired slot into the next test.
    sut._global_sem = threading.Semaphore(sut.MAX_CONCURRENT)
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
