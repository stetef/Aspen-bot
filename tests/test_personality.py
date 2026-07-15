"""
Tests for the red-wolf personality easter egg wired into the system prompt.

Hermetic, matching the rest of the suite: no live model, Slack, or network.
These are prompt-assembly / file-presence guards. They ensure the easter egg is
actually baked into ``SYSTEM_PROMPT`` and that its reference file ships with the
repo, so a moved/renamed/emptied file fails loudly in CI rather than silently
shipping an empty (or missing) easter egg. They also pin the two design
decisions we care about: the easter egg sits AFTER the core operating
instructions, and the sanctuary/donation pointer is REACTIVE only.
"""

from pathlib import Path

# tests/ lives at the repo root, so its parent is the repo root.
WOLF_FACTS = Path(__file__).resolve().parent.parent / "wolf_facts.md"


def test_wolf_facts_file_present_and_nonempty():
    assert WOLF_FACTS.is_file(), f"missing {WOLF_FACTS}"
    assert WOLF_FACTS.read_text(encoding="utf-8").strip(), "wolf_facts.md is empty"


def test_system_prompt_embeds_wolf_reference(sut):
    # The curated reference is inlined under a delimited section...
    p = sut.SYSTEM_PROMPT
    assert "<wolf_reference>" in p and "</wolf_reference>" in p
    # ...carrying known, stable content from the file.
    assert "Wolf Haven" in p
    assert "red wolf" in p.lower()


def test_personality_appended_after_core_instructions(sut):
    # Work behavior must stay highest-salience; the easter egg is appended after
    # the core operating instructions, not before them.
    p = sut.SYSTEM_PROMPT
    assert p.index("write_metadata") < p.index("<wolf_reference>")


def test_donation_pointer_is_reactive_only(sut):
    # Guard the design decision: the sanctuary/donation pointer is reactive.
    # The model must be told never to initiate donations on its own.
    p = sut.SYSTEM_PROMPT
    assert "REACTIVE ONLY" in p
    assert "never bring up donations on your own" in p.lower()
