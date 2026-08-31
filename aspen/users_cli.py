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

It also files workflows on people's behalf, for the common case where someone
hands you a document instead of typing it to Aspen themselves. The body goes in
verbatim — these are meant to be the user's own words — so the only authored part
is the one-line ``description``, which is what tells Aspen when to open the file
at all:

    python -m aspen.users_cli workflow import arun ArunDFTWorkflow.md
    python -m aspen.users_cli workflow describe arun "ORCA DFT for 3d metals…"
    python -m aspen.users_cli workflow list

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
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from . import config, pending, registry, roots, setup, telemetry, workflows


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
        if u.get("calc_root"):
            flags.append("root")
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


def cmd_set_root(args) -> int:
    """Point a user at their own calculations tree.

    Validation happens *here*, at write time, rather than only at startup: a bad
    root typed at the keyboard should fail with a message in front of the person
    who can fix it, not surface later as a confusing tool error in someone's
    Slack thread. The check runs as the bot's own Unix user, since that is the
    identity that will do the reading.
    """
    _migrate_bootstrap()
    user = registry.resolve(args.who, include_removed=False)
    if user is None:
        return _err(f"no active user matches '{args.who}'")
    uid = user["slack_user_id"]

    # Recording only the cluster account is its own errand: someone on the shared
    # default root has no path to re-supply, and requiring one to fill in an
    # account is how the field stayed empty long enough for the bot to guess.
    account_only = bool(args.unix_user) and not args.path and not args.clear

    if args.clear:
        new_root = ""
    elif account_only:
        new_root = user["calc_root"]
    else:
        if not args.path:
            return _err("give a path, pass --clear to fall back to the default, "
                        "or pass --unix-user on its own to record just the account")
        problem = roots.validate(args.path, for_uid=uid)
        if problem:
            return _err(problem)
        new_root = str(Path(args.path).expanduser().resolve())

    users = [dict(u, calc_root=new_root, **({"unix_user": args.unix_user} if args.unix_user else {}))
             if u["slack_user_id"] == uid else u
             for u in registry.users(include_removed=True)]
    registry.save(users)

    if account_only:
        print(f"{user['display_name']} (@{user['alias']}) submits as {args.unix_user}")
    elif new_root:
        print(f"{user['display_name']} (@{user['alias']}) now reads from {new_root}")
    else:
        print(f"{user['display_name']} (@{user['alias']}) falls back to the shared "
              f"default: {config.CALCULATIONS_ROOT}")
    if args.unix_user and not account_only:
        print(f"Unix account recorded as {args.unix_user}")
    print("Takes effect on their next message — no restart needed.")
    return 0


def cmd_roots(args) -> int:
    """Every root Aspen can read, and whether it is actually readable."""
    scopes = roots.scopes()
    rows = []
    for scope in scopes:
        path = scope["path"]
        if not path.is_dir():
            state = "MISSING"
        elif not os.access(path, os.R_OK | os.X_OK):
            state = "UNREADABLE"
        elif path == config.CALCULATIONS_ROOT and scope["kind"] == "user":
            state = "default"
        else:
            state = "ok"
        rows.append((f"@{scope['name']}", scope["kind"], state, str(path)))

    header = ("ROOT", "KIND", "STATE", "PATH")
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(3)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths) + "  {}"
    print(fmt.format(*header))
    for row in rows:
        print(fmt.format(*row))

    problems = roots.check_all()
    if problems:
        print("\nProblems (Aspen will refuse to start):")
        for p in problems:
            print(f"  {p}")
    else:
        print(f"\n{len(rows)} root(s), all readable by {getpass.getuser()}.")
    print(f"Default for anyone without their own: {config.CALCULATIONS_ROOT}")
    return 0


def cmd_requests(args) -> int:
    """What people have asked for, and the command that grants it."""
    satisfied = pending.resolve_satisfied()
    for entry in satisfied:
        print(f"(already done, dropping: {pending.describe(entry)})")

    queued = pending.load()
    if args.clear:
        for entry in queued:
            pending.resolve(entry["kind"], entry["slack_user_id"])
        print(f"Cleared {len(queued)} request(s). Nobody was granted anything by this.")
        return 0
    if not queued:
        print("Nothing waiting.")
        return 0

    for entry in queued:
        print(f"\n{pending.describe(entry)}")
        print(f"  first asked  {entry['first_seen']}")
        print(f"  run          {pending.command_for(entry)}")
    print(f"\n{len(queued)} request(s). Running the command is what grants it — "
          "this list only remembers who asked.")
    return 0


