"""Response evaluation and learning-support model boundary (contract §7.4).

Deterministic questions (MCQ) never call a model. Free-text / reasoning answers are judged by
`GradingAI` asynchronously (TX2). Hints and micro-explanations for the wrong-answer repair path
(hint -> retry -> micro-explanation -> related verification, doc 21) also come from here.
The stub is deterministic; the real model plugs in behind the same interface.
"""

from dataclasses import dataclass
from typing import Any, Protocol


class GradingUnavailable(Exception):
    """Transient model/provider failure; the grading job retries, then falls back to a teacher."""


@dataclass(frozen=True)
class Judgement:
    evaluation: str  # CORRECT | PARTIAL | INCORRECT
    confidence: float  # 0..1
    feedback: str
    input_tokens: int
    output_tokens: int


def question_hint(question: dict[str, Any], concept_name: str) -> str:
    """Hints are generated WITH the question (answer_spec.hint), so the answer path never waits on a model."""
    return (question.get("answer_spec") or {}).get(
        "hint"
    ) or f"Hint: think about what '{concept_name}' means before choosing."


def question_explanation(question: dict[str, Any], concept_name: str) -> str:
    spec = question.get("answer_spec") or {}
    if spec.get("explanation"):
        return spec["explanation"]
    options = spec.get("options") or []
    idx = spec.get("correct_index")
    answer = options[idx] if options and idx is not None and idx < len(options) else "the accurate description"
    return f"The answer is: {answer}. This is the key idea of '{concept_name}'."


class GradingAI(Protocol):
    model_name: str
    prompt_version: str

    async def judge(self, question: dict[str, Any], answer_text: str) -> Judgement: ...
    def hint(self, question: dict[str, Any], concept_name: str) -> str: ...
    def explanation(self, question: dict[str, Any], concept_name: str) -> str: ...
    async def draft_feedback(self, context: dict[str, Any]) -> str: ...


class StubGradingAI:
    """Markers in the answer text drive the outcome so tests are deterministic:
    "[timeout]" -> provider failure, "[unsure]" -> PARTIAL at low confidence,
    "[wrong]" or fewer than 5 words -> INCORRECT, otherwise CORRECT."""

    model_name = "stub-grading-ai"
    prompt_version = "stub-v1"

    async def judge(self, question: dict[str, Any], answer_text: str) -> Judgement:
        text = answer_text.strip()
        tokens = len(question["prompt_text"].split()) + len(text.split())
        if "[timeout]" in text:
            raise GradingUnavailable("grading model timed out")
        if "[unsure]" in text:
            return Judgement("PARTIAL", 0.4, "Partly there. Add an example.", tokens, 12)
        if "[wrong]" in text or len(text.split()) < 5:
            return Judgement("INCORRECT", 0.9, "Not yet. Revisit the key idea.", tokens, 10)
        return Judgement("CORRECT", 0.92, "Clear explanation with an example.", tokens, 8)

    def hint(self, question: dict[str, Any], concept_name: str) -> str:
        return question_hint(question, concept_name)

    def explanation(self, question: dict[str, Any], concept_name: str) -> str:
        return question_explanation(question, concept_name)

    async def draft_feedback(self, context: dict[str, Any]) -> str:
        strong = [c["name"] for c in context.get("concepts", []) if c.get("status") == "MASTERED"]
        weak = [c["name"] for c in context.get("concepts", []) if c.get("status") not in (None, "MASTERED")]
        parts = [f"Hi {context.get('student_first_name', 'there')},"]
        if strong:
            parts.append(f"good work on {', '.join(strong)}.")
        if weak:
            parts.append(f"Let's spend a little more time on {', '.join(weak)}.")
        if context.get("submission_note"):
            parts.append("Thanks for your submission.")
        return " ".join(parts)


_ai: GradingAI | None = None


def get_grading_ai() -> GradingAI:
    global _ai
    if _ai is None:
        from app.config import get_settings

        if get_settings().ai_provider == "anthropic":
            from app.ai.anthropic import ClaudeGradingAI

            _ai = ClaudeGradingAI()
        else:
            _ai = StubGradingAI()
    return _ai


def set_grading_ai(ai: GradingAI | None) -> None:
    global _ai
    _ai = ai
