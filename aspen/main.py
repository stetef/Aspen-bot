"""Entry point: start the agent loop and the Slack Socket Mode handler."""

import logging
import os
import threading
from pathlib import Path

from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import config, registry, roots, sessions, slack_app, telemetry

log = logging.getLogger("aspen")


def _under(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def _check_state_locations() -> None:
    """Fail loudly if the registry, the workflows tree, or the telemetry log sits
    somewhere the agent can write by other means.

    Admission and workflow ownership are enforced in Python, but that only holds
    if the files themselves aren't reachable through a writable path. The obvious
    footgun is putting them under WORKSPACE_ROOT (the analysis sandbox's writable
    area) or inside ASPEN_SANDBOX_WRITE_PATHS — either would let generated code
    edit the allowlist or another user's workflow directly. The telemetry log and
    its switch are held to the same rule: a record of what the agent did is worth
    little if the agent can rewrite it, or quietly stop it being written.
    """
    danger = [config.WORKSPACE_ROOT] + [Path(p).expanduser() for p in config.SANDBOX_WRITE_PATHS]
    for label, target in (("registry", config.USERS_FILE),
                          ("workflows root", config.WORKFLOWS_ROOT),
                          ("metadata root", config.METADATA_ROOT),
                          ("metadata history", config.METADATA_HISTORY_ROOT),
                          ("request queue", config.REQUESTS_FILE),
                          ("telemetry log", config.TELEMETRY_DIR),
                          ("telemetry switch", config.TELEMETRY_STATE_FILE)):
        for area in danger:
            if _under(target, area):
                raise SystemExit(
                    f"FATAL: the {label} ({target}) is inside a sandbox-writable area "
                    f"({area}). Sandboxed code could then edit it directly, bypassing the "
                    "ownership and admission checks. Move it — set ASPEN_STATE_DIR (or "
                    "ASPEN_USERS_FILE / ASPEN_WORKFLOWS_ROOT / ASPEN_TELEMETRY_DIR) to a "
                    "path outside WORKSPACE_ROOT and ASPEN_SANDBOX_WRITE_PATHS."
                )

    # 0700 against other users on the shared login node. The CLI does the same at
    # its own creation points, since it usually runs before the bot ever starts.
    registry.ensure_private_dir(config.STATE_DIR)
    registry.ensure_private_dir(config.WORKFLOWS_ROOT)
    if config.TELEMETRY_ENABLED:
        registry.ensure_private_dir(config.TELEMETRY_DIR)

    _check_calculations_roots()

    users = registry.users()
    if not users:
        log.warning(
            "No users registered and no ASPEN_ALLOWED_SLACK_USER_IDS bootstrap — "
            "Aspen will refuse everyone. Add someone with: ./aspen-users add <SLACK_ID>"
        )
    elif registry.load().get("source") == "env":
        log.warning(
            "Using the ASPEN_ALLOWED_SLACK_USER_IDS bootstrap (%d user(s)); no registry "
            "at %s yet. Run ./aspen-users add to create it.", len(users), config.USERS_FILE
        )
    else:
        log.info("User registry: %d active user(s) from %s", len(users), config.USERS_FILE)

    # Say plainly at every boot what is being recorded — a collection window that
    # nobody can see is one nobody remembers to close.
    tele = telemetry.effective()
    if not tele["metrics"]:
        log.info("Telemetry: off (%s)", tele.get("off_reason") or "switched off")
    else:
        detail = "metrics + question text" if tele["content"] else "metrics only"
        if tele["content"] and tele["content_until"]:
            detail += f" until {tele['content_until']}"
        elif not tele["content"] and tele.get("off_reason"):
            detail += f" ({tele['off_reason']})"
        if tele["excluded_users"]:
            detail += f"; {len(tele['excluded_users'])} user(s) excluded from text"
        log.info("Telemetry: %s -> %s", detail, config.TELEMETRY_DIR)


def _check_calculations_roots() -> None:
    """Report the configured roots, and refuse to start on a broken one.

    Two failures matter enough to be fatal rather than a turn-time surprise:

    * **Nesting.** ``roots.resolve`` fences a path by containment, so a root
      inside another root does not bound anything — one person's fence would
      silently enclose someone else's tree.
    * **Unreadable.** A root the operator can see but the bot's own account
      cannot is a root that fails on somebody's question instead of at boot.
      This is the check that will bite at the §18.1 service-account cutover,
      which is exactly when it should.
    """
    problems = roots.check_all()
    if problems:
        raise SystemExit(
            "FATAL: calculations roots are misconfigured:\n  "
            + "\n  ".join(problems)
            + "\n\nFix them with `aspen-users set-root <who> <path>` (or "
              "ASPEN_SHARED_CALC_ROOTS in .env). Roots must exist, be readable by "
              "the account Aspen runs as, and never contain one another."
        )

    scopes = roots.scopes()
    personal = [s for s in scopes if s["kind"] == "user" and s["path"] != config.CALCULATIONS_ROOT]
    shared = [s for s in scopes if s["kind"] == "shared"]
    if not personal and not shared:
        log.info("Calculations: one root for everyone — %s", config.CALCULATIONS_ROOT)
    else:
        log.info(
            "Calculations: %d personal root(s), %d shared, default %s",
            len(personal), len(shared), config.CALCULATIONS_ROOT,
        )
        for scope in personal + shared:
            log.info("  @%-20s %s", scope["name"], scope["path"])


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
    _check_state_locations()
    loop = sessions._ensure_loop()
    log.info(
        "Starting Aspen (Claude Agent SDK)  model=%s  calculations_root=%s  workflows=%s",
        config.MODEL, config.CALCULATIONS_ROOT, config.WORKFLOWS_ROOT,
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
