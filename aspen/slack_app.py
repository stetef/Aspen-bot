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

from slack_bolt import App

from . import config, figures, ratelimit, sessions, state

log = logging.getLogger("aspen")

app = App(token=config.SLACK_BOT_TOKEN)

# Generous ceiling for a single turn (the analysis sandbox has its own timeout).
_TURN_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "120")) + 600


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

        say(text="_Thinking…_", thread_ts=thread_ts)

        key     = sessions._thread_key(event)
        context = {"user_id": uid, "username": "", "thread_ts": thread_ts or "", "figures": []}

        try:
            loop = sessions._ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(
                sessions.MANAGER.handle(key, user_message, context), loop
            )
            reply, figs = fut.result(timeout=_TURN_TIMEOUT)
        except Exception:
            log.exception("Unexpected error for user %s", uid)
            reply, figs = "Sorry, something went wrong on my end. Please try again.", []

        say(text=reply, thread_ts=thread_ts)

        if figs:
            figures._upload_figures(figs, client, channel, thread_ts)

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
