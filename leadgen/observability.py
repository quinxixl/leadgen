"""Error reporting: Telegram alerts to the owner and, optionally, Sentry / GlitchTip.

Telegram alerts (primary): ERROR_ALERT_CHAT_ID (comma list) receives a short scrubbed
message per new error through the existing bot token. Empty = off. Sending happens in a
daemon thread, never raises and is rate limited PER PROCESS: one message per error
fingerprint per 10 minutes (then the suppressed count is reported) and at most
ALERT_HOURLY_CAP messages per hour. Gunicorn runs several workers, so the same error
can arrive once from each worker.

Sentry / GlitchTip (same DSN protocol) is optional.

Everything here is a no-op while SENTRY_DSN is empty, so tests and local runs need
neither the service nor network access. Events are scrubbed before leaving the
process (152-ФЗ): no request bodies, cookies, auth headers or query strings, and
phones, emails, tokens, API keys and URL credentials are redacted from all text.
Only the numeric internal user id is attached as user context.

Admin check of a deployment: python -m leadgen.observability --test
"""
import logging
import os
import queue
import re
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

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

# ---------------------------------------------------------------- Telegram alerts

ALERT_WINDOW = 600          # seconds: same fingerprint at most once per window
ALERT_HOURLY_CAP = 20       # alerts per hour per process
ALERT_MAX_CHARS = 3500
ALERT_TB_LINES = 15
ALERT_TIMEOUT = 5
MSK = timezone(timedelta(hours=3))
_alerts = {'service': None, 'chat_ids': [], 'sync': False, 'pid': None, 'queue': None, 'thread': None,
           'last': {}, 'suppressed': {}, 'sent_times': deque(), 'handler': None, 'sent': 0, 'failed': 0}
_alert_lock = threading.Lock()
_alert_local = threading.local()


def _chat_ids():
    raw = os.environ.get('ERROR_ALERT_CHAT_ID', '')
    return [part.strip() for part in raw.split(',') if re.fullmatch(r'-?\d{1,20}', part.strip())]


def alerts_enabled():
    return bool(_alerts['chat_ids'])


class AlertLogHandler(logging.Handler):
    """Forwards ERROR records (e.g. Flask's unhandled exceptions) to Telegram alerts."""

    def __init__(self):
        super().__init__(level=logging.ERROR)

    def emit(self, record):
        try:
            if record.name.startswith('leadgen.observability'):
                return
            exc = record.exc_info[1] if record.exc_info and record.exc_info[1] is not None else None
            if exc is not None:
                alert_exception(exc, logger=record.name)
            else:
                alert_message(record.getMessage(), fingerprint='log:%s:%s' % (record.pathname, record.lineno),
                              logger=record.name, where='%s:%s %s' % (_short_path(record.pathname), record.lineno, record.funcName))
        except Exception:
            pass


def init_alerts(service, sync=False):
    """Enable Telegram alerts for this process; returns False (no-op) without ERROR_ALERT_CHAT_ID."""
    try:
        chat_ids = _chat_ids()
        if not chat_ids:
            return False
        with _alert_lock:
            _alerts.update(service=service, chat_ids=chat_ids, sync=sync)
            if _alerts['handler'] is None:
                _alerts['handler'] = AlertLogHandler()
                logging.getLogger().addHandler(_alerts['handler'])
        if os.environ.get('ALERT_ON_START', '0').strip() == '1':
            release = os.environ.get('SENTRY_RELEASE', '').strip() or 'версия не указана'
            _enqueue(scrub_text('✅ %s запущен (%s)\n%s · %s' % (service, release, socket.gethostname(), _now())))
        return True
    except Exception:
        return False


def _reset_alerts():
    """Tests only: turn alerts off and forget all state."""
    with _alert_lock:
        if _alerts['handler'] is not None:
            logging.getLogger().removeHandler(_alerts['handler'])
        _alerts.update(service=None, chat_ids=[], sync=False, pid=None, queue=None, thread=None, last={},
                       suppressed={}, sent_times=deque(), handler=None, sent=0, failed=0)


def _now():
    return datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S МСК')


