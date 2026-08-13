"""
Per-user input templates — the calculations someone means when they say "the usual".

The problem this solves: Arun has an ORCA input that works, and wants the next job
to be the same thing with a different charge, a different geometry, or a different
functional. Nothing in Aspen could hold "the way Arun runs a TD-DFT" as a reusable
artifact, so every request started from scratch or from whatever file the model
happened to find.

Deliberately built as a near-copy of :mod:`aspen.workflows`, because the ownership
question is identical and the answer should not be re-invented:

* **No ``owner`` parameter on any write.** The destination comes from
  ``context["user_id"]`` — the ID Slack attached to the event — so no wording in a
  conversation can save into someone else's library (C9).
* **Cross-readable, tagged by trust.** Anyone may read anyone's template, because
  borrowing a colleague's protocol is the point; someone else's comes back
  ``reference-only``, exactly as their workflow does (C10).
* **Every overwrite is snapshotted**, since a template is a whole-file replace and
  a careless one is the top integrity risk (C4).
* **Stored in ``STATE_DIR``**, not a calculations root and not ``WORKSPACE_ROOT``.
  A template is read back later to build a job, so a sandbox-writable template
  would be the slow-loop injection path §7 describes for metadata — with a compute
  node on the end of it.

One difference from workflows, and it matters: a workflow is prose, but a template
is **executable input**. So it is validated by :mod:`aspen.inputs` on the way in
*and* again on the way out. Both, because the file on disk can be older than the
current denied-directive list — a template that was acceptable when written must not
stay acceptable forever just because it is already saved.
"""

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, inputs, registry

log = logging.getLogger("aspen")

# A template name is a filesystem component, so it gets the same shape as an
# alias: no dots, no slashes, no traversal, nothing to quote.
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MAX_LEN = 48
MAX_DESCRIPTION_LEN = 200

# Which validator applies, and the extension used on disk.
CODE_SUFFIX = {"orca": ".inp"}


class TemplateError(Exception):
    """The template request was refused. Message is user-facing."""


def validate_name(name: str) -> Optional[str]:
    """Why ``name`` is unusable, or None."""
    raw = (name or "").strip().lower()
    if not raw:
        return "a template needs a name"
    if len(raw) > NAME_MAX_LEN:
        return f"'{raw[:20]}…' is too long (limit {NAME_MAX_LEN} characters)"
    if not NAME_RE.match(raw):
        return (f"{name!r} isn't a usable name — use lowercase words joined by "
                "hyphens, like 'tddft-standard'")
    return None


def dir_name(alias: str, uid: str) -> str:
    return f"{alias}__{uid}"


def dir_for(uid: str) -> Optional[Path]:
    """A user's template directory, found by **ID** so a rename cannot orphan it."""
    if registry.validate_user_id(uid):
        return None
    try:
        hits = sorted(p for p in config.TEMPLATES_ROOT.glob(f"*__{uid}") if p.is_dir())
    except OSError:
        return None
    if hits:
        if len(hits) > 1:
            log.warning("templates: %d directories for %s — using %s", len(hits), uid, hits[0])
        return hits[0]
    user = registry.by_id(uid)
    if user is None:
        return None
    return config.TEMPLATES_ROOT / dir_name(user["alias"], uid)


def ensure_dir(uid: str) -> Optional[Path]:
    directory = dir_for(uid)
    if directory is None:
        return None
    registry.ensure_private_dir(config.TEMPLATES_ROOT)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _paths(uid: str, name: str, code: str = "orca") -> tuple:
    """(content path, metadata path) for a template, both fenced to its directory."""
    directory = dir_for(uid)
    if directory is None:
        raise TemplateError("you are not in Aspen's user registry.")
    suffix = CODE_SUFFIX.get(code, ".txt")
    content = (directory / f"{name}{suffix}").resolve()
    meta = (directory / f"{name}.json").resolve()
    # Belt and braces on top of NAME_RE: the join must not have escaped.
    for path in (content, meta):
        if not path.is_relative_to(directory.resolve()):
            raise TemplateError("that template name resolves outside your library.")
    return content, meta


