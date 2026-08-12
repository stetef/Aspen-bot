"""
Getting people set up — and knowing when to stop asking.

Two things each person can have: a **workflow** (how they work, in their own
words) and a **calculations root** (where their files are). Neither is required,
and the mix is the point — a PI may want to read across the group and own
neither, while a student wants both, and most people arrive with one before the
other.

That makes "do you want to set this up?" a question with three answers, not two:

    has it     — derived from reality (the file exists / the root is set)
    declined   — stored here
    neither    — everything else, and the only case worth a nudge

Only the decline is stored. Recording "done" would duplicate state that can
desync from the filesystem, so it is always re-derived.

**Declining a root is not just silence.** Someone with no tree of their own would
otherwise fall back to the shared default — which is somebody else's work, served
to them as "your files". So a declined root makes unqualified paths an error that
asks whose files were meant, which is the correct model of a reader who owns
nothing (see roots.resolve).

Nudges are rationed hard: **at most one item, on the first turn of a thread
only.** Two asks in one message gets both ignored, and the old behavior — a line
in every single turn, forever — is exactly the nagging this exists to stop.
"""

import logging
from datetime import date
from typing import Optional

from . import config, demo, metadata, pending, registry, roots, workflows

log = logging.getLogger("aspen")

ITEMS = ("workflow", "calc_root")

# Declinable, but not a first-turn item. A project description is per *project*,
# so there is no "they have it" to derive for the person — only for the directory
# in front of them. It is offered where it is noticed (a listing) rather than on
# the way in, and declining it silences the offer everywhere.
PROJECT_NOTES = "project_notes"
DECLINABLE = (*ITEMS, PROJECT_NOTES)

_LABEL = {
    "workflow": "a workflow file",
    "calc_root": "their own calculations directory",
    PROJECT_NOTES: "notes describing their projects",
}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def declined(uid: str, item: str) -> bool:
    user = registry.by_id(uid)
    if user is None:
        return False
    return bool((user.get("declined") or {}).get(item))


def has(uid: str, item: str) -> bool:
    """Derived, never stored — the filesystem and the registry are the truth."""
    if item == "workflow":
        return workflows.has_workflow(uid)
    if item == PROJECT_NOTES:
        return False        # per-project; nothing about the person settles it
    user = registry.by_id(uid)
    return bool(user and user.get("calc_root"))


def state(uid: str, item: str) -> str:
    if has(uid, item):
        return "has"
    return "declined" if declined(uid, item) else "missing"


def missing(uid: str) -> list[str]:
    """Items worth raising with this person, in the order to raise them."""
    return [item for item in ITEMS if state(uid, item) == "missing"]


def decline(uid: str, item: str) -> str:
    """Record that someone doesn't want one, so Aspen stops offering.

    Safe for the agent to call: this field can only ever turn nudges *off*. The
    worst a confused or injected agent achieves is Aspen becoming quieter, which
    is a reduction in what it does, not an escalation — unlike everything that
    grants access, which stays CLI-only.
    """
    if item not in DECLINABLE:
        return f"Error: '{item}' is not something to set up (expected: {', '.join(DECLINABLE)})."
    user = registry.by_id(uid)
    if user is None:
        return "Error: you are not in Aspen's user registry, so there's nothing to record."
    if has(uid, item):
        return (f"You already have {_LABEL[item]}, so there's nothing to decline. "
                "Tell me if you'd like it changed instead.")

    users = []
    for entry in registry.users(include_removed=True):
        if entry["slack_user_id"] == uid:
            marks = dict(entry.get("declined") or {})
            marks[item] = date.today().isoformat()
            entry = dict(entry, declined=marks)
        users.append(entry)
    registry.save(users)
    pending.resolve("calc_root" if item == "calc_root" else "access", uid)

    if item == "calc_root":
        return ("Noted — you don't have calculations of your own, so I won't ask "
                "again. I'll always need you to say whose files you mean "
                "(e.g. @arun/thermolysin), since there's no default for you.")
    if item == PROJECT_NOTES:
        return ("Noted — I won't suggest project READMEs again. Everything still "
                "works without them; ask any time if you want one drafted.")
    return ("Noted — I won't ask about a workflow again. Say the word any time "
            "if you change your mind.")


def undecline(uid: str, item: str) -> bool:
    """Clear a decline (CLI-side), for when someone changes their mind."""
    changed = False
    users = []
    for entry in registry.users(include_removed=True):
        if entry["slack_user_id"] == uid and (entry.get("declined") or {}).get(item):
            marks = dict(entry["declined"])
            marks.pop(item, None)
            entry = dict(entry, declined=marks)
            changed = True
        users.append(entry)
    if changed:
        registry.save(users)
    return changed


