"""Run the isolated A/B/C source-alignment comparison on synthetic data only."""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.agnes import (
    AgnesClient,
    AgnesConfigurationError,
    AgnesRateLimitError,
    AgnesSettings,
)
from app.config import agnes_base_url, agnes_default_model
from app.services.alignment_experiment import (
    VALIDATOR_VERSION,
    AlignmentExperimentError,
    experiment_messages,
    rule_prediction,
    score,
    validate_model_result,
)
from app.services.llm_config import AGNES_API_KEY, secret_value
from app.services.model_json import parse_model_json
from scripts.alignment_experiment_cases import build_cases, repeat_case_ids

MAX_CALLS = 40
BATCH_SIZE = 8
MAX_OUTPUT_TOKENS = 6144
MAX_INPUT_CHARS = 48_000
SOFT_BUDGET_SECONDS = 3600
STAGES = ('smoke', 'development', 'holdout', 'repeat-1', 'repeat-2')


def _selected(cases, mode):
    if mode == 'C':
        return list(cases)
    if mode == 'B':
        return [case for case in cases
                if case.split == 'smoke' or rule_prediction(case)['decision'] != 'accept'
                or case.requires_visual]
    return []


def _batches(items):
    return [items[index:index + BATCH_SIZE] for index in range(0, len(items), BATCH_SIZE)]


def _hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def _fingerprints(cases, mode, client, max_calls, max_elapsed_seconds):
    settings = getattr(client, 'settings', None)
    model = {'client': type(client).__name__ if client else None,
             'base_url': getattr(settings, 'base_url', None),
             'model': getattr(settings, 'model', None)}
    system_prompt = experiment_messages([cases[0]])[0]['content']
    return {
        'dataset': _hash([asdict(case) for case in cases]),
        'prompt': hashlib.sha256(system_prompt.encode()).hexdigest(),
        'model': _hash(model),
        'config': _hash({'mode': mode, 'batch_size': BATCH_SIZE,
                         'validator_version': VALIDATOR_VERSION,
                         'max_output_tokens': MAX_OUTPUT_TOKENS,
                         'max_input_chars': MAX_INPUT_CHARS,
                         'max_calls': max_calls,
                         'max_elapsed_seconds': max_elapsed_seconds}),
    }


def _new_progress(cases, mode, client, max_calls, max_elapsed_seconds):
    baseline = {case.id: rule_prediction(case) for case in cases}
    return {
        'contract': 'alignment-experiment-progress-v3',
        'mode': mode,
        'fingerprints': _fingerprints(cases, mode, client, max_calls, max_elapsed_seconds),
        'state': {'call_count': 0, 'retry_count': 0, 'max_calls': max_calls,
                  'max_elapsed_seconds': max_elapsed_seconds, 'elapsed_seconds': 0.0,
                  'usage': [], 'rejected_outputs': [], 'failures': [],
                  'campaign_attempts': []},
        'baseline': baseline,
        'predictions': dict(baseline),
        'calls': [],
        'repeat_runs': {},
        'completed_batches': [],
        'completed_stages': [],
    }


def _atomic_write(path, value):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _load_progress(path, expected):
    if path is None or not path.exists():
        return expected
    saved = json.loads(path.read_text(encoding='utf-8'))
    if saved.get('contract') != expected['contract']:
        raise RuntimeError('检查点契约不匹配，拒绝覆盖或续跑')
    if saved.get('mode') != expected['mode'] or saved.get('fingerprints') != expected['fingerprints']:
        raise RuntimeError('数据、提示词、模型或预算已变化；请使用新的输出路径重新开始')
    return saved


def _preflight(messages):
    total = sum(len(message['content']) for message in messages)
    if total > MAX_INPUT_CHARS:
        raise RuntimeError(f'冻结输入为{total}字符，超过{MAX_INPUT_CHARS}字符上限，未调用模型')


def _campaign_fingerprint(progress, campaign_max_calls):
    fingerprints = progress['fingerprints']
    return _hash({'dataset': fingerprints['dataset'], 'prompt': fingerprints['prompt'],
                  'model': fingerprints['model'], 'max_calls': campaign_max_calls})


