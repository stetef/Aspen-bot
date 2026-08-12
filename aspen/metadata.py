"""
Project metadata — Aspen's notes about a project, kept *outside* the project.

``metadata.md`` used to live at the root of each project directory. That only
worked while the calculations tree belonged to the account the bot runs as. With
one root per user (see ``roots.py``) it would be a write into someone else's
directory, and under the ``aspen-agent`` service account it stops being possible
at all. So metadata moved into a sidecar that **mirrors** the tree::

    <METADATA_ROOT>/<alias>__<slack-id>/<project>/metadata.md   # a user's root
    <METADATA_ROOT>/_shared__<name>/<project>/metadata.md       # a shared root
    <METADATA_HISTORY_ROOT>/<same>/<project>/<UTC>.md           # every overwrite

**The path is the key.** Mirroring derives the location by arithmetic, so there
is no mapping to keep in sync — the same trick as ``workflows.dir_for``, and the
reason to prefer it over an index file (a second source of truth that can
desync), flat encoded filenames (``a/b`` and ``a-b`` collide), or frontmatter
naming its own target (a full scan per lookup, and the *model's* declared target
becomes authoritative).

**History is keyed by owner as well as project.** It used to be keyed by project
alone, which silently collided the moment two people had a ``thermolysin``.

Metadata is Aspen-authored and read back into the model's context later, which is
why it is *not* served through ``read_file``: an agent that cannot tell its own
past notes from ground truth will eventually believe one for the other. It comes
back through ``read_metadata`` with its authorship stated, exactly as a workflow
does.

**A README is not this file.** The sidecar is *Aspen's* notes; a ``README.md`` in
the project directory is the *scientist's* — ground truth, written by them, and
visible to colleagues, backups, and every tool that is not Aspen. Keeping them
apart is the same distinction ``read_metadata``'s wrapper draws, so the two must
not be collapsed into one document. Aspen cannot write a README (no root is
writable to it), so the only move available is to draft one and ask the user to
save it; ``describes`` and ``nudge_lines`` below are that offer, rationed. Anyone
who would rather not keep a file gets the sidecar instead — same content, no
markdown, nothing to save.
"""

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, demo, registry, roots

log = logging.getLogger("aspen")

FILENAME = "metadata.md"
SHARED_PREFIX = "_shared__"
# A project is one directory name — never a nested path, never '..'.
_PROJECT_RE = re.compile(r"\A[^/\\]+\Z")

# In-tree files that already describe a project. A README under any of these
# names, or a pre-migration metadata.md, means the project is documented and
# there is nothing to nudge about.
README_NAMES = ("README.md", "README.txt", "README.rst", "README",
                "readme.md", "readme.txt", "readme")
IN_TREE_NAMES = (FILENAME, *README_NAMES)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def scope_dir_name(scope: dict) -> str:
    """``arun__U01ARUN`` for a user, ``_shared__smb`` for a shared root.

    Users get the workflows convention — alias for humans reading ``ls``, Slack
    ID for lookups — so a rename cannot orphan anyone's metadata.
    """
    if scope.get("kind") == "shared":
        return f"{SHARED_PREFIX}{scope['name']}"
    return f"{scope['name']}__{scope['owner_id']}"


def scope_dir(scope: dict, create: bool = False) -> Optional[Path]:
    """The metadata directory for a root, found by ID and not by alias."""
    base = config.METADATA_ROOT
    if scope.get("kind") == "shared":
        want = base / scope_dir_name(scope)
    else:
        uid = scope.get("owner_id", "")
        want = base / scope_dir_name(scope)
        try:                                # follow an alias rename, as workflows do
            hits = sorted(p for p in base.glob(f"*__{uid}") if p.is_dir())
        except OSError:
            hits = []
        if hits and hits[0] != want:
            try:
                hits[0].rename(want)
                log.info("metadata: renamed %s -> %s", hits[0].name, want.name)
            except OSError:
                log.exception("metadata: could not rename %s", hits[0])
                return hits[0]
    if create:
        registry.ensure_private_dir(base)
        want.mkdir(parents=True, exist_ok=True)
    return want


def _fenced(scope: dict, project: str, create: bool = False) -> Optional[Path]:
    """``<METADATA_ROOT>/<scope>/<project>/metadata.md``, fenced.

    The project name reaches us as a tool argument and is joined into a
    *writable* location, so it gets the same resolve-and-``relative_to`` check
    the read tools use — a traversal here would be worse than one into the
    read-only tree.
    """
    directory = scope_dir(scope, create=create)
    if directory is None:
        return None
    try:
        target = (directory / project / FILENAME).resolve()
        target.relative_to(config.METADATA_ROOT)
    except (ValueError, OSError):
        return None
    return target