def _short_path(path):
    path = str(path or '?').replace('\\', '/')
    for marker in ('/leadgen/', '/site-packages/'):
        if marker in path:
            return path.split(marker, 1)[1] if marker == '/site-packages/' else 'leadgen/' + path.split(marker, 1)[1]
    return path.rsplit('/', 1)[-1]


def _is_app_frame(filename):
    filename = str(filename).replace('\\', '/')
    return '/leadgen/' in filename and '/site-packages/' not in filename


def _frames(exc):
    try:
        return traceback.extract_tb(exc.__traceback__) if exc is not None and exc.__traceback__ else []
    except Exception:
        return []


def fingerprint(exc):
    frames = _frames(exc)
    app = [f for f in frames if _is_app_frame(f.filename)]
    top = (app or frames or [None])[-1]
    where = '%s:%s' % (_short_path(top.filename), top.lineno) if top else '?'
    return type(exc).__name__ + '@' + where


def _tag_lines(tags):
    lines = []
    for key, value in tags.items():
        if value is None or value == '':
            continue
        value = REDACTED if SENSITIVE_KEY.search(str(key)) else scrub_text(str(value))[:200]
        lines.append('%s: %s' % (key, value))
    return lines


def format_alert(service, title, message, tags=None, user_id=None, trace=None, repeats=0):
    lines = ['🔴 Ошибка в ' + str(service or 'signalid'), title]
    message = scrub_text(str(message or '')).strip()
    if message:
        lines.append(message[:500] + ('…' if len(message) > 500 else ''))
    lines += _tag_lines(tags or {})
    if user_id is not None and str(user_id).isdigit():
        lines.append('user_id: ' + str(user_id))
    if repeats:
        lines.append('повторилась %d раз за 10 мин' % repeats)
    if trace:
        lines.append('Traceback:')
        lines += [scrub_text(line) for line in trace]
    lines.append('%s · %s' % (scrub_text(socket.gethostname()), _now()))
    text = scrub_text('\n'.join(lines))
    return text if len(text) <= ALERT_MAX_CHARS else text[:ALERT_MAX_CHARS - 1] + '…'


def _trace_lines(exc):
    return ['%s:%s %s' % (_short_path(f.filename), f.lineno, f.name) for f in _frames(exc)[-ALERT_TB_LINES:]]


def _admit(fp, now=None):
    """Rate limit; returns (send?, suppressed count to report)."""
    now = time.monotonic() if now is None else now
    with _alert_lock:
        last, suppressed, sent = _alerts['last'], _alerts['suppressed'], _alerts['sent_times']
        while sent and now - sent[0] >= 3600:
            sent.popleft()
        if len(last) > 500:
            for key in [k for k, t in last.items() if now - t >= ALERT_WINDOW]:
                last.pop(key, None)
        if fp in last and now - last[fp] < ALERT_WINDOW:
            suppressed[fp] = suppressed.get(fp, 0) + 1
            return False, 0
        if len(sent) >= ALERT_HOURLY_CAP:
            suppressed[fp] = suppressed.get(fp, 0) + 1
            return False, 0
        last[fp] = now
        sent.append(now)
        return True, suppressed.pop(fp, 0)


def alert_exception(exc=None, user_id=None, service=None, **tags):
    """Queue a Telegram alert for an exception; returns True when queued. Never raises."""
    try:
        if not _alerts['chat_ids']:
            return False
        if exc is None:
            exc = sys.exc_info()[1]
        if exc is None:
            return False
        ok, repeats = _admit(fingerprint(exc))
        if not ok:
            return False
        text = format_alert(service or _alerts['service'], type(exc).__name__, str(exc), tags, user_id,
                            _trace_lines(exc), repeats)
        return _enqueue(text)
    except Exception:
        return False


def alert_message(message, fingerprint, where=None, service=None, **tags):
    """Queue an alert for an ERROR log record without an exception. Never raises."""
    try:
        if not _alerts['chat_ids']:
            return False
        ok, repeats = _admit(fingerprint)
        if not ok:
            return False
        trace = [where] if where else None
        return _enqueue(format_alert(service or _alerts['service'], 'ERROR в логе', message, tags, None, trace, repeats))
    except Exception:
        return False


