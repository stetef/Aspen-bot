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
    from aspen.agent import SdkSession

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
    from aspen.agent import SdkSession

    FakeClient.responder = staticmethod(_error_messages)
    s = SdkSession("C:1")
    reply, figs = asyncio.run(s.send("do it", {"figures": []}))

    assert reply == "Sorry, the SDK backend hit an error. Please try again."
    assert figs == []


def test_tool_handler_drains_figures_into_sink(sut, monkeypatch):
    from aspen.agent import SdkSession
    import aspen.tools as t

    monkeypatch.setitem(t.TOOL_FNS, "fake_fig", lambda inp, ctx: ("plotted", ["/w/x.png"]))
    s = SdkSession("C:1")
    ctx = {"figures": []}
    s._current = ctx

    out = asyncio.run(s._tool_handler("fake_fig", {}))

    assert out == {"content": [{"type": "text", "text": "plotted"}]}
    assert ctx["figures"] == ["/w/x.png"]


def test_can_use_tool_allows_only_aspen_tools(sut):
    from aspen.agent import SdkSession

    s = SdkSession("C:1")
    allow = asyncio.run(s._can_use_tool("mcp__aspen__read_file", {}, None))
    deny_other = asyncio.run(s._can_use_tool("WebFetch", {"url": "http://x"}, None))
    # An off-allowlist Bash command reaches the backstop and is denied; the
    # message names the offending command and the allowlist.
    deny_bash = asyncio.run(s._can_use_tool("Bash", {"command": "rm -rf /"}, None))

    assert allow.behavior == "allow"
    assert deny_other.behavior == "deny"
    assert deny_bash.behavior == "deny"
    assert "rm -rf /" in deny_bash.message


def test_build_options_locks_down_tools(sut):
    from aspen.agent import SdkSession
    from aspen import config, prompts

    s = SdkSession("C:1")
    opts = s._build_options(sdk)

    # MCP tools first, then the configured read-only Bash allowlist patterns.
    assert opts.allowed_tools == [
        "mcp__aspen__list_directory",
        "mcp__aspen__read_file",
        "mcp__aspen__run_python_analysis",
    ] + list(config.BASH_ALLOWLIST)
    assert "Bash(squeue:*)" in opts.allowed_tools
    # Host settings are ignored so the allowlist is the sole permission authority.
    assert opts.setting_sources == []
    assert opts.can_use_tool == s._can_use_tool
    assert opts.max_turns == config.AGENT_MAX_ROUNDS
    assert opts.system_prompt == prompts.SYSTEM_PROMPT
    assert opts.model == config.MODEL


def test_sandbox_disabled_by_default(sut):
    from aspen.backends.sdk import SdkSession
    from aspen import config

    assert config.SANDBOX_ENABLED is False
    s = SdkSession("C:1")
    assert s._sandbox_settings() is None
    opts = s._build_options(sdk)
    # No sandbox => no OS jail and cwd untouched (current behavior preserved).
    assert opts.sandbox is None
    assert opts.cwd is None


def test_sandbox_settings_built_from_config(sut, monkeypatch):
    from aspen.backends.sdk import SdkSession
    from aspen import config

    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_WRITE_PATHS", ["/scratch/aspen", "~/out"])
    monkeypatch.setattr(config, "SANDBOX_ALLOWED_DOMAINS", ["pypi.org"])
    monkeypatch.setattr(config, "SANDBOX_UNIX_SOCKETS", [])
    monkeypatch.setattr(config, "SANDBOX_WORKDIR", "/scratch/aspen")

    opts = SdkSession("C:1")._build_options(sdk)
    sb = opts.sandbox

    assert sb["enabled"] is True
    assert sb["autoAllowBashIfSandboxed"] is config.SANDBOX_AUTO_ALLOW
    assert sb["allowUnsandboxedCommands"] is config.SANDBOX_ALLOW_UNSANDBOXED
    assert sb["failIfUnavailable"] is config.SANDBOX_FAIL_IF_UNAVAILABLE
    assert sb["filesystem"]["allowWrite"] == ["/scratch/aspen", "~/out"]
    assert sb["network"]["allowedDomains"] == ["pypi.org"]
    assert "allowUnixSockets" not in sb["network"]   # omitted when empty
    # Slurm clients run outside the jail (need cluster network/munge).
    assert "squeue" in sb["excludedCommands"]
    # Session pinned to the configured workdir so the agent stays out of repo/home.
    assert opts.cwd == "/scratch/aspen"


