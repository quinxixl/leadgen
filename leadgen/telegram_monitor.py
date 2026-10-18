"""Read new messages from public Telegram groups and channels the shared service account follows.

Only new-message events are processed: no history is pulled. Private chats (no public @username)
are ignored because their content must not be redistributed to every customer."""
import asyncio
import getpass
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .core import Lead, UTC
from .db_store import MonitorStore
from .source_registry import JOINED, MODERATION, MODERATION_REASON, joined_metadata

ROOT = Path(__file__).resolve().parent
SESSION = ROOT/'data'/'telegram_reader'  # Telethon stores it as telegram_reader.session
SESSION_FILE = SESSION.with_name(SESSION.name+'.session')

HEARTBEAT_KEY = 'reader:heartbeat'
HEARTBEAT_SECONDS = 60
HEARTBEAT_STALE_SECONDS = 180
DIALOG_SYNC_SECONDS = 3600
REALTIME_REASON = 'получено в реальном времени'
UNAUTHORIZED_MESSAGE = 'Сессия служебного аккаунта не авторизована. Выполните setup-telegram'
UNAUTHORIZED_RETRY_SECONDS = 600
MIN_RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 300
HEALTHY_RUN_SECONDS = 600
QR_ROUNDS = 10
_SECRETS = (re.compile(r'\b[a-z][a-z0-9+.-]*://\S+', re.I),      # DSNs and URLs with credentials
            re.compile(r'\b\d{5,}:[A-Za-z0-9_-]{20,}'),            # bot tokens
            re.compile(r'\b[0-9a-f]{32}\b', re.I),                 # api_hash
            re.compile(r'\+?\d{10,15}\b'))                         # phone numbers


class ReaderUnauthorized(Exception):
    """The file session is missing or revoked; only setup-telegram can fix it."""


def _log(text):
    print(text, flush=True)


def safe_error(exc):
    """Exception type and message for logs, with anything that may be a secret masked."""
    text = str(exc).replace('\n', ' ').strip()
    for pattern in _SECRETS:
        text = pattern.sub('***', text)
    return type(exc).__name__ + (': ' + text[:300] if text else '')


def _telethon():
    try:
        from telethon import TelegramClient
        return TelegramClient
    except ImportError:
        raise ValueError('Установите зависимости: pip install -r requirements.txt') from None


def reader_credentials():
    """API credentials of the shared reader; the phone is needed only for the code login."""
    try:
        api_id = int(os.environ.get('TELEGRAM_API_ID', ''))
    except ValueError:
        api_id = 0
    api_hash = os.environ.get('TELEGRAM_API_HASH', '').strip()
    if not api_id or not api_hash:
        raise ValueError('Добавьте TELEGRAM_API_ID и TELEGRAM_API_HASH в .env')
    return api_id, api_hash


def credentials():
    api_id, api_hash = reader_credentials()
    phone = os.environ.get('TELEGRAM_PHONE', '').strip()
    if not phone:
        raise ValueError('Добавьте TELEGRAM_API_ID, TELEGRAM_API_HASH и TELEGRAM_PHONE в .env')
    return api_id, api_hash, phone


def reader_configured():
    return all(os.getenv(k) for k in ('TELEGRAM_API_ID', 'TELEGRAM_API_HASH')) and SESSION_FILE.exists()


# --- Login (setup-telegram) -------------------------------------------------------------

