"""
Named calculations roots — whose tree is whose.

Aspen used to have exactly one ``CALCULATIONS_ROOT``. It now has *N*: one per
user who has set one (``calc_root`` on their registry record), plus any number of
**shared** roots for group project data that belongs to nobody in particular.
Anything with no root of its own falls back to ``CALCULATIONS_ROOT``, so a
deployment that never sets a root behaves exactly as it did before.

**This module models naming and ownership, never permission.** The read boundary
is flat — everyone may read everyone, exactly as on the shared filesystem — and
if it ever stops being flat, the enforcement is Unix groups on the bot's own
account and the read simply fails. A permission model here would be a copy of the
real one, and a copy can only ever be wrong in the direction of showing something
it shouldn't. What ownership *does* decide: the default scope of a bare path, the
attribution prefix on every path handed back, and who may write.

**The model names a root, it never names a path.** Tools take an ``owner`` token
(an alias, a Slack ID, or a shared root's name) and this module resolves it
against the registry. That is the same property that makes ``write_workflow``
unspoofable: a model can be talked into passing any value it is told to, but it
cannot pass a value it never receives.

Display form is ``@name/relative/path`` — accepted on input, always used on
output, so a path in a reply says whose tree it came from.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from . import config, registry

log = logging.getLogger("aspen")

# Marks an owner-qualified path: "@arun-asundi/thermolysin/run-3".
PREFIX = "@"


# --------------------------------------------------------------------------- #
# The roster
# --------------------------------------------------------------------------- #
def _user_root(user: dict) -> Path:
    """A user's own root — theirs if set, else the shared default."""
    declared = (user.get("calc_root") or "").strip()
    return Path(declared).resolve() if declared else config.CALCULATIONS_ROOT


def scopes(include_removed: bool = False) -> list[dict]:
    """Every addressable root: ``{name, kind, path, owner_id, label}``.

    ``kind`` is ``"user"`` or ``"shared"``. Users come first and shared roots
    after, which is also the order they are offered to the model.
    """
    out = []
    for user in registry.users(include_removed=include_removed):
        out.append({
            "name": user["alias"],
            "kind": "user",
            "path": _user_root(user),
            "owner_id": user["slack_user_id"],
            "label": user["display_name"],
        })
    for name, path in config.SHARED_CALC_ROOTS.items():
        out.append({
            "name": name,
            "kind": "shared",
            "path": Path(path).resolve(),
            "owner_id": "",
            "label": f"shared: {name}",
        })
    return out


def for_user(uid: str) -> Path:
    """The root a bare (unqualified) path resolves against for this speaker."""
    user = registry.by_id(uid)
    return _user_root(user) if user else config.CALCULATIONS_ROOT


def by_name(token: str) -> Optional[dict]:
    """Resolve an alias / Slack ID / shared-root name to a scope, or None.

    Case matters in exactly one direction: aliases and shared-root names are
    lowercase by construction, but a Slack ID is not — ``U01ARUN`` must survive
    the lookup, so the raw token is tried before the folded one.
    """
    token = (token or "").strip().lstrip(PREFIX)
    if not token:
        return None
    for scope in scopes(include_removed=True):
        if scope["kind"] == "shared" and scope["name"] == token.lower():
            return scope
    user = registry.resolve(token, include_removed=True)
    if user is None and token != token.lower():
        user = registry.resolve(token.lower(), include_removed=True)
    if user is None:
        return None
    return {
        "name": user["alias"],
        "kind": "user",
        "path": _user_root(user),
        "owner_id": user["slack_user_id"],
        "label": user["display_name"],
    }


def scope_for_viewer(uid: str) -> dict:
    """The speaker's own scope, so bare paths can be qualified on the way out."""
    user = registry.by_id(uid)
    if user is None:
        return {"name": "", "kind": "user", "path": config.CALCULATIONS_ROOT,
                "owner_id": uid, "label": ""}
    return {"name": user["alias"], "kind": "user", "path": _user_root(user),
            "owner_id": user["slack_user_id"], "label": user["display_name"]}


def is_rootless(uid: str) -> bool:
    """True for someone who has said they have no calculations of their own.

    Read straight from the registry rather than through ``setup`` — that module
    imports this one, and the check has to be available inside ``resolve``.
    """
    user = registry.by_id(uid)
    if user is None or user.get("calc_root"):
        return False
    return bool((user.get("declined") or {}).get("calc_root"))


