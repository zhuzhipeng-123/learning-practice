"""Explicit small online semantic benchmark; never opens the live learning store for writes."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from app.domain import QuestionDraft, Submission
from app.services.learning_clock import local_today
from app.services.llm_config import get_module_config
from app.services.model_jobs import ModelJobError, run_evaluation_job
from app.services.practice import start_attempt, submit_theory
from app.services.sync import publish_snapshot
from app.services.tasks import add_tasks, create_daily_plan
from app.storage.database import connect_database, initialize_database


def run_benchmark(directory):
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / 'learning.db').exists():
        raise ValueError('Use a fresh benchmark directory; existing data is never overwritten.')
    connection = connect_database(directory / 'learning.db')
    initialize_database(connection)
    cases = json.loads((Path(__file__).resolve().parents[1] / 'tests/fixtures/evaluation_cases.json').read_text(encoding='utf-8'))
    connection.execute("INSERT INTO source(id,document_id,wiki_url,question_type) VALUES ('benchmark','synthetic','https://example.test','theory')")
    connection.commit()
    drafts = [QuestionDraft('benchmark', 'synthetic', 'theory', 'feishu', case['id'], (case['id'],), (case['id']+'-ref',),
                            'Semantic benchmark', case['question'], case['reference'], 'complete') for case in cases]
    publish_snapshot(connection, 'benchmark', '1', '1', [{'block_id': case['id']} for case in cases], drafts, 'benchmark')
    plan = create_daily_plan(connection, local_today(), 0, 0, {}, 'benchmark-plan')
    report = {'executed_at': datetime.now(UTC).isoformat(), 'model_config': get_module_config(connection, 'theory_evaluation'), 'cases': []}
    try:
        for case in cases:
            question = connection.execute('SELECT question_id FROM source_binding WHERE main_anchor_block_id=?', (case['id'],)).fetchone()[0]
            task = add_tasks(connection, plan['plan_id'], [question], 'benchmark', case['id'])['created_task_ids'][0]
            now = datetime.now(UTC)
            start_attempt(connection, task, 'benchmark', now)
            answer = submit_theory(connection, Submission(task, case['id']+'-submit', now, 'benchmark', answer_text=case['answer']))
            try:
                evaluation = run_evaluation_job(connection, answer['job_id'])
                row = connection.execute('SELECT verdict,raw_json,model_id FROM evaluation WHERE id=?', (evaluation,)).fetchone()
                outcome = {'id': case['id'], 'expected': case['expected'], 'actual': row['verdict'],
                           'passed': row['verdict'] == case['expected'], 'response': json.loads(row['raw_json']), 'model': row['model_id']}
            except ModelJobError as error:
                outcome = {'id': case['id'], 'expected': case['expected'], 'passed': False, 'error': str(error)}
                report['cases'].append(outcome)
                if error.retry_at or not connection.execute('SELECT 1 FROM model_request WHERE job_id=? AND response_text IS NOT NULL', (answer['job_id'],)).fetchone():
                    report['stopped'] = 'External service failed; remaining cases were not executed.'
                    break
                continue
            report['cases'].append(outcome)
    finally:
        connection.close()
        (directory / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-online', action='store_true', help='Explicitly allow paid/remote model calls')
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    if not args.run_online:
        parser.error('--run-online is required; normal pytest never uses credentials')
    report = run_benchmark(args.output_dir)
    print(json.dumps({'executed': len(report['cases']), 'passed': sum(case['passed'] for case in report['cases']),
                      'stopped': report.get('stopped'), 'results': str(args.output_dir / 'results.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
