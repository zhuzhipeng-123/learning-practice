from dataclasses import dataclass
from datetime import datetime
from typing import Literal

QuestionType = Literal["code", "theory"]
SourceKind = Literal["feishu", "derived"]


@dataclass(frozen=True)
class QuestionDraft:
    source_id: str
    document_id: str
    question_type: QuestionType
    source_kind: SourceKind
    main_anchor_block_id: str
    prompt_block_ids: tuple[str, ...]
    reference_block_ids: tuple[str, ...]
    category_path: str
    prompt: str
    reference_text: str | None
    material_status: str
    confirmation_status: str = "confirmed"
    materials: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Submission:
    task_id: str
    request_key: str
    submitted_at: datetime
    entry_mode: str
    answer_text: str | None = None
    code_self_result: Literal["can_solve", "cannot_solve"] | None = None
    note: str | None = None
    answer_exposed_at: datetime | None = None
