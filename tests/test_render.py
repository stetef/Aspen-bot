"""
Tests for ``aspen.render`` — the Markdown -> Slack ``markdown`` block transport.

The bug these guard: the agent emits GitHub-flavored Markdown but Slack's ``text``
field renders ``mrkdwn``, so replies showed literal ``**``/``##``/``[](...)``.
Routing replies through a Slack ``markdown`` block makes Slack render the GFM.
"""

from unittest.mock import MagicMock

import pytest

from aspen import render


def test_reply_goes_into_a_markdown_block_with_text_fallback():
    reply = "# Heading\n\n**bold**, a [link](https://x), and `code`."
    kw = render.slack_reply(reply)

    # Plaintext fallback preserved (notifications / non-block clients).
    assert kw["text"] == reply
    # The GFM is carried verbatim in a markdown block for Slack to render.
    assert kw["blocks"] == [{"type": "markdown", "text": reply}]


def test_no_translation_is_applied():
    """We rely on Slack to render; we must not rewrite the Markdown ourselves."""
    reply = "**bold** _italic_ [t](u) ~~strike~~ ## h"
    blocks = render.slack_reply(reply)["blocks"]
    assert blocks[0]["text"] == reply  # unchanged, char for char


def test_empty_reply_has_no_block():
    # An empty markdown block is rejected by Slack, so only the fallback remains.
    for empty in ("", "   ", "\n\t"):
        kw = render.slack_reply(empty)
        assert "blocks" not in kw
        assert kw["text"] == empty


def test_long_reply_is_chunked_under_the_block_limit():
    reply = "\n".join(f"line {i} " + "x" * 80 for i in range(400))
    assert len(reply) > render._MAX_BLOCK_CHARS  # precondition: needs splitting

    kw = render.slack_reply(reply)
    blocks = kw["blocks"]

    assert len(blocks) > 1
    assert all(b["type"] == "markdown" for b in blocks)
    assert all(len(b["text"]) <= render._MAX_BLOCK_CHARS for b in blocks)
    # No content lost: joining the chunks (the boundary newline is consumed)
    # reproduces the reply once newlines are normalized out.
    assert "".join(b["text"] for b in blocks).replace("\n", "") == reply.replace("\n", "")


def test_chunk_hard_cuts_when_no_newline_in_window():
    reply = "y" * (render._MAX_BLOCK_CHARS + 50)  # single unbroken line
    blocks = render.slack_reply(reply)["blocks"]
    assert len(blocks) == 2
    assert len(blocks[0]["text"]) == render._MAX_BLOCK_CHARS
    # Every character survives the hard cut.
    assert "".join(b["text"] for b in blocks) == reply


def test_handler_sends_reply_as_markdown_block(sut, say, monkeypatch):
    """End to end through ``_handle_event``: the reply reaches Slack as a block."""

    async def _fake_handle(key, user_message, context):
        return "## Result\n\n**done**", []

    monkeypatch.setattr(sut.MANAGER, "handle", _fake_handle)

    event = {"user": "U1", "text": "hi", "channel": "C", "ts": "1.0"}
    sut._handle_event(event, say, MagicMock(), strip_mention=False)

    reply_calls = [c for c in say.calls if c.get("blocks")]
    assert len(reply_calls) == 1
    block = reply_calls[0]["blocks"][0]
    assert block == {"type": "markdown", "text": "## Result\n\n**done**"}
    # Thinking status stays a plain text post (no block).
    assert "_Thinking…_" in say.texts
