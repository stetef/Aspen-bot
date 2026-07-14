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
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App

from . import attachments, config, ratelimit, render, sessions, state

log = logging.getLogger("aspen")

# Native Slack "working" indicator for AI apps (assistant.threads.setStatus).
# Slack renders it as "<App Name> <status>", so with the bot named "Aspen" this
# shows up as "Aspen is typing…" beneath the thread compose box.
_STATUS_TEXT = "is typing…"
# Slack expires a status after ~2 minutes, but Aspen turns routinely run longer,
# so a background thread re-asserts it well inside that window until the turn ends.
_STATUS_REFRESH_SECONDS = 50

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


def _start_typing_status(client, channel: str, thread_ts: str, say):
    """Show a native "Aspen is typing…" status for the duration of a turn.

    Calls ``assistant.threads.setStatus`` and keeps it alive on a daemon thread
    (the status expires after ~2 minutes; turns can run longer). Returns a
    ``stop()`` callable that ends the refresher and clears the status.

    The status only applies to *assistant* threads. For a channel @-mention — or
    if the app lacks the scope / assistant feature — the first call raises, and we
    fall back to posting the old ``_Thinking…_`` message so the user still sees
    that Aspen is working. In that case ``stop()`` is a no-op.
    """
    def _set(status: str) -> None:
        client.assistant_threads_setStatus(
            channel_id=channel, thread_ts=thread_ts, status=status
        )

    try:
        _set(_STATUS_TEXT)
    except Exception:
        log.debug("setStatus unavailable; falling back to a Thinking message", exc_info=True)
        say(text="_Thinking…_", thread_ts=thread_ts)
        return lambda: None

    stop_event = threading.Event()

    def _refresh() -> None:
        while not stop_event.wait(_STATUS_REFRESH_SECONDS):
            try:
                _set(_STATUS_TEXT)
            except Exception:
                log.debug("setStatus refresh failed; giving up", exc_info=True)
                return

    refresher = threading.Thread(target=_refresh, name="aspen-status", daemon=True)
    refresher.start()

    def _stop() -> None:
        stop_event.set()
        refresher.join(timeout=1)
        # Posting the reply auto-clears the status, but clear it explicitly first
        # so the heartbeat can't re-assert it in the gap before the reply lands.
        try:
            _set("")
        except Exception:
            log.debug("setStatus clear failed", exc_info=True)

    return _stop


def _handle_event(event: dict, say, client, strip_mention: bool) -> None:
    """Shared dispatch logic for both channel mentions and DMs."""
    uid       = event.get("user", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel   = event.get("channel", "")

    # 1. Allowlist check — first gate (the mentioner must be allowlisted)
    if uid not in config.ALLOWED_USER_IDS:
        say(
            text=(
                f"Sorry, you're not authorized to use Aspen. To request access, send your "
                f"Slack member ID to {_admin_mention()} and ask to be added to the "
                f"approved-users list.\n\n{_find_member_id_steps()}"
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
            say(
                text=(
                    "I couldn't verify everyone in this group, so I'm staying out to be "
                    f"safe. Approved users can DM me directly, or contact {_admin_mention()}."
                ),
                thread_ts=thread_ts,
            )
            return
        if outsiders:
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
        say(text=err, thread_ts=thread_ts)
        return

    # 3. Global concurrency cap
    if not state._global_sem.acquire(blocking=False):
        ratelimit._release_user(uid)
        say(text="Aspen is busy right now — please try again in a moment.", thread_ts=thread_ts)
        return

    try:
        raw          = event.get("text", "")
        user_message = re.sub(r"<@[A-Z0-9]+>", "", raw).strip() if strip_mention else raw.strip()

        if not user_message:
            say(text="Hi! Ask me anything about the calculations.", thread_ts=thread_ts)
            return

        stop_status = _start_typing_status(client, channel, thread_ts, say)

        key     = sessions._thread_key(event)
        context = {"user_id": uid, "username": "", "thread_ts": thread_ts or "", "attachments": []}

        try:
            loop = sessions._ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(
                sessions.MANAGER.handle(key, user_message, context), loop
            )
            reply, atts = fut.result(timeout=_TURN_TIMEOUT)
        except Exception:
            log.exception("Unexpected error for user %s", uid)
            reply, atts = "Sorry, something went wrong on my end. Please try again.", []
        finally:
            stop_status()

        # Slack's text field speaks mrkdwn, not the GFM the agent emits; send the
        # reply through a markdown block so Slack renders it (render.slack_reply).
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
