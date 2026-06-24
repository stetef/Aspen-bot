"""
Claude Agent SDK backend — STUB (implemented in Phase 3).

The real implementation will wrap a warm ``ClaudeSDKClient`` that stays connected
(CLI subprocess kept alive) across turns, with ``@tool`` wrappers built from
``tools.TOOL_SPECS`` and tools locked to ``mcp__aspen__*``. The SDK import is lazy
so the default ``messages`` backend never requires ``claude-agent-sdk``.
"""


class SdkSession:
    def __init__(self, key: str):
        self.key = key

    async def send(self, user_message: str, context: dict) -> tuple[str, list[str]]:
        raise NotImplementedError("SDK backend is implemented in Phase 3.")

    async def aclose(self) -> None:
        return