def _prompt_credentials():
    api_id_text = input('Telegram API ID с my.telegram.org: ').strip()
    api_hash = getpass.getpass('Telegram API Hash (ввод скрыт): ').strip()
    phone = input('Телефон аккаунта в международном формате: ').strip()
    if not api_id_text.isdigit() or not re.fullmatch(r'[0-9a-fA-F]{32}', api_hash) or not re.fullmatch(r'\+\d{8,15}', phone):
        raise ValueError('Неверный API ID, API Hash или номер телефона')
    env_path = ROOT.parent/'.env'; lines = env_path.read_text().splitlines() if env_path.exists() else []
    values = {'TELEGRAM_API_ID': api_id_text, 'TELEGRAM_API_HASH': api_hash, 'TELEGRAM_PHONE': phone}
    found = set(); output = []
    for line in lines:
        key = line.split('=', 1)[0].strip() if '=' in line else ''
        if key in values: output.append(key+'='+values[key]); found.add(key)
        else: output.append(line)
    output += [key+'='+value for key, value in values.items() if key not in found]
    env_path.write_text('\n'.join(output)+'\n'); os.chmod(env_path, 0o600)
    os.environ.update(values)
    return int(api_id_text), api_hash, phone


def print_qr(url):
    print('\nОткройте Telegram на телефоне служебного аккаунта: Настройки → Устройства → Подключить устройство,')
    print('и отсканируйте QR-код. Ссылка для входа (действует ~30 секунд):')
    print(url, flush=True)
    try:
        import qrcode
        code = qrcode.QRCode(border=1)
        code.add_data(url)
        code.print_ascii(invert=True)
    except Exception as exc:
        print('QR-код не нарисован в терминале ('+type(exc).__name__+'); откройте ссылку выше на устройстве с Telegram.')
    print(flush=True)


def ask_cloud_password():
    return getpass.getpass('Облачный пароль Telegram (двухэтапная проверка, ввод скрыт): ')


async def _password_sign_in(client, ask_password):
    from telethon.errors import PasswordHashInvalidError
    for _ in range(3):
        try:
            return await client.sign_in(password=ask_password())
        except PasswordHashInvalidError:
            print('Неверный облачный пароль, попробуйте ещё раз.', flush=True)
    raise ValueError('Облачный пароль введён неверно 3 раза; вход отменён')


def _qr_seconds(qr):
    """Wait until the token expires, but tolerate a skewed server clock (10–60 s)."""
    try:
        left = (qr.expires-datetime.now(UTC)).total_seconds()
    except (AttributeError, TypeError):
        left = 30
    return min(max(left, 10), 60)


async def qr_login(client, *, show=print_qr, ask_password=ask_cloud_password, rounds=QR_ROUNDS):
    """Log in by QR: reprint the code when the ~30 s token expires; 2FA asks for the cloud password."""
    from telethon.errors import SessionPasswordNeededError
    from telethon.tl import functions, types
    qr = await client.qr_login()
    for _ in range(rounds):
        show(qr.url)
        try:
            return await qr.wait(_qr_seconds(qr))  # QRLogin.wait() follows LoginTokenMigrateTo itself
        except SessionPasswordNeededError:
            return await _password_sign_in(client, ask_password)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        # A scan right at expiry is completed by the re-export (success or another DC).
        try:
            await qr.recreate()
            resp = qr._resp
            if isinstance(resp, types.auth.LoginTokenMigrateTo):
                await client._switch_dc(resp.dc_id)
                resp = await client(functions.auth.ImportLoginTokenRequest(resp.token))
                qr._resp = resp
        except SessionPasswordNeededError:
            return await _password_sign_in(client, ask_password)
        if isinstance(resp, types.auth.LoginTokenSuccess):
            await client._on_login(resp.authorization.user)
            return resp.authorization.user
        print('QR-код обновлён.', flush=True)
    raise ValueError('QR-код не отсканирован; запустите setup-telegram --qr ещё раз')


def phone_login(phone):
    async def login(client):
        await client.start(phone=phone)
        return await client.get_me()
    return login


async def _authorized_user(factory, session):
    """The account of an existing session, or None when the file is broken or revoked."""
    try:
        client = factory(str(session))
    except Exception:
        return None
    try:
        await asyncio.wait_for(client.connect(), 30)
        if await asyncio.wait_for(client.is_user_authorized(), 30):
            return await client.get_me()
    except Exception:
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return None


