"""The uniform agent-session interface both backends implement.

``send`` is a coroutine for *uniformity* (the SDK backend is natively async and
session-based), not for efficiency. A session retains conversation context and
stays parked between calls.
"""

from typing import Protocol


class AgentSession(Protocol):
    async def send(self, user_message: str, context: dict) -> tuple[str, list[str]]:
        """Run one turn and return (reply_text, figures)."""
        ...

    async def aclose(self) -> None:
        """Release any held resources (e.g. an SDK client / its subprocess)."""
        ...