def _reserve_campaign_attempt(path, fingerprint, max_calls):
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=30) as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            'CREATE TABLE IF NOT EXISTS campaign_budget('
            'id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL, attempts INTEGER NOT NULL)',
        )
        connection.execute(
            'CREATE TABLE IF NOT EXISTS campaign_config('
            'fingerprint TEXT PRIMARY KEY, first_attempt INTEGER NOT NULL)',
        )
        row = connection.execute(
            'SELECT fingerprint,attempts FROM campaign_budget WHERE id=1',
        ).fetchone()
        attempts = row[1] if row else 0
        if attempts >= max_calls:
            raise RuntimeError(f'达到整轮实验共享的{max_calls}次真实调用上限，实验已停止')
        if row:
            connection.execute('UPDATE campaign_budget SET attempts=attempts+1 WHERE id=1')
        else:
            connection.execute(
                'INSERT INTO campaign_budget(id,fingerprint,attempts) VALUES(1,?,1)',
                (fingerprint,),
            )
        connection.execute(
            'INSERT OR IGNORE INTO campaign_config(fingerprint,first_attempt) VALUES(?,?)',
            (fingerprint, attempts + 1),
        )
        return attempts + 1


def _attempt(client, messages, progress, checkpoint_path, budget_ledger,
             campaign_max_calls):
    state = progress['state']
    _preflight(messages)
    if state['call_count'] >= state['max_calls']:
        raise RuntimeError('达到冻结的真实调用上限，实验已停止')
    if state['elapsed_seconds'] >= state['max_elapsed_seconds']:
        raise RuntimeError('达到冻结的60分钟软预算，实验已暂停')
    campaign_attempt = _reserve_campaign_attempt(
        budget_ledger,
        _campaign_fingerprint(progress, campaign_max_calls),
        campaign_max_calls,
    )
    state['call_count'] += 1
    if campaign_attempt is not None:
        state['campaign_attempts'].append(campaign_attempt)
    attempt = state['call_count']
    _atomic_write(checkpoint_path, progress)
    started = time.perf_counter()
    try:
        reply = client.complete_json(messages, max_tokens=MAX_OUTPUT_TOKENS)
    except Exception as error:
        state['elapsed_seconds'] += time.perf_counter() - started
        state['failures'].append({'attempt': attempt, 'type': type(error).__name__,
                                  'message': str(error),
                                  'at': datetime.now(UTC).isoformat()})
        _atomic_write(checkpoint_path, progress)
        raise
    state['elapsed_seconds'] += time.perf_counter() - started
    state['usage'].append(reply.usage or {})
    _atomic_write(checkpoint_path, progress)
    return reply


def _call(client, cases, progress, checkpoint_path, budget_ledger, campaign_max_calls):
    reply = _attempt(client, experiment_messages(cases), progress, checkpoint_path,
                     budget_ledger, campaign_max_calls)
    try:
        return validate_model_result(cases, parse_model_json(reply.content)), reply
    except (AlignmentExperimentError, ValueError) as error:
        progress['state']['rejected_outputs'].append({
            'case_ids': [case.id for case in cases], 'error': str(error),
            'response': reply.content,
        })
        progress['state']['retry_count'] += 1
        _atomic_write(checkpoint_path, progress)
        corrected = _attempt(
            client, experiment_messages(cases, reply.content, str(error)), progress, checkpoint_path,
            budget_ledger, campaign_max_calls,
        )
        try:
            return validate_model_result(cases, parse_model_json(corrected.content)), corrected
        except (AlignmentExperimentError, ValueError) as corrected_error:
            progress['state']['rejected_outputs'].append({
                'case_ids': [case.id for case in cases], 'error': str(corrected_error),
                'response': corrected.content,
            })
            _atomic_write(checkpoint_path, progress)
            raise


def _stage_cases(cases, mode, stage):
    if stage.startswith('repeat-'):
        wanted = set(repeat_case_ids(cases))
        return [case for case in _selected(cases, mode) if case.id in wanted]
    return _selected([case for case in cases if case.split == stage], mode)


def _required_predecessor(stage):
    index = STAGES.index(stage)
    return STAGES[index - 1] if index else None


