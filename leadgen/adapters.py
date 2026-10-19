import io
import json
import os
import re
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from .core import Lead, UTC

MSK = timezone(timedelta(hours=3))
MONTHS = 'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()
MAINTENANCE = re.compile(r'технические работы|technical works|under maintenance', re.I)
BLOCKED = re.compile(r'captcha|капч|доступ (?:запрещ|ограничен)|access denied|подтвердите, что вы не робот', re.I)


class SourceError(ValueError):
    """A source answered, but not with listings; the message is safe to show in health."""


def error_label(exc):
    """Health text: our own source diagnostics keep their message, other errors only their type."""
    return f'{type(exc).__name__}: {exc}'[:200] if isinstance(exc, SourceError) else type(exc).__name__


def page_problem(soup):
    """Why a page without listings has none: maintenance stub or anti-bot block; None if unknown."""
    text = soup.get_text(' ', strip=True)[:5000]
    if MAINTENANCE.search(text):
        return 'на сайте технические работы, лента временно недоступна'
    if BLOCKED.search(text) or soup.select_one('form[action*="captcha"], [class*="captcha"], [id*="captcha"]'):
        return 'доступ заблокирован проверкой на робота (капча)'
    return None

FETCH_TIMEOUT = 30          # seconds for connecting and for each read, as with urllib before
MAX_BODY = 4_000_000
POOL_HOSTS = 16             # per-host pools kept: Bot API, the scraped sites, hh.ru
POOL_CONNECTIONS = 4        # idle keep-alive connections kept per host; extra concurrent ones are opened and dropped
_http_lock = threading.Lock()
_http_state = None          # (pid, cafile, PoolManager)


def _cafile():
    cafile = os.getenv('SSL_CERT_FILE')
    if not cafile and sys.platform == 'darwin' and os.path.exists('/etc/ssl/cert.pem'):
        cafile = '/etc/ssl/cert.pem'
    return cafile


def http_pool():
    """The shared keep-alive client: one urllib3 PoolManager with one SSL context per process.

    Bot API calls (getUpdates, sendMessage...) and scraper requests reuse its connections instead of a new
    TCP+TLS handshake per call (urllib.request always sent "Connection: close"). Rebuilt after a fork
    (gunicorn workers) or when SSL_CERT_FILE changes; urllib3 pools are thread-safe."""
    global _http_state
    pid, cafile = os.getpid(), _cafile()
    state = _http_state
    if state and state[0] == pid and state[1] == cafile:
        return state[2]
    with _http_lock:
        state = _http_state
        if state and state[0] == pid and state[1] == cafile:
            return state[2]
        import urllib3
        pool = urllib3.PoolManager(num_pools=POOL_HOSTS, maxsize=POOL_CONNECTIONS,
                                   ssl_context=ssl.create_default_context(cafile=cafile))
        _http_state = (pid, cafile, pool)
        return pool


def _no_retries():
    """As with urllib: a request is never repeated (a resent sendMessage would duplicate a card); redirects are followed."""
    import urllib3
    return urllib3.Retry(total=None, connect=0, read=0, other=0, status=0, redirect=10, raise_on_redirect=True)


def _first(items, kind):
    return next((item for item in items if isinstance(item, kind)), None)


def _urllib_error(exc):
    """The exception urllib.request raised for the same failure: callers keep their except clauses and messages."""
    from urllib3 import exceptions as u3
    if isinstance(exc, u3.MaxRetryError):
        if exc.reason is None or isinstance(exc.reason, u3.ResponseError):
            return urllib.error.URLError('too many redirects')
        return _urllib_error(exc.reason)
    if isinstance(exc, u3.ReadTimeoutError):
        return TimeoutError('The read operation timed out')
    related = (*exc.args, exc.__cause__, exc.__context__)
    # Before ConnectTimeoutError: urllib3 derives NewConnectionError (and NameResolutionError) from it.
    if isinstance(exc, u3.NameResolutionError):
        return urllib.error.URLError(_first(related, socket.gaierror) or socket.gaierror(type(exc).__name__))
    if isinstance(exc, u3.NewConnectionError):
        return urllib.error.URLError(_first(related, OSError) or ConnectionError(type(exc).__name__))
    if isinstance(exc, u3.ConnectTimeoutError):
        return urllib.error.URLError(TimeoutError('timed out'))
    if isinstance(exc, u3.SSLError):
        return urllib.error.URLError(_first(related, ssl.SSLError) or ssl.SSLError(type(exc).__name__))
    if isinstance(exc, u3.ProtocolError):
        return _first(related, OSError) or ConnectionError(type(exc).__name__)
    return urllib.error.URLError(exc)


def fetch(url, data=None, headers=None, timeout=FETCH_TIMEOUT):
    """GET (POST when data is given) over the shared keep-alive pool; returns at most MAX_BODY bytes.

    Errors are urllib's: HTTPError for non-2xx statuses, URLError, TimeoutError or ConnectionError otherwise."""
    from urllib3 import exceptions as u3
    try:
        response = http_pool().request('POST' if data is not None else 'GET', url, body=data,
                                       headers=headers or {'User-Agent': 'ITLeadFinder/0.1'},
                                       timeout=timeout, retries=_no_retries(), preload_content=False)
    except u3.HTTPError as exc:
        raise _urllib_error(exc) from None
    complete = False
    try:
        try:
            body = response.read(MAX_BODY + 1)
        except u3.HTTPError as exc:
            raise _urllib_error(exc) from None
        if len(body) > MAX_BODY:
            raise ValueError('Response too large')
        complete = True
    finally:
        if not complete:
            response.close()  # bytes left unread: the connection must not go back to the pool
        response.release_conn()
    if not 200 <= response.status < 300:
        raise urllib.error.HTTPError(url, response.status, response.reason or '', response.headers, io.BytesIO(body))
    return body