def cmd_setup(args) -> int:
    """What each person has set up, and what they've said no to."""
    if args.who:
        user = registry.resolve(args.who)
        if user is None:
            return _err(f"no user matches '{args.who}'")
        users = [user]
    else:
        users = registry.users()

    if args.reset:
        if not args.who:
            return _err("--reset needs a user")
        if not setup.undecline(users[0]["slack_user_id"], args.reset):
            return _err(f"{users[0]['alias']} hasn't declined {args.reset}")
        print(f"{users[0]['alias']} will be offered {args.reset} again.")
        return 0

    rows = [(u["alias"],) + tuple(setup.state(u["slack_user_id"], item) for item in setup.ITEMS)
            for u in users]
    header = ("WHO",) + setup.ITEMS
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*header))
    for row in rows:
        print(fmt.format(*row))
    print("\n'declined' means they said no and Aspen won't ask again — "
          "`aspen-users setup <who> --reset <item>` undoes that.")
    return 0


# --------------------------------------------------------------------------- #
# Slurm jobs (spec §19.8)
#
# The operator's path into the job ledger, deliberately outside the agent and
# outside Slack. It exists because the beta's realistic failure mode is a runaway
# batch at 2 a.m., and debugging that through a Slack thread is the wrong tool.
#
# `cancel` here is the ONE place that can cancel on someone else's behalf — an
# operator acting as a human, at a terminal, with the whole ledger visible. The
# agent has no equivalent, exactly as with admission and calculations roots: the
# things that grant or destroy stay CLI-only.
# --------------------------------------------------------------------------- #
def _job_state(row: dict) -> str:
    if row.get("cancelled_at"):
        return "cancelled"
    return row.get("state") or "unreconciled"


def cmd_jobs_list(args) -> int:
    from . import jobs

    who = ""
    if args.who:
        user = registry.resolve(args.who)
        if not user:
            print(f"No such user: {args.who}")
            return 1
        who = user["slack_user_id"]

    rows = jobs.active_rows(who) if not args.all else _all_rows()
    if not rows:
        print("No active Aspen jobs." if not args.all else "The ledger is empty.")
        return 0

    print(f"{'JOBID':<12} {'KIND':<12} {'WHO':<16} {'PROJECT':<20} {'STATE':<14} BATCH")
    for r in sorted(rows, key=lambda r: str(r.get("submitted_at") or "")):
        print(f"{str(r['job_id']):<12} {(r.get('kind') or '-'):<12} "
              f"{(r.get('alias') or '?'):<16} {(r.get('project') or '-'):<20} "
              f"{_job_state(r):<14} {r['batch_id']}")
    print(f"\n{len(rows)} job(s). Ledger: {config.JOBS_LEDGER}")
    return 0


def _all_rows() -> list:
    from . import jobs
    with jobs.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT j.*, b.alias, b.project, b.slack_user_id FROM jobs j "
            "JOIN batches b ON b.batch_id = j.batch_id ORDER BY j.submitted_at")]


def cmd_jobs_show(args) -> int:
    from . import jobs

    with jobs.connect() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE batch_id = ?",
                             (args.batch_id,)).fetchone()
    if batch is None:
        print(f"No such batch: {args.batch_id}")
        return 1
    batch = dict(batch)
    print(f"Batch {batch['batch_id']}")
    print(f"  requested by  {batch['alias']} ({batch['slack_user_id']})")
    print(f"  thread        {batch['thread_ts']}")
    print(f"  project       {batch['project'] or '-'}  (scope {batch['owner_scope'] or '-'})")
    print(f"  mode          {batch['template_mode']}")
    print(f"  structures    {batch['structures']}")
    print(f"  staging       {batch['staging_dir']}")
    print(f"  submitted     {batch['submitted_at']}")
    print(f"  argv          {' '.join(json.loads(batch['argv']))}")
    rows = jobs.jobs_for_batch(args.batch_id)
    print(f"\n  {len(rows)} scheduler job(s):")
    for r in rows:
        print(f"    {str(r['job_id']):<12} {(r.get('kind') or '-'):<12} "
              f"{_job_state(r):<14} {r.get('elapsed') or ''} {r.get('total_cpu') or ''}")
    return 0


