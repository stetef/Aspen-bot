"""
Messages API backend.

``_run_agent`` is the original agentic loop, relocated verbatim except that tool
calls go through ``tools.dispatch`` (figures land in the per-turn sink,
``context["figures"]``) and the round cap is the shared ``config.AGENT_MAX_ROUNDS``
(default 10). The ``anthropic.APIError`` handling is relocated here from the Slack
handler. ``MessagesSession`` wraps it behind the async session interface, offloading
the blocking loop to a worker thread so it never stalls the shared event loop.
"""

import asyncio
import logging

import anthropic

from .. import config, prompts, sessions, tools

log = logging.getLogger("aspen")


def _run_agent(user_message: str, history: list[dict], context: dict) -> tuple[str, list[str]]:
    """
    Call Anthropic with tool-use enabled. Iterate until the model produces a final
    text response or the tool-call round limit is reached.
    Returns (reply_text, figures). Figures accumulate in ``context["figures"]``.
    """
    figures = context.setdefault("figures", [])
    messages = history + [{"role": "user", "content": user_message}]

    try:
        for round_num in range(config.AGENT_MAX_ROUNDS):  # guard against runaway tool loops
            resp = config.anthropic_client.messages.create(
                model=config.MODEL,
                max_tokens=4096,
                system=prompts.SYSTEM_PROMPT,
                tools=tools.TOOLS,
                messages=messages,
            )
            log.debug("Round %d: stop_reason=%s", round_num, resp.stop_reason)

            if resp.stop_reason == "end_turn":
                text = "\n".join(
                    b.text for b in resp.content if hasattr(b, "text")
                ) or "(no text response)"
                return text, list(figures)

            if resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                tool_results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    result_text = tools.dispatch(block.name, block.input, context)
                    log.info("Tool %-22s → %d chars", block.name, len(result_text))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })
                messages.append({"role": "user", "content": tool_results})
                continue

            # Unexpected stop reason (e.g. "max_tokens")
            log.warning("Unexpected stop_reason: %s", resp.stop_reason)
            break
    except anthropic.APIError as exc:
        log.error("Anthropic API error: %s", type(exc).__name__)
        return f"Sorry, there was an API error ({type(exc).__name__}). Please try again.", list(figures)

    return (
        "I wasn't able to complete your request within the tool-call limit. Please try a simpler query.",
        list(figures),
    )


class MessagesSession:
    """Conversation session backed by the stateless Messages API + history store."""

    def __init__(self, key: str):
        self.key = key

    async def send(self, user_message: str, context: dict) -> tuple[str, list[str]]:
        context.setdefault("figures", [])
        history = sessions._get_history(self.key)
        # The loop is synchronous/blocking; run it off the shared event loop.
        reply, figures = await asyncio.to_thread(_run_agent, user_message, history, context)
        sessions._append_history(self.key, user_message, reply)
        return reply, figures

    async def aclose(self) -> None:
        return
