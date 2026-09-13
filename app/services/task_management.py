from datetime import UTC, datetime

from app.storage.transactions import atomic


@atomic
def cancel_unstarted_task(connection, task_id):
    task = connection.execute('SELECT status FROM task WHERE id=?', (task_id,)).fetchone()
    if not task:
        raise ValueError('任务不存在')
    if task['status'] == 'cancelled':
        return {'status': 'cancelled'}
    if task['status'] != 'pending' or connection.execute(
        'SELECT 1 FROM attempt WHERE task_id=? UNION ALL SELECT 1 FROM interview_session WHERE task_id=?',
        (task_id, task_id),
    ).fetchone():
        raise ValueError('这题已经开始或有面试记录，请继续完成，不能取消')
    connection.execute("UPDATE task SET status='cancelled',cancelled_at=? WHERE id=?", (datetime.now(UTC).isoformat(), task_id))
    return {'status': 'cancelled'}
