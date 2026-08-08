"""
``aspen-users`` — the admin CLI for Aspen's user registry.

This is the ONLY thing that writes ``config.USERS_FILE``. Admission is kept off
the agent entirely — there is no Slack command and no tool that adds or removes a
user — so no message, however phrased, can widen the allowlist. Changes take
effect on the affected user's next message (the bot hot-reloads the registry),
with no restart.

    python -m aspen.users_cli list
    python -m aspen.users_cli add U01ARUN --alias arun --name "Arun N."
    python -m aspen.users_cli rename arun --to arun-n
    python -m aspen.users_cli remove arun
    python -m aspen.users_cli sync --apply
    python -m aspen.users_cli whois arun

It also owns the turn-log switch (``aspen/telemetry.py``), which is admin-only for
the same reason admission is — the agent must not be able to stop the record of
what it did:

    python -m aspen.users_cli telemetry status
    python -m aspen.users_cli telemetry content on --days 30
    python -m aspen.users_cli telemetry exclude arun

``./aspen-users`` is a thin wrapper that activates the venv and calls this.
"""

import argparse
import getpass
import sys
from datetime import date, timedelta

from . import config, registry, telemetry, workflows


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _actor(args) -> str:
    return getattr(args, "by", "") or f"cli:{getpass.getuser()}"


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _slack_client():
    from slack_sdk import WebClient
    return WebClient(token=config.SLACK_BOT_TOKEN)


def _migrate_bootstrap(quiet: bool = False) -> bool:
    """Turn the ``ASPEN_ALLOWED_SLACK_USER_IDS`` bootstrap into a real registry.

    Any command that writes has to do this first. Without it, the first ``add``
    would either drop the bootstrap users (a silent mass-revocation) or carry them
    over with ID-derived junk aliases like ``u0a3mh52sn4``. So we migrate
    deliberately and loudly, resolving real names from Slack where we can — access
    is unchanged, which is the safe direction.
    """
    if registry.load().get("source") == "file":
        return False                        # a real registry already exists

    ids = list(config.BOOTSTRAP_USER_IDS)
    if not ids:
        return False

    if not quiet:
        print(f"No registry yet — migrating {len(ids)} user(s) from "
              "ASPEN_ALLOWED_SLACK_USER_IDS. Access is unchanged.")
    entries, taken = [], set()
    for i, uid in enumerate(ids):
        if registry.validate_user_id(uid):
            print(f"  skipping {uid!r}: not a Slack member ID")
            continue
        name, failure = _lookup_slack_profile(uid)
        alias = registry.slugify(name) or uid.lower()
        if registry.validate_alias(alias) or alias in taken:
            alias = uid.lower()
        taken.add(alias)
        # The first bootstrap ID was historically treated as the admin.
        entries.append(registry.new_user(
            uid, alias, name or uid, role="admin" if i == 0 else "member",
            added_by="bootstrap-migration",
        ))
        if not quiet:
            note = f"  {alias:<16} {uid}"
            print(note + (f"   (no Slack name: {failure.splitlines()[0][:60]})" if failure else f"   {name}"))
    if not entries:
        return False
    registry.save(entries)
    if not quiet:
        print("Fix any placeholder aliases with `aspen-users rename <alias> --to <new>`,\n"
              "or refresh names later with `aspen-users sync --apply`.\n")
    return True


def _lookup_slack_profile(uid: str) -> tuple[str, str]:
    """(display_name, error) for a Slack user ID."""
    try:
        user = _slack_client().users_info(user=uid)["user"]
    except Exception as exc:
        return "", str(exc)
    profile = user.get("profile", {})
    name = (profile.get("display_name") or profile.get("real_name")
            or user.get("real_name") or user.get("name") or "")
    return name, ""


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_list(args) -> int:
    users = registry.users(include_removed=args.all)
    if not users:
        print(f"No users registered. Registry: {config.USERS_FILE}")
        print("Add one with:  aspen-users add U01ABC2DEF --alias arun")
        return 0

    rows = []
    for u in users:
        uid = u["slack_user_id"]
        flags = []
        if u["role"] == "admin":
            flags.append("admin")
        if u["status"] != "active":
            flags.append(u["status"])
        if workflows.has_workflow(uid):
            flags.append("workflow")
        rows.append((u["alias"], uid, u["display_name"], ", ".join(flags) or "-"))

    widths = [max(len(r[i]) for r in [("ALIAS", "SLACK ID", "NAME", "FLAGS")] + rows)
              for i in range(4)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format("ALIAS", "SLACK ID", "NAME", "FLAGS"))
    for row in rows:
        print(fmt.format(*row))
    print(f"\n{len(rows)} user(s). Registry: {config.USERS_FILE}")
    if registry.load().get("source") == "env":
        print("NOTE: no registry file yet — this is the ASPEN_ALLOWED_SLACK_USER_IDS "
              "bootstrap. Run `aspen-users add …` to create the real registry.")
    return 0


