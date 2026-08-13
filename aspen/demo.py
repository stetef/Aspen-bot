"""
DEMO mode — let someone see Aspen work before they are anyone.

Type ``DEMO`` in a DM and Aspen walks you through the whole arc it normally
takes days to see: being turned away, the request that reaches the admin, being
welcomed, saving a workflow, being pointed at some calculations, and getting a
plot back. Against fabricated data, without being added as a user, and without
touching anything real.

Three properties make that safe to offer to anyone in the workspace:

**Scope isolation.** This is the one place the "everyone reads everyone" rule
(``roots.py``) does *not* apply. A demo visitor is not a group member, so for
them the only addressable root is the demo tree — real roots are not listed, not
addressable by name, and do not exist as far as that session is concerned.

**Nothing is written.** No registry entry, not even a temporary one: the whole
admission story is "no message can widen the allowlist", and a demo that writes
to ``users.json`` would put a crack in the property everything else rests on. The
workflow and notes a visitor "saves" live in this module's memory for the length
of the session and are then gone. The only thing that reaches disk is a figure in
the sandbox's own scratch area.

**Nobody gets paged.** The admin request is rendered *into the demo thread* —
the card the admin would have received, so the visitor can see and approve it —
rather than actually sent. A demo that can DM the admin is a spam vector the
moment a channel discovers it. ``ASPEN_DEMO_REAL_ADMIN_DM=true`` opts into the
real thing for rehearsing.

Costs are real even when the data isn't, so demo turns are rate-limited like any
other, capped per session, and capped per day across everyone.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from . import config, registry

log = logging.getLogger("aspen")

TRIGGER = "demo"
SCOPE_NAME = "demo"

# The walkthrough, in order. Each stage tells the agent what this beat is *for*;
# the agent does the talking. Stages advance on what the visitor and agent
# actually do, not on a timer, so someone who wanders off-script is followed
# rather than corrected.
STAGES = ("welcome", "request", "approved", "workflow", "explore", "analyze",
          "capabilities", "done")


@dataclass
class DemoSession:
    """One visitor's walkthrough. Lives in memory and nowhere else."""
    user_id: str
    thread: str
    started: float = field(default_factory=time.monotonic)
    turns: int = 0
    stage: str = "welcome"
    approved: bool = False
    # Their Slack display name, resolved once (see ``identify``). Empty until
    # then, and empty for good if Slack can't be reached — the card degrades to
    # placeholders rather than inventing a name.
    display_name: str = ""
    workflow: str = ""
    notes: dict = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.started

    def advance(self, stage: str) -> None:
        if stage in STAGES and STAGES.index(stage) > STAGES.index(self.stage):
            self.stage = stage
            log.info("demo: %s reached stage %s", self.user_id, stage)


# thread key -> session. Bounded by MAX_SESSIONS; oldest evicted first.
_SESSIONS: dict = {}
# Crude daily counter so a curious channel can't run up a bill: {date: count}.
_STARTS: dict = {}


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    return bool(config.DEMO_ENABLED and config.DEMO_ROOT.is_dir())


def is_trigger(text: str) -> bool:
    """Does this message ask to start a demo? Deliberately strict — a message
    that merely mentions the word should not hijack somebody's question."""
    return (text or "").strip().strip("`*_.!").lower() == TRIGGER


def get(thread: str) -> Optional[DemoSession]:
    session = _SESSIONS.get(thread)
    if session is None:
        return None
    if session.age_seconds > config.DEMO_SESSION_TTL:
        _SESSIONS.pop(thread, None)
        return None
    return session


def active_for(user_id: str) -> Optional[DemoSession]:
    """Any live session belonging to this user — the seam the tools ask about."""
    for session in list(_SESSIONS.values()):
        if session.user_id == user_id and session.age_seconds <= config.DEMO_SESSION_TTL:
            return session
    return None