def cmd_jobs_cancel(args) -> int:
    """Cancel on a user's behalf, going through the *same* verification the agent does.

    Deliberately not a raw ``scancel``: an operator typing a job ID at 2 a.m. is
    exactly as capable of typing the wrong one as a model is, and the WorkDir check
    is what catches that. ``--force`` skips only the interactive prompt, never the
    verification.
    """
    from . import jobs

    user = registry.resolve(args.who)
    if not user:
        print(f"No such user: {args.who}")
        return 1
    uid = user["slack_user_id"]

    try:
        approved, refused = jobs.resolve_cancellable(uid, args.selector)
    except jobs.JobsError as exc:
        print(f"Error: {exc}")
        return 1

    for jid, reason in refused:
        print(f"  skip {jid}: {reason}")
    if not approved:
        print("Nothing to cancel.")
        return 0

    print(f"Would cancel {len(approved)} job(s) for {user['alias']}:")
    for r in approved:
        print(f"  {r['job_id']}  {r.get('kind') or '-'}  {r.get('project') or '-'}  "
              f"[{r.get('live_state') or '?'}]")
    if not args.force and input("Cancel these? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Left alone.")
        return 0

    try:
        result = jobs.cancel(uid, args.selector)
    except jobs.JobsError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Cancelled {len(result['cancelled'])} job(s).")
    return 0


def cmd_jobs_backfill(args) -> int:
    from . import jobs

    result = jobs.backfill_jobs(args.batch_id or "")
    for batch, n in result["repaired"]:
        print(f"  {batch}: recorded {n} job(s)")
    for batch, why in result["still_empty"]:
        print(f"  {batch}: still empty — {why}")
    if not result["repaired"] and not result["still_empty"]:
        print("Every batch already has its jobs recorded.")
    return 0


def cmd_jobs_prune(args) -> int:
    from . import jobs

    result = jobs.prune_staging(max_age_hours=args.hours)
    print(f"Removed {result['removed']} abandoned staging dir(s), freeing "
          f"{result['bytes'] / 1e6:.1f} MB; kept {result['kept']}.")
    return 0


def cmd_jobs_reconcile(args) -> int:
    from . import jobs

    try:
        result = jobs.reconcile(days=args.days)
    except jobs.JobsError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Reconciled {result['updated']} of {result['scanned']} matching sacct row(s) "
          f"since {result['since']}.")
    return 0


# --------------------------------------------------------------------------- #
# Runner profiles (spec §20)
#
# Registering a runner is the moment a human reads the job script, and it happens
# once per protocol rather than once per job. Nothing agent-facing writes here —
# same discipline as admission and calculations roots.
# --------------------------------------------------------------------------- #
def cmd_runner_add(args) -> int:
    """Save a runner into a user's own library, from the terminal.

    Users normally do this through Aspen, which checks the script and asks them to
    confirm. This is the same operation for an operator with a file in hand — and it
    applies the same checks, so ``--force`` here is the terminal equivalent of a user
    saying "yes, that rm only clears the job's scratch dir".
    """
    from . import runners

    user = registry.resolve(args.who)
    if not user:
        return _err(f"no such user: {args.who}")
    try:
        body = Path(args.script).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _err(f"could not read {args.script}: {exc}")

    problems = runners.script_problems(body)
    if problems:
        print(f"{len(problems)} thing(s) to look at in {args.script}:")
        for problem in problems:
            print(f"  • {problem}")
        if not args.force:
            return _err("fix them, or pass --force if you have read the script and "
                        "accept them")
        print("Accepting them because --force was given; this is recorded on the runner.\n")

    try:
        meta = runners.save(
            user["slack_user_id"], args.name, body, description=args.description,
            ntasks=args.ntasks, mem_gb=args.mem_gb, time_limit=args.time,
            accept_problems=problems if args.force else [],
        )
    except runners.RunnerError as exc:
        return _err(str(exc))

    print(f"Saved '{meta['name']}' as @{user['alias']}'s runner.")
    print(f"  script frozen at {meta['script']}")
    d = meta["defaults"]
    print(f"  defaults         {d['ntasks']} tasks, {d['mem_gb']} GB, {d['time']}")
    if meta["problems_accepted"]:
        print(f"  accepted         {len(meta['problems_accepted'])} warning(s)")
    print(f"\nMake it their default with:  aspen-users set-runner {user['alias']} {meta['name']}")
    return 0