def cmd_add(args) -> int:
    _migrate_bootstrap()
    uid = args.slack_user_id.strip().lstrip("@")
    problem = registry.validate_user_id(uid)
    if problem:
        return _err(problem)

    existing = registry.by_id(uid)
    if existing and existing["status"] == "active":
        return _err(f"{uid} is already registered as '{existing['alias']}'")

    name = args.name or ""
    if not name:
        name, failure = _lookup_slack_profile(uid)
        if failure:
            print(f"warning: could not reach Slack for a display name ({failure})")
    alias = (args.alias or registry.slugify(name) or uid.lower()).strip().lower()
    problem = registry.validate_alias(alias)
    if problem:
        return _err(f"{problem} — pass --alias explicitly")

    clash = registry.by_alias(alias)
    if clash and clash["slack_user_id"] != uid:
        return _err(f"alias '{alias}' is already used by {clash['slack_user_id']} "
                    "— pick another with --alias")

    users = registry.users(include_removed=True)
    entry = registry.new_user(uid, alias, name or alias, role=args.role,
                              added_by=_actor(args), notes=args.notes or "")
    if existing:                       # reinstating a previously removed user
        users = [entry if u["slack_user_id"] == uid else u for u in users]
    else:
        users.append(entry)
    registry.save(users)

    print(f"Added {entry['display_name']} as '{alias}' ({uid}, role={entry['role']}).")
    restored = workflows.dir_for(uid, include_archived=True)
    if restored and workflows.ARCHIVE_DIR in restored.parts:
        print(f"note: an archived workflow exists at {restored} — move it back "
              f"to {config.WORKFLOWS_ROOT} to restore it.")
    print("They can use Aspen on their next message (no restart needed).")
    return 0


def cmd_rename(args) -> int:
    _migrate_bootstrap()
    user = registry.resolve(args.who)
    if user is None:
        return _err(f"no user matches '{args.who}'")
    new_alias = args.to.strip().lower()
    problem = registry.validate_alias(new_alias)
    if problem:
        return _err(problem)
    clash = registry.by_alias(new_alias)
    if clash and clash["slack_user_id"] != user["slack_user_id"]:
        return _err(f"alias '{new_alias}' is already used by {clash['slack_user_id']}")

    old_alias = user["alias"]
    users = [dict(u, alias=new_alias) if u["slack_user_id"] == user["slack_user_id"] else u
             for u in registry.users(include_removed=True)]
    registry.save(users)
    # Folder names track the alias; lookups go by Slack ID, so this is cosmetic
    # and safe to do (or skip) at any time.
    moved = workflows.ensure_dir(user["slack_user_id"])
    print(f"Renamed '{old_alias}' -> '{new_alias}' ({user['slack_user_id']}).")
    if moved:
        print(f"Workflow directory is now {moved}")
    return 0


def cmd_remove(args) -> int:
    _migrate_bootstrap()
    user = registry.resolve(args.who, include_removed=False)
    if user is None:
        return _err(f"no active user matches '{args.who}'")
    uid = user["slack_user_id"]
    if uid == config.ADMIN_USER_ID and not args.force:
        return _err("that user is Aspen's admin — pass --force if you really mean it")

    has_wf = workflows.has_workflow(uid)
    fate = ("DELETE their workflow permanently" if args.purge
            else ("archive their workflow" if has_wf else "no workflow to keep"))
    print(f"About to revoke access for {user['display_name']} ('{user['alias']}', {uid}) and {fate}.")
    if not _confirm("Proceed?", args.yes):
        print("Aborted.")
        return 1

    users = [
        dict(u, status="removed", removed=date.today().isoformat(), removed_by=_actor(args))
        if u["slack_user_id"] == uid else u
        for u in registry.users(include_removed=True)
    ]
    registry.save(users)
    print(f"Access revoked — {user['alias']} is blocked from their next message on.")

    if args.purge:
        if workflows.purge(uid):
            print("Workflow deleted.")
        if args.purge_history:
            import shutil
            hist = config.WORKSPACE_ROOT / "workflow_history" / uid
            if hist.is_dir():
                shutil.rmtree(hist)
                print("Workflow history deleted.")
        else:
            print(f"Backups remain in {config.WORKSPACE_ROOT / 'workflow_history' / uid} "
                  "(use --purge-history to remove them too).")
    elif has_wf:
        dest = workflows.archive(uid)
        print(f"Workflow archived to {dest} — still readable as reference-only.")
    return 0


