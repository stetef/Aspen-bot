"""
Render an agent reply for Slack.

The agent emits standard (GitHub-flavored) Markdown, but Slack's ``text`` field
speaks ``mrkdwn`` — a different, smaller dialect — so a raw reply renders with
literal ``**bold**``, ``## headings``, ``[text](url)`` links, etc. Slack's
``markdown`` *block* renders GFM natively, so we send the reply through that block
and let Slack do the rendering. A plaintext ``text`` value is kept alongside it as
the fallback Slack uses for notifications and clients that don't render blocks.

This is deliberately a thin transport shim: the agent keeps writing ordinary
Markdown, and no Markdown->mrkdwn translation happens on our side.
"""

# Slack caps a single ``markdown`` block's text at 12,000 characters; stay under
# it. A message may carry many blocks, so long replies are split across several.
_MAX_BLOCK_CHARS = 11_900


def _chunk(text: str, limit: int = _MAX_BLOCK_CHARS) -> list[str]:
    """Split ``text`` into pieces no longer than ``limit``.

    Cuts on a newline boundary when one exists within the window (so we don't
    slice through a line), otherwise hard-cuts at ``limit``. The boundary newline
    is consumed — blocks already render with separation — but no other content is
    dropped.
    """
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:  # no newline in window -> hard cut, keep every character
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
        else:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 1:]
    chunks.append(remaining)
    return chunks


def slack_reply(reply: str) -> dict:
    """Build the ``say(...)`` kwargs that make Slack render ``reply``'s Markdown.

    Returns ``{"text": ..., "blocks": ...}`` (the caller adds ``thread_ts``). When
    the reply is empty/whitespace there is nothing to render, so only the text
    fallback is returned (an empty ``markdown`` block is rejected by Slack).
    """
    reply = reply or ""
    if not reply.strip():
        return {"text": reply}
    blocks = [{"type": "markdown", "text": chunk} for chunk in _chunk(reply)]
    return {"text": reply, "blocks": blocks}
