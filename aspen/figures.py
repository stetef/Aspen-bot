"""Upload generated figures to Slack and archive them."""

import logging
import shutil
from pathlib import Path

from . import config

log = logging.getLogger("aspen")


def _upload_figures(figures: list[str], client, channel: str, thread_ts: str) -> None:
    """Upload PNGs to Slack and move them to the figure archive."""
    for fig_path in figures:
        p = Path(fig_path)
        if not p.exists() or p.suffix.lower() != ".png":
            continue
        try:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=str(p),
                title=p.stem,
            )
            # Move to archive after successful upload
            config.FIGURE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(config.FIGURE_ARCHIVE_DIR / p.name))
            log.info("Uploaded and archived figure: %s", p.name)
        except Exception:
            log.exception("Failed to upload figure %s", fig_path)