def _post_telegram(chat_id, text):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is missing')
    import json
    cafile = os.getenv('SSL_CERT_FILE')
    if not cafile and sys.platform == 'darwin' and os.path.exists('/etc/ssl/cert.pem'):
        cafile = '/etc/ssl/cert.pem'
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage',
                                     data=json.dumps({'chat_id': chat_id, 'text': text,
                                                      'disable_web_page_preview': True}).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=ALERT_TIMEOUT, context=ssl.create_default_context(cafile=cafile)) as resp:
        resp.read(65536)


def _deliver(text):
    for chat_id in list(_alerts['chat_ids']):
        try:
            _post_telegram(chat_id, text)
            _alerts['sent'] += 1
        except Exception as exc:
            _alerts['failed'] += 1
            # Never log the exception text: the URL contains the bot token.
            try:
                print('Telegram-алерт не отправлен: ' + type(exc).__name__, file=sys.stderr, flush=True)
            except Exception:
                pass


def _worker(q):
    while True:
        text = q.get()
        try:
            _alert_local.busy = True
            _deliver(text)
        except Exception:
            pass
        finally:
            _alert_local.busy = False
            q.task_done()


def _enqueue(text):
    if getattr(_alert_local, 'busy', False):
        return False
    if _alerts['sync']:
        _alert_local.busy = True
        try:
            _deliver(text)
        finally:
            _alert_local.busy = False
        return True
    with _alert_lock:
        # Gunicorn forks workers: a thread started in the parent does not exist in the child.
        if _alerts['pid'] != os.getpid() or _alerts['thread'] is None or not _alerts['thread'].is_alive():
            _alerts['queue'] = queue.Queue(maxsize=50)
            _alerts['thread'] = threading.Thread(target=_worker, args=(_alerts['queue'],),
                                                 name='error-alerts', daemon=True)
            _alerts['thread'].start()
            _alerts['pid'] = os.getpid()
        q = _alerts['queue']
    try:
        q.put_nowait(text)
        return True
    except queue.Full:
        return False


def flush_alerts(timeout=10):
    q = _alerts['queue']
    deadline = time.monotonic() + timeout
    while q is not None and q.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------- Sentry


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
    """Report a caught exception (Telegram alert + Sentry) without changing the caller's control flow."""
    alert_exception(exc, user_id=user_id, **tags)
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
    if alerts_enabled() and not _alerts['sync']:
        flush_alerts(timeout)
    if _enabled:
        try:
            import sentry_sdk
            sentry_sdk.flush(timeout=timeout)
        except Exception:
            pass


def send_test_event():
    """Send one scrubbed test exception to every configured channel; True when at least one worked."""
    from .app import env_load
    env_load()
    channels = []
    alerts_on = init_alerts('cli-test')
    sentry_on = init_sentry('cli-test')
    if not alerts_on and not sentry_on:
        print('Ни ERROR_ALERT_CHAT_ID, ни SENTRY_DSN не заданы — ничего не отправлено.')
        return False
    try:
        raise RuntimeError('Signalid: тестовая ошибка мониторинга (owner@example.com, +7 999 123-45-67)')
    except RuntimeError as exc:
        event_id = capture_exception(exc, component='observability-test')
    if alerts_on:
        flush_alerts(15)
        if _alerts['sent']:
            channels.append('Telegram (%d чат.)' % _alerts['sent'])
        else:
            print('Telegram-алерт не доставлен: проверьте TELEGRAM_BOT_TOKEN, ERROR_ALERT_CHAT_ID и что аккаунт написал боту /start.')
    else:
        print('ERROR_ALERT_CHAT_ID не задан — Telegram-алерт не отправлялся.')
    if sentry_on:
        flush(10)
        if event_id:
            channels.append('Sentry/GlitchTip (событие ' + str(event_id) + ')')
    else:
        print('SENTRY_DSN не задан — Sentry не использовался.')
    print('Тестовая ошибка отправлена: ' + (', '.join(channels) if channels else 'ни в один канал'))
    return bool(channels)


if __name__ == '__main__':
    if sys.argv[1:] != ['--test']:
        print('Использование: python -m leadgen.observability --test')
        raise SystemExit(2)
    raise SystemExit(0 if send_test_event() else 1)
