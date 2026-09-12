import json
from pathlib import Path

from app.domain import QuestionDraft

SAMPLES_PATH = Path(__file__).with_name("fixtures") / "p0_questions.json"


class SampleValidationError(RuntimeError):
    """A reviewed fixture no longer satisfies its publication contract."""


def load_reviewed_samples() -> tuple[list[QuestionDraft], list[dict[str, object]]]:
    """Load P0 samples while keeping ambiguous adaptations unpublished."""
    payload = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
    drafts: list[QuestionDraft] = []
    candidates: list[dict[str, object]] = []
    for item in payload["questions"]:
        if item["material_status"] == "candidate_requires_user_confirmation":
            candidates.append(item)
            continue
        drafts.append(_to_draft(item))
    _validate_counts(payload["questions"])
    return drafts, candidates


def _to_draft(item: dict[str, object]) -> QuestionDraft:
    return QuestionDraft(
        source_id=f"source-{item['question_type']}",
        document_id=str(item["document_id"]),
        question_type=item["question_type"],  # type: ignore[arg-type]
        source_kind="feishu",
        main_anchor_block_id=str(item["main_anchor_block_id"]),
        prompt_block_ids=tuple(item["prompt_block_ids"]),  # type: ignore[arg-type]
        reference_block_ids=tuple(item["reference_block_ids"]),  # type: ignore[arg-type]
        category_path=str(item["category_path"]),
        prompt=str(item["prompt"]),
        reference_text=str(item["reference_text"]),
        material_status=str(item["material_status"]),
    )


def _validate_counts(questions: list[dict[str, object]]) -> None:
    code_count = sum(item["question_type"] == "code" for item in questions)
    theory_count = sum(item["question_type"] == "theory" for item in questions)
    if (code_count, theory_count) != (2, 3):
        raise SampleValidationError("P0 requires exactly two code and three theory samples")