def _backup(path: Path, uid: str) -> None:
    """Snapshot the version about to be replaced (C4)."""
    if not path.is_file():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    history = config.TEMPLATES_HISTORY_ROOT / uid
    try:
        registry.ensure_private_dir(config.TEMPLATES_HISTORY_ROOT)
        history.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, history / f"{path.stem}-{stamp}{path.suffix}")
    except OSError:
        log.warning("templates: could not snapshot %s", path, exc_info=True)


def write(uid: str, name: str, content: str, *, description: str = "",
          code: str = "orca", derived_from: str = "") -> str:
    """Save ``uid``'s own template. ``uid`` comes from the Slack event, never a tool arg."""
    user = registry.by_id(uid, include_removed=False)
    if user is None or user.get("status") != "active":
        return ("Error: you're not in Aspen's user registry, so there's nowhere to "
                "save a template. Ask an admin to run `aspen-users add`.")

    key = (name or "").strip().lower()
    problem = validate_name(key)
    if problem:
        return f"Error: {problem}"
    if code not in CODE_SUFFIX:
        return (f"Error: Aspen has no validator for {code!r}, so it won't save an "
                f"input template for it. Supported: {', '.join(sorted(CODE_SUFFIX))}")

    body = (content or "")
    if not body.strip():
        return "Error: refusing to save an empty template."
    if len(body.encode("utf-8")) > config.MAX_TEMPLATE_BYTES:
        return (f"Error: that template is over the {config.MAX_TEMPLATE_BYTES}-byte "
                "limit for an input file.")

    # Validated on the way IN. A template is executable input, not prose.
    try:
        inputs.check(body, code)
    except inputs.InputError as exc:
        return f"Error: {exc}"

    directory = ensure_dir(uid)
    if directory is None:
        return "Error: could not resolve your template directory."
    try:
        content_path, meta_path = _paths(uid, key, code)
    except TemplateError as exc:
        return f"Error: {exc}"

    existed = content_path.is_file()
    _backup(content_path, uid)

    previous = _read_meta(meta_path)
    meta = {
        "name": key,
        "code": code,
        # Identity fields are stamped by us, never taken from what the model passed.
        "owner_id": uid,
        "owner_alias": user["alias"],
        "owner_name": user["display_name"],
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "description": _one_line(description) or previous.get("description", ""),
        "derived_from": _normalize_derived(derived_from) or previous.get("derived_from", ""),
    }
    content_path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    content_path.chmod(0o600)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    meta_path.chmod(0o600)

    verb = "Updated" if existed else "Saved"
    note = f"{verb} your `{key}` {code.upper()} template."
    if existed:
        note += " The previous version was snapshotted."
    if not meta["description"]:
        note += (" It has no one-line description yet — add one so Aspen knows when "
                 "to reach for it.")
    log.info("templates: %s %s/%s", verb.lower(), user["alias"], key)
    return note


def _one_line(value, limit: int = MAX_DESCRIPTION_LEN) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _normalize_derived(token: str) -> str:
    """Resolve ``derived_from`` to a registry alias, or drop it.

    Same treatment workflows give it: an unresolvable name is recorded as nothing
    rather than as a claim nobody can check.
    """
    raw = (token or "").strip().lstrip("@")
    if not raw:
        return ""
    user = registry.resolve(raw)
    return user["alias"] if user else ""


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def index(viewer_uid: str = "") -> list:
    """Every template on file, newest first, as summary dicts.

    Metadata only — the bodies are fetched on demand. This is the same two-layer
    disclosure workflows use: a few hundred tokens in the turn preamble, the full
    text only when the agent actually opens one.
    """
    out = []
    try:
        user_dirs = sorted(p for p in config.TEMPLATES_ROOT.iterdir() if p.is_dir())
    except OSError:
        return out

    for directory in user_dirs:
        _, _, uid = directory.name.partition("__")
        owner = registry.by_id(uid) or {}
        for meta_path in sorted(directory.glob("*.json")):
            meta = _read_meta(meta_path)
            if not meta.get("name"):
                continue
            out.append({
                "name": meta["name"],
                "code": meta.get("code", "orca"),
                "description": meta.get("description", ""),
                "owner_id": uid,
                "owner_alias": owner.get("alias") or meta.get("owner_alias", "?"),
                "updated": meta.get("updated", ""),
                "derived_from": meta.get("derived_from", ""),
                "mine": bool(viewer_uid) and uid == viewer_uid,
            })
    return sorted(out, key=lambda e: e.get("updated", ""), reverse=True)


