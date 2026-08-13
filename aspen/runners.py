"""
Runner profiles — what actually executes, registered by a human, never chosen at
request time.

Different people in the group run calculations differently. Sam drives the
``xas_pipeline`` ORCA→CORVUS batch; Arun submits a single ORCA job from a script
he has been using for years. Both are legitimate and neither should be forced into
the other's shape.

The design question is not *whose script* but **where the authority comes from**.
Two answers were rejected:

* *Read it from the user's ``WORKFLOW.md``.* A workflow is user-authored text that
  spec §6 and C10 deliberately make non-authoritative — it "cannot grant tools,
  relax the sandbox, or change file access". A workflow that named the command to
  run would make a user-writable text file into code execution as the bot's Unix
  account, and workflows are cross-readable, so it would not even be confined to
  the author.
* *Submit whatever ``.sh`` is in the user's tree at request time.* That is the
  design the operator's own threat reframing rules out. Users are used to owning
  their compute account and are now sharing one, so the realistic accident is a
  habitual "clean up my directory" line running as somebody else. Worse, the
  parent of the group's user trees (``/sdf/data/ssrl/smb/xas/users``) is
  group-writable with no sticky bit, so *any* member of that group — a larger set
  than the beta — could substitute a whole user directory.

So a runner is **registered once, by an operator, from a file they have read**, and
frozen. Registration is where the human review happens, and it happens once per
protocol rather than once per job. The evidence that this costs nothing: Arun's real
script is thirty-five lines of ``module load`` and ``export PATH``, plus a single
``orca input.inp > input.out``. Only the job name, the resources and the input
filename vary per job, and those are typed fields.

Per-user assignment lives on the registry record (``job_runner``), beside
``calc_root``, and is CLI-only for the reason §5.0 gives for roots: a tool that
wrote it would reinstate exactly the surface that taking paths out of the model's
hands removes.
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


def _registry_path() -> Path:
    return config.RUNNERS_FILE


def load() -> dict:
    """Every registered runner, ``{name: profile}``. Missing file is not an error."""
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("runners: %s is unreadable; treating as empty", _registry_path(),
                    exc_info=True)
        return {}
    runners = data.get("runners") if isinstance(data, dict) else None
    return {r["name"]: r for r in (runners or []) if isinstance(r, dict) and r.get("name")}


def save(profiles: dict) -> None:
    """Write the runner registry. CLI-only — nothing agent-facing calls this."""
    path = _registry_path()
    registry.ensure_private_dir(path.parent)
    payload = {"version": 1, "runners": sorted(profiles.values(), key=lambda r: r["name"])}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


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


def register(name: str, script_path: Path, *, kind: str = "direct",
             code: str = "orca", description: str = "",
             ntasks: int = 16, mem_gb: int = 64, time_limit: str = "48:00:00",
             actor: str = "", force: bool = False) -> dict:
    """Register a job-script template as a runner. Raises on refusal.

    ``script_path`` is read **now**, copied into Aspen's own storage, and frozen.
    Pointing at a file the operator does not control would defeat the point: the
    review has to bind to the bytes, not to a path whose contents can change.
    """
    key = (name or "").strip().lower()
    if not key or not NAME_RE.match(key):
        raise RunnerError(f"{name!r} isn't a usable runner name — lowercase words "
                          "joined by hyphens, like 'orca-nbo'")
    if kind not in KINDS:
        raise RunnerError(f"kind must be one of {', '.join(KINDS)}")
    if code not in ("orca",):
        raise RunnerError(f"no input validator exists for {code!r}, so Aspen will not "
                          "register a runner for it")

    source = Path(script_path).expanduser()
    try:
        body = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RunnerError(f"could not read {source}: {exc}") from exc

    problems = script_problems(body)
    if problems and not force:
        raise RunnerError(
            f"that script has {len(problems)} problem(s):\n"
            + "\n".join(f"  • {p}" for p in problems)
            + "\n\nFix them, or pass --force if you have read the script and accept it."
        )
    if problems:
        log.warning("runners: registering %s with %d unresolved problem(s) via --force",
                    key, len(problems))

    profiles = load()
    if key in profiles and not force:
        raise RunnerError(f"a runner called {key!r} already exists; pass --force to replace it")

    # Copy the reviewed bytes into Aspen's storage and freeze them there.
    registry.ensure_private_dir(config.RUNNERS_DIR)
    stored = config.RUNNERS_DIR / f"{key}.sh"
    if stored.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copyfile(stored, config.RUNNERS_DIR / f"{key}-{stamp}.sh.bak")
    stored.write_text(body, encoding="utf-8")
    stored.chmod(0o600)

    profiles[key] = {
        "name": key,
        "kind": kind,
        "code": code,
        "description": " ".join((description or "").split())[:200],
        "script": str(stored),
        "source": str(source.resolve()),
        "defaults": {"ntasks": int(ntasks), "mem_gb": int(mem_gb), "time": time_limit},
        "registered": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "registered_by": actor or f"cli:{os.environ.get('USER', '?')}",
        "problems_accepted": problems,
    }
    save(profiles)
    log.info("runners: registered %s (%s/%s)", key, kind, code)
    return profiles[key]


def get(name: str) -> Optional[dict]:
    return load().get((name or "").strip().lower())


def for_user(uid: str) -> Optional[dict]:
    """The runner assigned to a user, or None.

    Read from the registry record, which only ``aspen-users`` writes. There is no
    agent-facing way to change it, and no way for a conversation to select a
    different one — the same discipline as ``calc_root``.
    """
    user = registry.by_id(uid, include_removed=False) or {}
    name = (user.get("job_runner") or "").strip().lower()
    if not name:
        return None
    profile = get(name)
    if profile is None:
        log.warning("runners: %s is assigned runner %r, which is not registered",
                    uid, name)
    return profile


def script_for(profile: dict) -> str:
    """The frozen script body for a runner. Re-checked every time it is used.

    Registration froze these bytes, but the destructive-command list can grow
    afterwards — and the file lives on disk, where an operator could edit it
    outside the CLI. Re-checking on use means the guardrail applies to what is
    about to run rather than only to what was once approved.
    """
    path = Path(profile.get("script", ""))
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RunnerError(
            f"the script for runner {profile.get('name', '?')!r} is unreadable "
            f"({exc}). An operator needs to re-register it."
        ) from exc

    fresh = script_problems(body)
    accepted = set(profile.get("problems_accepted") or [])
    unaccepted = [p for p in fresh if p not in accepted]
    if unaccepted:
        raise RunnerError(
            f"the script for runner {profile.get('name')!r} no longer passes its "
            f"checks ({len(unaccepted)} problem(s)):\n"
            + "\n".join(f"  • {p}" for p in unaccepted)
            + "\nAn operator needs to review and re-register it."
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
