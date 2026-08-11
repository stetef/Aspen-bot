"""
Slack Socket Mode front-end.

Sync Bolt handlers do the admission gates (allowlist → per-user rate limit →
global concurrency semaphore), then feed the user message into the async session
system via ``run_coroutine_threadsafe`` on the persistent loop and block for the
reply. The SessionManager runs the turn on a warm Claude Agent SDK session.
"""

import asyncio
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout

from slack_bolt import App

from . import (attachments, config, pending, ratelimit, registry, render, sessions,
               setup, state, telemetry, workflows)

log = logging.getLogger("aspen")

# Native Slack "working" indicator for AI apps (assistant.threads.setStatus).
# Slack renders it as "<App Name> <status>", so with the bot named "Aspen" this
# shows up as "Aspen is typing…" beneath the thread compose box.
_STATUS_TEXT = "is typing…"
# Slack expires a status after ~2 minutes, but Aspen turns routinely run longer,
# so a background thread re-asserts it well inside that window until the turn ends.
_STATUS_REFRESH_SECONDS = 50
# Floor between two status pushes. The agent can fire several tools a second and
# these calls are rate-limited, so bursts coalesce onto the most recent one.
_STATUS_MIN_INTERVAL = 1.5
# Longest a path/pattern may be inside a progress line before it's shortened.
_PROGRESS_MAX_DETAIL = 40

# Each in-flight turn blocks a Bolt listener thread (it waits on the agent loop),
# so size the pool above MAX_CONCURRENT — otherwise the pool, not the semaphore,
# becomes the limiter and quick rejections get starved while turns are running.
app = App(
    token=config.SLACK_BOT_TOKEN,
    listener_executor=ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT + 4),
)

# Generous ceiling for a single turn (the analysis sandbox has its own timeout).
_TURN_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "120")) + 600

# Aspen's own Slack user ID, resolved once via auth.test and cached. Used to skip
# the bot itself when checking who is present in a group DM.
_bot_uid_cache: str | None = None


def _bot_user_id(client) -> str:
    global _bot_uid_cache
    if _bot_uid_cache is None:
        _bot_uid_cache = client.auth_test()["user_id"]
    return _bot_uid_cache


def _admin_mention() -> str:
    """A Slack mention for the admin (``<@U…>``), or a generic phrase if unset.

    Rendered in Slack as a clickable @-mention that pings the admin, so users in a
    shared room can reach out to be added.
    """
    return f"<@{config.ADMIN_USER_ID}>" if config.ADMIN_USER_ID else "an Aspen admin"


def _find_member_id_steps() -> str:
    """How to copy your own Slack member ID, to send to the admin.

    Kept as a shared fragment so the "not authorized" refusal and the group-DM
    participant-gate message give identical, correct instructions. Mirrors the
    README's "Requesting access" section.
    """
    return (
        "To find your Slack member ID: click your name or profile picture, choose "
        "*View full profile*, then the *⋮ More* button → *Copy member ID* "
        "(it looks like `U01AB2CD3EF`)."
    )


def _request_access(uid: str, client) -> bool:
    """Queue an access request for the admin and DM them. Never raises.

    Returns whether the admin was actually reached, so the refusal can promise
    only what happened: told, or told-them-yourself. Failing quietly and claiming
    someone was notified would leave a person waiting on a message nobody got.
    """
    try:
        display = ""
        try:
            info = client.users_info(user=uid)["user"]
            profile = info.get("profile", {})
            display = (profile.get("display_name") or profile.get("real_name")
                       or info.get("real_name") or info.get("name") or "")
        except Exception:
            log.debug("users_info failed for %s; queueing without a name", uid, exc_info=True)
        entry = pending.record("access", uid, display_name=display)
        return pending.notify_admin(client, entry) or bool(entry.get("notified"))
    except Exception:
        log.warning("Could not queue an access request for %s", uid, exc_info=True)
        return False