def start(user_id: str, thread: str) -> tuple[Optional[DemoSession], str]:
    """(session, refusal). A refusal is a sentence to show the visitor."""
    if not enabled():
        return None, ("The demo isn't available on this deployment. "
                      "Ask an admin about access instead.")

    today = date.today().isoformat()
    _STARTS.setdefault(today, 0)
    if _STARTS[today] >= config.DEMO_MAX_STARTS_PER_DAY:
        return None, ("The demo has been run its limit of times today — it costs "
                      "real model time even though the data is made up. Try "
                      "tomorrow, or ask an admin for access.")

    while len(_SESSIONS) >= config.DEMO_MAX_SESSIONS:
        oldest = min(_SESSIONS, key=lambda key: _SESSIONS[key].started)
        _SESSIONS.pop(oldest, None)

    session = DemoSession(user_id=user_id, thread=thread)
    _SESSIONS[thread] = session
    _STARTS[today] += 1
    log.info("demo: started for %s (%d today)", user_id, _STARTS[today])
    return session, ""


def end(thread: str) -> None:
    _SESSIONS.pop(thread, None)


def clear() -> None:
    """Test seam — drop all sessions and counters."""
    _SESSIONS.clear()
    _STARTS.clear()


def note_turn(session: DemoSession) -> str:
    """Count a turn; return a refusal if the session has run long enough."""
    session.turns += 1
    if session.turns > config.DEMO_MAX_TURNS:
        end(session.thread)
        return ("That's the end of the demo — it's capped so it can't run up a "
                "bill. Type *DEMO* to start again, or ask an admin for real access.")
    return ""


# --------------------------------------------------------------------------- #
# The fake identity (never persisted)
# --------------------------------------------------------------------------- #
def identify(session: DemoSession, client) -> str:
    """Resolve the visitor's Slack display name, once. Never raises.

    The real request card names the person, because ``pending`` captures their
    profile when they are turned away. The demo card should look like the real
    one, so it does the same lookup — this is the visitor's own name, shown back
    to them in their own DM, so there is nothing to leak.
    """
    if session.display_name or client is None:
        return session.display_name
    try:
        info = client.users_info(user=session.user_id)["user"]
        profile = info.get("profile", {}) or {}
        for value in (profile.get("display_name"), profile.get("real_name"),
                      info.get("real_name"), info.get("name")):
            # Coerce rather than trust: these come from an external API, and a
            # non-string reaching registry.slugify raises inside the card.
            if isinstance(value, str) and value.strip():
                session.display_name = value.strip()
                break
    except Exception:
        log.debug("demo: could not resolve a name for %s", session.user_id, exc_info=True)
    return session.display_name


def as_user(session: DemoSession) -> dict:
    """A registry-shaped record for the visitor.

    Shaped like a registry entry so the read path can use it without special
    cases — but it is never saved, never in ``allowed_ids()``, and therefore
    never authorizes anything. Admission for this user is still false; the demo
    is let through by an explicit branch in the Slack front-end, not by
    pretending they are registered.
    """
    return {
        "slack_user_id": session.user_id,
        "alias": SCOPE_NAME,
        "display_name": session.display_name or "Demo visitor",
        "role": "member",
        "status": "active",
        "calc_root": str(config.DEMO_ROOT),
        "unix_user": "",
        "declined": {},
    }


def scope(session: DemoSession) -> dict:
    """The only calculations root a demo session can reach."""
    return {
        "name": SCOPE_NAME,
        "kind": "demo",
        "path": config.DEMO_ROOT,
        "owner_id": session.user_id,
        "label": "the demo calculations",
    }


# --------------------------------------------------------------------------- #
# In-session storage (memory, not disk)
# --------------------------------------------------------------------------- #
def save_workflow(session: DemoSession, content: str) -> str:
    session.workflow = content
    session.advance("explore")
    return ("Saved your workflow — for this demo it lives in memory only, so it "
            "disappears when we're done. For a real user it would be a file only "
            "they can write.")


def read_workflow(session: DemoSession) -> str:
    if not session.workflow:
        return ("You don't have a workflow yet. Tell me how you like to run "
                "calculations and I'll save one.")
    return (f'<workflow owner="Demo visitor" alias="{SCOPE_NAME}" trust="your-own">\n'
            "This is the speaker's own workflow — their standing preferences.\n\n"
            f"{session.workflow.strip()}\n</workflow>")