async def login_session(factory, method, session=SESSION):
    """Keep an authorized session; otherwise log in into a temp file and atomically replace the old one.

    A revoked file is never reused: the new auth key starts clean and a running bot picks the
    new file on its next reconnect. Returns (user, replaced)."""
    current = session.with_name(session.name+'.session')
    if current.exists():
        user = await _authorized_user(factory, session)
        if user:
            return user, False
    temp = session.with_name(session.name+'.new')
    temp_file = temp.with_name(temp.name+'.session')
    for path in (temp_file, temp_file.with_name(temp_file.name+'-journal')):
        path.unlink(missing_ok=True)
    client = factory(str(temp))
    try:
        await client.connect()
        user = await method(client)
    finally:
        await client.disconnect()
    if not user:
        temp_file.unlink(missing_ok=True)
        raise ValueError('Telegram не подтвердил вход')
    os.chmod(temp_file, 0o600)
    os.replace(temp_file, current)
    return user, True


def setup_session(qr=False):
    TelegramClient = _telethon()
    if qr:
        api_id, api_hash = reader_credentials()
        method = qr_login
    else:
        try:
            api_id, api_hash, phone = credentials()
        except ValueError:
            api_id, api_hash, phone = _prompt_credentials()
        method = phone_login(phone)
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    user, replaced = asyncio.run(login_session(lambda path: TelegramClient(path, api_id, api_hash), method))
    name = ' '.join(x for x in (getattr(user, 'first_name', None), getattr(user, 'last_name', None)) if x) or 'аккаунт'
    if replaced:
        print(f'Сессия Telegram подключена: {name}. Перезапустите бота, чтобы чтение групп и каналов началось сразу.')
    else:
        print(f'Сессия уже подключена: {name}. Чтобы войти другим аккаунтом, удалите {SESSION_FILE.name} и повторите.')


# --- Message handling -------------------------------------------------------------------

def message_link(username, message_id):
    return f'https://t.me/{username}/{message_id}'


def to_lead(row, msg):
    text = (msg.message or '').strip()
    title = next((x.strip() for x in text.splitlines() if len(x.strip()) >= 8), text)[:220]
    return Lead(source='Telegram: '+row['title'], url=message_link(row['username'], msg.id),
                title=title, text=text, published=msg.date.astimezone(UTC).isoformat(), budget_text=text)


def public_username(chat):
    """The public @username of a chat, including collectible usernames; None for private chats."""
    username = (getattr(chat, 'username', None) or '').strip()
    if not username:
        username = next((x.username for x in getattr(chat, 'usernames', None) or []
                         if getattr(x, 'active', False) and getattr(x, 'username', None)), '')
    username = username.lower()
    return username if re.fullmatch(r'[a-z][a-z0-9_]{3,31}', username) else None


def chat_kind(chat):
    return 'channel' if getattr(chat, 'broadcast', False) else 'group'


def realtime_group_username(event, chat):
    """Username of a public group, supergroup or channel; reject users, bots and private chats."""
    if chat is None or getattr(event, 'is_private', False):
        return None
    if not (getattr(event, 'is_group', False) or getattr(event, 'is_channel', False)):
        return None
    if getattr(chat, 'first_name', None) is not None or getattr(chat, 'bot', False):
        return None
    return public_username(chat)


def register_joined_chat(db, username, title, kind='group', now=None):
    """Register a public chat the service account follows. True when a row was inserted or marked.

    Only a brand-new chat is inserted, disabled and waiting for the platform admin's approval in /admin:
    joining a chat must never start reading it on its own. An existing row keeps its enabled, status
    and check_reason: an admin or the registry may have disabled or rejected it, and following the chat
    must never switch it back on. Such a row only gets the joined marker in its metadata."""
    now = now or datetime.now(UTC).isoformat()
    row = db.execute('SELECT metadata FROM telegram_sources WHERE username=?', (username,)).fetchone()
    if row is None:
        db.execute('''INSERT INTO telegram_sources
            (username,url,title,segment,priority,selection_group,status,enabled,checked_at,check_reason,metadata)
            VALUES(?,?,?,?,2,?,?,0,?,?,?) ON CONFLICT(username) DO NOTHING''',
            (username, 'https://t.me/'+username, (title or username)[:200], JOINED, JOINED, MODERATION, now,
             MODERATION_REASON, joined_metadata(None, kind)))
        return True
    metadata = joined_metadata(row['metadata'], kind)
    if metadata == row['metadata']:
        return False
    db.execute('UPDATE telegram_sources SET metadata=? WHERE username=?', (metadata, username))
    return True


