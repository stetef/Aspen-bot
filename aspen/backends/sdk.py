"""
Claude Agent SDK backend.

A conversation is a warm ``ClaudeSDKClient`` session: ``connect()`` once, then
``query()`` per turn with the SDK retaining context natively. Between turns the
client (and its Claude Code CLI subprocess) stays parked — this is the "pause
between a response and the next user message". The session lives on the persistent
event loop (``sessions._ensure_loop``) for its whole lifetime, as the SDK requires.

Tools are the shared impls wrapped as ``@tool`` and bundled with
``create_sdk_mcp_server`` (surfaced as ``mcp__aspen__*``). The agent is locked to
ONLY those tools: ``allowed_tools`` auto-approves them and a ``can_use_tool``
callback denies everything else (any name not ``mcp__aspen__*`` — robust across CLI
versions, unlike a static built-in denylist). So the SDK agent has exactly the
read-only + sandbox-only surface of the Messages backend and never gains Bash/file/
web access via the CLI.

``claude-agent-sdk`` is imported lazily so the default ``messages`` backend never
requires it (or the Claude Code CLI binary).

Auth: by default (``ASPEN_SDK_USE_SUBSCRIPTION``) the CLI uses the Claude Code login
(subscription) — the SDK subprocess is given a blank ``ANTHROPIC_API_KEY`` so the
key in the environment (which the CLI would otherwise prefer) doesn't take over.
"""

import asyncio
import logging

from .. import config, prompts, tools

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
        """Allow only our MCP tools; deny everything else (built-ins, etc.)."""
        import claude_agent_sdk as sdk
        if tool_name.startswith(_TOOL_PREFIX):
            return sdk.PermissionResultAllow()
        return sdk.PermissionResultDeny(message=f"{tool_name} is not permitted for Aspen.")

    def _build_options(self, sdk):
        server = sdk.create_sdk_mcp_server(
            name=_SERVER, version="1.0.0", tools=self._make_tools(sdk)
        )
        allowed = [f"{_TOOL_PREFIX}{s['name']}" for s in tools.TOOL_SPECS]
        opts = dict(
            system_prompt=prompts.SYSTEM_PROMPT,   # plain string -> replaces (no preset)
            model=config.MODEL,
            mcp_servers={_SERVER: server},
            allowed_tools=allowed,                 # auto-approve only our tools
            can_use_tool=self._can_use_tool,       # lockdown: deny anything not mcp__aspen__*
            max_turns=config.AGENT_MAX_ROUNDS,
        )
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