def _run_stage(cases, mode, client, stage, progress, checkpoint_path, budget_ledger,
               campaign_max_calls):
    predecessor = _required_predecessor(stage)
    if predecessor and predecessor not in progress['completed_stages']:
        raise RuntimeError(f'必须先完成并检查 {predecessor}，才能运行 {stage}')
    target = _stage_cases(cases, mode, stage)
    for index, batch in enumerate(_batches(target)):
        batch_key = f'{stage}:{index}:' + ','.join(case.id for case in batch)
        if batch_key in progress['completed_batches']:
            continue
        items, reply = _call(
            client, batch, progress, checkpoint_path, budget_ledger, campaign_max_calls,
        )
        if stage.startswith('repeat-'):
            progress['repeat_runs'].setdefault(stage, {}).update(
                {item['id']: item for item in items},
            )
        else:
            progress['predictions'].update({item['id']: item for item in items})
        progress['calls'].append({'stage': stage, 'case_ids': [case.id for case in batch],
                                  'model': reply.model})
        progress['completed_batches'].append(batch_key)
        _atomic_write(checkpoint_path, progress)
    if stage not in progress['completed_stages']:
        progress['completed_stages'].append(stage)
    _atomic_write(checkpoint_path, progress)


def run(mode, client=None, max_calls=MAX_CALLS, *, stage='all', checkpoint_path=None,
        max_elapsed_seconds=SOFT_BUDGET_SECONDS, budget_ledger=None,
        campaign_max_calls=MAX_CALLS):
    if mode not in {'A', 'B', 'C'}:
        raise ValueError('mode must be A, B, or C')
    if stage != 'all' and stage not in STAGES:
        raise ValueError('unknown experiment stage')
    if not 1 <= max_calls <= MAX_CALLS:
        raise ValueError('max_calls must be between 1 and 40')
    if not 0 <= max_elapsed_seconds <= SOFT_BUDGET_SECONDS:
        raise ValueError('max_elapsed_seconds must be between 0 and 3600')
    if not 1 <= campaign_max_calls <= MAX_CALLS:
        raise ValueError('campaign_max_calls must be between 1 and 40')
    cases = build_cases()
    max_calls = min(max_calls, MAX_CALLS)
    checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
    budget_ledger = Path(budget_ledger) if budget_ledger else None
    fresh = _new_progress(cases, mode, client, max_calls, max_elapsed_seconds)
    progress = _load_progress(checkpoint_path, fresh)
    if mode != 'A':
        if client is None:
            raise RuntimeError('B/C需要显式提供Agnes客户端')
        if stage == 'all' and isinstance(client, AgnesClient):
            raise RuntimeError('真实Agnes实验必须一次只运行一个冻结阶段')
        stages = STAGES if stage == 'all' else (stage,)
        for current_stage in stages:
            _run_stage(cases, mode, client, current_stage, progress, checkpoint_path,
                       budget_ledger, campaign_max_calls)
    baseline = progress['baseline']
    predictions = progress['predictions']
    repeat_runs = [progress['repeat_runs'][key] for key in ('repeat-1', 'repeat-2')
                   if key in progress['repeat_runs']]
    repeat_consistency = _repeat_consistency(repeat_runs)
    per_split = {split: score([case for case in cases if case.split == split],
                              list(predictions.values()))
                 for split in ('smoke', 'development', 'holdout')}
    result = {'contract': 'alignment-experiment-v3', 'mode': mode,
              'created_at': datetime.now(UTC).isoformat(), 'case_count': len(cases),
              'split_counts': {split: sum(case.split == split for case in cases)
                               for split in ('smoke', 'development', 'holdout')},
              'repeat_case_ids': list(repeat_case_ids(cases)),
              'fingerprints': progress['fingerprints'],
              'rule_baseline': score(cases, list(baseline.values())),
              'result': score(cases, list(predictions.values())),
              'per_split': per_split, 'state': progress['state'],
              'calls': progress['calls'], 'completed_stages': progress['completed_stages'],
              'metrics': _metrics(progress['state']),
              'decision_support': _decision_support(
                  score(cases, list(baseline.values())),
                  score(cases, list(predictions.values())),
                  progress['completed_stages'],
                  repeat_consistency,
              ),
              'repeat_consistency': repeat_consistency,
              'cases': [asdict(case) for case in cases]}
    _atomic_write(checkpoint_path, progress)
    return result


def _metrics(state):
    usage_totals = {}
    for entry in state['usage']:
        for key, value in entry.items():
            if isinstance(value, (int, float)):
                usage_totals[key] = usage_totals.get(key, 0) + value
    failure_types = {}
    for failure in state['failures']:
        key = failure['type']
        failure_types[key] = failure_types.get(key, 0) + 1
    return {'usage_totals': usage_totals, 'failure_types': failure_types,
            'rate_limit_failures': failure_types.get('AgnesRateLimitError', 0),
            'elapsed_seconds': state['elapsed_seconds']}