def path_for(project: str, owner: str, viewer_uid: str) -> tuple[Optional[Path], dict, str]:
    """Resolve (project, owner) to a sidecar path, validating both.

    The project must *exist* under the resolved root — that check doubles as
    validation, so metadata can never describe a project that does not.
    """
    if not project or not _PROJECT_RE.match(project) or project in (".", ".."):
        return None, {}, (f"Error: '{project}' is not a valid project name "
                          "(use a single project directory name).")

    source, scope, error = roots.resolve(project, owner, viewer_uid)
    if error:
        return None, scope, error
    if not source.is_dir():
        return None, scope, (
            f"Error: project '{roots.qualify(scope['name'], project)}' does not exist. "
            "Metadata can only be recorded for a project directory that is already there."
        )

    target = _fenced(scope, project)
    if target is None:
        return None, scope, f"Error: '{project}' resolves outside the metadata area."
    return target, scope, ""


def exists(project: str, scope: dict) -> bool:
    target = _fenced(scope, project)
    return bool(target and target.is_file())


def in_tree_notes(project: str, scope: dict) -> Optional[Path]:
    """A README (or pre-migration metadata.md) the owner keeps in the project.

    Read-only and best-effort: an unreadable or missing root simply means "no
    description", never an error — this only ever decides whether to *offer*
    something.
    """
    base = scope.get("path")
    if not base or not _PROJECT_RE.match(project or "") or project in (".", ".."):
        return None
    try:
        directory = (Path(base) / project).resolve()
        directory.relative_to(Path(base).resolve())
    except (ValueError, OSError):
        return None
    for name in IN_TREE_NAMES:
        try:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def describes(project: str, scope: dict) -> bool:
    """Does anything at all say what this project is?

    Any one source is enough. Nudging someone who already told Aspen the same
    thing through a different door is the nagging this is meant to avoid.
    """
    return exists(project, scope) or in_tree_notes(project, scope) is not None


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def read(project: str, owner: str, viewer_uid: str) -> str:
    """Return a project's metadata, saying plainly that Aspen wrote it."""
    session = demo.active_for(viewer_uid)
    if session is not None:
        return demo.read_notes(session, project)
    target, scope, error = path_for(project, owner, viewer_uid)
    if error:
        return error
    label = roots.qualify(scope["name"], project)
    if not target.is_file():
        # Point at the owner's own README if they keep one: it is a better answer
        # to "what is this project?" than Aspen's empty notes, and it is theirs.
        own = in_tree_notes(project, scope)
        pointer = (f" {label} has its own {own.name} — read_file(path=\"{project}/{own.name}\""
                   + (f", owner=\"{scope['name']}\"" if scope.get("name") else "")
                   + ") for the owner's description of it." if own else "")
        return (f"No metadata recorded for {label} yet.{pointer} "
                "Use write_metadata to start one once you know something worth keeping.")
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Error: could not read the metadata for {label} ({exc})."

    stamp = datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return (
        f'<metadata project="{label}" written_by="aspen" updated="{stamp}">\n'
        "Aspen's own notes on this project — not a file in the calculations tree, "
        "and not evidence. Verify against the data before relying on it.\n\n"
        f"{raw.strip()}\n</metadata>"
    )


def summary_line(project: str, scope: dict) -> str:
    """One line for a directory listing, so metadata is discoverable."""
    if not exists(project, scope):
        return ""
    return (f"(Aspen has metadata for {roots.qualify(scope['name'], project)} — "
            f'read_metadata(project="{project}"'
            + (f', owner="{scope["name"]}"' if scope.get("name") else "")
            + "))")


# --------------------------------------------------------------------------- #
# Encouraging a description (the nudge, not a requirement)
# --------------------------------------------------------------------------- #
# The shape of the draft Aspen offers. Deliberately short and deliberately not a
# blank template: a form to fill in is work, and the whole point is that Aspen has
# just read the directory and can fill it in *for* them. Only the last section is
# ever parsed (the tool server looks for a heading containing "librar" to build
# the import advisory); everything else is read as prose, which is why plain
# sentences are fine and why nobody needs to know markdown to write one.
README_SHAPE = """\
# <project> — <one line: system, technique, software>

## Summary
<What this is and why, in a sentence or two.>

## Questions of interest
- <what you actually want out of it>

## Runs
<which directories are what — groups, variants, what changed between them>

## Where the files are
<for a run: the input, structure, and log filenames>

## Python libraries available for analysis
- numpy
- pandas
- matplotlib"""

