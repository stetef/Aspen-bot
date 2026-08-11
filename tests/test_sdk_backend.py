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
        r1, _ = await s.send("hi", {"user_id": "U1", "attachments": []})
        r2, _ = await s.send("again", {"user_id": "U1", "attachments": []})
        return r1, r2

    r1, r2 = asyncio.run(two_turns())

    assert r1 == "reply: hi"
    assert r2 == "reply: again"
    # One client, connected once, queried twice — the session parked and stayed warm.
    assert len(FakeClient.instances) == 1
    assert FakeClient.instances[0].connect_count == 1
    assert FakeClient.instances[0].queries == ["hi", "again"]


def test_error_subtype_returns_specific_reply_keeping_partial_text(sut):
    from aspen.agent import SdkSession

    FakeClient.responder = staticmethod(_error_messages)
    s = SdkSession("C:1")
    reply, atts = asyncio.run(s.send("do it", {"attachments": []}))

    # Specific reason surfaced, and the partial text the agent produced is preserved.
    assert "partial" in reply
    assert "error_during_execution" in reply
    assert "ended early" in reply
    assert atts == []


def test_max_turns_reports_soft_pause_not_failure(sut):
    from aspen.agent import SdkSession
    from aspen import config

    def _max_turns_messages(prompt):
        return [
            sdk.AssistantMessage(content=[sdk.TextBlock(text="did step 1")], model="m"),
            _result("error_max_turns"),
        ]

    FakeClient.responder = staticmethod(_max_turns_messages)
    s = SdkSession("C:1")
    reply, _ = asyncio.run(s.send("big task", {"attachments": []}))

    assert "did step 1" in reply                       # partial progress kept
    assert str(config.AGENT_MAX_ROUNDS) in reply        # names the actual cap
    assert "continue" in reply.lower()                  # tells the user how to resume


def test_tool_handler_drains_attachments_into_sink(sut, monkeypatch):
    from aspen.agent import SdkSession
    import aspen.tools as t

    monkeypatch.setitem(t.TOOL_FNS, "fake_fig", lambda inp, ctx: ("plotted", ["/w/x.png"]))
    s = SdkSession("C:1")
    ctx = {"attachments": []}
    s._current = ctx

    out = asyncio.run(s._tool_handler("fake_fig", {}))

    assert out == {"content": [{"type": "text", "text": "plotted"}]}
    assert ctx["attachments"] == ["/w/x.png"]


def test_tool_uses_are_reported_to_the_progress_hook(sut):
    """Each tool the agent invokes is announced mid-turn, so the Slack front-end
    can show what it's working on instead of a static "typing" indicator."""
    from aspen.agent import SdkSession

    def _tool_messages(prompt):
        return [
            sdk.AssistantMessage(
                content=[
                    sdk.TextBlock(text="looking"),
                    sdk.ToolUseBlock(id="t1", name="mcp__aspen__read_file",
                                     input={"path": "runs/orca.out"}),
                ],
                model="m",
            ),
            sdk.AssistantMessage(content=[sdk.TextBlock(text="found it")], model="m"),
            _result("success"),
        ]

    FakeClient.responder = staticmethod(_tool_messages)
    seen = []
    reply, _ = asyncio.run(SdkSession("C:1").send(
        "check", {"attachments": [], "on_progress": lambda n, i: seen.append((n, i))}
    ))

    assert seen == [("mcp__aspen__read_file", {"path": "runs/orca.out"})]
    # Text from both messages still lands in the reply; tool blocks aren't text.
    assert reply == "looking\nfound it"


def test_progress_hook_failure_never_breaks_a_turn(sut):
    """Progress is cosmetic — a raising callback must not lose the user's answer."""
    from aspen.agent import SdkSession

    def _tool_messages(prompt):
        return [
            sdk.AssistantMessage(
                content=[sdk.ToolUseBlock(id="t1", name="Bash", input={"command": "squeue"})],
                model="m",
            ),
            sdk.AssistantMessage(content=[sdk.TextBlock(text="all done")], model="m"),
            _result("success"),
        ]

    def _boom(name, inp):
        raise RuntimeError("slack exploded")

    FakeClient.responder = staticmethod(_tool_messages)
    reply, _ = asyncio.run(SdkSession("C:1").send(
        "check", {"attachments": [], "on_progress": _boom}
    ))

    assert reply == "all done"