@dataclass(frozen=True)
class PublicChat:
    username: str
    title: str
    kind: str


@dataclass(frozen=True)
class StoreResult:
    stored: bool      # the message was ingested
    inserted: bool    # the chat was new and has been registered, waiting for moderation


# Only what a message needs: metadata (the registry's JSON) stays in the database.
SOURCE_COLUMNS = 'username,title,status,enabled,check_reason,last_message_at'
LAST_MESSAGE_WRITE_SECONDS = 300  # a busy chat's last_message_* is refreshed at most this often


def _source(db, username):
    return db.execute('SELECT '+SOURCE_COLUMNS+' FROM telegram_sources WHERE username=?', (username,)).fetchone()


def _marked_recently(row, now):
    """The chat's realtime marker was written less than LAST_MESSAGE_WRITE_SECONDS before `now`.

    Busy chats deliver many messages a minute; rewriting their telegram_sources row for each one only churns the
    table. Readers of last_message_at (catalog, 24 h realtime count) need minute precision at most."""
    if row['check_reason'] != REALTIME_REASON or not row['last_message_at']:
        return False
    try:
        age = (datetime.fromisoformat(now)-datetime.fromisoformat(str(row['last_message_at']))).total_seconds()
    except (TypeError, ValueError):
        return False
    return 0 <= age < LAST_MESSAGE_WRITE_SECONDS


def store_public_message(db, chat, message, config, now, known=False):
    """Ingest one message of a public chat inside the caller's transaction. Synchronous DB work only.

    A chat without a row is registered first (waiting for moderation); a known chat (registered earlier
    by this process) skips registration. A disabled, rejected or not yet approved source is left as is
    and its messages are not ingested."""
    from .app import ingest
    row = _source(db, chat.username)
    inserted = False
    if row is None or not known:
        changed = register_joined_chat(db, chat.username, chat.title, chat.kind, now)
        if row is None:
            inserted, row = changed, _source(db, chat.username)
    if row is None or not (row['enabled'] and row['status'] == 'active'):
        return StoreResult(stored=False, inserted=inserted)
    ingest(db, to_lead(row, message), config)
    if not _marked_recently(row, now):
        db.execute('UPDATE telegram_sources SET last_message_id=?,last_message_at=?,check_reason=? WHERE username=?',
                   (message.id, now, REALTIME_REASON, chat.username))
    return StoreResult(stored=True, inserted=inserted)


class SourceGate:
    """In-memory snapshot of which known public chats are read (status='active' AND enabled=1).

    It lets the reader drop messages of disabled, rejected or moderated chats without touching the
    database. The snapshot is replaced wholesale by the reader's timer (every HEARTBEAT_SECONDS); between
    refreshes the result of each stored message updates the one chat it concerns. Only a definite "not
    read" blocks a message: an unknown chat, or one the snapshot says is active, still goes to the
    database, where store_public_message re-checks the row. So disabling a chat takes effect at once,
    and an admin's approval within one refresh interval."""

    def __init__(self, active=None):
        self.active = dict(active or {})  # username -> bool, for every row in telegram_sources

    def blocks(self, username):
        return self.active.get(username) is False

    def remember(self, username, active):
        if self.active.get(username) is not active:
            self.active = {**self.active, username: active}

    def replace(self, active):
        self.active = dict(active)