NUDGE_TEMPLATE = (
    "Nothing describes {label} — no README in the directory, and no notes on "
    "Aspen's side. If this conversation gives you a natural opening (and only "
    "then), offer once to write one: draft it FROM WHAT YOU JUST SAW in the "
    "directory — real run names, real filenames, the software you can identify — "
    "never a blank form, and keep it short. Show it in a code block and say they "
    "can save it as `{project}/README.md`; you cannot write it for them, since "
    "every calculations root is read-only to you. Plain sentences are fine — "
    "there is no format to get right. If they would rather not keep a file, offer "
    "to hold the same notes on your side with write_metadata instead. If they are "
    "not interested either way, call decline_setup(item=\"project_notes\"). Do not "
    "derail what they actually asked for.\n"
    "The shape to follow:\n{shape}"
)


def nudge_text(project: str, scope: dict) -> str:
    """The model-facing offer, or '' if this project already has a description."""
    if describes(project, scope):
        return ""
    return NUDGE_TEMPLATE.format(
        label=roots.qualify(scope.get("name", ""), project),
        project=project,
        shape=README_SHAPE,
    )


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #
def _backup(target: Path, scope: dict, project: str) -> None:
    """Snapshot the version about to be overwritten. Best-effort, never fatal.

    Keyed by owner *and* project: keyed by project alone, two users with the same
    project name would share a history directory and overwrite each other.
    """
    try:
        hist = config.METADATA_HISTORY_ROOT / scope_dir_name(scope) / project
        hist.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = hist / f"{ts}.md"
        n = 1
        while dest.exists():
            dest = hist / f"{ts}-{n}.md"
            n += 1
        shutil.copy2(target, dest)
    except Exception:
        log.exception("metadata backup failed (non-fatal) for %s/%s",
                      scope.get("name"), project)


def may_write(scope: dict, viewer_uid: str) -> Optional[str]:
    """Ownership decides writes: your own projects, or shared group data.

    Someone else's project is readable by everyone and writable by nobody but its
    owner — annotating a colleague's work is a different feature (per-viewer
    notes), and folding it in here would fork metadata into per-reader copies.
    """
    if scope.get("kind") == "shared":
        return None if registry.by_id(viewer_uid) else (
            "Error: only registered users can write metadata.")
    if scope.get("owner_id") == viewer_uid:
        return None
    return (f"Error: {roots.PREFIX}{scope.get('name')}'s projects belong to them — you can "
            "read their metadata, but only they can change it. Tell them what you'd "
            "add, or record it in your own project's metadata.")


def write(project: str, owner: str, content: str, viewer_uid: str, actor: str = "") -> str:
    """Create or replace the metadata for one project.

    ``viewer_uid`` comes from the Slack event, never from a tool argument — the
    same property that makes workflow ownership unspoofable.
    """
    session = demo.active_for(viewer_uid)
    if session is not None:
        _target, _scope, error = path_for(project, owner, viewer_uid)
        return error or demo.save_notes(session, project, content)

    target, scope, error = path_for(project, owner, viewer_uid)
    if error:
        return error
    refusal = may_write(scope, viewer_uid)
    if refusal:
        return refusal

    data = (content or "").encode("utf-8")
    if not data.strip():
        return "Error: refusing to write empty metadata."
    if len(data) > config.MAX_FILE_BYTES:
        return (f"Error: content is {len(data)} bytes, over the "
                f"{config.MAX_FILE_BYTES}-byte metadata limit. Keep it concise.")

    target = _fenced(scope, project, create=True)
    if target is None:
        return f"Error: '{project}' resolves outside the metadata area."
    target.parent.mkdir(parents=True, exist_ok=True)

    existed = target.is_file()
    if existed:
        _backup(target, scope, project)
    try:
        tmp = target.with_suffix(".md.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)
    except OSError as exc:
        return f"Error: could not write the metadata for '{project}' ({exc})."

    if actor:
        log.info("metadata: %s wrote %s/%s", actor, scope.get("name"), project)
    verb = "Updated" if existed else "Created"
    return (f"{verb} metadata for {roots.qualify(scope['name'], project)} "
            f"({len(data)} bytes). It is stored in Aspen's own area, not in the "
            "calculations tree.")