def _decision_support(baseline, result, completed_stages, repeat_consistency):
    reduction = ((baseline['manual_items'] - result['manual_items'])
                 / baseline['manual_items'] if baseline['manual_items'] else 0)
    ready = (all(stage in completed_stages for stage in STAGES)
             and repeat_consistency['runs'] == 2
             and repeat_consistency['stable'] is not None)
    return {
        'ready_for_human_decision': ready,
        'critical_error_delta': result['critical_errors'] - baseline['critical_errors'],
        'boundary_accuracy_delta': result['boundary_accuracy'] - baseline['boundary_accuracy'],
        'manual_rate_delta': result['manual_rate'] - baseline['manual_rate'],
        'manual_reduction_ratio': reduction,
        'no_more_critical_errors': result['critical_errors'] <= baseline['critical_errors'],
        'value_signal': (result['critical_errors'] < baseline['critical_errors']
                         or reduction >= 0.30) if ready else None,
        'note': '这些是程序计算的门槛事实，A/B/C仍需人工决定。',
    }


def _repeat_consistency(runs):
    if not runs:
        return {'runs': 0, 'stable': None, 'unstable_ids': []}
    ids = set.intersection(*(set(run) for run in runs)) if runs else set()
    unstable = []
    for case_id in ids:
        values = {(run[case_id]['decision'], tuple(run[case_id]['prompt_block_ids']),
                   tuple(run[case_id]['reference_block_ids'])) for run in runs}
        if len(values) != 1:
            unstable.append(case_id)
    return {'runs': len(runs), 'stable': len(ids) - len(unstable),
            'unstable_ids': sorted(unstable)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('A', 'B', 'C'), default='A')
    parser.add_argument('--stage', choices=STAGES)
    parser.add_argument('--allow-real-agnes', action='store_true')
    parser.add_argument('--max-calls', type=int, default=MAX_CALLS)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_calls <= MAX_CALLS:
        parser.error('--max-calls must be between 1 and 40')
    client = None
    if args.mode != 'A':
        if not args.allow_real_agnes:
            parser.error('B/C真实调用必须显式传入 --allow-real-agnes')
        if not args.stage:
            parser.error('B/C必须用 --stage 每次只运行一个冻结阶段')
        client = _configured_client()
    output = args.output or Path('.runtime') / f'alignment-experiment-{args.mode.lower()}.json'
    checkpoint = output.with_suffix('.checkpoint.json')
    budget_ledger = PROJECT_ROOT / '.runtime' / 'alignment-experiment-budget.sqlite3'
    try:
        result = run(args.mode, client, args.max_calls, stage=args.stage or 'all',
                     checkpoint_path=checkpoint,
                     budget_ledger=budget_ledger if args.mode != 'A' else None)
    except AgnesRateLimitError as error:
        _write_interrupted_report(output, checkpoint, args, error)
        raise SystemExit(
            f'Agnes 429，已计数并保存报告；可重试时间：{error.retry_at.isoformat()}',
        ) from error
    except Exception as error:
        _write_interrupted_report(output, checkpoint, args, error)
        raise SystemExit(f'实验已停止，部分报告已保存到 {output}：{error}') from error
    _atomic_write(output, result)
    print(json.dumps({'output': str(output), 'mode': result['mode'],
                      'completed_stages': result['completed_stages'],
                      'cases': result['case_count'], 'calls': result['state']['call_count'],
                      'score': result['result']}, ensure_ascii=False, indent=2))


def _write_interrupted_report(output, checkpoint, args, error):
    progress = {}
    if checkpoint.exists():
        progress = json.loads(checkpoint.read_text(encoding='utf-8'))
    report = {
        'contract': 'alignment-experiment-interrupted-v3',
        'status': 'interrupted',
        'mode': args.mode,
        'stage': args.stage,
        'created_at': datetime.now(UTC).isoformat(),
        'error': {'type': type(error).__name__, 'message': str(error)},
        'fingerprints': progress.get('fingerprints', {}),
        'state': progress.get('state', {}),
        'completed_stages': progress.get('completed_stages', []),
        'completed_batches': progress.get('completed_batches', []),
        'calls': progress.get('calls', []),
    }
    _atomic_write(output, report)


def _configured_client():
    key = secret_value(AGNES_API_KEY)
    if not key:
        raise AgnesConfigurationError(f'请在本机 .env 中配置 {AGNES_API_KEY}')
    return AgnesClient(AgnesSettings(agnes_base_url(), key, agnes_default_model()))


if __name__ == '__main__':
    main()