def cmd_runner_list(args) -> int:
    from . import runners

    entries = runners.index()
    if not entries:
        print(f"No runners saved. Library: {config.RUNNERS_ROOT}")
        print("Users can save their own by showing Aspen the job script they submit,")
        print("or:  aspen-users runner add <who> <name> --script <file>")
        return 0

    defaults = {}
    for user in registry.users():
        name = (user.get("job_runner") or "").strip().lower()
        if name:
            defaults.setdefault(name, []).append(user["alias"])

    print(f"{'OWNER':<20} {'NAME':<20} {'TASKS':>6} {'MEM':>6}  FLAGS")
    for e in entries:
        d = e.get("defaults", {})
        flags = []
        if e["name"] in defaults.get(e["name"], []) or e["owner_alias"] in defaults.get(e["name"], []):
            flags.append("default")
        if e.get("problems_accepted"):
            flags.append(f"{len(e['problems_accepted'])} accepted warning(s)")
        print(f"@{e['owner_alias']:<19} {e['name']:<20} {d.get('ntasks','?'):>6} "
              f"{d.get('mem_gb','?'):>6}  {', '.join(flags) or '-'}")
    print(f"\n{len(entries)} runner(s). Library: {config.RUNNERS_ROOT}")
    return 0


def cmd_runner_show(args) -> int:
    from . import runners

    user = registry.resolve(args.who)
    if not user:
        return _err(f"no such user: {args.who}")
    try:
        meta = runners.resolve(args.name, user["slack_user_id"],
                               owner=user["alias"])
        body = runners.script_for(meta)
    except runners.RunnerError as exc:
        return _err(str(exc))
    print(json.dumps(meta, indent=2))
    print("\n--- frozen script ---")
    print(body)
    return 0


def cmd_set_runner(args) -> int:
    from . import runners

    user = registry.resolve(args.who)
    if not user:
        return _err(f"no such user: {args.who}")

    if args.clear:
        name = ""
    else:
        name = args.runner.strip().lower()
        try:
            runners.resolve(name, user["slack_user_id"], owner=user["alias"])
        except runners.RunnerError:
            theirs = [e["name"] for e in runners.index(user["slack_user_id"]) if e["mine"]]
            return _err(f"@{user['alias']} has no runner called {name!r}. "
                        f"Theirs: {', '.join(theirs) or '(none)'}")

    users = [dict(u, job_runner=name) if u["slack_user_id"] == user["slack_user_id"] else u
             for u in registry.users(include_removed=True)]
    registry.save(users)
    if name:
        print(f"@{user['alias']} will submit jobs with the '{name}' runner.")
    else:
        print(f"@{user['alias']} has no runner — they cannot submit jobs until one is set.")
    print("Takes effect on their next message — no restart needed.")
    return 0


def cmd_whois(args) -> int:
    user = registry.resolve(args.who)
    if user is None:
        return _err(f"no user matches '{args.who}'")
    uid = user["slack_user_id"]
    for field in ("slack_user_id", "alias", "display_name", "role", "status",
                  "added", "added_by", "removed", "removed_by", "notes", "unix_user",
                  "job_runner"):
        if user.get(field):
            print(f"{field:<15} {user[field]}")
    root = roots.for_user(uid)
    if roots.is_rootless(uid):
        print(f"{'calculations':<15} (none — declined; unqualified paths are an error)")
    else:
        suffix = "" if user.get("calc_root") else "  (shared default)"
        print(f"{'calculations':<15} {root}{suffix}")
    declined = ", ".join(sorted((user.get("declined") or {}))) or "-"
    print(f"{'declined':<15} {declined}")
    directory = workflows.dir_for(uid, include_archived=True)
    print(f"{'workflow':<15} {directory or '(none)'}")
    if uid == config.ADMIN_USER_ID:
        print(f"{'admin':<15} yes")
    return 0


# --------------------------------------------------------------------------- #
# Workflow filing
#
# Ingestion is deliberately dumb about the *body*: it goes in exactly as written.
# A workflow is fed to its owner's sessions as their standing preferences, so
# reformatting someone's prose would mean rewriting instructions that still carry
# their name. The one authored field is ``description`` — the only routing signal
# in workflows.turn_preamble, and therefore the whole difference between a file
# Aspen opens and a file it never thinks to look at.
# --------------------------------------------------------------------------- #
_DRAFT_TIMEOUT = 90
_DRAFT_PROMPT = (
    "Below is one scientist's personal computational-chemistry workflow, filed so "
    "an assistant can decide when to consult it. Reply with ONE line of at most "
    "200 characters saying what it covers — the methods, software, and kind of "
    "problem — specific enough to tell it apart from a colleague's workflow on a "
    "neighboring topic. Output only that line: no preamble, no quotes, no bullets."
)