def _is_group_dm(event: dict, client, channel: str) -> bool:
    """True only for multi-person DMs (``mpim``).

    ``message`` events carry ``channel_type`` directly; ``app_mention`` events do
    not, so fall back to ``conversations.info`` (readable for mpims via the
    ``mpim:read`` scope). Any failure → treat as not-a-group-DM and fall through to
    the existing per-mentioner behavior.
    """
    ctype = event.get("channel_type")
    if ctype:
        return ctype == "mpim"
    try:
        return bool(client.conversations_info(channel=channel)["channel"].get("is_mpim"))
    except Exception:
        log.debug("conversations_info failed for %s; treating as non-mpim", channel, exc_info=True)
        return False


def _unauthorized_group_members(client, channel: str) -> list[str]:
    """Display names of *human* members of a group DM not on the allowlist.

    Raises on Slack API failure so the caller can fail closed (decline rather than
    answer in a room it can't vet). Only non-allowlisted members are resolved via
    ``users.info`` (needs ``users:read``); bots/apps among them are skipped — only
    humans must be on the allowlist. Group DMs cap at ~9 members, so no pagination.
    """
    members = client.conversations_members(channel=channel).get("members", [])
    bot_uid = _bot_user_id(client)
    outsiders: list[str] = []
    for m in members:
        if m == bot_uid or m in config.ALLOWED_USER_IDS:
            continue
        try:
            info = client.users_info(user=m)["user"]
        except Exception:
            info = {}
        if info.get("is_bot"):
            continue  # other apps/bots needn't be on the human allowlist
        prof = info.get("profile", {})
        outsiders.append(prof.get("display_name") or prof.get("real_name") or m)
    return outsiders


def _shorten(value, limit: int = _PROGRESS_MAX_DETAIL) -> str:
    """A path/pattern trimmed to fit a one-line progress indicator."""
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    tail = text.rsplit("/", 1)[-1]          # a bare filename usually says enough
    return tail if len(tail) <= limit else "…" + text[-(limit - 1):]


def _progress_phrase(tool_name: str, tool_input: dict) -> str:
    """A short gerund phrase for the tool the agent just invoked.

    Rendered as "Aspen is <phrase>" in the thread status, so it must read as a
    continuation of "is". Falls back to a generic phrase for anything unmapped —
    a new tool must never produce a broken indicator.
    """
    inp = tool_input or {}
    name = tool_name.removeprefix("mcp__aspen__")

    if name == "list_directory":
        target = _shorten(inp.get("path")) or "the calculations"
        return f"browsing {target}…"
    if name == "read_file":
        return f"reading {_shorten(inp.get('path')) or 'a file'}…"
    if name == "search_files":
        pattern = _shorten(inp.get("pattern") or inp.get("query"))
        return f"searching for “{pattern}”…" if pattern else "searching the calculations…"
    if name == "run_python_analysis":
        return "running analysis code…"
    if name == "attach_file":
        return f"attaching {_shorten(inp.get('path')) or 'a file'}…"
    if name == "write_metadata":
        return "updating metadata.md…"
    if tool_name == "Bash":
        command = str(inp.get("command", "")).strip()
        verb = command.split()[0] if command else ""
        return f"running {verb}…" if verb else "checking the cluster…"
    return "working…"


