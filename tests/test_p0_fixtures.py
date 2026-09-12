from app.parsers.p0_samples import load_reviewed_samples


def test_p0_fixture_contains_two_code_and_three_theory_questions() -> None:
    drafts, candidates = load_reviewed_samples()
    all_items = [*drafts, *candidates]

    assert len(all_items) == 5
    assert sum(item.question_type == "code" for item in drafts) == 2
    assert sum(item.question_type == "theory" for item in drafts) == 2
    assert len(candidates) == 1


def test_every_published_sample_has_traceable_boundaries() -> None:
    drafts, _ = load_reviewed_samples()

    for question in drafts:
        assert question.document_id
        assert question.main_anchor_block_id
        assert question.prompt_block_ids
        assert question.prompt
        assert question.reference_text


def test_ambiguous_note_adaptation_stays_unpublished() -> None:
    drafts, candidates = load_reviewed_samples()
    published_anchors = {draft.main_anchor_block_id for draft in drafts}

    assert candidates[0]["sample_id"] == "theory-arithmetic-intensity"
    assert candidates[0]["main_anchor_block_id"] not in published_anchors
