"""
The agent session — a warm Claude Agent SDK conversation.

A conversation is a warm ``ClaudeSDKClient`` session: ``connect()`` once, then
``query()`` per turn with the SDK retaining context natively. Between turns the
client (and its Claude Code CLI subprocess) stays parked — this is the "pause
between a response and the next user message". The session lives on the persistent
event loop (``sessions._ensure_loop``) for its whole lifetime, as the SDK requires.

Tools are the shared impls wrapped as ``@tool`` and bundled with
``create_sdk_mcp_server`` (surfaced as ``mcp__aspen__*``). Tool access is layered:
``allowed_tools`` auto-approves our MCP tools plus a read-only Bash allowlist
(``config.BASH_ALLOWLIST``, e.g. ``Bash(squeue:*)``) for HPC job investigation,
and the ``can_use_tool`` callback denies everything else. So beyond the read-only
browsing + sandboxed-analysis surface the agent gets only the enumerated Bash
commands — no file/web access via the CLI.

Optionally (``config.SANDBOX_ENABLED``) Bash runs inside Claude Code's OS-level
sandbox (bubblewrap on Linux / Seatbelt on macOS) via the ``sandbox`` option. The
operator defines the agent's read/write/network boundary in ``config`` — giving it
a *write* surface independent of the bot's Unix user — and sandboxed commands are
auto-approved by that boundary. The read-only Slurm clients are excluded from the
jail (they need cluster network/munge) but stay gated by the allowlist.

``claude-agent-sdk`` is imported lazily (inside methods) so importing this module
— e.g. in tests — doesn't require the SDK package or the Claude Code CLI binary.

Auth: by default (``ASPEN_SDK_USE_SUBSCRIPTION``) the CLI uses the Claude Code login
(subscription) — the SDK subprocess is given a blank ``ANTHROPIC_API_KEY`` so the
key in the environment (which the CLI would otherwise prefer) doesn't take over.
"""

import asyncio
import logging

from . import config, prompts, tools

log = logging.getLogger("aspen")

# Server (MCP) name; tools are surfaced to the model as mcp__aspen__<tool>.
_SERVER = "aspen"
_TOOL_PREFIX = f"mcp__{_SERVER}__"


