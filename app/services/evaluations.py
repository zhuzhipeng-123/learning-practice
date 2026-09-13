import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.services.practice import adopt_theory_evaluation

ALLOWED_VERDICTS = {"aligned", "needs_review", "unable_to_assess"}


class EvaluationValidationError(RuntimeError):
    """Model output cannot be safely adopted."""


def validate_evaluation(payload: dict[str, Any], allowed_reference_ids: set[str]) -> None:
    if not isinstance(payload, dict):
        raise EvaluationValidationError("evaluation must be a JSON object")
    verdict = payload.get("verdict")
    if not isinstance(verdict, str) or verdict not in ALLOWED_VERDICTS:
        raise EvaluationValidationError("invalid verdict")
    reference_status = payload.get('reference_status')
    if reference_status is not None:
        if not isinstance(reference_status, str) or reference_status not in {'sufficient', 'insufficient', 'contradictory'}:
            raise EvaluationValidationError('参考充分性状态无效')
        if reference_status != 'sufficient' and verdict != 'unable_to_assess':
            raise EvaluationValidationError('模型声明参考依据不足或矛盾，却给出评分；本次结果未采用，请核对或重试')
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        raise EvaluationValidationError("evidence_refs must be a list")
    if any(not isinstance(reference_id, str) or reference_id not in allowed_reference_ids
           for reference_id in evidence_refs):
        raise EvaluationValidationError("evaluation referenced unknown source material")
    for field in ("covered_points", "missing_points", "errors"):
        if not isinstance(payload.get(field), list) or any(not isinstance(item, str) for item in payload[field]):
            raise EvaluationValidationError(f"{field} must be a list of strings")
    if not isinstance(payload.get("brief_feedback"), str):
        raise EvaluationValidationError("brief_feedback must be text")
    if verdict == 'aligned' and (payload['errors'] or payload['missing_points']):
        raise EvaluationValidationError('模型结论说通过，但同时列出了错误或核心遗漏，本次未采用；请重试或手动评价')


def adopt_model_evaluation(
    connection: sqlite3.Connection,
    attempt_id: str,
    payload: dict[str, Any],
    allowed_reference_ids: set[str],
    created_at: datetime,
    expected_adoption: str | None = None,
    protect_adoption: bool = False,
    model_id: str | None = None,
) -> str:
    validate_evaluation(payload, allowed_reference_ids)
    evaluation_id = adopt_theory_evaluation(
        connection,
        attempt_id,
        str(payload["verdict"]),
        created_at.astimezone(UTC),
        expected_adoption=expected_adoption,
        protect_adoption=protect_adoption,
        raw_json=json.dumps(payload, ensure_ascii=False),
        model_id=model_id,
    )
    return evaluation_id
