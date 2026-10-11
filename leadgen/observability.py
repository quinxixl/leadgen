"""Error tracking for Sentry SaaS or self-hosted GlitchTip (same DSN protocol).

Everything here is a no-op while SENTRY_DSN is empty, so tests and local runs need
neither the service nor network access. Events are scrubbed before leaving the
process (152-ФЗ): no request bodies, cookies, auth headers or query strings, and
phones, emails, tokens, API keys and URL credentials are redacted from all text.
Only the numeric internal user id is attached as user context.

Admin check of a deployment: python -m leadgen.observability --test
"""
import os
import re
import sys

_enabled = False

REDACTED = '[redacted]'
# Headers that cannot carry secrets or personal data. Everything else is dropped.
SAFE_HEADERS = {'host', 'user-agent', 'content-type', 'content-length', 'accept', 'accept-language'}
# Dict keys whose values are never sent, whatever they contain.
SENSITIVE_KEY = re.compile(r'(?i)(pass(word|wd)?|secret|token|api[_-]?key|api[_-]?hash|authorization|cookie|'
                           r'session|csrf|dsn|database_url|cipher|phone|e-?mail|username|display_name|'
                           r'first_name|last_name|full_name|init_?data|signature)')
PATTERNS = [
    # Credentials inside any URL, e.g. postgresql://user:password@host/db.
    (re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]*@'), r'\1' + REDACTED + '@'),
    # Telegram bot tokens, also as part of api.telegram.org/bot<token>/method.
    (re.compile(r'(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}'), REDACTED),
    # Groq / OpenAI-style keys, bearer tokens, Sentry DSN keys in text.
    (re.compile(r'\bgsk_[A-Za-z0-9]{8,}'), REDACTED),
    (re.compile(r'\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}'), REDACTED),
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}'), 'Bearer ' + REDACTED),
    (re.compile(r'(?i)(token|key|secret|password|api_hash)(["\']?\s*[=:]\s*["\']?)[^\s"\'&,;]{6,}'), r'\1\2' + REDACTED),
    (re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'), '[email]'),
    # International numbers with a leading plus, then Russian numbers written with 7/8 or as 9XX XXX-XX-XX.
    (re.compile(r'(?<![\w+])\+\d[\d\s().-]{8,18}\d(?!\w)'), '[phone]'),
    (re.compile(r'(?<![\w+])[78][\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\w)'), '[phone]'),
    (re.compile(r'(?<![\w+])\(?9\d{2}\)?[\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\w)'), '[phone]'),
]


def scrub_text(value):
    for pattern, replacement in PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def scrub(value, depth=0):
    """Recursively redact strings; values under sensitive keys are dropped entirely."""
    if depth > 20:
        return REDACTED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and SENSITIVE_KEY.search(k) else scrub(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth + 1) for v in value]
    return value


def _strip_query(url):
    return url.split('?', 1)[0].split('#', 1)[0] if isinstance(url, str) else url


def _clean_user(user):
    uid = (user or {}).get('id')
    return {'id': str(uid)} if uid is not None and str(uid).isdigit() else None


def before_send(event, hint=None):
    request = event.get('request')
    if isinstance(request, dict):
        headers = request.get('headers') or {}
        event['request'] = {
            'method': request.get('method'),
            'url': _strip_query(request.get('url')),
            'headers': {k: _strip_query(v) for k, v in headers.items() if str(k).lower() in SAFE_HEADERS},
        }
    user = _clean_user(event.get('user'))
    if user:
        event['user'] = user
    else:
        event.pop('user', None)
    for exc in (event.get('exception') or {}).get('values') or []:
        for frame in (exc.get('stacktrace') or {}).get('frames') or []:
            frame.pop('vars', None)
    for frame in (event.get('stacktrace') or {}).get('frames') or []:
        frame.pop('vars', None)
    for key in ('message', 'logentry', 'exception', 'extra', 'breadcrumbs', 'contexts', 'tags', 'request', 'threads'):
        if key in event:
            event[key] = scrub(event[key])
    return event


def before_breadcrumb(crumb, hint=None):
    data = crumb.get('data')
    if isinstance(data, dict):
        for key in ('url', 'http.query', 'http.fragment'):
            if key in data:
                data[key] = _strip_query(data[key]) if key == 'url' else ''
    return scrub(crumb)


def _rate(name):
    try:
        return min(1.0, max(0.0, float(os.environ.get(name, '0') or 0)))
    except ValueError:
        return 0.0


def enabled():
    return _enabled


def init_sentry(service):
    """Initialise once per process; returns False (and does nothing) without SENTRY_DSN."""
    global _enabled
    dsn = os.environ.get('SENTRY_DSN', '').strip()
    if not dsn:
        return False
    if _enabled:
        return True
    try:
        import logging
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get('SENTRY_ENVIRONMENT', '').strip() or 'production',
            # Without SENTRY_RELEASE the SDK falls back to `git rev-parse HEAD` when available.
            release=os.environ.get('SENTRY_RELEASE', '').strip() or None,
            traces_sample_rate=_rate('SENTRY_TRACES_SAMPLE_RATE'),
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size='never',
            before_send=before_send,
            before_breadcrumb=before_breadcrumb,
            integrations=[FlaskIntegration(), LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        )
        sentry_sdk.get_global_scope().set_tag('service', service)
    except Exception as exc:
        # Monitoring must never stop the product from starting.
        print('Sentry не подключён: ' + type(exc).__name__, flush=True)
        return False
    _enabled = True
    return True


def set_user(user_id):
    """Attach only the internal numeric user id to the current request scope."""
    if not _enabled or user_id is None or not str(user_id).isdigit():
        return
    try:
        import sentry_sdk
        sentry_sdk.set_user({'id': str(user_id)})
    except Exception:
        pass


def capture_exception(exc=None, user_id=None, **tags):
    """Report a caught exception without changing the caller's control flow."""
    if not _enabled:
        return None
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            if user_id is not None and str(user_id).isdigit():
                scope.set_user({'id': str(user_id)})
            for key, value in tags.items():
                scope.set_tag(key, value)
            return sentry_sdk.capture_exception(exc)
    except Exception:
        return None


def flush(timeout=5):
    if _enabled:
        try:
            import sentry_sdk
            sentry_sdk.flush(timeout=timeout)
        except Exception:
            pass


def send_test_event():
    """Send one scrubbed test exception; returns the event id or None."""
    from .app import env_load
    env_load()
    if not init_sentry('cli-test'):
        print('SENTRY_DSN не задан или Sentry не подключился — событие не отправлено.')
        return None
    try:
        raise RuntimeError('Signalid: тестовое событие Sentry/GlitchTip (owner@example.com, +7 999 123-45-67)')
    except RuntimeError as exc:
        event_id = capture_exception(exc, component='observability-test')
    flush(10)
    print('Тестовое событие отправлено: ' + str(event_id))
    return event_id


if __name__ == '__main__':
    if sys.argv[1:] != ['--test']:
        print('Использование: python -m leadgen.observability --test')
        raise SystemExit(2)
    raise SystemExit(0 if send_test_event() else 1)