def txt(node, selector=None):
    element = node.select_one(selector) if selector else node
    return element.get_text(' ', strip=True) if element else ''

def russian_date(text, now):
    m = re.search(r'(\d{1,2})\s+(' + '|'.join(MONTHS) + r')(?:\s+(20\d{2}))?', text)
    if m:
        year = int(m[3] or now.year)
        return datetime(year, MONTHS.index(m[2])+1, int(m[1]), tzinfo=MSK).isoformat()
    if 'назад' in text:
        units = [(r'(\d+)\s*(?:секунд)',1), (r'(\d+)\s*(?:минут)',60), (r'(\d+)\s*(?:час)',3600), (r'(\d+)\s*(?:дн|день)',86400)]
        seconds = sum(int(m[1])*factor for pattern,factor in units if (m := re.search(pattern,text)))
        if seconds:
            return (now-timedelta(seconds=seconds)).isoformat()
    return None

def parse(source, body, now=None):
    now = now or datetime.now(UTC)
    soup = BeautifulSoup(body, 'html.parser')
    kind, name, base = source['kind'], source['name'], source['url']
    result = []
    if kind == 'telegram':
        cards = soup.select('.tgme_widget_message[data-post]')
        for card in cards:
            node = card.select_one('.tgme_widget_message_text')
            if not node:
                continue
            text = node.get_text('\n', strip=True)
            links = [a.get('href','') for a in node.select('a[href]')]
            date = card.select_one('time[datetime]')
            # Multiple distinct orders in one digest cannot share a budget safely.
            is_digest = len(re.findall(r'УЗНАТЬ БОЛЬШЕ|➡️',text)) > 1
            title = next((line for line in text.split('\n') if re.search(r'[a-zA-Zа-яА-Я]{3}',line)),text)[:220]
            result.append(Lead(name, 'https://t.me/'+card['data-post'], title,
                               text + ('\nСсылки: ' + ' '.join(dict.fromkeys(links)) if links else ''),
                               date['datetime'] if date else None, '' if is_digest else text))
    elif kind == 'fl':
        cards = soup.select('[id^="project-item"]')
        for card in cards:
            a = card.select_one('h2 a[href]')
            if not a:
                continue
            foot = txt(card,'.b-post__foot')
            result.append(Lead(name, urllib.parse.urljoin(base,a['href']),txt(a),txt(card,'.b-post__txt'),
                               russian_date(foot,now),txt(card,'.b-post__price'),True,
                               'vacancy' if 'Вакансия' in foot else 'order'))
    elif kind == 'freelance':
        cards = soup.select('article.task-card')
        for card in cards:
            a = card.select_one('.task-card__title-link')
            if not a:
                continue
            date = card.select_one('.task-card__foot-item[title]')
            try:
                published = datetime.strptime(date['title'],'%d.%m.%Y %H:%M').replace(tzinfo=MSK).isoformat()
            except (ValueError,TypeError):
                published = None
            result.append(Lead(name,urllib.parse.urljoin(base,a['href']),txt(a),txt(card,'.task-card__desc'),
                               published,txt(card,'.task-card__budget'),True))
    elif kind == 'workspace':
        cards = soup.select('[data-tender-card]')
        for card in cards:
            if not card.select_one('[data-change-status-text="open"]'):
                continue
            a = card.select_one('.b-tender__title a')
            dates = card.select('.b-tender__info .b-tender__info-item-text')
            if not a:
                continue
            result.append(Lead(name,urllib.parse.urljoin(base,a['href']),txt(a),txt(a),
                               russian_date(txt(dates[0]),now) if dates else None,
                               txt(card,'.b-tender__block--title .b-tender__info-item-text')+' ₽',True))
    else:
        raise ValueError('Unsupported adapter: '+kind)
    if not cards:
        problem = page_problem(soup)
        raise SourceError(f'{name}: ' + (problem or 'нет карточек объявлений: изменилась вёрстка или доступ закрыт'))
    return result

def collect(source):
    if source['kind'] != 'hh':
        try:
            body = fetch(source['url'])
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429, 451, 503):
                raise SourceError(f"{source['name']}: HTTP {exc.code}, сайт не отдаёт ленту (блокировка или ограничение частоты)") from None
            raise
        return parse(source,body)
    ua = os.getenv('HH_USER_AGENT','')
    if not ua:
        raise ValueError('Set HH_USER_AGENT to application name and contact email')
    result = []
    for query in ['разработчик сайтов','Telegram бот','мобильное приложение','автоматизация','CRM']:
        args = urllib.parse.urlencode({'text':query,'area':'113','employment':'project',
                                       'period':3,'per_page':100,'order_by':'publication_time'})
        payload = json.loads(fetch(source['url']+'?'+args,headers={'User-Agent':ua}))
        for item in payload['items']:
            text = BeautifulSoup(' '.join(v or '' for v in item.get('snippet',{}).values()),'html.parser').get_text(' ')
            result.append(Lead(source['name'],item['alternate_url'],item['name'],text,item['published_at'],
                               json.dumps(item.get('salary'),ensure_ascii=False),True,'vacancy'))
    return result