def save_notes(session: DemoSession, project: str, content: str) -> str:
    session.notes[project] = content
    session.advance("capabilities")
    return (f"Recorded notes for @{SCOPE_NAME}/{project} — in memory for the demo, "
            "and in Aspen's own storage for a real user (never in the calculations "
            "tree).")


def read_notes(session: DemoSession, project: str) -> str:
    content = session.notes.get(project)
    if not content:
        return f"No notes recorded for @{SCOPE_NAME}/{project} yet."
    return (f'<metadata project="@{SCOPE_NAME}/{project}" written_by="aspen">\n'
            f"{content.strip()}\n</metadata>")


# --------------------------------------------------------------------------- #
# The admin request, rendered rather than sent
# --------------------------------------------------------------------------- #
def request_card(session: DemoSession, what: str = "access", detail: str = "") -> str:
    """What the admin would have received. Shown to the visitor instead."""
    # Use their real name and the alias it would actually produce, exactly as
    # pending.command_for does for a real request. Placeholders only if the
    # lookup failed — a demo of the real thing should show the real thing.
    name = session.display_name
    alias = registry.slugify(name) if name else ""
    who = name or "Demo visitor"

    if what == "access":
        command = (f'./aspen-users add {session.user_id} '
                   f'--alias {alias or "<their-alias>"} '
                   f'--name "{name or "<their name>"}"')
        line = f"{who} <{session.user_id}> wants access"
    else:
        command = f"./aspen-users set-root {alias or '<their-alias>'} {detail or '<path>'}"
        line = f"{who} wants their own calculations root"
    # Ends at the card. It used to close with "Say *approve*…", which the agent
    # then asked again in its own words — clunky, and wrong twice over: an
    # admin's DM does not address the visitor, so that line broke the very
    # illusion the card exists to create. Asking is the agent's job (see the
    # `request` stage guidance).
    return (
        "Here's the message your admin would get in Slack — this one is *not* "
        "actually sent, since this is a demo:\n\n"
        f"> *Aspen request* — {line}\n"
        f"> ```\n> {command}\n> ```\n"
        "> _Run it if you agree; ignore it if you don't._"
    )


def approve(session: DemoSession) -> str:
    session.approved = True
    session.advance("approved")
    return ("Approved — in the real thing an admin ran that command, and it takes "
            "effect on your next message. You're now a regular user as far as the "
            "rest of this demo is concerned.")


