import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.parsers.docx import block_text
from app.services.alignment_experiment import (
    AlignmentExperimentError,
    rule_prediction,
    validate_model_result,
)
from scripts.alignment_experiment_cases import build_cases, repeat_case_ids
from scripts.run_alignment_experiment import run

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _valid_item(case):
    expected = case.expected
    decision = 'insufficient_material' if case.requires_visual else expected['decision']
    check = 'pass' if decision == 'accept' else 'uncertain'
    evidence_block = next(block for block in case.blocks if block_text(block))
    quote = block_text(evidence_block)[:20]
    return {'id': case.id, 'anchor_id': expected['anchor_id'], 'decision': decision,
            'prompt_block_ids': expected['prompt_block_ids'],
            'reference_block_ids': expected['reference_block_ids'],
            'checks': {'boundary': check, 'question_answer_alignment': check,
                       'material_completeness': check, 'obvious_contradictions': check},
            'evidence': [{'block_id': evidence_block['block_id'], 'quote': quote}],
            'issues': [] if decision == 'accept' else ['需要人工确认或补充材料']}


class ValidExperimentClient:
    def __init__(self, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first
        self.cases = {case.id: case for case in build_cases()}

    def complete_json(self, messages, max_tokens):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            content = '{"contract_version":"wrong","items":[]}'
        else:
            payload = json.loads(messages[1]['content'])
            items = [_valid_item(self.cases[item['id']]) for item in payload['items']]
            content = json.dumps({'contract_version': 'source-review-v2', 'items': items},
                                 ensure_ascii=False)
        return SimpleNamespace(content=content, model='local-fake',
                               usage={'prompt_tokens': 100, 'completion_tokens': 50})


def test_experiment_fixture_has_frozen_split_and_varied_synthetic_cases():
    cases = build_cases()
    assert len(cases) == 56
    assert {split: sum(case.split == split for case in cases)
            for split in ('smoke', 'development', 'holdout')} == {
                'smoke': 8, 'development': 36, 'holdout': 12,
            }
    assert len({case.scenario for case in cases}) == 14
    assert len(repeat_case_ids(cases)) == 8
    assert {case.scenario for case in cases[:8]} == {
        'clear-h3', 'ambiguous-h4', 'one-prompt-image', 'native-table',
        'sheet-grid', 'unsupported-reference', 'prompt-injection', 'keyword-mismatch',
    }
    assert all('example.test' not in json.dumps(case.blocks) for case in cases)


def test_rule_baseline_uses_current_parser_and_records_known_limits():
    result = run('A')
    assert result['state']['call_count'] == 0
    assert result['case_count'] == 56
    assert result['rule_baseline']['boundary_accuracy'] > 0.9
    assert result['rule_baseline']['false_accepts'] > 0  # semantic mismatch is beyond pure rules
    assert result['rule_baseline']['critical_errors'] > 0
    assert rule_prediction(build_cases()[0])['decision'] == 'accept'


@pytest.mark.parametrize('mutation', ['unknown-id', 'fake-quote', 'visual-accept', 'contradictory-accept'])
def test_model_contract_rejects_unsafe_or_unverifiable_results(mutation):
    case = next(case for case in build_cases() if case.requires_visual)
    item = _valid_item(case)
    if mutation == 'unknown-id':
        item['reference_block_ids'].append('invented')
    elif mutation == 'fake-quote':
        item['evidence'][0]['quote'] = 'not present in source block'
    elif mutation == 'visual-accept':
        item['decision'] = 'accept'
        item['checks'] = {key: 'pass' for key in item['checks']}
    else:
        item['decision'] = 'accept'
        item['checks']['boundary'] = 'uncertain'
    with pytest.raises(AlignmentExperimentError):
        validate_model_result([case], {'contract_version': 'source-review-v2', 'items': [item]})


def test_b_mode_uses_only_selected_cases_retries_format_once_and_stays_under_cap():
    client = ValidExperimentClient(fail_first=True)
    result = run('B', client, max_calls=40)
    assert result['state']['retry_count'] == 1
    assert result['state']['call_count'] == client.calls < 40
    assert result['state']['rejected_outputs'][0]['error']
    assert result['repeat_consistency']['runs'] == 2
    assert result['repeat_consistency']['unstable_ids'] == []
    assert result['calls'] and all(call['model'] == 'local-fake' for call in result['calls'])


@pytest.mark.parametrize('decision,checks', [
    ('needs_confirmation', {'boundary': 'pass', 'question_answer_alignment': 'pass',
                            'material_completeness': 'pass', 'obvious_contradictions': 'pass'}),
    ('reject', {'boundary': 'uncertain', 'question_answer_alignment': 'uncertain',
                'material_completeness': 'uncertain', 'obvious_contradictions': 'uncertain'}),
    ('insufficient_material', {'boundary': 'pass', 'question_answer_alignment': 'pass',
                               'material_completeness': 'pass',
                               'obvious_contradictions': 'uncertain'}),
])
def test_all_non_accept_decisions_must_match_checks(decision, checks):
    case = build_cases()[0]
    item = _valid_item(case)
    item['decision'] = decision
    item['checks'] = checks
    with pytest.raises(AlignmentExperimentError):
        validate_model_result([case], {'contract_version': 'source-review-v2', 'items': [item]})


def test_table_metadata_without_cells_cannot_be_accepted():
    case = next(case for case in build_cases() if case.scenario == 'native-table')
    item = _valid_item(case)
    item['decision'] = 'accept'
    item['checks'] = {key: 'pass' for key in item['checks']}
    with pytest.raises(AlignmentExperimentError, match='单元格'):
        validate_model_result([case], {'contract_version': 'source-review-v2', 'items': [item]})


def test_module_heading_cannot_be_used_as_anchor_or_boundary():
    case = build_cases()[0]
    item = _valid_item(case)
    item['anchor_id'] = case.blocks[0]['block_id']
    with pytest.raises(AlignmentExperimentError, match='模块标题'):
        validate_model_result([case], {'contract_version': 'source-review-v2', 'items': [item]})


def test_failed_attempt_is_counted_and_checkpointed(tmp_path):
    class FailingClient(ValidExperimentClient):
        def complete_json(self, messages, max_tokens):
            self.calls += 1
            raise RuntimeError('synthetic transport failure')

    checkpoint = tmp_path / 'experiment.json'
    client = FailingClient()
    with pytest.raises(RuntimeError, match='transport failure'):
        run('B', client, stage='smoke', checkpoint_path=checkpoint)
    saved = json.loads(checkpoint.read_text(encoding='utf-8'))
    assert client.calls == saved['state']['call_count'] == 1
    assert saved['state']['failures'][0]['type'] == 'RuntimeError'


def test_stages_require_a_gate_and_resume_completed_batches(tmp_path):
    checkpoint = tmp_path / 'experiment.json'
    client = ValidExperimentClient()
    with pytest.raises(RuntimeError, match='必须先完成'):
        run('B', client, stage='development', checkpoint_path=checkpoint)
    smoke = run('B', client, stage='smoke', checkpoint_path=checkpoint)
    calls_after_smoke = client.calls
    replay = run('B', client, stage='smoke', checkpoint_path=checkpoint)
    assert replay['state']['call_count'] == smoke['state']['call_count']
    assert client.calls == calls_after_smoke
    development = run('B', client, stage='development', checkpoint_path=checkpoint)
    assert development['completed_stages'][:2] == ['smoke', 'development']


def test_human_decision_waits_for_both_repeat_stability_stages(tmp_path):
    checkpoint = tmp_path / 'experiment.json'
    client = ValidExperimentClient()
    result = None
    for stage in ('smoke', 'development', 'holdout'):
        result = run('B', client, stage=stage, checkpoint_path=checkpoint)
    assert result['decision_support']['ready_for_human_decision'] is False
    assert result['repeat_consistency'] == {'runs': 0, 'stable': None, 'unstable_ids': []}
    result = run('B', client, stage='repeat-1', checkpoint_path=checkpoint)
    assert result['decision_support']['ready_for_human_decision'] is False
    assert result['repeat_consistency']['runs'] == 1
    result = run('B', client, stage='repeat-2', checkpoint_path=checkpoint)
    assert result['decision_support']['ready_for_human_decision'] is True
    assert result['repeat_consistency']['runs'] == 2


def test_soft_budget_stops_before_network_call():
    client = ValidExperimentClient()
    with pytest.raises(RuntimeError, match='软预算'):
        run('B', client, stage='smoke', max_elapsed_seconds=0)
    assert client.calls == 0


def test_b_and_c_share_one_atomic_real_call_budget(tmp_path):
    ledger = tmp_path / 'budget.sqlite3'
    first = ValidExperimentClient()
    run('B', first, stage='smoke', checkpoint_path=tmp_path / 'b.json',
        budget_ledger=ledger, campaign_max_calls=1)
    assert first.calls == 1

    second = ValidExperimentClient()
    with pytest.raises(RuntimeError, match='共享的1次'):
        run('C', second, stage='smoke', checkpoint_path=tmp_path / 'c.json',
            budget_ledger=ledger, campaign_max_calls=1)
    assert second.calls == 0


def test_experiment_script_runs_directly_outside_project_directory(tmp_path):
    output = tmp_path / 'baseline.json'
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / 'scripts' / 'run_alignment_experiment.py'),
         '--mode', 'A', '--output', str(output)],
        cwd=tmp_path, capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text(encoding='utf-8'))['state']['call_count'] == 0
