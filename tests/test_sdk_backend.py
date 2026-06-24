"""
Hermetic contract tests for the Claude Agent SDK backend.

Only the CLI-spawning ``ClaudeSDKClient`` is faked; the real ``@tool``,
``create_sdk_mcp_server``, ``ClaudeAgentOptions`` and message types are exercised.
The suite skips if ``claude-agent-sdk`` is not installed.

Coroutines are driven with ``asyncio.run`` (no pytest-asyncio dependency). All
aspen imports happen inside tests so the ``sut`` fixture sets env first.
"""

import asyncio

import pytest

sdk = pytest.importorskip("claude_agent_sdk")


def _result(subtype):
    return sdk.ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1,
        is_error=(subtype != "success"), num_turns=1, session_id="s",
    )


def _ok_messages(prompt):
    return [
        sdk.AssistantMessage(content=[sdk.TextBlock(text=f"reply: {prompt}")], model="m"),
        _result("success"),
    ]


def _error_messages(prompt):
    return [
        sdk.AssistantMessage(content=[sdk.TextBlock(text="partial")], model="m"),
        _result("error_during_execution"),
    ]


class FakeClient:
    """Stand-in for ClaudeSDKClient — no subprocess, scripted responses."""

    instances: list = []
    responder = staticmethod(_ok_messages)

    def __init__(self, options=None, **kwargs):
        self.options = options
        self.connect_count = 0
        self.disconnect_count = 0
        self.queries: list[str] = []
        FakeClient.instances.append(self)

    async def connect(self, *a, **k):
        self.connect_count += 1

    async def disconnect(self):
        self.disconnect_count += 1

    async def query(self, prompt, **k):
        self.queries.append(prompt)

    async def receive_response(self):
        for m in FakeClient.responder(self.queries[-1]):
            yield m


@pytest.fixture(autouse=True)
def _fake_client(monkeypatch):
    FakeClient.instances.clear()
    FakeClient.responder = staticmethod(_ok_messages)
    monkeypatch.setattr(sdk, "ClaudeSDKClient", FakeClient)
    yield


def test_warm_session_reuses_client_across_turns(sut):
    from aspen.backends.sdk import SdkSession

    s = SdkSession("C:1")

    async def two_turns():
        r1, _ = await s.send("hi", {"user_id": "U1", "figures": []})
        r2, _ = await s.send("again", {"user_id": "U1", "figures": []})
        return r1, r2

    r1, r2 = asyncio.run(two_turns())

    assert r1 == "reply: hi"
    assert r2 == "reply: again"
    # One client, connected once, queried twice — the session parked and stayed warm.
    assert len(FakeClient.instances) == 1
    assert FakeClient.instances[0].connect_count == 1
    assert FakeClient.instances[0].queries == ["hi", "again"]


def test_error_subtype_returns_error_reply(sut):
    from aspen.backends.sdk import SdkSession

    FakeClient.responder = staticmethod(_error_messages)
    s = SdkSession("C:1")
    reply, figs = asyncio.run(s.send("do it", {"figures": []}))

    assert reply == "Sorry, the SDK backend hit an error. Please try again."
    assert figs == []


def test_tool_handler_drains_figures_into_sink(sut, monkeypatch):
    from aspen.backends.sdk import SdkSession
    import aspen.tools as t

    monkeypatch.setitem(t.TOOL_FNS, "fake_fig", lambda inp, ctx: ("plotted", ["/w/x.png"]))
    s = SdkSession("C:1")
    ctx = {"figures": []}
    s._current = ctx

    out = asyncio.run(s._tool_handler("fake_fig", {}))

    assert out == {"content": [{"type": "text", "text": "plotted"}]}
    assert ctx["figures"] == ["/w/x.png"]


def test_can_use_tool_allows_only_aspen_tools(sut):
    from aspen.backends.sdk import SdkSession

    s = SdkSession("C:1")
    allow = asyncio.run(s._can_use_tool("mcp__aspen__read_file", {}, None))
    deny = asyncio.run(s._can_use_tool("Bash", {"command": "rm -rf /"}, None))

    assert allow.behavior == "allow"
    assert deny.behavior == "deny"


def test_build_options_locks_down_tools(sut):
    from aspen.backends.sdk import SdkSession
    from aspen import config, prompts

    s = SdkSession("C:1")
    opts = s._build_options(sdk)

    assert opts.allowed_tools == [
        "mcp__aspen__list_directory",
        "mcp__aspen__read_file",
        "mcp__aspen__run_python_analysis",
    ]
    assert "Bash" in opts.disallowed_tools
    assert opts.can_use_tool == s._can_use_tool
    assert opts.max_turns == config.AGENT_MAX_ROUNDS
    assert opts.system_prompt == prompts.SYSTEM_PROMPT
    assert opts.model == config.MODEL


def test_cli_path_passed_only_when_set(sut, monkeypatch):
    from aspen.backends.sdk import SdkSession
    from aspen import config

    s = SdkSession("C:1")

    # Default (empty) -> rely on PATH discovery; cli_path stays unset (None).
    monkeypatch.setattr(config, "CLAUDE_CLI_PATH", "")
    assert s._build_options(sdk).cli_path is None

    # Explicit override -> forwarded to ClaudeAgentOptions.
    monkeypatch.setattr(config, "CLAUDE_CLI_PATH", "/home/u/.local/bin/claude")
    assert s._build_options(sdk).cli_path == "/home/u/.local/bin/claude"
