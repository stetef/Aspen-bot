"""
Runner profiles — the job script a submission actually runs.

Different people in the group run calculations differently. Sam drives the
``xas_pipeline`` ORCA→CORVUS batch; Arun submits a single ORCA job from a script he
has used for years. Both are legitimate and neither should be forced into the
other's shape.

**Users own their own runners**, the way they own their templates and their
workflow. Aspen can save one for the person it is talking to, after checking it and
getting them to confirm — it is not an admin-gated act. That is a deliberate
reversal of the first design here, which required an operator to register every
script. The operator's reasoning: waiting on an admin to add a runner is friction on
the main path, and the beta group is people they would trust with this account
anyway.

What that changes, stated plainly rather than left implicit: a beta user can get a
shell script of their choosing to run as the account Aspen submits under. That risk
was already accepted for this group (THREAT_MODEL §8) — what moves is *who reads the
script before it runs*. It is now the author, prompted by Aspen, instead of an admin.
So the checks below matter more, not less:

* :func:`script_problems` is what "Aspen double-checks it" means. It surfaces the
  destructive habits that turn dangerous when a personal compute account becomes a
  shared one — ``rm``, ``find -delete``, ``rsync --delete``, recursive ``chmod``,
  ``curl | sh``, ``sudo``, absolute redirects, writes into ``$HOME``.
* Saving is **two calls with a confirmation token**, like every other action that
  spends or destroys something. The user sees the flagged lines before agreeing.
* An override is **recorded** on the profile, not silent, so "who accepted this and
  what did they accept" is answerable later.
* The bytes are **frozen at save time** and re-checked on every use. Review binds to
  content, not to a path whose contents can change afterwards — which also means a
  runner cannot be quietly swapped between saving and submitting.

Storage mirrors :mod:`aspen.templates` exactly, because the ownership question is
identical: ``<root>/<alias>__<slack-id>/<name>.sh``, no ``owner`` parameter on any
write, a colleague's runner readable as ``reference-only`` so protocols can be
borrowed and adapted, and every overwrite snapshotted.

One thing stays out of the model's reach: the *default* runner for a user
(``job_runner`` on the registry record) is still CLI-only, so an assignment made by
an operator cannot be silently redirected by a conversation. Picking a different
saved runner per submission is just naming one, and is fine.
"""

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, registry

log = logging.getLogger("aspen")

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Placeholders a job-script template may use. Every one is filled from a typed
# field or a value Aspen derived — never from conversation text.
PLACEHOLDERS = {
    "[JOB_NAME]":  "the Slurm job name Aspen assigns",
    "[INPUT]":     "the input filename inside the run directory",
    "[OUTPUT]":    "the output filename inside the run directory",
    "[NTASKS]":    "cores, from a validated integer",
    "[MEM_GB]":    "memory in GB, from a validated integer",
    "[TIME]":      "walltime as HH:MM:SS",
}
REQUIRED_PLACEHOLDERS = ("[INPUT]",)

# The destructive-command guardrail.
#
# Framed correctly: this is not a security boundary. Shell has unlimited ways to
# spell any command, so a determined author walks through it. It is aimed at the
# actor the threat model actually names — the careless member — and specifically at
# the habit of cleaning up your own compute account, which now belongs to someone
# else. It runs at registration, so a human is present to read the explanation.
_DESTRUCTIVE = (
    # Note: deleting a job's OWN scratch directory (`rm -rf "$tdir"`) is normal and
    # legitimate — the pipeline's own templates do it. It is still surfaced, because
    # a human reading one line is cheap and telling the two cases apart
    # automatically is not. That is what --force plus a recorded acceptance is for.
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*", re.I),
     "rm — deleting files as the shared account (if this only removes the job's own "
     "scratch directory, that is what --force is for)"),
    (re.compile(r"\bfind\b[^\n]*-(delete|exec\s+rm)", re.I),
     "find -delete / -exec rm"),
    (re.compile(r"\b(shred|truncate|dd|mkfs|wipefs)\b", re.I),
     "a destructive disk utility"),
    (re.compile(r"\brsync\b[^\n]*--delete", re.I),
     "rsync --delete, which removes files at the destination"),
    (re.compile(r"\bch(mod|own)\b[^\n]*-R", re.I),
     "a recursive chmod/chown"),
    (re.compile(r"\b(curl|wget)\b[^\n]*\|\s*(ba)?sh", re.I),
     "piping a download into a shell"),
    (re.compile(r"\bsudo\b", re.I), "sudo"),
    # Command position only. `#SBATCH --account ...` is the normal content of
    # every job script, and an earlier version of this pattern flagged it on every
    # real script in the group — caught by running it against Arun's.
    (re.compile(r"(?m)(?:^|[;&|]\s*)\s*(scancel|sbatch|qdel)\b", re.I),
     "scheduler job control from inside a job"),
    (re.compile(r">\s*(/|~|\$HOME)", re.I),
     "redirecting output to an absolute path or a home directory"),
    (re.compile(r"\bmv\b[^\n]*\s(/|~|\$HOME)\S*", re.I),
     "moving files to an absolute path or a home directory"),
)