class _Progress:
    """Live "what Aspen is doing right now" indicator for one turn.

    Preferred channel is the native ``assistant.threads.setStatus``, which Slack
    renders as "Aspen is …" beneath the thread compose box — no message clutter.
    That only applies to *assistant* threads, so for a channel @-mention (or an app
    without the scope / assistant feature) the first call raises and we fall back to
    posting a ``_Thinking…_`` message and editing it in place instead.

    ``update()`` is called from the agent's event loop, so it only stamps an
    in-memory phrase and pokes a daemon thread. That thread owns *every* Slack call,
    which keeps the pushes serialized and rate-limit-friendly.
    """

    def __init__(self, client, channel: str, thread_ts: str, say):
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._status = _STATUS_TEXT          # current phrase, as an "is …" clause
        self._msg_ts = None                  # set only in fallback (message) mode
        self._mode = "status"                # "status" | "message" | "none"
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread = None

        try:
            self._set_status(_STATUS_TEXT)
        except Exception:
            log.debug("setStatus unavailable; falling back to a Thinking message",
                      exc_info=True)
            self._msg_ts = self._post_placeholder(say)
            # Without a ts there's nothing to edit — the turn keeps the single
            # static "_Thinking…_" post (the pre-existing behavior) and we stay quiet.
            self._mode = "message" if self._msg_ts else "none"
            if self._mode == "none":
                return
        self._thread = threading.Thread(target=self._run, name="aspen-status", daemon=True)
        self._thread.start()

    # --- Slack calls (only ever from the indicator thread, or __init__) ------ #
    def _set_status(self, status: str) -> None:
        self._client.assistant_threads_setStatus(
            channel_id=self._channel, thread_ts=self._thread_ts, status=status
        )

    def _post_placeholder(self, say):
        """Post the fallback message and return its ``ts`` (None if unavailable)."""
        try:
            return (say(text="_Thinking…_", thread_ts=self._thread_ts) or {}).get("ts")
        except Exception:
            log.debug("Could not post the fallback progress message", exc_info=True)
            return None

    def _push(self, status: str) -> None:
        if self._mode == "status":
            self._set_status(status)
        else:
            # Standalone message: no app-name prefix from Slack, so drop the "is ".
            phrase = status.removeprefix("is ")
            self._client.chat_update(
                channel=self._channel, ts=self._msg_ts,
                text=f"_{phrase[:1].upper()}{phrase[1:]}_",
            )

    # --- indicator thread --------------------------------------------------- #
    def _run(self) -> None:
        last_push = time.monotonic()
        while True:
            woken = self._wake.wait(_STATUS_REFRESH_SECONDS)
            if self._stopped.is_set():
                return
            if woken:
                # Coalesce a burst of tool calls into one push of the latest phrase.
                delay = _STATUS_MIN_INTERVAL - (time.monotonic() - last_push)
                if delay > 0 and self._stopped.wait(delay):
                    return
                self._wake.clear()
            try:
                self._push(self._status)
            except Exception:
                log.debug("Progress update failed; giving up for this turn", exc_info=True)
                return
            last_push = time.monotonic()

    # --- public API --------------------------------------------------------- #
    def update(self, tool_name: str, tool_input: dict) -> None:
        """Record what the agent is doing now (called from the agent loop)."""
        self._status = "is " + _progress_phrase(tool_name, tool_input)
        self._wake.set()

    def stop(self) -> None:
        """End the indicator and clear it, just before the reply is posted."""
        self._stopped.set()
        self._wake.set()                     # unblock the thread immediately
        if self._thread is not None:
            self._thread.join(timeout=1)
        try:
            if self._mode == "status":
                # Posting the reply auto-clears the status, but clear it explicitly
                # first so nothing can re-assert it in the gap before the reply lands.
                self._set_status("")
            elif self._mode == "message":
                # Remove the placeholder so a stale "Reading foo.out…" doesn't sit
                # above the answer. If we can't delete it, leaving it is harmless.
                self._client.chat_delete(channel=self._channel, ts=self._msg_ts)
        except Exception:
            log.debug("Clearing the progress indicator failed", exc_info=True)


def _start_typing_status(client, channel: str, thread_ts: str, say) -> _Progress:
    """Start the live progress indicator for a turn (see ``_Progress``)."""
    return _Progress(client, channel, thread_ts, say)


def _clean_text(event: dict, strip_mention: bool) -> str:
    """The user's message with any @-mentions removed."""
    raw = event.get("text", "")
    return re.sub(r"<@[A-Z0-9]+>", "", raw).strip() if strip_mention else raw.strip()


