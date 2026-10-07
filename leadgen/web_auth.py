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


def login_status(db, token, browser):
    """'pending', 'approved' or 'expired' for the browser's own login request; never consumes it."""
    try:
        record = login_record(db, token)
    except ValueError:
        return 'expired'
    if not hmac.compare_digest(record['browser'], digest(browser)):
        return 'expired'
    return 'pending' if record['user_id'] is None else 'approved'


HANDOFF_TTL = 60


def handoff_key(token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token or ''):
        raise ValueError('Ссылка входа недействительна')
    return 'web_handoff:' + digest(token)


def begin_handoff(db, user_id, session_version=0):
    """Single-use, 60-second token that signs the Mini App user into the website."""
    token = secrets.token_urlsafe(32)
    record = {'user_id': user_id, 'sv': session_version, 'expires': time.time() + HANDOFF_TTL}
    with db:
        db.execute('INSERT INTO settings(key,value) VALUES(?,?)', (handoff_key(token), json.dumps(record)))
    return token


def consume_handoff(db, token):
    """Return the user id once; a second use, an expired token or a revoked session fails."""
    key = handoff_key(token)
    with db:
        row = db.execute('DELETE FROM settings WHERE key=? RETURNING value', (key,)).fetchone()
        if db.backend == 'postgres':
            db.execute("""DELETE FROM settings WHERE key LIKE 'web_handoff:%%'
                AND (value::jsonb->>'expires')::float8 < ?""", (time.time(),))
    if not row:
        raise ValueError('Ссылка входа уже использована или устарела')
    record = json.loads(row['value'])
    if record['expires'] <= time.time():
        raise ValueError('Ссылка входа устарела. Откройте сайт из Telegram ещё раз')
    return record['user_id'], record.get('sv', 0)
