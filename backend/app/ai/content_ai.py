"""ContentIntelligence model boundary (contract §6.2).

Everything the pipeline asks of an AI model goes through `ContentAI`. The stub is deterministic
so the end-to-end path and its tests are stable; the real model plugs in behind the same
interface later. Every call is logged to `ai_invocations` by the caller (contract §12).
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


class ContentExtractionError(Exception):
    """The source could not be turned into evidence chunks (corrupt file, private video...)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Chunk:
    chunk_type: str  # TRANSCRIPT | OCR | DOCUMENT_TEXT | CODE | VISUAL_DESCRIPTION | TABLE
    text: str
    page_number: int | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None


@dataclass(frozen=True)
class Concept:
    name: str
    description: str
    importance: str  # CORE | SUPPORTING | STRETCH | PREREQUISITE
    evidence_chunk_indexes: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class Question:
    concept_name: str
    question_type: str  # MCQ | SHORT_ANSWER | FREE_TEXT
    cognitive_type: str  # RECALL | UNDERSTANDING | REASONING | APPLICATION | TRANSFER
    prompt_text: str
    difficulty: str
    answer_spec: dict[str, Any]


class ContentAI(Protocol):
    model_name: str
    prompt_version: str

    async def extract(self, item: dict[str, Any]) -> tuple[list[Chunk], Usage]: ...
    async def extract_concepts(self, lesson: dict[str, Any], chunks: list[str]) -> tuple[list[Concept], Usage]: ...
    async def generate_questions(
        self, lesson: dict[str, Any], concepts: list[Concept]
    ) -> tuple[list[Question], Usage]: ...


def _usage(*texts: str) -> Usage:
    n = sum(len(t.split()) for t in texts)
    return Usage(input_tokens=max(1, n), output_tokens=max(1, n // 2))


class StubContentAI:
    """Deterministic stand-in. A content title containing "[fail]" simulates an unreadable file;
    "[fail-once]" fails the first attempt only (a transient provider error, fixed by retrying)."""

    model_name = "stub-content-ai"
    prompt_version = "stub-v1"

    def __init__(self) -> None:
        self._failed_once: set[Any] = set()

    async def extract(self, item: dict[str, Any]) -> tuple[list[Chunk], Usage]:
        title = item.get("title") or item["content_type"].lower()
        if "[fail]" in title:
            raise ContentExtractionError("UNREADABLE_SOURCE", f"Could not read '{title}'.")
        if "[fail-once]" in title and item["content_item_id"] not in self._failed_once:
            self._failed_once.add(item["content_item_id"])
            raise ContentExtractionError("PROVIDER_UNAVAILABLE", f"Temporary failure reading '{title}'.")
        kind = {"VIDEO": "TRANSCRIPT", "AUDIO": "TRANSCRIPT", "YOUTUBE": "TRANSCRIPT", "IMAGE": "OCR", "PHOTO": "OCR"}
        chunk_type = kind.get(item["content_type"], "DOCUMENT_TEXT")
        chunks = [
            Chunk(chunk_type, f"{title}: overview of the main idea and why it matters.", page_number=1),
            Chunk(chunk_type, f"{title}: the step-by-step process and the terms involved.", page_number=2),
            Chunk(chunk_type, f"{title}: a worked real-world example.", page_number=3),
        ]
        return chunks, _usage(*(c.text for c in chunks))

    async def extract_concepts(self, lesson: dict[str, Any], chunks: list[str]) -> tuple[list[Concept], Usage]:
        t = lesson["title"]
        concepts = [
            Concept(f"{t}: core idea", f"What {t} is and why it matters.", "CORE", [0]),
            Concept(f"{t}: key process", f"The steps and vocabulary of {t}.", "CORE", [1]),
            Concept(f"{t}: application", f"Using {t} to explain a real situation.", "SUPPORTING", [2]),
        ]
        return concepts, _usage(*chunks)

    async def generate_questions(self, lesson: dict[str, Any], concepts: list[Concept]) -> tuple[list[Question], Usage]:
        questions: list[Question] = []
        for c in concepts:
            questions.append(
                Question(
                    c.name,
                    "MCQ",
                    "RECALL",
                    f"Which statement best describes {c.name.lower()}?",
                    "EASY",
                    {
                        "options": [
                            f"The accurate description of {c.name.lower()}",
                            "A common misconception",
                            "An unrelated idea",
                            "A partially correct idea",
                        ],
                        "correct_index": 0,
                    },
                )
            )
            if c.importance == "CORE":
                questions.append(
                    Question(
                        c.name,
                        "FREE_TEXT",
                        "APPLICATION",
                        f"In your own words, explain {c.name.lower()} using an example.",
                        "MEDIUM",
                        {"rubric": ["States the idea correctly", "Gives a relevant example", "Uses key terms"]},
                    )
                )
        return questions, _usage(*(q.prompt_text for q in questions))


_ai: ContentAI | None = None


def get_content_ai() -> ContentAI:
    global _ai
    if _ai is None:
        _ai = StubContentAI()
    return _ai


def set_content_ai(ai: ContentAI | None) -> None:
    global _ai
    _ai = ai
