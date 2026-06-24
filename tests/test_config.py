"""The Messages API client is built lazily; the SDK path needs no API key."""

import pytest


def test_messages_client_built_lazily_when_key_present(sut, monkeypatch):
    import aspen.config as cfg

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz")
    monkeypatch.delattr(cfg, "anthropic_client", raising=False)  # clear any cache

    client = cfg.anthropic_client
    assert client is not None


def test_messages_client_errors_clearly_without_key(sut, monkeypatch):
    import aspen.config as cfg

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delattr(cfg, "anthropic_client", raising=False)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is required"):
        _ = cfg.anthropic_client


def test_sdk_options_build_without_api_key(sut, monkeypatch):
    """The SDK backend must construct fully even with no ANTHROPIC_API_KEY set."""
    sdk = pytest.importorskip("claude_agent_sdk")
    from aspen.backends.sdk import SdkSession

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delattr(__import__("aspen.config", fromlist=["x"]), "anthropic_client", raising=False)

    opts = SdkSession("C:1")._build_options(sdk)  # must not touch anthropic_client
    assert opts.allowed_tools[0] == "mcp__aspen__list_directory"
