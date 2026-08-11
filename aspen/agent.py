"""
The agent session — a warm Claude Agent SDK conversation.

A conversation is a warm ``ClaudeSDKClient`` session: ``connect()`` once, then
``query()`` per turn with the SDK retaining context natively. Between turns the
client (and its Claude Code CLI subprocess) stays parked — this is the "pause
between a response and the next user message". The session lives on the persistent
event loop (``sessions._ensure_loop``) for its whole lifetime, as the SDK requires.

Tools are the shared impls wrapped as ``@tool`` and bundled with
``create_sdk_mcp_server`` (surfaced as ``mcp__aspen__*``). Tool access is layered:
``tools=["Bash"]`` is the *availability* gate — the only built-in tool the model
is even shown is Bash, so Read/Write/Edit/Glob/Grep/Task/WebSearch/WebFetch/Skill
never enter its context (smaller prompt, and no wasted round trips attempting a
tool that would only be denied). On top of that ``allowed_tools`` auto-approves our
MCP tools plus a read-only Bash allowlist (``config.BASH_ALLOWLIST``, e.g.
``Bash(squeue:*)``) for HPC job investigation, and the ``can_use_tool`` callback
denies everything else. So beyond the read-only browsing + sandboxed-analysis
surface the agent gets only the enumerated Bash commands — no file/web access via
the CLI.

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
        self._current: dict | None = None   # current turn's context (attachment sink)

    # --- tool wiring ------------------------------------------------------- #
    async def _tool_handler(self, name: str, args: dict) -> dict:
        """Run a shared tool impl off the loop; attachments land in the turn's sink."""
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
        # Verified enforcing 2026-06-24 (CLI 2.1.190 + bubblewrap 0.4.0): writes
        # outside allowWrite are blocked ("Read-only file system"). One gotcha —
        # Claude Code suppresses its Bash sandbox when it detects it is running
        # *nested* inside another Claude Code session (CLAUDECODE / CLAUDE_CODE_*
        # in the environment). The bot launched normally via start.sh is a
        # top-level process, so it sandboxes; just don't launch it from inside a
        # Claude session. Re-check with ./verify_sandbox.sh from a plain shell.
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
            # Built-in tools: Bash ONLY. `allowed_tools` governs auto-approval, not
            # availability — left unset, the model still *sees* Claude Code's whole
            # toolset (Read/Write/Edit/Glob/Grep/Task/WebSearch/WebFetch/Skill/...)
            # and burns a full round trip trying one before can_use_tool denies it.
            # Measured: dropping them takes the per-turn prompt from ~20.2k to ~7.2k
            # tokens. Our own tools are unaffected — they arrive via mcp_servers.
            tools=["Bash"],
            # Only the SDK server above; ignore any .mcp.json / operator-configured
            # MCP servers the CLI would otherwise load (setting_sources doesn't cover
            # MCP discovery). Keeps startup deterministic and the tool surface closed.
            strict_mcp_config=True,
            # No skills. Belt-and-braces: tools=["Bash"] already removes the Skill
            # tool, but this also drops the inherited skill listing from the prompt.
            skills=[],
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

    @staticmethod
    def _log_quota(info) -> None:
        """Say plainly in the log when the account's rate limit is running down.

        The operator otherwise learns about it from a user reporting a failed
        turn; the CLI tells us in advance.
        """
        status = getattr(info, "status", None)
        if status not in ("allowed_warning", "rejected"):
            return
        used = getattr(info, "utilization", None)
        log.warning(
            "Rate limit %s — %s of the %s window consumed; resets at %s",
            status,
            f"{used:.0%}" if isinstance(used, (int, float)) else "an unknown share",
            getattr(info, "rate_limit_type", None) or "current",
            getattr(info, "resets_at", None) or "an unreported time",
        )

    def _format_result_reply(self, text: str, result_msg, sent_interim: bool = False) -> str:
        """Turn the turn's final ``ResultMessage`` into the user-facing reply.

        On success, return the agent's text. On a non-success result, surface the
        *specific* reason (and keep any partial text the agent already produced)
        instead of a generic "backend error" — most importantly distinguishing a
        soft ``error_max_turns`` pause (work continues next turn) from a real error.

        ``sent_interim`` says whether commentary already reached the thread. If it
        did and the agent ends with nothing further to add, the reply is empty
        rather than "(no text response)" — the turn *did* produce output, and the
        front-end skips posting an empty message. Errors are still always reported.
        """
        subtype = getattr(result_msg, "subtype", "success") if result_msg is not None else "success"
        if subtype == "success":
            if text:
                return text
            return "" if sent_interim else "(no text response)"

        denials = getattr(result_msg, "permission_denials", None) or []
        api_status = getattr(result_msg, "api_error_status", None)
        log.warning(
            "Turn ended non-success: subtype=%s num_turns=%s stop_reason=%s "
            "denials=%d api_error_status=%s",
            subtype, getattr(result_msg, "num_turns", None),
            getattr(result_msg, "stop_reason", None), len(denials), api_status,
        )

        if subtype == "error_max_turns":
            note = (
                f"⚠️ I hit my per-turn limit of {config.AGENT_MAX_ROUNDS} tool-call rounds and "
                "paused before finishing — I didn't complete everything in one turn. Reply "
                "“continue” and I'll pick up where I left off (this thread keeps its context)."
            )
        else:
            bits = [f"reason: {subtype}"]
            if api_status:
                bits.append(f"API status {api_status}")
            if denials:
                bits.append(f"{len(denials)} tool call(s) blocked by the allowlist/sandbox")
            note = "⚠️ My turn ended early — " + "; ".join(bits) + "."
            detail = (getattr(result_msg, "result", None) or "").strip()
            if detail and detail != text:
                note += f"\nDetails: {detail[:500]}"
            note += "\nYou can ask me to try again or rephrase; the thread keeps its context."

        return f"{text}\n\n{note}".strip() if text else note

    @staticmethod
    def _result_meta(result_msg) -> dict:
        """The turn's cost/outcome facts, for the caller to record.

        Handed back through ``context`` (the same channel attachments already use)
        rather than the return value, so no signature changes: the front-end reads
        ``context["result_meta"]`` after ``send`` returns. Everything here is
        otherwise discarded once the reply is formatted.

        ``denials`` is the interesting one — it is the list of commands users
        wanted that the allowlist or sandbox refused, i.e. a feature backlog.
        """
        if result_msg is None:
            return {}
        usage = getattr(result_msg, "usage", None) or {}
        if not isinstance(usage, dict):     # older/newer SDKs may hand back an object
            usage = getattr(usage, "__dict__", {}) or {}
        denials = []
        for d in getattr(result_msg, "permission_denials", None) or []:
            get = d.get if isinstance(d, dict) else lambda k, _d=d: getattr(_d, k, None)
            tool_input = get("tool_input") or {}
            denials.append({
                "tool": get("tool_name"),
                "command": str(tool_input.get("command", ""))[:200] if isinstance(tool_input, dict) else "",
            })
        return {
            "result_subtype": getattr(result_msg, "subtype", None),
            "num_turns": getattr(result_msg, "num_turns", None),
            "denials": denials or None,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost_usd": getattr(result_msg, "total_cost_usd", None),
            "agent_ms": getattr(result_msg, "duration_ms", None),
            # Splitting total from API time separates *our* overhead (session
            # wait, pre-warm miss, preamble) from model time from tool time —
            # three different problems with three different fixes.
            "api_ms": getattr(result_msg, "duration_api_ms", None),
            "model_usage": getattr(result_msg, "model_usage", None),
        }

    @staticmethod
    def _quota_meta(info) -> dict:
        """The account's rate-limit meter, as of this turn.

        Under a subscription seat there is no per-request dollar cost, so this
        is the closest thing to a spend signal: ``utilization`` is the fraction
        of the window consumed (0.0-1.0), account-wide. It cannot say *who*
        consumed it — that join is against per-user tokens in the turn log.

        The CLI emits the event only when the state *transitions*, so most turns
        carry nothing here and the ones that do mark the step changes.
        """
        if info is None:
            return {}
        return {k: v for k, v in {
            "quota_status": getattr(info, "status", None),
            "quota_utilization": getattr(info, "utilization", None),
            "quota_resets_at": getattr(info, "resets_at", None),
            "quota_type": getattr(info, "rate_limit_type", None),
            "quota_overage_status": getattr(info, "overage_status", None),
        }.items() if v is not None}

    async def send(self, user_message: str, context: dict) -> tuple[str, list[str]]:
        """Run one turn.

        Two optional callbacks ride on ``context``, both called from the agent
        loop, so neither may block:

        * ``on_progress(tool_name, tool_input)`` — every tool the agent invokes,
          so the front-end can show what it's working on.
        * ``on_interim(text)`` — text the agent wrote *before* calling a tool.
          Without this sink the text is held to the end (the old behavior); with
          it, the agent's running commentary reaches the user while it works.
        """
        import claude_agent_sdk as sdk
        context.setdefault("attachments", [])
        self._current = context
        on_progress = context.get("on_progress")
        on_interim = context.get("on_interim")
        # Older SDKs don't emit rate-limit events; absence just means no meter.
        rate_limit_event = getattr(sdk, "RateLimitEvent", None)
        try:
            await self._ensure()
            await self._client.query(user_message)
            parts: list[str] = []       # text written since the last flush
            result_msg = None
            quota = None
            sent_interim = False

            def _flush_interim() -> None:
                """Hand over what the agent has said so far, before it acts.

                Message-level, not token-level: each flush is a complete block of
                markdown, so it renders correctly and never has to be revised.
                """
                nonlocal sent_interim
                if on_interim is None:
                    return          # no sink — keep accumulating for one final reply
                text = "\n".join(p for p in parts if p).strip()
                parts.clear()
                if not text:
                    return
                try:
                    on_interim(text)
                    sent_interim = True
                except Exception:   # commentary is a bonus; never fail a turn for it
                    log.debug("on_interim callback failed", exc_info=True)

            # Do NOT break out of receive_response early (SDK cleanup caveat).
            async for msg in self._client.receive_response():
                if isinstance(msg, sdk.AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, sdk.TextBlock):
                            parts.append(b.text)
                        elif isinstance(b, sdk.ToolUseBlock):
                            # Anything said before a tool call is narration of the
                            # step about to happen — it belongs in the thread now,
                            # not bolted to the top of an answer minutes later.
                            _flush_interim()
                            if on_progress is not None:
                                try:
                                    on_progress(b.name, b.input or {})
                                except Exception:   # progress is cosmetic
                                    log.debug("on_progress callback failed", exc_info=True)
                elif isinstance(msg, sdk.ResultMessage):
                    result_msg = msg
                elif rate_limit_event is not None and isinstance(msg, rate_limit_event):
                    quota = msg.rate_limit_info
                    self._log_quota(quota)
            text = "\n".join(p for p in parts if p).strip()
            context["result_meta"] = {**self._result_meta(result_msg),
                                      **self._quota_meta(quota)}
            return (self._format_result_reply(text, result_msg, sent_interim),
                    list(context["attachments"]))
        except sdk.ClaudeSDKError as exc:
            name = type(exc).__name__
            context["result_meta"] = {"result_subtype": f"sdk_error:{name}"}
            log.error("SDK backend error: %s: %s", name, exc)
            await self.aclose()                    # reset; next turn reconnects
            hint = {
                "CLINotFoundError": "the Claude CLI binary wasn't found (check CLAUDE_CLI_PATH)",
                "CLIConnectionError": "couldn't connect to the Claude CLI process",
                "ProcessError": "the Claude CLI process exited unexpectedly",
                "CLIJSONDecodeError": "the Claude CLI returned output the SDK couldn't parse",
            }.get(name, "an unexpected SDK error occurred")
            detail = str(exc).strip()
            suffix = f" Details: {detail[:300]}" if detail else ""
            return (
                f"⚠️ SDK error — {hint} ({name}).{suffix} The session was reset; please try again.",
                list(context["attachments"]),
            )

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()    # kills the CLI subprocess
            except Exception:
                log.exception("Error disconnecting SDK client for %s", self.key)
            finally:
                self._client = None