def known_names() -> str:
    """Comma-separated roots, for error messages that have to teach."""
    return ", ".join(f"{PREFIX}{s['name']}" for s in scopes()) or "(none)"


# --------------------------------------------------------------------------- #
# The @name/path grammar
# --------------------------------------------------------------------------- #
def split(rel: str) -> tuple[str, str]:
    """``'@arun/thermolysin/x'`` -> ``('arun', 'thermolysin/x')``.

    An unqualified path returns ``('', rel)`` — it belongs to whoever is speaking.
    """
    rel = (rel or "").strip()
    if not rel.startswith(PREFIX):
        return "", rel
    body = rel[len(PREFIX):]
    name, _, remainder = body.partition("/")
    return name.strip(), (remainder.strip() or ".")


def qualify(scope_name: str, rel: str) -> str:
    """The canonical display form for a path — always says whose tree it is."""
    rel = (rel or ".").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel or "."
    if not scope_name:
        return rel
    return f"{PREFIX}{scope_name}" if rel == "." else f"{PREFIX}{scope_name}/{rel}"


# --------------------------------------------------------------------------- #
# Resolution + fencing
# --------------------------------------------------------------------------- #
def resolve(rel: str, owner: str, viewer_uid: str) -> tuple[Optional[Path], dict, str]:
    """Resolve ``rel`` (optionally ``@name/``-prefixed) to an absolute path.

    Returns ``(path, scope, error)``; ``path`` is None exactly when ``error`` is
    set. The path is confirmed to sit inside the resolved root *after* symlinks
    are followed, so neither ``..`` nor a symlink out of the tree can escape.

    ``owner`` (a tool argument) and an ``@name/`` prefix are two spellings of the
    same thing. Both given and disagreeing is an error rather than a silent
    precedence rule — the ambiguity is the model's to resolve, not ours to guess.
    """
    prefix_name, bare = split(rel)
    owner = (owner or "").strip().lstrip(PREFIX)
    if prefix_name and owner and prefix_name.lower() != owner.lower():
        return None, {}, (
            f"Error: '{rel}' is under {PREFIX}{prefix_name} but owner='{owner}' was "
            "also given. Pass one or the other, not both."
        )

    token = prefix_name or owner
    if token:
        scope = by_name(token)
        if scope is None:
            return None, {}, (
                f"Error: no root named '{token}'. Known roots: {known_names()}."
            )
    else:
        scope = scope_for_viewer(viewer_uid)
        # Someone who has said they have no calculations of their own — a reader
        # rather than a runner — must not silently get the shared default, which
        # is somebody else's work served to them as "your files". Make them say
        # whose they mean instead of guessing wrong.
        if is_rootless(viewer_uid):
            return None, scope, (
                f"Error: '{rel}' is unqualified, but this user has no calculations "
                f"of their own — there is no default for them. Say whose files you "
                f"mean: {PREFIX}<name>/{bare.lstrip('./') or '...'} or owner=<name>. "
                f"Known roots: {known_names()}."
            )

    root = scope["path"]
    try:
        resolved = (root / bare).resolve()
        resolved.relative_to(root)          # raises ValueError if it escapes
    except (ValueError, OSError):
        return None, scope, (
            f"Error: '{qualify(scope['name'], bare)}' is outside the allowed directory."
        )
    return resolved, scope, ""


def relative_to_scope(path: Path, scope: dict) -> str:
    """A path back in display form, for echoing results."""
    try:
        return qualify(scope.get("name", ""), str(path.relative_to(scope["path"])))
    except (ValueError, KeyError):
        return str(path)