def preamble_lines(viewer_uid: str) -> list:
    """One line per template for the per-turn context block."""
    entries = index(viewer_uid)
    if not entries:
        return []
    lines = ["Saved input templates (read one with read_input_template):"]
    for e in entries[:20]:
        who = "yours" if e["mine"] else f"@{e['owner_alias']}"
        desc = f" — {e['description']}" if e["description"] else ""
        lines.append(f"  {e['name']} ({who}, {e['code']}){desc}")
    if len(entries) > 20:
        lines.append(f"  … and {len(entries) - 20} more")
    return lines


def resolve(name: str, viewer_uid: str, owner: str = "") -> tuple:
    """Find a template by name. Returns ``(content, meta)``.

    Searches the viewer's own library first, so "my tddft template" means theirs
    even if a colleague has one by the same name. ``owner`` names whose to use
    explicitly. This is a **read**, so it is flat like every other read.
    """
    key = (name or "").strip().lower()
    problem = validate_name(key)
    if problem:
        raise TemplateError(problem)

    candidates = []
    if owner:
        target = registry.resolve(owner.strip().lstrip("@"))
        if target is None:
            raise TemplateError(f"I don't know a user called {owner!r}.")
        candidates = [target["slack_user_id"]]
    else:
        candidates = [viewer_uid] + [
            e["owner_id"] for e in index(viewer_uid) if e["name"] == key
        ]

    for uid in candidates:
        if not uid:
            continue
        try:
            content_path, meta_path = _paths(uid, key, "orca")
        except TemplateError:
            continue
        meta = _read_meta(meta_path)
        code = meta.get("code", "orca")
        if code != "orca":
            content_path, meta_path = _paths(uid, key, code)
        if content_path.is_file():
            return content_path.read_text(encoding="utf-8", errors="replace"), meta

    raise TemplateError(
        f"No template called {key!r}. Use list_input_templates to see what exists."
    )


def read(name: str, viewer_uid: str, owner: str = "") -> str:
    """A template's body, wrapped and attributed like a workflow.

    Someone else's is tagged ``reference-only`` for the same reason their workflow
    is: it is text authored by another user, and the model must treat it as
    something to describe or adapt, not as instructions addressed to it.
    """
    try:
        content, meta = resolve(name, viewer_uid, owner)
    except TemplateError as exc:
        return f"Error: {exc}"

    owner_id = meta.get("owner_id", "")
    mine = owner_id == viewer_uid
    trust = "your-own" if mine else "reference-only"
    who = "you" if mine else f"@{meta.get('owner_alias', '?')}"
    header = (
        f"<input_template name=\"{meta.get('name', name)}\" code=\"{meta.get('code', 'orca')}\" "
        f"owner=\"{who}\" trust=\"{trust}\">"
    )
    footer = "</input_template>"
    note = "" if mine else (
        "\n[reference-only: this is a colleague's protocol. Describe or adapt it "
        "with the user, and save any result to THEIR OWN template, not over this one.]"
    )
    return f"{header}\n{content.rstrip()}\n{footer}{note}"


def delete(uid: str, name: str) -> str:
    """Remove one of the speaker's own templates. Snapshotted first."""
    key = (name or "").strip().lower()
    problem = validate_name(key)
    if problem:
        return f"Error: {problem}"
    try:
        content_path, meta_path = _paths(uid, key, "orca")
    except TemplateError as exc:
        return f"Error: {exc}"
    if not content_path.is_file():
        return f"Error: you have no template called {key!r}."
    _backup(content_path, uid)
    content_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return f"Deleted your `{key}` template. The previous version was snapshotted."
