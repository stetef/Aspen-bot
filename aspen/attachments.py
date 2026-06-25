"""Upload reply attachments (generated figures + agent-attached files) to Slack.

This is the generic upload channel: a turn accumulates a list of file paths in
``context["attachments"]`` and the front-end hands them here. Any file type is
accepted — plots produced by ``run_python_analysis`` and arbitrary files the
agent chose to attach via the ``attach_file`` tool flow through the same path.

Archival rule is by *origin*, not type: files generated under the workspace
(ephemeral plots, etc.) are moved to the archive after a successful upload;
files attached from anywhere else (e.g. the read-only calculations root) are
uploaded in place and never moved.
"""

import logging
import shutil
from pathlib import Path

from . import config

log = logging.getLogger("aspen")


def _under(path: Path, root: Path) -> bool:
    """True if ``path`` resolves to somewhere inside ``root``."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _upload_attachments(attachments: list[str], client, channel: str, thread_ts: str) -> None:
    """Upload each file to the Slack thread; archive the ones we generated."""
    for att_path in attachments:
        p = Path(att_path)
        if not p.exists() or not p.is_file():
            continue
        try:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=str(p),
                filename=p.name,   # preserve the real name/extension for download
                title=p.stem,
            )
        except Exception:
            log.exception("Failed to upload attachment %s", att_path)
            continue

        # Only sweep up files we produced in the workspace. Files attached from
        # elsewhere (calculations root) are source data — upload, never move.
        if _under(p, config.WORKSPACE_ROOT):
            try:
                config.FIGURE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(config.FIGURE_ARCHIVE_DIR / p.name))
                log.info("Uploaded and archived: %s", p.name)
            except Exception:
                log.exception("Uploaded but failed to archive %s", p.name)
        else:
            log.info("Uploaded attachment: %s", p.name)