# --------------------------------------------------------------------------- #
# Asking (once, and only once)
# --------------------------------------------------------------------------- #
def nudge_lines(uid: str, first_turn: bool) -> list[str]:
    """At most one setup nudge, on a thread's opening turn only."""
    if not first_turn:
        return []
    outstanding = missing(uid)
    if not outstanding:
        return []

    item = outstanding[0]
    if item == "workflow":
        return [
            "They have no workflow file. If — and only if — this conversation "
            "gives you a natural opening, offer once to save how they work with "
            "write_workflow. If they say no or aren't interested, call "
            "decline_setup(item=\"workflow\") so you never ask again. Don't "
            "derail the question they actually asked."
        ]
    return [
        "They have no calculations directory of their own, so unqualified paths "
        "fall back to the shared default — which may be someone else's work. If "
        "there's a natural opening, ask once where their calculations live and "
        "call request_calc_root with the path (an admin approves it; you cannot "
        "set it). If they don't have any of their own — a reader rather than a "
        "runner — call decline_setup(item=\"calc_root\")."
    ]


# Which (person, project) pairs have already had the offer this process. Held in
# memory on purpose: it is a politeness counter, not a decision. Writing it to the
# registry would put a row per project in a file whose whole point is that it
# holds grants, and a restart re-offering once is a far smaller cost than that.
# Someone who actually does not want them declines, which *is* persisted.
_offered: set = set()
_OFFERED_CAP = 2048


def project_notes_nudge(uid: str, project: str, scope: dict) -> str:
    """Offer to draft a README for a project of the speaker's that has none.

    Rationed three ways, because a line on every listing is nagging: their own
    projects only (a colleague's directory is not theirs to document), nothing
    that already has a description from any source, and once per project per
    process. A recorded decline silences it everywhere.
    """
    if not uid or not project or demo.active_for(uid) is not None:
        return ""
    if scope.get("owner_id") != uid or scope.get("kind") == "shared":
        return ""
    if registry.by_id(uid) is None or declined(uid, PROJECT_NOTES):
        return ""

    key = (uid, str(scope.get("path", "")), project)
    if key in _offered:
        return ""
    text = metadata.nudge_text(project, scope)
    if not text:
        return ""
    if len(_offered) >= _OFFERED_CAP:       # unbounded growth is the only real risk
        _offered.clear()
    _offered.add(key)
    return text


def request_root(uid: str, path: str, client=None) -> str:
    """Ask the admin to point this person at a calculations directory.

    The agent deliberately cannot set the root itself. The whole read surface
    rests on *the model passes a name, never a path*; a tool that wrote a path
    into the registry would reinstate exactly the surface that removes, and
    validation cannot help — pointing someone at a colleague's tree passes every
    check while silently relabelling whose work is whose. So this records the ask
    and hands the admin a command.
    """
    user = registry.by_id(uid)
    if user is None:
        return "Error: you are not in Aspen's user registry, so I can't file that."

    path = (path or "").strip()
    problem = roots.validate(path, for_uid=uid) if path else "no path given"
    entry = pending.raise_request(
        "calc_root", uid, client=client,
        display_name=user["display_name"], detail=path if not problem else "",
    )
    who = "an admin" if not config.ADMIN_USER_ID else "the admin"

    if problem and path:
        # File it anyway: a path Aspen can't use is still a person asking, and the
        # admin can resolve what a validation message cannot (a typo, a mount that
        # isn't up yet, a directory only they can create).
        return (f"I've asked {who} to set up your calculations directory, but I "
                f"couldn't verify `{path}` myself: {problem}. They'll take it from "
                "here — you may want to double-check the path with them.")
    if not path:
        return (f"I've let {who} know you'd like your own calculations directory. "
                "If you tell me the full path, I'll pass that along too and save "
                "them a question.")
    return (f"Asked {who} to point Aspen at `{path}` for you"
            + (f" (asked {entry['count']}× now)" if entry.get("count", 1) > 1 else "")
            + ". They'll need to approve it; it takes effect on your next message "
              "after that.")


def summary(uid: str) -> Optional[str]:
    """One line for the CLI, e.g. ``workflow: has   calculations: declined``."""
    user = registry.by_id(uid)
    if user is None:
        return None
    return "  ".join(f"{item}: {state(uid, item)}" for item in ITEMS)
