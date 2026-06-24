"""Agent backends + the session factory.

Both backends implement the ``AgentSession`` interface (``backends/base.py``):
a uniform ``async def send(user_message, context) -> (reply, figures)`` plus
``aclose()``. Imports are lazy so selecting ``messages`` never imports the SDK.
"""

from .base import AgentSession


def make_session(backend: str, key: str) -> "AgentSession":
    if backend == "messages":
        from .messages import MessagesSession
        return MessagesSession(key)
    if backend == "sdk":
        from .sdk import SdkSession
        return SdkSession(key)
    raise ValueError(f"Unknown ASPEN_BACKEND: {backend!r}")
