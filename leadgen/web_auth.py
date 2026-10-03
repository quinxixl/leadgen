"""Browser-bound, single-use login approvals through the existing Telegram bot.

The bot never reveals the code shown on the site. The user must pick it among
decoys, so a forwarded login link alone cannot be approved by a victim."""
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


def new_code():
    return str(secrets.randbelow(900000) + 100000)


def begin_login(db, requester=''):
    token, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    record = {'browser': digest(browser), 'expires': time.time() + TTL,
              'code': new_code(), 'user_id': None, 'requester': requester[:120]}
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


def code_choices(record):
    """The real code among three decoys, in a stable per-request order."""
    rng = secrets.SystemRandom()
    choices = {record['code']}
    while len(choices) < 4:
        choices.add(new_code())
    choices = list(choices)
    rng.shuffle(choices)
    return choices


def approve_login(db, token, user_id, code):
    with db:
        record = login_record(db, token, lock=True)
        if record['user_id'] is not None:
            raise ValueError('Вход уже подтверждён')
        matched = hmac.compare_digest(str(code), record['code'])
        if matched:
            record['user_id'] = user_id
            db.execute('UPDATE settings SET value=? WHERE key=?',
                       (json.dumps(record), challenge_key(token)))
        else:
            # A wrong guess burns the request: the attacker cannot retry on the victim.
            db.execute('DELETE FROM settings WHERE key=?', (challenge_key(token),))
    if not matched:
        raise ValueError('Код не совпал. Запрос входа отменён — получите новый код на сайте')


def consume_login(db, token, browser):
    with db:
        record = login_record(db, token, lock=True)
        if not hmac.compare_digest(record['browser'], digest(browser)):
            raise ValueError('Вход можно завершить только в исходном браузере')
        if record['user_id'] is None:
            return None
        db.execute('DELETE FROM settings WHERE key=?', (challenge_key(token),))
        return record['user_id']
