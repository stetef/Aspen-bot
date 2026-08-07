"""Entry point: start the agent loop and the Slack Socket Mode handler."""

import logging
import threading

from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import config, sessions, slack_app

log = logging.getLogger("aspen")


def _warm_sdk_import() -> None:
    """Import ``claude_agent_sdk`` now, off the critical path.

    ``agent.py`` imports the SDK lazily so the package stays importable (and
    testable) without it. That defers ~4 s of import — mostly ``mcp`` /
    ``pydantic`` module loading, worse on a network filesystem — into the *first*
    user's turn. Doing it here at boot lands it in ``sys.modules``, so the lazy
    imports later are free. Failures are ignored: the lazy import will raise the
    real error at turn time, with the existing handling around it.
    """
    try:
        import claude_agent_sdk  # noqa: F401
    except Exception:
        log.debug("SDK pre-import failed; the lazy import will report it", exc_info=True)


def main() -> None:
    loop = sessions._ensure_loop()
    log.info(
        "Starting Aspen (Claude Agent SDK)  model=%s  calculations_root=%s",
        config.MODEL, config.CALCULATIONS_ROOT,
    )

    def _warm() -> None:
        _warm_sdk_import()
        # Only now can the pool connect anything — warming schedules work on the
        # agent loop, which would otherwise block on the same import.
        loop.call_soon_threadsafe(sessions.MANAGER.warm)

    threading.Thread(target=_warm, name="aspen-warm", daemon=True).start()

    SocketModeHandler(slack_app.app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
