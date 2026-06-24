"""Entry point: start the agent loop and the Slack Socket Mode handler."""

import logging

from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import config, sessions, slack_app

log = logging.getLogger("aspen")


def main() -> None:
    sessions._ensure_loop()
    log.info(
        "Starting Aspen (Claude Agent SDK)  model=%s  calculations_root=%s",
        config.MODEL, config.CALCULATIONS_ROOT,
    )
    SocketModeHandler(slack_app.app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