def cmd_sync(args) -> int:
    """Refresh display names from Slack and report alias drift."""
    changes, users = [], registry.users(include_removed=True)
    updated = []
    for u in users:
        uid = u["slack_user_id"]
        if u["status"] != "active":
            updated.append(u)
            continue
        name, failure = _lookup_slack_profile(uid)
        if failure:
            print(f"warning: {uid}: {failure}")
            updated.append(u)
            continue
        if name and name != u["display_name"]:
            changes.append(f"  {u['alias']}: name '{u['display_name']}' -> '{name}'")
            u = dict(u, display_name=name)
        suggested = registry.slugify(name)
        if suggested and suggested != u["alias"] and not registry.validate_alias(suggested):
            print(f"  {u['alias']}: alias no longer matches their name "
                  f"(suggest '{suggested}' — rename with `aspen-users rename {u['alias']} "
                  f"--to {suggested}`)")
        updated.append(u)

    if not changes:
        print("Display names are up to date.")
        return 0
    print("Display-name changes:")
    print("\n".join(changes))
    if not args.apply:
        print("\nDry run — re-run with --apply to write them.")
        return 0
    registry.save(updated)
    print("Applied.")
    return 0


def cmd_init(args) -> int:
    """Create the registry from the bootstrap allowlist, deliberately."""
    if not _migrate_bootstrap():
        if registry.load().get("source") == "file":
            print(f"Registry already exists: {config.USERS_FILE}")
        else:
            print("Nothing to migrate — ASPEN_ALLOWED_SLACK_USER_IDS is empty. "
                  "Add your first user with `aspen-users add <SLACK_ID>`.")
        return 0
    return cmd_list(args)


def _telemetry_summary() -> None:
    """Print what is being recorded right now, and why."""
    state = telemetry.effective()
    print(f"metrics        {'on' if state['metrics'] else 'off'}")
    content = "on" if state["content"] else "off"
    if state["content"] and state["content_until"]:
        content += f" (until {state['content_until']}, inclusive)"
    elif not state["content"] and state.get("off_reason"):
        content += f" ({state['off_reason']})"
    print(f"question text  {content}")

    excluded = state["excluded_users"]
    if excluded:
        print(f"excluded       {', '.join(registry.label(u) for u in excluded)}")
    else:
        print("excluded       (nobody)")
    print(f"log            {config.TELEMETRY_DIR}")
    print(f"settings       {config.TELEMETRY_STATE_FILE} ({state['source']})")
    if state.get("updated"):
        print(f"last change    {state['updated']} by {state.get('updated_by') or 'unknown'}")
    if not config.TELEMETRY_ENABLED:
        print("\nNOTE: ASPEN_TELEMETRY is off in .env — that overrides everything above. "
              "Nothing is being recorded until it is turned back on (needs a restart).")

    files = telemetry.log_files()
    if files:
        print(f"\n{len(files)} daily log(s), {files[0].stem} to {files[-1].stem}.")


def _telemetry_write(args, **changes) -> int:
    """Apply ``changes`` to the stored settings and re-print the summary."""
    current = telemetry.load(force=True)
    telemetry.save({**current, **changes}, actor=_actor(args))
    print("Updated. Takes effect on the next message — no restart needed.\n")
    _telemetry_summary()
    return 0


def cmd_telemetry_status(args) -> int:
    _telemetry_summary()
    return 0


def cmd_telemetry_on(args) -> int:
    return _telemetry_write(args, metrics=True)


def cmd_telemetry_off(args) -> int:
    """Stop recording entirely — no metrics, no text."""
    return _telemetry_write(args, metrics=False, content=False)


def cmd_telemetry_content(args) -> int:
    """Turn question-text collection on (optionally time-boxed) or off."""
    if args.state == "off":
        return _telemetry_write(args, content=False, content_until="")

    until = ""
    if args.days is not None:
        if args.days < 1:
            return _err("--days must be at least 1")
        # UTC, matching how telemetry.py reads the window back (one clock).
        until = (telemetry.today() + timedelta(days=args.days)).isoformat()
    elif args.until:
        try:
            until = date.fromisoformat(args.until).isoformat()
        except ValueError:
            return _err(f"'{args.until}' is not a YYYY-MM-DD date")
        if date.fromisoformat(until) < telemetry.today():
            return _err(f"{until} is in the past — that window is already closed")

    # Metrics have to be on for anything at all to be written.
    return _telemetry_write(args, metrics=True, content=True, content_until=until)


def cmd_telemetry_exclude(args) -> int:
    """Keep a person's metrics but never record their question text."""
    user = registry.resolve(args.who)
    if user is None:
        return _err(f"no user matches '{args.who}'")
    current = telemetry.load(force=True)
    excluded = set(current["excluded_users"]) | {user["slack_user_id"]}
    return _telemetry_write(args, excluded_users=sorted(excluded))


