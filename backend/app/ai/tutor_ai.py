"""In-lesson AI tutor model boundary (docs 10/11 MVP; doc 05 academic scope).

The tutor only helps with the lesson the student is learning: it answers from the lesson's own
evidence chunks, refuses unrelated requests politely, never writes graded work, and speaks to a
13+ learner. It is paused while a mastery check or review for that lesson is in progress (enforced
by the router, not the model).

- `StubTutorAI` (local/tests): deterministic, grounded in the first matching chunk. A message
  containing "[offtopic]" is treated as off-topic.
- `ClaudeTutorAI`: Claude via the shared client (app.ai.anthropic), one forced tool call.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.ai.content_ai import Usage

MODES = {
    "ASK": "Answer the student's question about the lesson.",
    "EXPLAIN": "Explain the idea simply, step by step, in plain words.",
    "EXAMPLE": "Give one concrete, relatable example or analogy, then link it back to the idea.",
    "HINT": "Give a small hint that moves the student forward without giving the full answer.",
    "PRACTICE": "Ask one short practice question about the lesson and wait for the student's answer. Don't give the answer.",
}


class TutorUnavailable(Exception):
    """The model is down / rate limited; the student is asked to try again."""


@dataclass
class TutorReply:
    text: str
    off_topic: bool = False
    used_chunks: list[int] = field(default_factory=list)
    usage: Usage | None = None


class TutorAI(Protocol):
    model_name: str
    prompt_version: str

    async def reply(self, context: dict[str, Any]) -> TutorReply: ...


OFF_TOPIC = "I can only help with this lesson. Try asking me to explain part of it, give an example, or quiz you on it."


class StubTutorAI:
    model_name = "stub-tutor-ai"
    prompt_version = "stub-v1"

    async def reply(self, context: dict[str, Any]) -> TutorReply:
        msg = context["message"]
        if "[offtopic]" in msg:
            return TutorReply(OFF_TOPIC, off_topic=True, usage=Usage(10, 10))
        chunks = context["chunks"]
        lead = chunks[0] if chunks else context["lesson_title"]
        mode = context["mode"]
        text = {
            "EXPLAIN": f"Let's break it down. {lead}",
            "EXAMPLE": f"Here's an example from everyday life that matches this: {lead}",
            "HINT": f"Hint: look again at this part of the lesson - {lead[:80]}",
            "PRACTICE": f'Quick check: in your own words, what does this mean? "{lead[:80]}"',
        }.get(mode, f"Good question. From the lesson: {lead}")
        return TutorReply(text, used_chunks=[0] if chunks else [], usage=Usage(len(msg.split()) + 50, 40))


_ai: TutorAI | None = None


def get_tutor_ai() -> TutorAI:
    global _ai
    if _ai is None:
        from app.config import get_settings

        if get_settings().ai_provider == "anthropic":
            from app.ai.anthropic import ClaudeTutorAI

            _ai = ClaudeTutorAI()
        else:
            _ai = StubTutorAI()
    return _ai


def set_tutor_ai(ai: TutorAI | None) -> None:
    global _ai
    _ai = ai
