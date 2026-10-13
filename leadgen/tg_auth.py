"""Telegram Mini App initData validation (core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_SKEW = 60


def validate_init_data(raw, bot_token, max_age=86400, now=None):
    """Return dict(user, start_param, auth_date) for a genuine, fresh initData string.

    Raises ValueError on any failure; the message never echoes the input."""
    if not raw or not isinstance(raw, str) or not bot_token:
        raise ValueError('initData отсутствует')
    if len(raw) > 8192:
        raise ValueError('initData слишком длинный')
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ValueError('initData повреждён') from None
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError('initData повреждён')
        fields[key] = value
    received = fields.pop('hash', '')
    if not received:
        raise ValueError('Нет подписи initData')
    check = '\n'.join(f'{key}={fields[key]}' for key in sorted(fields))
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), received.lower().encode()):
        raise ValueError('Подпись initData не совпала')
    try:
        auth_date = int(fields.get('auth_date', ''))
    except ValueError:
        raise ValueError('Нет даты initData') from None
    current = time.time() if now is None else now
    if auth_date > current + MAX_SKEW:
        raise ValueError('Дата initData в будущем')
    if current - auth_date > max_age:
        raise ValueError('initData устарел')
    try:
        user = json.loads(fields.get('user', ''))
    except ValueError:
        raise ValueError('Нет пользователя в initData') from None
    if not isinstance(user, dict) or not isinstance(user.get('id'), int) or isinstance(user.get('id'), bool) or user['id'] <= 0:
        raise ValueError('Нет пользователя в initData')
    return {'user': user, 'start_param': fields.get('start_param', ''), 'auth_date': auth_date}


def sign_init_data(fields, bot_token):
    """Build a signed initData query string; used by tests and local tooling only."""
    from urllib.parse import urlencode
    fields = {key: value for key, value in fields.items() if key != 'hash'}
    check = '\n'.join(f'{key}={fields[key]}' for key in sorted(fields))
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    return urlencode(fields | {'hash': hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})
