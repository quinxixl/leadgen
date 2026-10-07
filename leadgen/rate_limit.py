"""Fixed-window limits stored in the shared settings table, so all web workers see them."""
import hashlib
import json
import time


class RateLimited(ValueError):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after))
        super().__init__(f'Слишком много попыток. Повторите через {self.retry_after} с.')


def hit(db, scope, subject, limit, window):
    """Count one attempt for subject in scope; raise RateLimited when over limit."""
    now = time.time()
    key = 'rl:' + scope + ':' + hashlib.sha256(str(subject).encode()).hexdigest()[:32]
    suffix = ' FOR UPDATE' if db.backend == 'postgres' else ''
    with db:
        row = db.execute('SELECT value FROM settings WHERE key=?' + suffix, (key,)).fetchone()
        record = json.loads(row['value']) if row else None
        if not record or record['reset'] <= now:
            record = {'count': 0, 'reset': now + window}
        if record['count'] >= limit:
            raise RateLimited(record['reset'] - now)
        record['count'] += 1
        db.execute('''INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (key, json.dumps(record)))


def purge_expired(db):
    """Drop finished windows and expired web login challenges."""
    now = time.time()
    if db.backend == 'postgres':
        with db:
            db.execute('''DELETE FROM settings WHERE (key LIKE 'rl:%%' AND (value::jsonb->>'reset')::float8 < ?)
                OR (key LIKE 'web_login:%%' AND (value::jsonb->>'expires')::float8 < ?)''', (now, now))
        return
    rows = db.execute("SELECT key,value FROM settings WHERE key LIKE 'rl:%' OR key LIKE 'web_login:%'").fetchall()
    stale = []
    for row in rows:
        value = json.loads(row['value'])
        if value.get('reset', value.get('expires', now + 1)) < now:
            stale.append((row['key'],))
    with db:
        for key in stale:
            db.execute('DELETE FROM settings WHERE key=?', key)
