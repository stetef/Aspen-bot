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

    # 1. Allowlist check — first gate
    if uid not in config.ALLOWED_USER_IDS:
        say(text="Sorry, you're not authorized to use Aspen.", thread_ts=thread_ts)
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
def handle_dm(event: dict, say, client) -> None:
    """Respond to direct messages sent to the bot."""
    # Only handle DMs; ignore bot messages and message subtypes (edits, deletions, etc.)
    if event.get("channel_type") != "im":
        return
    if event.get("subtype") or event.get("bot_id"):
        return
    _handle_event(event, say, client, strip_mention=False)