def test_send_works_without_a_progress_hook(sut):
    """The hook is optional — a context without one behaves exactly as before."""
    from aspen.agent import SdkSession

    def _tool_messages(prompt):
        return [
            sdk.AssistantMessage(
                content=[sdk.ToolUseBlock(id="t1", name="Bash", input={"command": "squeue"}),
                         sdk.TextBlock(text="queue is empty")],
                model="m",
            ),
            _result("success"),
        ]

    FakeClient.responder = staticmethod(_tool_messages)
    reply, _ = asyncio.run(SdkSession("C:1").send("check", {"attachments": []}))

    assert reply == "queue is empty"


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
        "mcp__aspen__search_files",
        "mcp__aspen__attach_file",
        "mcp__aspen__read_metadata",
        "mcp__aspen__write_metadata",
        "mcp__aspen__read_workflow",
        "mcp__aspen__write_workflow",
        "mcp__aspen__run_python_analysis",
    ] + list(config.BASH_ALLOWLIST)
    assert "Bash(squeue:*)" in opts.allowed_tools
    # Bash is the ONLY built-in tool the model is shown. allowed_tools governs
    # auto-approval, not availability — without this the model still sees (and
    # wastes a round trip attempting) Read/Write/Glob/Grep/WebSearch/Task/...
    assert opts.tools == ["Bash"]
    # No inherited MCP servers or skills: the tool surface is exactly what we pass.
    assert opts.strict_mcp_config is True
    assert opts.skills == []
    # Host settings are ignored so the allowlist is the sole permission authority.
    assert opts.setting_sources == []
    assert opts.can_use_tool == s._can_use_tool
    assert opts.max_turns == config.AGENT_MAX_ROUNDS
    assert opts.system_prompt == prompts.SYSTEM_PROMPT
    assert opts.model == config.MODEL


def test_sandbox_disabled_by_default(sut):
    from aspen.agent import SdkSession
    from aspen import config

    assert config.SANDBOX_ENABLED is False
    s = SdkSession("C:1")
    assert s._sandbox_settings() is None
    opts = s._build_options(sdk)
    # No sandbox => no OS jail and cwd untouched (current behavior preserved).
    assert opts.sandbox is None
    assert opts.cwd is None


def test_sandbox_settings_built_from_config(sut, monkeypatch):
    from aspen.agent import SdkSession
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

    # The Slurm investigation commands are present...
    for rule in ("Bash(squeue:*)", "Bash(sacct:*)", "Bash(sinfo:*)",
                 "Bash(sstat:*)", "Bash(sprio:*)"):
        assert rule in config.BASH_ALLOWLIST
    # ...scontrol is restricted to the read-only 'show' subcommand (no bare scontrol,
    # which could update/requeue jobs).
    assert "Bash(scontrol show:*)" in config.BASH_ALLOWLIST
    assert "Bash(scontrol:*)" not in config.BASH_ALLOWLIST
    # SECURITY: the default is Slurm-ONLY. General file-readers must NOT be in the
    # default — with the OS sandbox off, Bash runs as the bot's Unix user with no
    # path restriction, so cat/grep/head/tail/ls/wc/sort/uniq would let an
    # allowlisted user read any file it can (SSH keys, .env, ~/.claude). find/awk/sed
    # are excluded too (their flags can write/execute, unseen by the prefix match).
    cmds = " ".join(config.BASH_ALLOWLIST)
    for forbidden in ("cat", "grep", "head", "tail", "ls", "wc", "sort", "uniq",
                      "find", "awk", "sed"):
        assert forbidden not in cmds, f"{forbidden!r} must not be in the default allowlist"


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
        "mcp__aspen__search_files",
        "mcp__aspen__attach_file",
        "mcp__aspen__read_metadata",
        "mcp__aspen__write_metadata",
        "mcp__aspen__read_workflow",
        "mcp__aspen__write_workflow",
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


# --------------------------------------------------------------------------- #
# Interim commentary — the agent's words reach the thread while it works
# --------------------------------------------------------------------------- #
def _narrating_messages(prompt):
    """Says something, calls a tool, then answers — the shape of a real turn."""
    return [
        sdk.AssistantMessage(
            content=[
                sdk.TextBlock(text="Let me check the queue first."),
                sdk.ToolUseBlock(id="t1", name="Bash", input={"command": "squeue"}),
            ],
            model="m",
        ),
        sdk.AssistantMessage(content=[sdk.TextBlock(text="12 jobs running.")], model="m"),
        _result("success"),
    ]


def _narrate_only_messages(prompt):
    """Narrates, acts, and ends with nothing further to add."""
    return [
        sdk.AssistantMessage(
            content=[
                sdk.TextBlock(text="Cancelling the stuck job."),
                sdk.ToolUseBlock(id="t1", name="Bash", input={"command": "squeue"}),
            ],
            model="m",
        ),
        _result("success"),
    ]


