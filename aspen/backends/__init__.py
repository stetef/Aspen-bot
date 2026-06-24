"""Agent backend + the session factory.

The bot runs on the Claude Agent SDK. ``SdkSession`` implements the
``AgentSession`` interface (``backends/base.py``): a uniform
``async def send(user_message, context) -> (reply, figures)`` plus ``aclose()``.
The SDK import is lazy (inside ``SdkSession``), so importing the package doesn't
require ``claude-agent-sdk`` until a session is actually created.
"""

from .base import AgentSession


def make_session(key: str) -> "AgentSession":
    from .sdk import SdkSession
    return SdkSession(key)