def test_sandbox_default_excludes_slurm_clients(sut):
    from aspen import config

    for cmd in ("squeue", "sacct", "sinfo", "scontrol"):
        assert cmd in config.SANDBOX_EXCLUDED_COMMANDS


def test_default_bash_allowlist_is_readonly(sut):
    from aspen import config

    # The investigation commands the user asked for are present...
    for rule in ("Bash(squeue:*)", "Bash(sacct:*)", "Bash(sinfo:*)", "Bash(grep:*)"):
        assert rule in config.BASH_ALLOWLIST
    # ...scontrol is restricted to the read-only 'show' subcommand (no bare scontrol,
    # which could update/requeue jobs).
    assert "Bash(scontrol show:*)" in config.BASH_ALLOWLIST
    assert "Bash(scontrol:*)" not in config.BASH_ALLOWLIST
    # find/awk/sed are excluded on purpose — their flags can write/execute and the
    # prefix match can't see that.
    cmds = " ".join(config.BASH_ALLOWLIST)
    assert "find" not in cmds and "awk" not in cmds and "sed" not in cmds


def test_bash_allowlist_override_flows_into_allowed_tools(sut, monkeypatch):
    """The allowlist is config-driven: whatever config holds lands in allowed_tools
    (after the MCP tools), so an operator's ASPEN_BASH_ALLOWLIST takes effect."""
    from aspen.agent import SdkSession
    from aspen import config

    monkeypatch.setattr(config, "BASH_ALLOWLIST", ["Bash(squeue:*)", "Bash(sacct:*)"])
    opts = SdkSession("C:1")._build_options(sdk)

    assert opts.allowed_tools == [
        "mcp__aspen__list_directory",
        "mcp__aspen__read_file",
        "mcp__aspen__run_python_analysis",
        "Bash(squeue:*)",
        "Bash(sacct:*)",
    ]


def test_bash_deny_message_lists_the_allowlist(sut, monkeypatch):
    from aspen.agent import SdkSession
    from aspen import config

    monkeypatch.setattr(config, "BASH_ALLOWLIST", ["Bash(squeue:*)"])
    deny = asyncio.run(
        SdkSession("C:1")._can_use_tool("Bash", {"command": "scancel 42"}, None)
    )

    assert deny.behavior == "deny"
    assert "scancel 42" in deny.message      # names the offending command
    assert "Bash(squeue:*)" in deny.message  # tells the model what IS allowed


def test_bash_allowlist_env_parsing(sut, monkeypatch):
    """ASPEN_BASH_ALLOWLIST is parsed comma-separated, trimmed, blanks dropped."""
    import importlib
    from aspen import config

    monkeypatch.setenv("ASPEN_BASH_ALLOWLIST", " Bash(squeue:*) , ,Bash(grep:*) ")
    try:
        importlib.reload(config)
        assert config.BASH_ALLOWLIST == ["Bash(squeue:*)", "Bash(grep:*)"]
    finally:
        monkeypatch.delenv("ASPEN_BASH_ALLOWLIST", raising=False)
        importlib.reload(config)  # restore module-level default for later tests


def test_cli_path_passed_only_when_set(sut, monkeypatch):
    from aspen.agent import SdkSession
    from aspen import config

    s = SdkSession("C:1")

    # Default (empty) -> rely on PATH discovery; cli_path stays unset (None).
    monkeypatch.setattr(config, "CLAUDE_CLI_PATH", "")
    assert s._build_options(sdk).cli_path is None

    # Explicit override -> forwarded to ClaudeAgentOptions.
    monkeypatch.setattr(config, "CLAUDE_CLI_PATH", "/home/u/.local/bin/claude")
    assert s._build_options(sdk).cli_path == "/home/u/.local/bin/claude"


def test_subscription_auth_blanks_api_key_for_cli(sut, monkeypatch):
    from aspen.agent import SdkSession
    from aspen import config

    s = SdkSession("C:1")

    # Subscription mode (default): blank ANTHROPIC_API_KEY so the CLI uses the login.
    monkeypatch.setattr(config, "ASPEN_SDK_USE_SUBSCRIPTION", True)
    assert s._build_options(sdk).env == {"ANTHROPIC_API_KEY": ""}

    # API-key mode: don't touch the subprocess env (CLI inherits ANTHROPIC_API_KEY).
    monkeypatch.setattr(config, "ASPEN_SDK_USE_SUBSCRIPTION", False)
    assert s._build_options(sdk).env == {}