def _resolve_workflow_target(who: str) -> tuple[str, str, str, int]:
    """(uid, target, label, exit_code) for a workflow subcommand's ``who``.

    ``_group`` resolves to the admin's own ID because ``workflows.write`` gates
    the shared file on ``uid == ADMIN_USER_ID`` — the CLI is admin-only anyway,
    but going through the same check keeps one rule instead of two.
    """
    if who.strip().lstrip("_").lower() == "group":
        admin = config.ADMIN_USER_ID
        if not admin:
            return "", "", "", _err("no admin in the registry — cannot write the _group workflow")
        return admin, "_group", "the shared _group workflow", 0
    user = registry.resolve(who)
    if user is None:
        known = ", ".join(u["alias"] for u in registry.users()) or "(none)"
        return "", "", "", _err(f"no user matches '{who}'. Known aliases: {known}")
    return user["slack_user_id"], "", f"{user['display_name']}'s workflow", 0


def _workflow_path(uid: str, target: str):
    if target == "_group":
        return config.WORKFLOWS_ROOT / workflows.GROUP_DIR / workflows.WORKFLOW_FILENAME
    directory = workflows.dir_for(uid)
    return (directory / workflows.WORKFLOW_FILENAME) if directory else None


def _draft_description(body: str) -> tuple[str, str]:
    """(description, error) — one line drafted by the Claude Code CLI.

    Shelling out to the CLI rather than importing an SDK keeps this dependency-free:
    the binary is already required to run the bot at all (config.CLAUDE_CLI_PATH),
    and it carries its own auth, so the import path needs no key of its own.
    """
    cli = config.CLAUDE_CLI_PATH or "claude"
    env = dict(os.environ)
    if config.ASPEN_SDK_USE_SUBSCRIPTION:
        env["ANTHROPIC_API_KEY"] = ""       # prefer the Code login, as agent.py does
    try:
        proc = subprocess.run(
            [cli, "-p", _DRAFT_PROMPT], input=body, text=True,
            capture_output=True, timeout=_DRAFT_TIMEOUT, env=env,
        )
    except FileNotFoundError:
        return "", f"the Claude CLI ({cli}) wasn't found — set CLAUDE_CLI_PATH, or pass --description"
    except subprocess.TimeoutExpired:
        return "", f"the Claude CLI didn't answer within {_DRAFT_TIMEOUT}s"
    except OSError as exc:
        return "", str(exc)
    if proc.returncode != 0:
        return "", (proc.stderr.strip().splitlines() or [f"claude exited {proc.returncode}"])[0]
    return workflows._one_line(proc.stdout.strip().strip('"').strip("'")), ""