class SdkSession:
    """Warm, parked Claude Agent SDK conversation session."""

    def __init__(self, key: str):
        self.key = key
        self._client = None
        self._current: dict | None = None   # current turn's context (figure sink)

    # --- tool wiring ------------------------------------------------------- #
    async def _tool_handler(self, name: str, args: dict) -> dict:
        """Run a shared tool impl off the loop; figures land in the turn's sink."""
        text = await asyncio.to_thread(tools.dispatch, name, args, self._current)
        return {"content": [{"type": "text", "text": text}]}

    def _make_tools(self, sdk):
        built = []
        for spec in tools.TOOL_SPECS:
            @sdk.tool(spec["name"], spec["description"], spec["input_schema"])
            async def _handler(args, _name=spec["name"]):
                return await self._tool_handler(_name, args)
            built.append(_handler)
        return built

    async def _can_use_tool(self, tool_name, tool_input, context):
        """Backstop deny. Reached only for tool calls the allowlist did NOT
        pre-approve (our MCP tools + the Bash patterns in ``allowed_tools`` never
        get here). So a Bash call arriving here is an off-allowlist command."""
        import claude_agent_sdk as sdk
        if tool_name.startswith(_TOOL_PREFIX):
            return sdk.PermissionResultAllow()     # defensive; normally auto-approved
        if tool_name == "Bash":
            cmd = (tool_input or {}).get("command", "")
            return sdk.PermissionResultDeny(message=(
                f"Command not in Aspen's allowlist: {cmd!r}. Only specific read-only "
                "investigation commands are permitted (e.g. squeue, sacct, sinfo, "
                f"grep). Allowlist: {', '.join(config.BASH_ALLOWLIST)}"
            ))
        return sdk.PermissionResultDeny(message=f"{tool_name} is not permitted for Aspen.")

    def _sandbox_settings(self):
        """Claude Code sandbox config from ``config``, or ``None`` when disabled.

        Passed via the SDK's ``sandbox`` option, which the SDK merges into the
        ``--settings`` flag layer — independent of ``setting_sources`` (so the
        host-settings lockdown stays intact). The dict is forwarded verbatim, so
        CLI-only keys not in the SDK TypedDict (``filesystem``, ``failIfUnavailable``)
        are honored by the CLI. See https://code.claude.com/docs/en/sandboxing."""
        if not config.SANDBOX_ENABLED:
            return None
        # CAVEAT (verified 2026-06-24 on Claude Code CLI 2.1.190 + bubblewrap 0.4.0):
        # in SDK/headless mode the CLI auto-approves Bash as "sandboxed" but does
        # NOT actually confine it (writes outside allowWrite still succeed), and
        # that auto-approval bypasses the can_use_tool allowlist backstop. So on
        # this CLI, enabling the sandbox is a net regression. Re-verify enforcement
        # on a newer CLI before trusting it.
        log.warning(
            "ASPEN_SANDBOX_ENABLED=true: the Bash sandbox was NOT enforced in "
            "SDK/headless mode on CLI 2.1.190 (writes weren't confined and the "
            "can_use_tool allowlist was bypassed). Re-verify on your CLI version "
            "before relying on it; otherwise prefer ASPEN_BASH_ALLOWLIST alone."
        )
        fs = {}
        if config.SANDBOX_WRITE_PATHS:
            fs["allowWrite"] = config.SANDBOX_WRITE_PATHS
        if config.SANDBOX_DENY_READ_PATHS:
            fs["denyRead"] = config.SANDBOX_DENY_READ_PATHS
        if config.SANDBOX_ALLOW_READ_PATHS:
            fs["allowRead"] = config.SANDBOX_ALLOW_READ_PATHS
        net = {"allowedDomains": config.SANDBOX_ALLOWED_DOMAINS}  # [] => no network
        if config.SANDBOX_UNIX_SOCKETS:
            net["allowUnixSockets"] = config.SANDBOX_UNIX_SOCKETS
        sandbox = {
            "enabled": True,
            "autoAllowBashIfSandboxed": config.SANDBOX_AUTO_ALLOW,
            "allowUnsandboxedCommands": config.SANDBOX_ALLOW_UNSANDBOXED,
            "failIfUnavailable": config.SANDBOX_FAIL_IF_UNAVAILABLE,
            "network": net,
        }
        if config.SANDBOX_EXCLUDED_COMMANDS:
            sandbox["excludedCommands"] = config.SANDBOX_EXCLUDED_COMMANDS
        if fs:
            sandbox["filesystem"] = fs
        return sandbox

    def _build_options(self, sdk):
        server = sdk.create_sdk_mcp_server(
            name=_SERVER, version="1.0.0", tools=self._make_tools(sdk)
        )
        # Auto-approve our MCP tools plus the configured read-only Bash patterns
        # (e.g. "Bash(squeue:*)"). Anything else falls through to the can_use_tool
        # backstop, which denies it.
        allowed = [f"{_TOOL_PREFIX}{s['name']}" for s in tools.TOOL_SPECS] + list(config.BASH_ALLOWLIST)
        opts = dict(
            system_prompt=prompts.SYSTEM_PROMPT,   # plain string -> replaces (no preset)
            model=config.MODEL,
            mcp_servers={_SERVER: server},
            allowed_tools=allowed,                 # auto-approve our tools + Bash allowlist
            can_use_tool=self._can_use_tool,       # lockdown: deny anything not pre-approved
            # Ignore host settings (~/.claude, project) so the allowlist above is the
            # sole authority — an operator's permissions.allow can't widen the bot.
            setting_sources=[],
            max_turns=config.AGENT_MAX_ROUNDS
        )
        sandbox = self._sandbox_settings()
        if sandbox is not None:
            # OS-level jail for Bash; the operator's write boundary lives here,
            # separate from the bot user's own filesystem permissions.
            opts["sandbox"] = sandbox
            if config.SANDBOX_WORKDIR:             # keep the agent out of the repo/home
                opts["cwd"] = str(config.SANDBOX_WORKDIR)
        if config.CLAUDE_CLI_PATH:                 # else the SDK finds "claude" on PATH
            opts["cli_path"] = config.CLAUDE_CLI_PATH
        if config.ASPEN_SDK_USE_SUBSCRIPTION:
            # The CLI prefers ANTHROPIC_API_KEY over the Claude Code login; blank it
            # for the subprocess so it authenticates via the subscription instead.
            opts["env"] = {"ANTHROPIC_API_KEY": ""}
        return sdk.ClaudeAgentOptions(**opts)

    # --- lifecycle --------------------------------------------------------- #
    async def _ensure(self):
        if self._client is None:
            import claude_agent_sdk as sdk
            self._client = sdk.ClaudeSDKClient(options=self._build_options(sdk))
            await self._client.connect()           # spawns the CLI subprocess once (warm)

    async def send(self, user_message: str, context: dict) -> tuple[str, list[str]]:
        import claude_agent_sdk as sdk
        context.setdefault("figures", [])
        self._current = context
        try:
            await self._ensure()
            await self._client.query(user_message)
            parts: list[str] = []
            errored = False
            # Do NOT break out of receive_response early (SDK cleanup caveat).
            async for msg in self._client.receive_response():
                if isinstance(msg, sdk.AssistantMessage):
                    parts += [b.text for b in msg.content if isinstance(b, sdk.TextBlock)]
                elif isinstance(msg, sdk.ResultMessage):
                    if getattr(msg, "subtype", "success") != "success":
                        errored = True
            reply = (
                "Sorry, the SDK backend hit an error. Please try again."
                if errored else ("\n".join(parts) or "(no text response)")
            )
            return reply, list(context["figures"])
        except sdk.ClaudeSDKError as exc:
            log.error("SDK backend error: %s", type(exc).__name__)
            await self.aclose()                    # reset; next turn reconnects
            return (
                f"Sorry, there was an SDK error ({type(exc).__name__}). Please try again.",
                list(context["figures"]),
            )

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()    # kills the CLI subprocess
            except Exception:
                log.exception("Error disconnecting SDK client for %s", self.key)
            finally:
                self._client = None