FLAGS_CHUNK = 500  # usernames per lookup when the gate is refreshed


def load_source_flags(db, usernames=None):
    """username -> True when the source is read; for the given usernames, or every registered source when None."""
    if usernames is None:
        rows = db.execute('SELECT username,status,enabled FROM telegram_sources').fetchall()
    else:
        names, rows = sorted(usernames), []
        for start in range(0, len(names), FLAGS_CHUNK):
            chunk = names[start:start+FLAGS_CHUNK]
            rows += db.execute('SELECT username,status,enabled FROM telegram_sources WHERE username IN ('
                               + ','.join('?'*len(chunk))+')', chunk).fetchall()
    return {row['username']: bool(row['enabled'] and row['status'] == 'active') for row in rows}


async def refresh_sources(store, gate, log=_log):
    """Reload the gate's snapshot on the DB thread. On failure the old snapshot stays (it only drops
    messages of chats known to be off; anything else is checked against the database).

    Only chats the gate already holds are reloaded: the gate is consulted only for chats this connection has seen
    a message from, and each of those is remembered on its first message. Reading all ~30k registry rows every
    minute was the reader's largest steady read."""
    try:
        names = set(gate.active)
        gate.replace(await store.run(lambda db: load_source_flags(db, names)) if names else {})
    except Exception as exc:
        log('Монитор Telegram: список источников не обновлён: '+safe_error(exc))


def new_state():
    return {'state': 'starting', 'connected': False, 'authorized': None, 'chats': 0, 'registered': 0,
            'last_event_at': None, 'last_ingest_at': None, 'messages': 0, 'ingested': 0,
            'started_at': datetime.now(UTC).isoformat(), 'connected_at': None, 'dialogs_synced_at': None,
            'restarts': 0, 'last_error': None, 'next_retry_at': None}