# Kinds of runner. `pipeline` delegates to an external orchestrator that submits
# its own jobs (the xas_pipeline case); `direct` means Aspen builds the sbatch argv
# itself, which is what makes --comment available for that path.
KINDS = ("pipeline", "direct")


class RunnerError(Exception):
    """A runner could not be registered or resolved. Message is user-facing."""


NAME_MAX_LEN = 48
MAX_SCRIPT_BYTES = 100_000


def dir_for(uid: str) -> Optional[Path]:
    """A user's runner directory, found by **ID** so a rename cannot orphan it."""
    if registry.validate_user_id(uid):
        return None
    try:
        hits = sorted(p for p in config.RUNNERS_ROOT.glob(f"*__{uid}") if p.is_dir())
    except OSError:
        return None
    if hits:
        return hits[0]
    user = registry.by_id(uid)
    if user is None:
        return None
    return config.RUNNERS_ROOT / f"{user['alias']}__{uid}"


def validate_name(name: str) -> Optional[str]:
    raw = (name or "").strip().lower()
    if not raw:
        return "a runner needs a name"
    if len(raw) > NAME_MAX_LEN:
        return f"that name is too long (limit {NAME_MAX_LEN})"
    if not NAME_RE.match(raw):
        return (f"{name!r} isn't a usable name — lowercase words joined by hyphens, "
                "like 'orca-single'")
    return None


def _paths(uid: str, name: str) -> tuple:
    directory = dir_for(uid)
    if directory is None:
        raise RunnerError("you are not in Aspen's user registry.")
    script = (directory / f"{name}.sh").resolve()
    meta = (directory / f"{name}.json").resolve()
    for path in (script, meta):
        if not path.is_relative_to(directory.resolve()):
            raise RunnerError("that runner name resolves outside your library.")
    return script, meta


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _backup(path: Path, uid: str) -> None:
    if not path.is_file():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    history = config.RUNNERS_HISTORY_ROOT / uid
    try:
        registry.ensure_private_dir(config.RUNNERS_HISTORY_ROOT)
        history.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, history / f"{path.stem}-{stamp}.sh")
    except OSError:
        log.warning("runners: could not snapshot %s", path, exc_info=True)


def script_problems(text: str) -> list:
    """Everything wrong with a job-script template, as user-facing strings.

    Returns all of them, because someone adapting a script wants the whole list.
    """
    problems = []
    body = text or ""
    if not body.strip():
        return ["the script is empty"]
    if not body.lstrip().startswith("#!"):
        problems.append("it has no #! line, so Slurm may not run it as expected")

    for placeholder in REQUIRED_PLACEHOLDERS:
        if placeholder not in body:
            problems.append(
                f"it never uses {placeholder} — Aspen would have no way to tell the "
                "job which input file to run"
            )

    unknown = {m for m in re.findall(r"\[[A-Z_]+\]", body)} - set(PLACEHOLDERS)
    for token in sorted(unknown):
        problems.append(f"{token} is not a placeholder Aspen knows how to fill "
                        f"(known: {', '.join(sorted(PLACEHOLDERS))})")

    for pattern, why in _DESTRUCTIVE:
        match = pattern.search(body)
        if match:
            line = body[:match.start()].count("\n") + 1
            problems.append(
                f"line {line}: {why}. Aspen submits jobs under one shared account, "
                "so a cleanup step that was safe in your own account can delete "
                "someone else's work. Remove it, or keep it in a script you run "
                "yourself."
            )

    # A job script that hardcodes an absolute output path escapes the run directory
    # the same way an input directive would.
    for match in re.finditer(r"#SBATCH\s+--(output|error|chdir)[=\s]+(\S+)", body, re.I):
        value = match.group(2)
        if value.startswith(("/", "~")):
            problems.append(
                f"#SBATCH --{match.group(1)}={value} is an absolute path; Aspen sets "
                "the run directory itself so results land in the staging area"
            )
    return problems