def cmd_telemetry_include(args) -> int:
    user = registry.resolve(args.who)
    uid = user["slack_user_id"] if user else args.who.strip()
    current = telemetry.load(force=True)
    excluded = set(current["excluded_users"])
    if uid not in excluded:
        return _err(f"'{args.who}' is not excluded")
    return _telemetry_write(args, excluded_users=sorted(excluded - {uid}))


def cmd_telemetry_prune(args) -> int:
    removed = telemetry.prune(args.older_than)
    if not removed:
        print(f"Nothing older than {args.older_than} days.")
        return 0
    print(f"Deleted {len(removed)} daily log(s): {', '.join(removed)}")
    return 0


def cmd_whois(args) -> int:
    user = registry.resolve(args.who)
    if user is None:
        return _err(f"no user matches '{args.who}'")
    uid = user["slack_user_id"]
    for field in ("slack_user_id", "alias", "display_name", "role", "status",
                  "added", "added_by", "removed", "removed_by", "notes"):
        if user.get(field):
            print(f"{field:<15} {user[field]}")
    directory = workflows.dir_for(uid, include_archived=True)
    print(f"{'workflow':<15} {directory or '(none)'}")
    if uid == config.ADMIN_USER_ID:
        print(f"{'admin':<15} yes")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aspen-users",
        description="Manage Aspen's user registry (who may talk to the bot, and their alias).",
        epilog="Changes apply on the user's next message — no restart needed.",
    )
    p.add_argument("--by", default="", help="record this actor in added_by/removed_by")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("list", help="list registered users")
    s.add_argument("--all", action="store_true", help="include removed users")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("add", help="register a user (grants access)")
    s.add_argument("slack_user_id", help="Slack member ID, e.g. U01ABC2DEF")
    s.add_argument("--alias", default="", help="kebab-case alias (default: from their name)")
    s.add_argument("--name", default="", help="display name (default: looked up from Slack)")
    s.add_argument("--role", default="member", choices=registry.ROLES)
    s.add_argument("--notes", default="", help="free-text note, e.g. 'beta'")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("rename", help="change a user's alias (and their folder name)")
    s.add_argument("who", help="current alias or Slack ID")
    s.add_argument("--to", required=True, help="new kebab-case alias")
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("remove", help="revoke access; archives their workflow by default")
    s.add_argument("who", help="alias or Slack ID")
    s.add_argument("--purge", action="store_true", help="delete the workflow instead of archiving it")
    s.add_argument("--purge-history", action="store_true", help="with --purge, also delete backups")
    s.add_argument("--force", action="store_true", help="allow removing the admin")
    s.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("sync", help="refresh display names from Slack; report alias drift")
    s.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("init", help="create the registry from the bootstrap allowlist")
    s.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("whois", help="show one user's registry entry")
    s.add_argument("who", help="alias or Slack ID")
    s.set_defaults(func=cmd_whois)

    # --- telemetry ---------------------------------------------------------- #
    # Metrics and question text are switched separately on purpose: metrics stay
    # useful indefinitely, while the text is what you collect for a few weeks to
    # learn what people ask, then stop. See aspen/telemetry.py.
    s = sub.add_parser(
        "telemetry",
        help="what Aspen records about how it is used",
        description="Control Aspen's turn log. Changes apply on the next message.",
    )
    tsub = s.add_subparsers(dest="telemetry_command", required=True)

    t = tsub.add_parser("status", help="show what is being recorded right now")
    t.set_defaults(func=cmd_telemetry_status)

    t = tsub.add_parser("on", help="resume recording metrics")
    t.set_defaults(func=cmd_telemetry_on)

    t = tsub.add_parser("off", help="stop recording entirely (metrics and text)")
    t.set_defaults(func=cmd_telemetry_off)

    t = tsub.add_parser(
        "content",
        help="collect the question text, optionally for a fixed window",
        description="With --days/--until the window closes by itself, so a "
                    "collection period doesn't outlive the reason for it.",
    )
    t.add_argument("state", choices=("on", "off"))
    window = t.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, help="collect for N more days, then stop")
    window.add_argument("--until", default="", help="collect through this date (YYYY-MM-DD)")
    t.set_defaults(func=cmd_telemetry_content)

    t = tsub.add_parser("exclude", help="never record one person's question text")
    t.add_argument("who", help="alias or Slack ID")
    t.set_defaults(func=cmd_telemetry_exclude)

    t = tsub.add_parser("include", help="undo an exclude")
    t.add_argument("who", help="alias or Slack ID")
    t.set_defaults(func=cmd_telemetry_include)

    t = tsub.add_parser("prune", help="delete old daily logs")
    t.add_argument("--older-than", type=int, default=90, metavar="DAYS",
                   help="delete logs older than this many days (default: 90)")
    t.set_defaults(func=cmd_telemetry_prune)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    registry.invalidate()          # always act on what's on disk right now
    telemetry.invalidate()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