def test_text_before_a_tool_call_is_handed_over_immediately(sut):
    """The point of the feature: narration arrives while the work happens, and
    is not repeated in the answer minutes later."""
    from aspen.agent import SdkSession

    FakeClient.responder = staticmethod(_narrating_messages)
    said: list[str] = []
    reply, _ = asyncio.run(SdkSession("C:1").send(
        "how many jobs?", {"attachments": [], "on_interim": said.append}))

    assert said == ["Let me check the queue first."]
    assert reply == "12 jobs running."          # narration not duplicated here


def test_without_an_interim_sink_every_word_lands_in_the_reply(sut):
    """Unconfigured, the turn behaves exactly as it did before — nothing is lost."""
    from aspen.agent import SdkSession

    FakeClient.responder = staticmethod(_narrating_messages)
    reply, _ = asyncio.run(SdkSession("C:1").send("how many jobs?", {"attachments": []}))

    assert "Let me check the queue first." in reply
    assert "12 jobs running." in reply


def test_a_turn_that_only_narrates_ends_quietly(sut):
    """Everything was already said, so the reply is empty rather than the
    '(no text response)' placeholder — the front-end posts nothing more."""
    from aspen.agent import SdkSession

    FakeClient.responder = staticmethod(_narrate_only_messages)
    said: list[str] = []
    reply, _ = asyncio.run(SdkSession("C:1").send(
        "cancel it", {"attachments": [], "on_interim": said.append}))

    assert said == ["Cancelling the stuck job."]
    assert reply == ""


def test_a_silent_turn_still_says_something(sut):
    """With no narration and no answer, the placeholder is still the right reply."""
    from aspen.agent import SdkSession

    FakeClient.responder = staticmethod(lambda p: [_result("success")])
    reply, _ = asyncio.run(SdkSession("C:1").send(
        "hi", {"attachments": [], "on_interim": lambda t: None}))

    assert reply == "(no text response)"


def test_a_failing_interim_sink_never_costs_the_turn(sut):
    from aspen.agent import SdkSession

    def _boom(_text):
        raise RuntimeError("slack is down")

    FakeClient.responder = staticmethod(_narrating_messages)
    reply, _ = asyncio.run(SdkSession("C:1").send(
        "how many jobs?", {"attachments": [], "on_interim": _boom}))

    assert reply == "12 jobs running."          # the answer still lands


# --------------------------------------------------------------------------- #
# Quota meter and the timing split
# --------------------------------------------------------------------------- #
def test_rate_limit_event_is_captured_instead_of_discarded(sut):
    """Under a subscription seat this meter is the closest thing to a spend
    signal, and it arrives on the same stream as everything else."""
    from aspen.agent import SdkSession

    def _quota_messages(prompt):
        return [
            sdk.RateLimitEvent(
                rate_limit_info=sdk.RateLimitInfo(
                    status="allowed_warning", utilization=0.82,
                    resets_at=1800000000, rate_limit_type="five_hour",
                ),
                uuid="u", session_id="s",
            ),
            sdk.AssistantMessage(content=[sdk.TextBlock(text="ok")], model="m"),
            _result("success"),
        ]

    FakeClient.responder = staticmethod(_quota_messages)
    context = {"attachments": []}
    asyncio.run(SdkSession("C:1").send("hi", context))

    meta = context["result_meta"]
    assert meta["quota_utilization"] == 0.82
    assert meta["quota_status"] == "allowed_warning"
    assert meta["quota_resets_at"] == 1800000000
    assert meta["quota_type"] == "five_hour"


def test_a_turn_without_a_quota_event_carries_no_quota_fields(sut):
    """The CLI emits the meter only on a state change, so most turns have none —
    absent, not zero, so the dashboard can forward-fill instead of seeing a drop."""
    from aspen.agent import SdkSession

    context = {"attachments": []}
    asyncio.run(SdkSession("C:1").send("hi", context))

    assert not any(k.startswith("quota_") for k in context["result_meta"])


def test_result_meta_carries_the_timing_split(sut):
    """total vs API time is what separates our overhead from model time."""
    from aspen.agent import SdkSession

    FakeClient.responder = staticmethod(
        lambda p: [sdk.ResultMessage(
            subtype="success", duration_ms=9000, duration_api_ms=4000,
            is_error=False, num_turns=3, session_id="s",
        )]
    )
    context = {"attachments": []}
    asyncio.run(SdkSession("C:1").send("hi", context))

    assert context["result_meta"]["agent_ms"] == 9000
    assert context["result_meta"]["api_ms"] == 4000
