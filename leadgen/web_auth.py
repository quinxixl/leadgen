"""Browser-bound, single-use login approvals through the existing Telegram bot."""
import hashlib
import hmac
import json
import re
import secrets
import time

TTL = 300


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def challenge_key(token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise ValueError('Некорректный запрос входа')
    return 'web_login:' + digest(token)


def begin_login(db):
    token, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    record = {'browser': digest(browser), 'expires': time.time() + TTL,
              'code': str(secrets.randbelow(900000) + 100000), 'user_id': None}
    with db:
        db.execute('INSERT INTO settings(key,value) VALUES(?,?)',
                   (challenge_key(token), json.dumps(record)))
    return token, browser, record['code']


def login_record(db, token, lock=False):
    suffix = ' FOR UPDATE' if lock and db.backend == 'postgres' else ''
    row = db.execute('SELECT value FROM settings WHERE key=?' + suffix,
                     (challenge_key(token),)).fetchone()
    if not row:
        raise ValueError('Запрос входа уже использован или не существует')
    record = json.loads(row['value'])
    if record['expires'] <= time.time():
        raise ValueError('Время входа истекло. Откройте сайт и повторите вход')
    return record


def approve_login(db, token, user_id):
    with db:
        record = login_record(db, token, lock=True)
        if record['user_id'] is not None:
            raise ValueError('Вход уже подтверждён')
        record['user_id'] = user_id
        db.execute('UPDATE settings SET value=? WHERE key=?',
                   (json.dumps(record), challenge_key(token)))


def consume_login(db, token, browser):
    with db:
        record = login_record(db, token, lock=True)
        if not hmac.compare_digest(record['browser'], digest(browser)):
            raise ValueError('Вход можно завершить только в исходном браузере')
        if record['user_id'] is None:
            return None
        db.execute('DELETE FROM settings WHERE key=?', (challenge_key(token),))
        return record['user_id']