# --------------------------------------------------------------------------- #
# Validation (CLI write-time and startup)
# --------------------------------------------------------------------------- #
def validate(path: str, *, for_uid: str = "") -> Optional[str]:
    """Return an error message if ``path`` is unusable as a calculations root.

    Checked as the *bot's own Unix user*, which is the identity that will do the
    reading — a root the admin can see but the service account cannot is a root
    that fails at turn time instead of at configuration time.
    """
    if not path or not str(path).strip():
        return "path is empty"
    try:
        candidate = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return f"could not resolve {path!r} ({exc})"

    if not candidate.exists():
        return f"{candidate} does not exist"
    if not candidate.is_dir():
        return f"{candidate} is not a directory"
    if not os.access(candidate, os.R_OK | os.X_OK):
        return (f"{candidate} is not readable by {_whoami()} — the account Aspen runs "
                "as needs read+execute on it")

    # Never inside the agent's own state or writable areas: a root there would let
    # sandboxed code edit "project data" that steers later turns, and a root that
    # contains the registry would expose it through the read tools.
    for label, area in (("the state directory", config.STATE_DIR),
                        ("the workspace", config.WORKSPACE_ROOT)):
        if _under(candidate, area) or _under(area, candidate):
            return f"{candidate} overlaps {label} ({area})"
    for area in config.SANDBOX_WRITE_PATHS:
        if _under(candidate, Path(area).expanduser()):
            return f"{candidate} is inside a sandbox-writable path ({area})"

    # Nesting would make one person's fence silently enclose another's tree:
    # _safe_path checks containment, so a root inside a root is not a boundary.
    for scope in scopes(include_removed=True):
        if for_uid and scope["owner_id"] == for_uid:
            continue
        other = scope["path"]
        if other == candidate:
            return f"{candidate} is already {PREFIX}{scope['name']}'s root"
        if _under(candidate, other) or _under(other, candidate):
            return (f"{candidate} is nested with {PREFIX}{scope['name']}'s root ({other}) "
                    "— roots must not contain one another")
    return None


def _whoami() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:                       # pragma: no cover — getpass is robust
        return "the bot's user"


def _under(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def check_all() -> list[str]:
    """Problems with the configured roots, for the startup guard. Empty = fine."""
    problems, seen = [], {}
    for scope in scopes():
        path = scope["path"]
        name = f"{PREFIX}{scope['name']}"
        if not path.is_dir():
            problems.append(f"{name}: {path} is not a readable directory")
            continue
        if not os.access(path, os.R_OK | os.X_OK):
            problems.append(f"{name}: {path} is not readable by {_whoami()}")
        for other_name, other in seen.items():
            if other == path:
                continue                    # sharing the fallback root is expected
            if _under(path, other) or _under(other, path):
                problems.append(f"{name} ({path}) is nested with {other_name} ({other})")
        seen[name] = path
    return problems


# --------------------------------------------------------------------------- #
# Roster for the per-turn context block
# --------------------------------------------------------------------------- #
def preamble_lines(viewer_uid: str) -> list[str]:
    """How the roots are described to the model each turn.

    Cheap by design: a name and a one-line role per root, no directory listing.
    Without this the model cannot discover that anyone else's tree exists, since
    every tool takes a name rather than a path.
    """
    all_scopes = scopes()
    if len(all_scopes) <= 1 and not config.SHARED_CALC_ROOTS:
        return []                            # single-root deployment: nothing to say

    mine = scope_for_viewer(viewer_uid)
    lines = [
        f"Calculations are split by owner. An unqualified path means {PREFIX}{mine['name']}"
        f"'s own files ({mine['path']}); write {PREFIX}<name>/path to read someone "
        "else's, or pass owner=<name>. Everyone may READ every root."
    ]

    # Group by directory, not by person: everyone without a root of their own
    # shares the default, and listing seven aliases for one tree would both cost
    # tokens and imply seven trees that do not exist.
    groups: dict = {}
    for scope in all_scopes:
        groups.setdefault(str(scope["path"]), []).append(scope)

    described = []
    for path, members in groups.items():
        if path == str(mine["path"]):
            continue                        # that's the unqualified default, said above
        shared = next((m for m in members if m["kind"] == "shared"), None)
        if shared is not None:
            described.append(f"{PREFIX}{shared['name']} (shared group data)")
        elif len(members) == 1:
            described.append(f"{PREFIX}{members[0]['name']} ({members[0]['label']})")
        else:
            names = ", ".join(f"{PREFIX}{m['name']}" for m in members[:4])
            more = f" (+{len(members) - 4} more)" if len(members) > 4 else ""
            described.append(f"{names}{more} — all the same shared tree")
    if described:
        lines.append("Other roots: " + "; ".join(described))

    peers = [s["name"] for s in groups.get(str(mine["path"]), []) if s["name"] != mine["name"]]
    if peers:
        lines.append(
            f"Note: {', '.join(PREFIX + p for p in peers[:6])}"
            + (" and others" if len(peers) > 6 else "")
            + " read the same tree as the speaker — an unqualified path covers their work too."
        )
    lines.append(
        "You may WRITE only metadata, only for a root you are entitled to — and it "
        "is stored in Aspen's own area, never inside anyone's calculations tree."
    )
    return lines