def _settle_description(args, supplied: dict, body: str) -> str:
    """The description to file: explicit flag, then the file's own, then a draft."""
    if args.description.strip():
        return args.description.strip()

    existing = workflows._one_line(supplied.get("description"))
    if existing:
        print(f"Using the description already in the file: {existing}")
        return existing

    if args.no_draft:
        return ""

    draft, failure = _draft_description(body)
    if failure:
        print(f"warning: could not draft a description ({failure})")
        return ""
    print(f"\nDrafted description:\n  {draft}\n")
    if _confirm("File it with this description?", args.yes):
        return draft
    try:
        return input("Description (blank to file without one): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def cmd_workflow_import(args) -> int:
    """File a document someone handed you as their workflow."""
    src = Path(args.file).expanduser()
    if not src.is_file():
        return _err(f"{src} is not a file")
    try:
        raw = src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _err(f"could not read {src} ({exc}) — this takes text/markdown, "
                    "so convert anything else first")

    uid, target, label, code = _resolve_workflow_target(args.who)
    if code:
        return code

    supplied, body = workflows.parse(raw)
    if not body.strip():
        return _err(f"{src} has no content below its frontmatter")

    path = _workflow_path(uid, target)
    if path is not None and path.is_file():
        print(f"{label} already exists at {path} — importing REPLACES its body.")
        print(f"(the current version is backed up to "
              f"{config.WORKSPACE_ROOT / 'workflow_history' / (target or uid)} either way)")
        if not _confirm("Proceed?", args.yes):
            print("Aborted.")
            return 1

    description = _settle_description(args, supplied, body)
    content = workflows.render(
        {"description": description, "derived_from": supplied.get("derived_from")}, body
    )
    result = workflows.write(uid, content, target=target, actor=_actor(args))
    if result.startswith("Error"):
        return _err(result[len("Error: "):] if result.startswith("Error: ") else result)
    print(result)

    written = _workflow_path(uid, target)
    if written:
        print(f"Filed at {written}")
    if args.archive_source:
        moved = _archive_source(src)
        print(f"Source moved to {moved}" if moved else f"warning: could not move {src}")
    else:
        print(f"The source file is untouched at {src} — delete it, or it will "
              "drift away from what Aspen serves.")
    print("Live on their next message — no restart needed.")
    return 0


def _archive_source(src: Path):
    """Move an imported source aside so it can't drift from the filed copy."""
    dest = src.with_name(f"{src.name}.imported-{date.today().isoformat()}")
    n = 1
    while dest.exists():
        dest = src.with_name(f"{src.name}.imported-{date.today().isoformat()}-{n}")
        n += 1
    try:
        src.rename(dest)
        return dest
    except OSError:
        return None


def cmd_workflow_describe(args) -> int:
    """Rewrite the one line that decides whether Aspen opens a workflow."""
    uid, target, label, code = _resolve_workflow_target(args.who)
    if code:
        return code

    path = _workflow_path(uid, target)
    if path is None or not path.is_file():
        return _err(f"no workflow on file for '{args.who}' — import one first with "
                    "`aspen-users workflow import`")
    try:
        _, body = workflows.parse(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return _err(f"could not read {path} ({exc})")

    description = args.description.strip()
    if not description:
        return _err("refusing to blank the description — it is the only thing that "
                    "tells Aspen when to open this file")

    content = workflows.render({"description": description}, body)
    result = workflows.write(uid, content, target=target, actor=_actor(args))
    if result.startswith("Error"):
        return _err(result[len("Error: "):] if result.startswith("Error: ") else result)
    print(f"{label}: description updated (the body is unchanged).")
    print(f"  {description}")
    return 0


def cmd_workflow_list(args) -> int:
    """Every description on file, side by side — how you tune them."""
    entries = workflows.index(include_archived=args.all)
    if not entries:
        print(f"No workflows on file. Root: {config.WORKFLOWS_ROOT}")
        print("File one with:  aspen-users workflow import <alias> <file.md>")
        return 0

    rows = [
        (e["alias"] + (" [archived]" if e["archived"] else ""),
         e["updated"] or "-", e["description"])
        for e in entries
    ]
    header = ("WHO", "UPDATED", "DESCRIPTION")
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(2)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths) + "  {}"
    print(fmt.format(*header))
    for row in rows:
        print(fmt.format(*row))

    missing = [u["alias"] for u in registry.users()
               if not workflows.has_workflow(u["slack_user_id"])]
    print(f"\n{len(rows)} workflow(s). Root: {config.WORKFLOWS_ROOT}")
    if missing:
        print(f"No workflow yet: {', '.join(missing)}")
    return 0


def cmd_workflow_show(args) -> int:
    uid, target, label, code = _resolve_workflow_target(args.who)
    if code:
        return code
    path = _workflow_path(uid, target)
    if path is None or not path.is_file():
        archived = workflows.dir_for(uid, include_archived=True) if not target else None
        if archived and workflows.ARCHIVE_DIR in archived.parts:
            print(f"Archived: {archived / workflows.WORKFLOW_FILENAME}")
            path = archived / workflows.WORKFLOW_FILENAME
        else:
            return _err(f"no workflow on file for '{args.who}'")
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _err(f"could not read {path} ({exc})")

    meta, body = workflows.parse(raw)
    print(f"# {label}")
    print(f"{'path':<13} {path}")
    for field in ("description", "updated", "updated_by", "derived_from", "archived"):
        if meta.get(field):
            print(f"{field:<13} {meta[field]}")
    print(f"{'bytes':<13} {len(raw.encode('utf-8'))}\n")
    print(body.strip())
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

    # --- calculations roots -------------------------------------------------- #
    # Who reads from where. Everyone may READ every root — this sets whose files
    # an unqualified path means, and where their own work lives. See aspen/roots.py.
    s = sub.add_parser(
        "set-root",
        help="point a user at their own calculations directory",
        description="Set (or clear) where one user's calculations live. Validated "
                    "here so a bad path fails in front of you rather than in "
                    "somebody's Slack thread.",
    )
    s.add_argument("who", help="alias or Slack ID")
    s.add_argument("path", nargs="?", default="", help="absolute path to their calculations tree")
    s.add_argument("--clear", action="store_true",
                   help="unset it — they fall back to the shared CALCULATIONS_ROOT")
    s.add_argument("--unix-user", default="",
                   help="their cluster account (a Slack ID doesn't name one). May "
                        "be given on its own, with no path, to record just this")
    s.set_defaults(func=cmd_set_root)

    s = sub.add_parser("roots", help="list every calculations root and its state")
    s.set_defaults(func=cmd_roots)

    s = sub.add_parser(
        "setup",
        help="who has a workflow / their own root, and who declined",
        description="Aspen offers each once, on a thread's first turn, and stops "
                    "for good when someone says no. This is that state.",
    )
    s.add_argument("who", nargs="?", default="", help="one user (default: everyone)")
    s.add_argument("--reset", choices=setup.ITEMS, default="",
                   help="clear a decline so they get asked again")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser(
        "requests",
        help="who has asked for access or a root, and the command to grant it",
        description="Aspen cannot grant either — it records the ask and DMs you. "
                    "This is that queue; running the printed command is what "
                    "actually grants anything.",
    )
    s.add_argument("--clear", action="store_true", help="forget every pending request")
    s.set_defaults(func=cmd_requests)

    # --- workflows ---------------------------------------------------------- #
    # Filing on someone's behalf, for when they hand you a document instead of
    # typing it to Aspen. Everyone can still write their own from Slack; this is
    # the admin path, and it is the only one that can write another user's file.
    s = sub.add_parser(
        "workflow",
        help="file and maintain per-user workflow documents",
        description="Import documents as workflows and keep their descriptions "
                    "sharp. Bodies are stored verbatim — only the description is "
                    "authored here, and it is what routes Aspen to the file.",
    )
    wsub = s.add_subparsers(dest="workflow_command", required=True)

    w = wsub.add_parser("import", help="file a markdown/text document as someone's workflow")
    w.add_argument("who", help="alias, Slack ID, or '_group' for the shared file")
    w.add_argument("file", help="path to the document (text or markdown)")
    w.add_argument("--description", default="",
                   help="the one-line routing summary (default: the file's own, else drafted)")
    w.add_argument("--no-draft", action="store_true",
                   help="never call the Claude CLI to draft a description")
    w.add_argument("--archive-source", action="store_true",
                   help="rename the source file aside once it is filed")
    w.add_argument("-y", "--yes", action="store_true",
                   help="accept a drafted description and any overwrite without asking")
    w.set_defaults(func=cmd_workflow_import)

    w = wsub.add_parser("describe", help="rewrite a workflow's one-line description")
    w.add_argument("who", help="alias, Slack ID, or '_group'")
    w.add_argument("description", help="the new one-line summary")
    w.set_defaults(func=cmd_workflow_describe)

    w = wsub.add_parser("list", help="every workflow on file, with its description")
    w.add_argument("--all", action="store_true", help="include archived workflows")
    w.set_defaults(func=cmd_workflow_list)

    w = wsub.add_parser("show", help="print one workflow in full")
    w.add_argument("who", help="alias, Slack ID, or '_group'")
    w.set_defaults(func=cmd_workflow_show)

    # --- runners ------------------------------------------------------------ #
    s = sub.add_parser(
        "runner",
        help="inspect and save job-script runners",
        description="A runner is the job script Aspen fills in and submits. Users "
                    "normally save their own through Aspen, which checks the script "
                    "and asks them to confirm; these commands are the same operation "
                    "from a terminal. The bytes are frozen on save, so review binds "
                    "to content rather than to a path that can change afterwards.",
    )
    rsub = s.add_subparsers(dest="runner_command", required=True)

    r = rsub.add_parser("add", help="save a job script as a user's runner")
    r.add_argument("who", help="whose library to save it in (alias or Slack ID)")
    r.add_argument("name", help="short name, e.g. 'orca-nbo'")
    r.add_argument("--script", required=True,
                   help="path to the job script to freeze (use [INPUT] where the "
                        "input filename goes; also [JOB_NAME] [OUTPUT] [NTASKS] "
                        "[MEM_GB] [TIME])")
    r.add_argument("--code", default="orca", help="input format (only 'orca' so far)")
    r.add_argument("--description", default="", help="one line, shown to the agent")
    r.add_argument("--ntasks", type=int, default=16, help="default cores")
    r.add_argument("--mem-gb", type=int, default=64, dest="mem_gb", help="default memory (GB)")
    r.add_argument("--time", default="48:00:00", help="default walltime HH:MM:SS")
    r.add_argument("-f", "--force", action="store_true",
                   help="accept the script despite failed checks (you have read it)")
    r.set_defaults(func=cmd_runner_add)

    r = rsub.add_parser("list", help="every registered runner and who uses it")
    r.set_defaults(func=cmd_runner_list)

    r = rsub.add_parser("show", help="one runner in full, including its frozen script")
    r.add_argument("who", help="whose runner (alias or Slack ID)")
    r.add_argument("name")
    r.set_defaults(func=cmd_runner_show)

    s = sub.add_parser(
        "set-runner",
        help="set a user's DEFAULT runner (CLI-only, like set-root)",
        description="Which runner is used when none is named. Users may save and "
                    "pick their own runners through Aspen; the default stays here so "
                    "an operator's choice cannot be redirected by a conversation.",
    )
    s.add_argument("who", help="alias or Slack ID")
    s.add_argument("runner", nargs="?", default="", help="registered runner name")
    s.add_argument("--clear", action="store_true", help="unset it")
    s.set_defaults(func=cmd_set_runner)

    # --- slurm jobs --------------------------------------------------------- #
    # The operator's way into the job ledger, outside the agent and outside Slack.
    # `cancel` is the only path that can cancel for someone else — a human at a
    # terminal — and it still goes through the same WorkDir verification the agent
    # does, because an operator can mistype a job ID just as easily as a model can.
    s = sub.add_parser(
        "jobs",
        help="inspect and cancel Aspen-submitted Slurm jobs",
        description="Aspen's own job ledger. Cancellation here runs the same "
                    "verification the agent does (the job must be Aspen's, and its "
                    "WorkDir must be inside that user's staging area), so a mistyped "
                    "ID is refused rather than obeyed.",
    )
    jsub = s.add_subparsers(dest="jobs_command", required=True)

    j = jsub.add_parser("list", help="active Aspen jobs (all users by default)")
    j.add_argument("who", nargs="?", default="", help="limit to one alias or Slack ID")
    j.add_argument("--all", action="store_true",
                   help="every ledger row, including finished ones")
    j.set_defaults(func=cmd_jobs_list)

    j = jsub.add_parser("show", help="one batch in full, with its jobs and argv")
    j.add_argument("batch_id")
    j.set_defaults(func=cmd_jobs_show)

    j = jsub.add_parser("cancel", help="cancel a user's Aspen jobs (verified per job)")
    j.add_argument("who", help="alias or Slack ID whose jobs to cancel")
    j.add_argument("selector", nargs="?", default="all",
                   help="'all' (default), a job ID, a batch ID, or a project name")
    j.add_argument("-f", "--force", action="store_true", help="skip the confirmation prompt")
    j.set_defaults(func=cmd_jobs_cancel)

    j = jsub.add_parser(
        "backfill",
        help="recover job IDs for batches that recorded none",
        description="Re-reads batch-jobs.log for batches with no jobs in the ledger. "
                    "A batch in that state is uncancellable through Aspen even though "
                    "its jobs are running, so recovery must not need a database edit. "
                    "Safe to re-run: only empty batches are touched.",
    )
    j.add_argument("batch_id", nargs="?", default="",
                   help="one batch, or omit for every empty one")
    j.set_defaults(func=cmd_jobs_backfill)

    j = jsub.add_parser(
        "prune",
        help="delete staging directories no batch ever claimed",
        description="A dry run stages a copy of the structures before the pipeline "
                    "validates them; if the user then declines, that copy is "
                    "orphaned. Only directories with no ledger row and older than "
                    "--hours are removed, so nothing submitted is ever touched. "
                    "Runs opportunistically on each dry run too.",
    )
    j.add_argument("--hours", type=float, default=48.0,
                   help="minimum age to remove (default 48)")
    j.set_defaults(func=cmd_jobs_prune)

    j = jsub.add_parser(
        "reconcile",
        help="fill in what jobs actually consumed, from sacct",
        description="Attribution is two-phase: submit time knows who and what, but "
                    "elapsed time, CPU-hours and exit state exist only after a job "
                    "ends. Run this to answer 'who used the compute'.",
    )
    j.add_argument("--days", type=int, default=30, help="how far back to scan (default 30)")
    j.set_defaults(func=cmd_jobs_reconcile)

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