def _handle_event(event: dict, say, client, strip_mention: bool) -> None:
    """Shared dispatch logic for both channel mentions and DMs."""
    uid       = event.get("user", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel   = event.get("channel", "")
    started   = time.monotonic()

    def _record(outcome: str, **extra) -> None:
        """One telemetry line for this turn, whichever way it ends.

        Every exit path goes through here — the refusals as much as the answers,
        since a rate limit or an allowlist gate is demand Aspen is turning away.
        """
        telemetry.record(
            uid=uid,
            outcome=outcome,
            channel=event.get("channel_type", ""),
            thread=sessions._thread_key(event),
            latency_ms=int((time.monotonic() - started) * 1000),
            **extra,
        )

    # 1. Allowlist check — first gate (the mentioner must be allowlisted)
    if uid not in config.ALLOWED_USER_IDS:
        _record("not_authorized")
        # Being turned away IS the request. Nothing here grants anything — the
        # admin still has to run a command — but they now get told, with the
        # command in hand, instead of the user having to work out who to ask.
        told = _request_access(uid, client)
        say(
            text=(
                "Sorry, you're not authorized to use Aspen. "
                + (f"I've let {_admin_mention()} know you'd like access — they'll "
                   "need to approve it.\n\n"
                   if told else
                   f"To request access, send your Slack member ID to {_admin_mention()} "
                   f"and ask to be added to the approved-users list.\n\n"
                   f"{_find_member_id_steps()}")
            ),
            thread_ts=thread_ts,
        )
        return

    # 1b. Participant gate — in a group DM, *every* human member must be
    # allowlisted, not just the mentioner. This keeps Aspen's answers and the
    # thread context it reads out of any room containing an unapproved person.
    # Fail closed: if membership can't be verified, decline rather than answer.
    if _is_group_dm(event, client, channel):
        try:
            outsiders = _unauthorized_group_members(client, channel)
        except Exception:
            log.exception("Could not verify group-DM membership for %s", channel)
            _record("group_gate")
            say(
                text=(
                    "I couldn't verify everyone in this group, so I'm staying out to be "
                    f"safe. Approved users can DM me directly, or contact {_admin_mention()}."
                ),
                thread_ts=thread_ts,
            )
            return
        if outsiders:
            _record("group_gate")
            names = ", ".join(f"*{n}*" for n in outsiders)
            say(
                text=(
                    "I can only work in a group where everyone is on my approved-users "
                    f"list. These members aren't yet: {names}. To be added, each of them can "
                    f"send their Slack member ID to {_admin_mention()}. {_find_member_id_steps()} "
                    "In the meantime, any approved user can DM me directly with questions."
                ),
                thread_ts=thread_ts,
            )
            return

    # 2. Per-user rate limit + concurrency check
    err = ratelimit._check_rate_limit(uid)
    if err:
        _record("rate_limited", text=_clean_text(event, strip_mention))
        say(text=err, thread_ts=thread_ts)
        return

    # 3. Global concurrency cap
    if not state._global_sem.acquire(blocking=False):
        ratelimit._release_user(uid)
        _record("busy", text=_clean_text(event, strip_mention))
        say(text="Aspen is busy right now — please try again in a moment.", thread_ts=thread_ts)
        return

    try:
        user_message = _clean_text(event, strip_mention)

        if not user_message:
            _record("empty")
            say(text="Hi! Ask me anything about the calculations.", thread_ts=thread_ts)
            return

        progress = _start_typing_status(client, channel, thread_ts, say)

        # Every tool the agent invokes, in call order — the repeated sequences are
        # what tell you which multi-step task deserves a purpose-built tool.
        tools_used: list[str] = []

        def _on_progress(tool_name: str, tool_input: dict) -> None:
            tools_used.append(tool_name)
            progress.update(tool_name, tool_input)

        # Time to the agent's first words. A long turn is mostly tool work, so
        # this is the number that moves when the agent starts narrating, while
        # latency_ms (time to the answer) barely changes.
        first_output: list[int] = []

        def _stamp_first_output() -> None:
            if not first_output:
                first_output.append(int((time.monotonic() - started) * 1000))

        def _on_interim(text: str) -> None:
            """Post what the agent said before acting, as its own message.

            Called from the agent loop, so it must not block: one Slack post,
            no waiting. Failures propagate to the SDK side, which logs and
            carries on — an unsent aside never costs the answer.
            """
            _stamp_first_output()
            say(thread_ts=thread_ts, **render.slack_reply(text))

        key     = sessions._thread_key(event)
        # ``on_progress`` lets the agent report each tool call mid-turn, so the
        # indicator tracks the actual work instead of a static "is typing…";
        # ``on_interim`` carries the agent's own words out while it works.
        context = {"user_id": uid, "username": registry.display_name(uid),
                   "thread_ts": thread_ts or "",
                   "attachments": [], "on_progress": _on_progress,
                   # So a tool can DM the admin about a request. Nothing the agent
                   # can reach grants anything — the client is here to *ask*.
                   "slack_client": client,
                   # True only on the thread's first turn, which is the one place
                   # a setup nudge is allowed to appear (setup.py). Read before
                   # MANAGER.handle creates the session, so it is still absent.
                   "first_turn": not sessions.MANAGER.has_session(
                       sessions._thread_key(event))}
        if config.INTERIM_UPDATES:
            context["on_interim"] = _on_interim

        # Who is speaking, and what workflows exist, must ride on the *message*:
        # a session is keyed per thread and pre-warmed before the speaker is
        # known, and a group DM has several speakers sharing one session — so
        # this can't live in the system prompt. Cheap (frontmatter only) and
        # bounded by the size of the registry.
        try:
            turn_message = workflows.turn_preamble(
                uid, extra_lines=setup.nudge_lines(uid, context["first_turn"])
            ) + user_message
        except Exception:      # context is an enhancement; never fail a turn for it
            log.exception("Could not build the workflow preamble for %s", uid)
            turn_message = user_message

        outcome = "ok"
        try:
            loop = sessions._ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(
                sessions.MANAGER.handle(key, turn_message, context), loop
            )
            reply, atts = fut.result(timeout=_TURN_TIMEOUT)
        except _FutureTimeout:
            outcome = "timeout"
            log.exception("Turn exceeded %ss for user %s", _TURN_TIMEOUT, uid)
            reply, atts = "Sorry, something went wrong on my end. Please try again.", []
        except Exception:
            outcome = "error"
            log.exception("Unexpected error for user %s", uid)
            reply, atts = "Sorry, something went wrong on my end. Please try again.", []
        finally:
            progress.stop()

        if reply:
            _stamp_first_output()

        # Recorded before the reply is posted, so the latency measured is the
        # agent's — the number worth optimizing — and a failing say() can't cost
        # us the record of a turn that actually ran.
        _record(outcome, text=user_message, tools=tools_used,
                first_output_ms=first_output[0] if first_output else None,
                reply_chars=len(reply or ""), attachments=len(atts),
                meta=context.get("result_meta"))

        # An empty reply means the agent already said everything as it worked
        # (see agent._format_result_reply) — posting "" would be a blank message.
        if reply.strip():
            # Slack's text field speaks mrkdwn, not the GFM the agent emits; send
            # the reply through a markdown block so Slack renders it.
            say(thread_ts=thread_ts, **render.slack_reply(reply))

        if atts:
            attachments._upload_attachments(atts, client, channel, thread_ts)

    finally:
        ratelimit._release_user(uid)
        state._global_sem.release()


@app.event("app_mention")
def handle_mention(event: dict, say, client) -> None:
    """Respond to @Aspen mentions in channels."""
    _handle_event(event, say, client, strip_mention=True)


@app.event("message")
def handle_message(event: dict, say, client) -> None:
    """Handle non-mention messages: 1:1 DMs, and follow-ups in group-DM threads.

    Both ``message.im`` and ``message.mpim`` are delivered here (see the app
    manifest). Ignore bot messages and subtypes (edits, deletions, etc.) in either.
    """
    if event.get("subtype") or event.get("bot_id"):
        return

    ctype = event.get("channel_type")

    # 1:1 DM: every message is for Aspen — no @-mention needed, ever.
    if ctype == "im":
        _handle_event(event, say, client, strip_mention=False)
        return

    # Group DM: only *continue* a thread Aspen already joined. A mention starts a
    # thread (via app_mention); after that, plain replies in that thread reach it
    # too. Everything else in a group DM still requires an @-mention.
    if ctype == "mpim":
        # A mention also arrives here as a message event — let app_mention own it,
        # so the turn isn't handled twice.
        if f"<@{_bot_user_id(client)}>" in event.get("text", ""):
            return
        # Only true thread replies, and only for a thread with a live session.
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return
        if not sessions.MANAGER.has_session(sessions._thread_key(event)):
            return
        _handle_event(event, say, client, strip_mention=True)
