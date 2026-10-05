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


class GradingAI(Protocol):
    model_name: str
    prompt_version: str

    async def judge(self, question: dict[str, Any], answer_text: str) -> Judgement: ...
    def hint(self, question: dict[str, Any], concept_name: str) -> str: ...
    def explanation(self, question: dict[str, Any], concept_name: str) -> str: ...


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
        return f"Hint: think about what '{concept_name}' means before choosing."

    def explanation(self, question: dict[str, Any], concept_name: str) -> str:
        spec = question.get("answer_spec") or {}
        options = spec.get("options") or []
        idx = spec.get("correct_index")
        answer = options[idx] if options and idx is not None and idx < len(options) else "the accurate description"
        return f"The answer is: {answer}. This is the key idea of '{concept_name}'."


_ai: GradingAI | None = None


def get_grading_ai() -> GradingAI:
    global _ai
    if _ai is None:
        _ai = StubGradingAI()
    return _ai


def set_grading_ai(ai: GradingAI | None) -> None:
    global _ai
    _ai = ai