def write_heartbeat(db, state, now=None):
    payload = dict(state, at=(now or datetime.now(UTC)).isoformat())
    with db:
        db.execute('''INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (HEARTBEAT_KEY, json.dumps(payload, ensure_ascii=False)))


def _beat(store, state, log=_log):
    """Heartbeat from synchronous code (the supervisor between connection attempts)."""
    snapshot = dict(state)
    try:
        store.submit(lambda db: write_heartbeat(db, snapshot))
    except Exception as exc:
        log('Монитор Telegram: heartbeat не записан: '+safe_error(exc))


async def _beat_async(store, state, log=_log):
    """Heartbeat from the event loop: the write runs on the DB thread."""
    snapshot = dict(state)
    try:
        await store.run(lambda db: write_heartbeat(db, snapshot))
    except Exception as exc:
        log('Монитор Telegram: heartbeat не записан: '+safe_error(exc))


async def process_event(event, store, config, state, log=_log, known=None, gate=None):
    """Ingest one new message from a public chat; register the chat when it has no source row yet.

    Telegram work first; then one short transaction on the store's DB thread, off the event loop.
    `known` caches usernames already registered by this process, so their messages skip registration.
    A message of a registered chat the `gate` snapshot says is not read is dropped without any DB access.
    The heartbeat is written by the reader's timer, not per message."""
    known = set() if known is None else known
    gate = SourceGate() if gate is None else gate
    chat = getattr(event, 'chat', None)
    if chat is None:
        # Entities missing from the update cache must not drop the message.
        try:
            chat = await event.get_chat()
        except Exception as exc:
            log('Монитор Telegram: чат сообщения не получен: '+safe_error(exc))
            return False
    username = realtime_group_username(event, chat)
    if not username:
        return False
    now = datetime.now(UTC).isoformat()
    state['messages'] = state.get('messages', 0)+1; state['last_event_at'] = now
    if not (event.message.message or '').strip():
        return False
    message, marked = event.message, username in known
    if marked and gate.blocks(username):
        return False  # registered this connection and not read: nothing to write
    public = PublicChat(username, getattr(chat, 'title', None) or username, chat_kind(chat))
    try:
        result = await store.run(lambda db: store_public_message(db, public, message, config, now, marked))
        known.add(username)
        gate.remember(username, result.stored)
        if result.inserted:
            log(f'Монитор Telegram: новый публичный источник @{username} ждёт одобрения в /admin.')
        if not result.stored:
            return False
        state['ingested'] = state.get('ingested', 0)+1; state['last_ingest_at'] = now
        return True
    except Exception as exc:
        log(f'Монитор Telegram: сообщение из @{username} не сохранено: {safe_error(exc)}')
        return False


async def sync_dialogs(client, store, state, log=_log, known=None):
    """Register every public group and channel the account follows. Reads the dialog list, not history.

    Listing the dialogs also caches channel access hashes, so Telethon can recover update gaps.
    Chats in `known` were registered earlier by this process and are not written again."""
    known = set() if known is None else known
    chats = {}
    async for dialog in client.iter_dialogs():
        entity = getattr(dialog, 'entity', None)
        if getattr(dialog, 'is_user', False) or entity is None or getattr(entity, 'left', False):
            continue
        username = public_username(entity)
        if username:
            chats[username] = (getattr(entity, 'title', None) or username, chat_kind(entity))
    fresh = {username: chat for username, chat in chats.items() if username not in known}
    changed = await store.run(
        lambda db: sum(register_joined_chat(db, username, title, kind) for username, (title, kind) in fresh.items()))
    known.update(fresh)
    state.update(chats=len(chats), registered=changed, dialogs_synced_at=datetime.now(UTC).isoformat())
    return len(chats), changed


async def read_once(client_factory, store, config, state, log=_log):
    """One connection lifetime. Returns when Telegram disconnects; raises on errors."""
    from telethon import events
    client = client_factory()
    known = set()  # usernames registered by this connection; their messages skip registration
    gate = SourceGate()  # which of them are read; refreshed by the timer below
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ReaderUnauthorized(UNAUTHORIZED_MESSAGE)

        async def live(event):
            await process_event(event, store, config, state, log, known, gate)
        # incoming=True keeps channel posts: Telethon marks them out=False unless this account wrote them.
        client.add_event_handler(live, events.NewMessage(incoming=True))
        state.update(state='running', connected=True, authorized=True, last_error=None, next_retry_at=None,
                     connected_at=datetime.now(UTC).isoformat())
        try:
            chats, changed = await sync_dialogs(client, store, state, log, known)
        except Exception as exc:
            chats, changed = state.get('chats', 0), 0
            log('Монитор Telegram: список чатов не прочитан: '+safe_error(exc))
        await refresh_sources(store, gate, log)
        log(f'Мониторинг Telegram запущен в реальном времени без чтения истории: '
            f'публичных групп и каналов {chats}, новых источников {changed}.')
        await _beat_async(store, state, log)

        async def heartbeat():
            next_sync = time.monotonic()+DIALOG_SYNC_SECONDS
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                state['connected'] = bool(client.is_connected())
                if time.monotonic() >= next_sync:
                    next_sync = time.monotonic()+DIALOG_SYNC_SECONDS
                    try:
                        _, changed = await sync_dialogs(client, store, state, log, known)
                        if changed:
                            log(f'Монитор Telegram: новых публичных источников {changed}.')
                    except Exception as exc:
                        log('Монитор Telegram: список чатов не обновлён: '+safe_error(exc))
                await refresh_sources(store, gate, log)
                await _beat_async(store, state, log)
        beat = asyncio.ensure_future(heartbeat())
        try:
            await client.run_until_disconnected()
        finally:
            beat.cancel()
    finally:
        state['connected'] = False
        try:
            await client.disconnect()
        except Exception:
            pass


def flood_wait_seconds(exc):
    try:
        from telethon.errors import FloodError
    except ImportError:
        return None
    if isinstance(exc, FloodError):
        return int(getattr(exc, 'seconds', 0) or 0)
    return None


def is_unauthorized(exc):
    if isinstance(exc, ReaderUnauthorized):
        return True
    try:
        from telethon.errors import AuthKeyError, AuthKeyNotFound, UnauthorizedError
    except ImportError:
        return False
    return isinstance(exc, (UnauthorizedError, AuthKeyError, AuthKeyNotFound))


def _alert(exc, component='telegram_reader', **tags):
    """Owner alert + Sentry for unexpected reader failures; never breaks the supervisor."""
    try:
        from .observability import capture_exception
        capture_exception(exc, component=component, **tags)
    except Exception:
        pass


def supervise(attempt, state, *, heartbeat=lambda: None, sleep=time.sleep, log=_log,
              clock=time.monotonic, max_attempts=None):
    """Run attempt() forever: restart after errors or disconnects with exponential backoff (5 s → 5 min).

    FloodWait waits the seconds Telegram asked for; a revoked session waits 10 minutes between checks.
    Pauses are cut into heartbeat-sized steps so status always sees a fresh heartbeat."""
    delay = MIN_RETRY_SECONDS; attempts = 0
    while max_attempts is None or attempts < max_attempts:
        attempts += 1
        if attempts > 1:
            state['restarts'] = state.get('restarts', 0)+1
            log(f'Монитор Telegram перезапускается (перезапуск {state["restarts"]}).')
        started = clock()
        try:
            attempt()
            error = None
        except Exception as exc:
            error = exc
        if clock()-started >= HEALTHY_RUN_SECONDS:
            delay = MIN_RETRY_SECONDS  # a long healthy run starts the backoff over
        if error is not None and is_unauthorized(error):
            wait, kind, reason = UNAUTHORIZED_RETRY_SECONDS, 'unauthorized', UNAUTHORIZED_MESSAGE
            if not isinstance(error, ReaderUnauthorized):
                reason += ' ('+safe_error(error)+')'
            log(f'{reason}. Повторная проверка через {wait} с.')
            if not state.get('unauthorized_alerted'):
                state['unauthorized_alerted'] = True  # alert once per revoked-session episode
                _alert(error, reader_state='unauthorized')
            state['authorized'] = False
        elif error is not None and flood_wait_seconds(error) is not None:
            wait, kind, reason = max(MIN_RETRY_SECONDS, flood_wait_seconds(error)+1), 'flood_wait', safe_error(error)
            log(f'Монитор Telegram остановлен: {reason}; Telegram просит подождать, перезапуск через {wait} с.')
        else:
            reason = safe_error(error) if error is not None else 'соединение с Telegram закрыто'
            wait, kind = delay, 'backoff'
            delay = min(delay*2, MAX_RETRY_SECONDS)
            log(f'Монитор Telegram остановлен: {reason}; перезапуск через {wait} с.')
            if error is not None and not isinstance(error, ConnectionError):  # disconnects are not alerted
                _alert(error, reader_state='backoff')
        if error is None or not is_unauthorized(error):
            state.pop('unauthorized_alerted', None)
        state.update(state=kind, connected=False, last_error=reason,
                     next_retry_at=(datetime.now(UTC)+timedelta(seconds=wait)).isoformat())
        heartbeat()
        if max_attempts is not None and attempts >= max_attempts:
            break
        remaining = wait
        while remaining > 0:
            step = min(HEARTBEAT_SECONDS, remaining)
            sleep(step); remaining -= step
            heartbeat()


def run_monitor(db_path, config, *, client_factory=None, sleep=time.sleep, max_attempts=None, log=_log):
    """Supervised event-only reader: never polls chat history; survives crashes and disconnects."""
    if client_factory is None:
        from .app import env_load
        env_load()
        TelegramClient = _telethon(); api_id, api_hash = reader_credentials()

        def client_factory():
            return TelegramClient(str(SESSION), api_id, api_hash, retry_delay=5)
    state = new_state()
    # One long-lived connection for the whole reader, used on its own thread (never per message).
    store = MonitorStore(db_path, thread_name='telegram-reader-db')
    log('Монитор Telegram: запуск.')

    def attempt():
        asyncio.run(read_once(client_factory, store, config, state, log))
    try:
        supervise(attempt, state, heartbeat=lambda: _beat(store, state, log), sleep=sleep, log=log,
                  max_attempts=max_attempts)
    finally:
        store.close()
    return state


class ReaderThread:
    """Owns a long-running monitor thread: start once, restart if it ever dies (at most once a minute).

    Used for the shared reader and for the personal-monitors manager (`name`, `label`, `component`)."""
    def __init__(self, target, clock=time.monotonic, restart_delay=60, log=_log,
                 name='telegram-reader', label='Монитор Telegram', component='telegram_reader'):
        self.target, self.clock, self.restart_delay, self.log = target, clock, restart_delay, log
        self.name, self.label, self.component = name, label, component
        self.thread = None; self.next_start = 0.0

    def _run(self):
        try:
            self.target()
            self.log(f'{self.label} остановлен: поток чтения завершился; будет перезапущен.')
        except Exception as exc:
            self.log(f'{self.label} остановлен: '+safe_error(exc)+'; поток будет перезапущен.')
            _alert(exc, component=self.component, reader_state='thread_crash')

    def ensure(self):
        """'started' on the first start, 'restarted' after a dead thread, None when nothing changed."""
        if self.thread is not None and self.thread.is_alive():
            return None
        if self.clock() < self.next_start:
            return None
        restarted = self.thread is not None
        self.thread = threading.Thread(target=self._run, daemon=True, name=self.name)
        self.thread.start()
        self.next_start = self.clock()+self.restart_delay
        if restarted:
            self.log(f'{self.label}: поток чтения перезапущен.')
        return 'restarted' if restarted else 'started'


# --- Status -------------------------------------------------------------------------------

def reader_status(db, now=None):
    """Heartbeat of the shared reader plus registry facts, for `status` and the admin page."""
    now = now or datetime.now(UTC)
    row = db.execute('SELECT value FROM settings WHERE key=?', (HEARTBEAT_KEY,)).fetchone()
    try:
        beat = json.loads(row['value']) if row else {}
    except (TypeError, ValueError):
        beat = {}
    age = None
    try:
        age = max(0, int((now-datetime.fromisoformat(beat['at'])).total_seconds())) if beat.get('at') else None
    except (TypeError, ValueError):
        pass
    cutoff = (now-timedelta(hours=24)).isoformat()
    realtime = db.execute('SELECT count(*) AS total FROM telegram_sources WHERE check_reason=? AND last_message_at>=?',
                          (REALTIME_REASON, cutoff)).fetchone()['total']
    joined = db.execute('SELECT count(*) AS total FROM telegram_sources WHERE selection_group=? OR metadata LIKE ?',
                        (JOINED, '%"joined": true%')).fetchone()['total']
    state = beat.get('state') or 'never_started'
    if age is not None and age > HEARTBEAT_STALE_SECONDS:
        state = 'stale'  # the bot process or its reader thread is gone
    return {'state': state, 'heartbeat_age_seconds': age, 'connected': bool(beat.get('connected')) and state == 'running',
            'authorized': beat.get('authorized'), 'chats': beat.get('chats'), 'last_event_at': beat.get('last_event_at'),
            'last_ingest_at': beat.get('last_ingest_at'), 'realtime_sources_24h': realtime, 'joined_sources': joined,
            'messages_since_start': beat.get('messages'), 'ingested_since_start': beat.get('ingested'),
            'started_at': beat.get('started_at'), 'restarts': beat.get('restarts'), 'last_error': beat.get('last_error'),
            'next_retry_at': beat.get('next_retry_at')}
