"""
Characterization tests for the agent loop's externally observable contract:
given a (mocked) model, ``_run_agent`` returns (reply_text, figures) and routes
tool-use blocks through the shared tool dispatch.

This is the "past the seam" test. After the refactor it becomes the backend-contract
test that both the Messages and SDK backends must satisfy; in Phase 1 it targets the
legacy ``_run_agent``.
"""


class _TextBlock:
    def __init__(self, text):
        self.text = text
        self.type = "text"


class _ToolUseBlock:
    def __init__(self, name, inp, id="tool-1"):
        self.type = "tool_use"
        self.name = name
        self.input = inp
        self.id = id


class _Resp:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _ctx():
    return {"user_id": "U1", "username": "", "thread_ts": "1.0"}


def test_tool_use_then_end_turn(sut, monkeypatch):
    client = _FakeClient(
        [
            _Resp("tool_use", [_ToolUseBlock("list_directory", {"path": "."})]),
            _Resp("end_turn", [_TextBlock("all done")]),
        ]
    )
    monkeypatch.setattr(sut, "anthropic_client", client)

    reply, figures = sut._run_agent("list the root", [], _ctx())

    assert reply == "all done"
    assert figures == []
    # One create per round: tool_use round, then the final text round.
    assert len(client.messages.calls) == 2
    # First call carries the configured model / system prompt / tool schemas.
    first = client.messages.calls[0]
    assert first["model"] == sut.MODEL
    assert first["max_tokens"] == 4096
    assert first["system"] == sut.SYSTEM_PROMPT
    assert first["tools"] == sut.TOOLS


def test_history_is_prepended_to_messages(sut, monkeypatch):
    client = _FakeClient([_Resp("end_turn", [_TextBlock("a2")])])
    monkeypatch.setattr(sut, "anthropic_client", client)
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]

    reply, _ = sut._run_agent("q2", history, _ctx())

    assert reply == "a2"
    assert client.messages.calls[0]["messages"] == history + [
        {"role": "user", "content": "q2"}
    ]


def test_figures_accumulate_across_tool_calls(sut, monkeypatch):
    # Inject a fake tool that yields a figure path, to characterize accumulation.
    monkeypatch.setitem(
        sut.TOOL_FNS, "fake_plot", lambda inp, ctx: ("plotted", ["/workspace/figures/x.png"])
    )
    client = _FakeClient(
        [
            _Resp("tool_use", [_ToolUseBlock("fake_plot", {})]),
            _Resp("end_turn", [_TextBlock("done")]),
        ]
    )
    monkeypatch.setattr(sut, "anthropic_client", client)

    reply, figures = sut._run_agent("make a plot", [], _ctx())

    assert reply == "done"
    assert figures == ["/workspace/figures/x.png"]


def test_tool_call_round_limit(sut, monkeypatch):
    # Model never stops requesting tools -> the loop guard cuts it off after 10 rounds.
    responses = [
        _Resp("tool_use", [_ToolUseBlock("list_directory", {"path": "."})]) for _ in range(10)
    ]
    client = _FakeClient(responses)
    monkeypatch.setattr(sut, "anthropic_client", client)

    reply, _ = sut._run_agent("loop forever", [], _ctx())

    assert "tool-call limit" in reply
    assert len(client.messages.calls) == 10