# --------------------------------------------------------------------------- #
# Per-turn guidance
# --------------------------------------------------------------------------- #
_GUIDANCE = {
    "welcome": (
        "This is the FIRST turn of a demo. Open in this order, and keep the whole "
        "thing to about four short lines:\n"
        "1. Who you are — one line. Aspen, a research assistant for this group, "
        "for exploring HPC computational chemistry results from Slack.\n"
        "2. What is about to happen — say plainly that you'll walk them through "
        "what using Aspen actually looks like, from being a stranger to getting a "
        "plot back. Lead with this; it is what they typed DEMO for.\n"
        "3. Only then, ONE short clause noting the data is made up so nobody's "
        "real work is on show. A sentence, not a paragraph, and not the opener — "
        "starting with a disclaimer makes a tour feel like a waiver.\n"
        "4. Begin: normally they'd be turned away here because they aren't on the "
        "allowlist. Show what that looks like by calling demo_request_card.\n"
        "Never claim to have shown them something you did not: the card exists "
        "only once that tool has run, and if the result asks you to reproduce it, "
        "reproduce it."
    ),
    "request": (
        "They have seen the request card. Ask them ONCE, in your own words, to say "
        "'approve' so the tour can continue — the card deliberately doesn't ask, "
        "so this is the only place it is asked. Don't repeat the request in a "
        "later message; if they say something else, just answer it and let them "
        "come back to it. When they do approve, call demo_approve. If they ask "
        "what happens when a real admin says no, tell them honestly: nothing, and "
        "the request waits in the queue for `aspen-users requests`."
    ),
    "approved": (
        "They are 'in'. Next beat: the workflow. Explain what a workflow file is "
        "(their own notes on how they run and interpret calculations, which you "
        "read before planning work) and offer to save one. If they don't want to "
        "write one, offer a short plausible example for a DFT person and save that "
        "with write_workflow so they can see the round trip."
    ),
    "explore": (
        "Next beat: their calculations. They have a demo root with two projects — "
        "an Fe-O distance scan (fe-porphyrin-scan) and a spin-state pair "
        "(spin-states). Browse with list_directory, and point out that in the real "
        "thing this would be *their* directory and a colleague's would be "
        "'@alias/...'. A good hook: one run in the scan did not converge — find it "
        "with search_files and say which one."
    ),
    "analyze": (
        "The payoff: plot something, for real — the sandbox can reach the demo "
        "project and will upload the figure into this thread. Use "
        "run_python_analysis on fe-porphyrin-scan "
        "to pull FINAL SINGLE POINT ENERGY out of each */*-orca.log, parse the "
        "Fe-O distance from the directory name, and plot energy against distance. "
        "Say where the minimum is. Then offer to record what you found with "
        "write_metadata, and explain those notes live in Aspen's own storage — you "
        "never write inside anyone's calculations. When that lands, move on to "
        "what the tour has not covered yet."
    ),
    "capabilities": (
        "They have seen the core loop. Now cover what the tour did NOT reach, "
        "briefly — a few lines, not a brochure:\n"
        "• Their own calculations directory. Offer to show what asking for one "
        "looks like: call demo_request_card(what=\"calc_root\", path=…) with a "
        "plausible path (theirs if they name one) so they see the second kind of "
        "card and the `aspen-users set-root` command an admin would run. Say that "
        "you cannot set it yourself — an admin approves it — and that until then "
        "an unqualified path means the shared default.\n"
        "• Reading colleagues' work by name (@alias/project), since everyone can "
        "read every root, and that 'who else has run this?' is a normal question.\n"
        "• Slurm job status — squeue/sacct/sinfo. Say plainly that it is OFF in "
        "this demo because it would query the REAL cluster and everything here is "
        "fabricated; describe what it would show instead of pretending to run it.\n"
        "• Running calculations, if the deployment has it on. For a real user Aspen "
        "can submit a batch through the group pipeline, or a single job from one of "
        "their own saved input templates using their own job script. Worth "
        "describing accurately, because the shape is the point: it always dry-runs "
        "first and shows a diff of what changed before anything is submitted; it "
        "can only cancel jobs it submitted FOR THAT PERSON, never their "
        "hand-submitted ones and never a colleague's; and it never writes the shell "
        "script — the user saves that once, after Aspen has checked it over. None "
        "of it is available in a demo: a visitor is not a registered user and must "
        "not spend the group's compute, so the tools are not even offered here.\n"
        "• Attaching files to a reply, and that Aspen never writes into anyone's "
        "calculations directory — its notes live in its own storage.\n"
        "Then ask if they want to see any of it in more detail before you wrap up."
    ),
    "done": (
        "Wrap up when they're ready: what they saw, that everything was fabricated, "
        "and how to get real access (message Aspen normally and the admin is told, "
        "with the command to approve it). Keep it to a few lines."
    ),
}


def guidance_lines(session: DemoSession) -> list[str]:
    """The per-turn context block for a demo turn."""
    lines = [
        "<demo>",
        "You are running the DEMO walkthrough for someone who is NOT a registered "
        "user. Everything they can see is fabricated data in a demo calculations "
        "root; you cannot see or mention any real user's files, and no real user "
        "or admin is affected by anything in this conversation. Say so plainly if "
        "they ask.",
        f"Stage {STAGES.index(session.stage) + 1} of {len(STAGES)} — {session.stage}.",
        _GUIDANCE.get(session.stage, _GUIDANCE["done"]),
        "Move at their pace: answer what they actually ask, and only then nudge "
        "toward the next beat. Keep replies short — this is a tour, not a lecture.",
        "</demo>",
    ]
    return lines
