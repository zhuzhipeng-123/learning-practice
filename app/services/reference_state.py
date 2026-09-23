"""Reference trust belongs to a frozen version, never to a generation job."""


def verification(connection, version_id):
    row = connection.execute('SELECT q.source_kind,r.verified,r.evidence,r.created_at FROM question_version v '
        'JOIN question q ON q.id=v.question_id LEFT JOIN reference_verification r ON r.version_id=v.id '
        'WHERE v.id=?', (version_id,)).fetchone()
    if row is None:
        raise ValueError('题目版本不存在')
    verified = bool(row['verified']) if row['verified'] is not None else row['source_kind'] == 'feishu'
    kind = 'source' if row['source_kind'] == 'feishu' else 'human' if verified else 'unverified'
    return {'verified': verified, 'kind': kind, 'human_verified': kind == 'human',
            'evidence': row['evidence'], 'verified_at': row['created_at']}


def save_verification(connection, version_id, verified, created_at):
    connection.execute('INSERT INTO reference_verification VALUES (?,?,?,?)',
        (version_id, int(verified), '用户核对本版本题干与参考答案' if verified else '尚未独立核对', created_at))