def save(uid: str, name: str, script: str, *, description: str = "",
         code: str = "orca", ntasks: int = 16, mem_gb: int = 64,
         time_limit: str = "48:00:00", accept_problems: Optional[list] = None,
         derived_from: str = "") -> dict:
    """Save ``uid``'s own runner. ``uid`` comes from the Slack event, never a tool arg.

    ``accept_problems`` is the user's informed override, and it must name the exact
    problems they were shown. Anything flagged that is *not* in that list still
    refuses — so "accept everything" cannot be spelled as an empty gesture, and a
    check added later cannot be pre-accepted by an older confirmation.
    """
    user = registry.by_id(uid, include_removed=False)
    if user is None or user.get("status") != "active":
        raise RunnerError("you are not in Aspen's user registry, so there is nowhere "
                          "to save a runner.")
    key = (name or "").strip().lower()
    problem = validate_name(key)
    if problem:
        raise RunnerError(problem)
    if code not in ("orca",):
        raise RunnerError(f"no input validator exists for {code!r}, so Aspen will not "
                          "save a runner for it")

    body = script or ""
    if len(body.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise RunnerError(f"that script is over the {MAX_SCRIPT_BYTES}-byte limit")

    found = script_problems(body)
    accepted = list(accept_problems or [])
    outstanding = [p for p in found if p not in accepted]
    if outstanding:
        raise RunnerError(
            f"that script has {len(outstanding)} unresolved problem(s):\n"
            + "\n".join(f"  • {p}" for p in outstanding)
            + "\n\nFix them, or confirm each one explicitly if you know it is safe."
        )

    directory = dir_for(uid)
    if directory is None:
        raise RunnerError("could not resolve your runner directory.")
    registry.ensure_private_dir(config.RUNNERS_ROOT)
    directory.mkdir(parents=True, exist_ok=True)
    script_path, meta_path = _paths(uid, key)

    existed = script_path.is_file()
    _backup(script_path, uid)
    previous = _read_meta(meta_path)

    script_path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    script_path.chmod(0o600)
    meta = {
        "name": key,
        "kind": "direct",
        "code": code,
        # Identity is stamped, never taken from what the model passed.
        "owner_id": uid,
        "owner_alias": user["alias"],
        "description": " ".join((description or "").split())[:200]
                       or previous.get("description", ""),
        "defaults": {"ntasks": int(ntasks), "mem_gb": int(mem_gb), "time": time_limit},
        "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "problems_accepted": [p for p in found if p in accepted],
        "derived_from": _normalize_derived(derived_from) or previous.get("derived_from", ""),
        "script": str(script_path),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    meta_path.chmod(0o600)
    log.info("runners: %s %s/%s", "updated" if existed else "saved", user["alias"], key)
    return meta


def _normalize_derived(token: str) -> str:
    raw = (token or "").strip().lstrip("@")
    if not raw:
        return ""
    user = registry.resolve(raw)
    return user["alias"] if user else ""


def index(viewer_uid: str = "") -> list:
    """Every saved runner, metadata only."""
    out = []
    try:
        dirs = sorted(p for p in config.RUNNERS_ROOT.iterdir() if p.is_dir())
    except OSError:
        return out
    for directory in dirs:
        _, _, uid = directory.name.partition("__")
        owner = registry.by_id(uid) or {}
        for meta_path in sorted(directory.glob("*.json")):
            meta = _read_meta(meta_path)
            if not meta.get("name"):
                continue
            out.append({**meta,
                        "owner_alias": owner.get("alias") or meta.get("owner_alias", "?"),
                        "mine": bool(viewer_uid) and uid == viewer_uid})
    return sorted(out, key=lambda e: e.get("saved", ""), reverse=True)


def resolve(name: str, viewer_uid: str, owner: str = "") -> dict:
    """Find a runner by name — the viewer's own first, then a named colleague's."""
    key = (name or "").strip().lower()
    problem = validate_name(key)
    if problem:
        raise RunnerError(problem)

    if owner:
        target = registry.resolve(owner.strip().lstrip("@"))
        if target is None:
            raise RunnerError(f"I don't know a user called {owner!r}.")
        candidates = [target["slack_user_id"]]
    else:
        candidates = [viewer_uid] + [e["owner_id"] for e in index(viewer_uid)
                                     if e["name"] == key]

    for uid in candidates:
        if not uid:
            continue
        try:
            _, meta_path = _paths(uid, key)
        except RunnerError:
            continue
        meta = _read_meta(meta_path)
        if meta.get("name"):
            return meta
    raise RunnerError(
        f"No runner called {key!r}. Use list_job_runners to see what exists, or save "
        "one with save_job_runner."
    )


def for_user(uid: str, name: str = "") -> Optional[dict]:
    """Which runner to use: one named explicitly, the registry default, or the only one.

    Naming a saved runner is an ordinary read, so the model may do it. Changing the
    *default* stays CLI-only, so an operator's assignment cannot be redirected by a
    conversation.
    """
    if name:
        return resolve(name, uid)
    user = registry.by_id(uid, include_removed=False) or {}
    default = (user.get("job_runner") or "").strip().lower()
    if default:
        try:
            return resolve(default, uid, owner=user.get("alias", ""))
        except RunnerError:
            log.warning("runners: %s's default runner %r is not saved", uid, default)
            return None
    mine = [e for e in index(uid) if e["mine"]]
    return mine[0] if len(mine) == 1 else None


def read(name: str, viewer_uid: str, owner: str = "") -> str:
    """A runner's script, wrapped and attributed like a workflow or a template."""
    meta = resolve(name, viewer_uid, owner)
    body = script_for(meta)
    mine = meta.get("owner_id") == viewer_uid
    trust = "your-own" if mine else "reference-only"
    who = "you" if mine else f"@{meta.get('owner_alias', '?')}"
    note = "" if mine else (
        "\n[reference-only: a colleague's job script. Adapt it with the user and save "
        "the result as THEIR OWN runner, never over this one.]")
    return (f'<job_runner name="{meta["name"]}" owner="{who}" trust="{trust}">\n'
            f"{body.rstrip()}\n</job_runner>{note}")


def delete(uid: str, name: str) -> str:
    key = (name or "").strip().lower()
    problem = validate_name(key)
    if problem:
        raise RunnerError(problem)
    script_path, meta_path = _paths(uid, key)
    if not script_path.is_file():
        raise RunnerError(f"you have no runner called {key!r}.")
    _backup(script_path, uid)
    script_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return f"Deleted your `{key}` runner. The previous version was snapshotted."


def script_for(profile: dict) -> str:
    """The saved script for a runner. Re-checked every time it is used.

    Saving froze these bytes, but the destructive list can grow afterwards and the
    file lives on disk where it could be edited outside Aspen. Re-checking on use
    means the guardrail applies to what is about to run, not only to what was once
    approved — and that a runner cannot be swapped between saving and submitting.
    """
    path = Path(profile.get("script", ""))
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RunnerError(
            f"the script for runner {profile.get('name', '?')!r} is unreadable "
            f"({exc}). Save it again."
        ) from exc
    accepted = set(profile.get("problems_accepted") or [])
    outstanding = [p for p in script_problems(body) if p not in accepted]
    if outstanding:
        raise RunnerError(
            f"the script for runner {profile.get('name')!r} no longer passes its "
            f"checks ({len(outstanding)} problem(s)):\n"
            + "\n".join(f"  • {p}" for p in outstanding)
            + "\nReview and save it again."
        )
    return body


def render(profile: dict, *, job_name: str, input_name: str, output_name: str,
           ntasks: Optional[int] = None, mem_gb: Optional[int] = None,
           time_limit: str = "") -> str:
    """Fill a runner's placeholders. Every value is typed or Aspen-derived."""
    body = script_for(profile)
    defaults = profile.get("defaults") or {}

    values = {
        "[JOB_NAME]": _safe_token(job_name),
        "[INPUT]":    _safe_token(input_name),
        "[OUTPUT]":   _safe_token(output_name),
        "[NTASKS]":   str(_bounded(ntasks, defaults.get("ntasks", 16),
                                   1, config.RUNNER_MAX_NTASKS, "ntasks")),
        "[MEM_GB]":   str(_bounded(mem_gb, defaults.get("mem_gb", 64),
                                   1, config.RUNNER_MAX_MEM_GB, "memory")),
        "[TIME]":     _safe_time(time_limit or defaults.get("time", "48:00:00")),
    }
    for token, value in values.items():
        body = body.replace(token, value)
    return body


def _safe_token(value: str) -> str:
    """A filename or job name with nothing a shell could act on."""
    raw = str(value or "").strip()
    if not raw or not re.fullmatch(r"[A-Za-z0-9._-]+", raw):
        raise RunnerError(f"{value!r} is not a usable filename or job name")
    if raw.startswith("-") or raw in (".", ".."):
        raise RunnerError(f"{value!r} is not a usable filename")
    return raw


def _safe_time(value: str) -> str:
    raw = str(value or "").strip()
    if not re.fullmatch(r"\d{1,3}:[0-5]\d:[0-5]\d", raw):
        raise RunnerError(f"{value!r} is not a walltime in HH:MM:SS form")
    hours = int(raw.split(":")[0])
    if hours > config.RUNNER_MAX_HOURS:
        raise RunnerError(f"{raw} exceeds the {config.RUNNER_MAX_HOURS}-hour limit "
                          "for Aspen-submitted jobs")
    return raw


def _bounded(value, default, low: int, high: int, label: str) -> int:
    number = default if value in (None, "") else value
    try:
        number = int(number)
    except (TypeError, ValueError):
        raise RunnerError(f"{label} must be a whole number") from None
    if not low <= number <= high:
        raise RunnerError(f"{label} must be between {low} and {high} (got {number})")
    return number
