"""Encrypted personal Telegram sessions and explicit group selection."""
import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken

from .db_store import MonitorStore, _connection_lost  # noqa: F401  (re-exported for callers and tests)


PHONE = re.compile(r'^\+[1-9]\d{7,14}$')
AUTH_TTL = timedelta(minutes=10)
# gunicorn kills a worker after 30 s (compose.web.yaml). Every Telegram call made
# while serving one HTTP request shares this budget so the request always returns
# (and records its outcome) before the worker can be killed mid-operation.
REQUEST_BUDGET = 24
CALL_TIMEOUT = 20
# QR polling is a short poll: a request must never hold one of the few gunicorn
# threads for long. The browser simply asks again.
QR_POLL_SECONDS = 3


class TelegramAccountError(ValueError):
    pass


def _delivery_label(value):
    return {
        'SentCodeTypeApp': 'в служебный чат «Telegram» на уже авторизованном устройстве',
        'SentCodeTypeSms': 'по SMS',
        'SentCodeTypeSmsPhrase': 'по SMS с кодовой фразой',
        'SentCodeTypeSmsWord': 'по SMS с кодовым словом',
        'SentCodeTypeCall': 'телефонным звонком',
        'SentCodeTypeFlashCall': 'входящим звонком',
        'SentCodeTypeMissedCall': 'пропущенным звонком',
        'SentCodeTypeEmailCode': 'на привязанную электронную почту',
        'SentCodeTypeSetUpEmailRequired': 'после настройки резервной электронной почты',
        'SentCodeTypeFragmentSms': 'через Fragment',
        'SentCodeTypeFirebaseSms': 'через системную доставку Telegram',
        'CodeTypeSms': 'по SMS',
        'CodeTypeCall': 'телефонным звонком',
        'CodeTypeFlashCall': 'входящим звонком',
        'CodeTypeMissedCall': 'пропущенным звонком',
        'CodeTypeFragmentSms': 'через Fragment',
    }.get(type(value).__name__, 'через Telegram') if value else ''


def _telegram_code_error(exc):
    messages = {
        'ApiIdInvalidError': 'Telegram отклонил API ID или API Hash сервера.',
        'AuthRestartError': 'Telegram попросил перезапустить авторизацию. Повторите попытку через минуту.',
        'AuthTokenAlreadyAcceptedError': 'QR-код уже был использован. Создайте новый.',
        'AuthTokenExpiredError': 'QR-код истёк. Создайте новый.',
        'AuthTokenInvalidError': 'Telegram отклонил QR-код. Создайте новый.',
        'PhoneCodeExpiredError': 'Запрос кода истёк. Начните подключение заново.',
        'PhoneNumberAppSignupForbiddenError': 'Этот номер нельзя зарегистрировать через подключение приложения.',
        'PhoneNumberBannedError': 'Telegram заблокировал этот номер телефона.',
        'PhoneNumberFloodError': 'Для этого номера запрошено слишком много кодов. Подождите перед новой попыткой.',
        'PhoneNumberInvalidError': 'Telegram не распознал номер телефона. Проверьте код страны и формат.',
        'SendCodeUnavailableError': 'Telegram временно не может отправить код этому аккаунту.',
    }
    return messages.get(type(exc).__name__,
                        'Telegram отклонил запрос кода. Подождите несколько минут и повторите попытку один раз.')


class SessionCipher:
    def __init__(self, key):
        try:
            self.fernet = Fernet((key or '').encode())
        except (ValueError, TypeError):
            raise TelegramAccountError('Ключ шифрования Telegram-сессий не настроен.') from None

    def encrypt(self, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        return self.fernet.encrypt(raw).decode()

    def decrypt(self, value):
        try:
            return json.loads(self.fernet.decrypt(value.encode()).decode())
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
            raise TelegramAccountError('Не удалось расшифровать Telegram-сессию. Проверьте ключ сервера.') from None


def masked_phone(phone):
    return phone[:3] + '•' * max(3, len(phone) - 6) + phone[-3:]


def telegram_credentials():
    try:
        api_id = int(os.environ.get('TELEGRAM_API_ID', ''))
    except ValueError:
        api_id = 0
    api_hash = os.environ.get('TELEGRAM_API_HASH', '').strip()
    if not api_id or not re.fullmatch(r'[0-9a-fA-F]{32}', api_hash):
        raise TelegramAccountError('TELEGRAM_API_ID и TELEGRAM_API_HASH не настроены на сервере.')
    return api_id, api_hash


class TelethonGateway:
    """Small boundary around Telethon so auth flows can be tested without Telegram."""
    def __init__(self):
        self.api_id, self.api_hash = telegram_credentials()
        self.deadline = time.monotonic() + REQUEST_BUDGET

    def _timeout(self, awaitable=None, limit=CALL_TIMEOUT):
        deadline = getattr(self, 'deadline', None)
        remaining = limit if deadline is None else min(limit, deadline - time.monotonic())
        if remaining < 2:
            if awaitable is not None:
                awaitable.close()
            raise TelegramAccountError('Telegram отвечает слишком долго. Повторите попытку.')
        return remaining

    def _run(self, awaitable):
        timeout = self._timeout(awaitable)
        try:
            return asyncio.run(asyncio.wait_for(awaitable, timeout=timeout))
        except TimeoutError:
            raise TelegramAccountError('Telegram не ответил вовремя. Повторите попытку.') from None

    def begin(self, phone):
        return self._run(self._begin(phone))

    async def _begin(self, phone):
        from telethon import TelegramClient
        from telethon.errors import FloodWaitError, RPCError
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(), self.api_id, self.api_hash)
        try:
            await client.connect()
            try:
                sent = await client.send_code_request(phone)
            except FloodWaitError as exc:
                raise TelegramAccountError(f'Telegram ограничил частоту. Повторите через {exc.seconds} секунд.') from None
            except RPCError as exc:
                raise TelegramAccountError(_telegram_code_error(exc)) from None
            delivery = _delivery_label(sent.type)
            next_delivery = _delivery_label(sent.next_type)
            return {'phone': phone, 'phone_code_hash': sent.phone_code_hash,
                    'session': client.session.save(), 'delivery': delivery,
                    'next_delivery': next_delivery,
                    'resend_after': time.time() + max(1, sent.timeout or 60)}
        finally:
            await client.disconnect()

    def resend(self, state):
        return self._run(self._resend(state))

    async def _resend(self, state):
        from telethon import TelegramClient
        from telethon.errors import FloodWaitError, RPCError
        from telethon.sessions import StringSession
        from telethon.tl.functions.auth import ResendCodeRequest
        client = TelegramClient(StringSession(state['session']), self.api_id, self.api_hash)
        try:
            await client.connect()
            try:
                sent = await client(ResendCodeRequest(state['phone'], state['phone_code_hash']))
            except FloodWaitError as exc:
                raise TelegramAccountError(f'Telegram ограничил частоту. Повторите через {exc.seconds} секунд.') from None
            except RPCError as exc:
                raise TelegramAccountError(_telegram_code_error(exc)) from None
            if not hasattr(sent, 'phone_code_hash'):
                raise TelegramAccountError('Telegram не вернул новый код. Начните подключение заново.')
            state.update(phone_code_hash=sent.phone_code_hash, session=client.session.save(),
                         delivery=_delivery_label(sent.type),
                         next_delivery=_delivery_label(sent.next_type),
                         resend_after=time.time() + max(1, sent.timeout or 60))
            return state
        finally:
            await client.disconnect()

    def begin_qr(self):
        return self._run(self._begin_qr())

    async def _begin_qr(self):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(), self.api_id, self.api_hash)
        try:
            await client.connect()
            qr = await client.qr_login()
            return {'session': client.session.save(), 'token': base64.b64encode(qr.token).decode(),
                    'url': qr.url, 'expires': qr.expires.isoformat(),
                    'deadline': (datetime.now(timezone.utc) + AUTH_TTL).isoformat()}
        finally:
            await client.disconnect()

    def wait_qr(self, state):
        return self._run(self._wait_qr(state))

    async def _wait_qr(self, state):
        from telethon import TelegramClient
        from telethon.errors import RPCError, SessionPasswordNeededError
        from telethon.sessions import StringSession
        from telethon.tl import types
        from telethon.tl.custom.qrlogin import QRLogin
        from telethon.tl.functions.auth import ImportLoginTokenRequest
        client = TelegramClient(StringSession(state['session']), self.api_id, self.api_hash)
        try:
            await client.connect()
            # QR approval authorizes the MTProto key on Telegram's side. The short-lived
            # UpdateLoginToken event may be missed between two HTTP polling requests,
            # so always check the durable authorization state before waiting for it.
            probed = await self._qr_probe(client, state)
            if probed:
                return probed
            expires = datetime.fromisoformat(state['expires'])
            now = datetime.now(timezone.utc)
            remaining = (expires - now).total_seconds()
            qr = QRLogin(client, [])
            if remaining <= 1:
                # Telegram QR tokens live ~30 s. Re-exporting is also how a scan that
                # happened between two polls is completed (success or DC migration).
                deadline = state.get('deadline')
                if not deadline or datetime.fromisoformat(deadline) <= now:
                    raise TelegramAccountError('QR-код истёк. Создайте новый.')
                try:
                    await qr.recreate()
                    resp = qr._resp
                    if isinstance(resp, types.auth.LoginTokenMigrateTo):
                        await client._switch_dc(resp.dc_id)
                        resp = await client(ImportLoginTokenRequest(resp.token))
                except SessionPasswordNeededError:
                    state['session'] = client.session.save()
                    return {'password_required': True, 'state': state}
                except RPCError as exc:
                    raise TelegramAccountError(_telegram_code_error(exc)) from None
                if isinstance(resp, types.auth.LoginTokenSuccess):
                    user = resp.authorization.user
                    await client._on_login(user)
                    return self._authorized(client, user)
                if not isinstance(resp, types.auth.LoginToken):
                    raise TelegramAccountError('Telegram вернул неожиданный ответ. Создайте новый QR-код.')
                state.update(session=client.session.save(), token=base64.b64encode(resp.token).decode(),
                             url=qr.url, expires=resp.expires.isoformat())
                return {'pending': True, 'refreshed': True, 'state': state}
            qr._resp = types.auth.LoginToken(expires=expires, token=base64.b64decode(state['token']))
            try:
                user = await qr.wait(timeout=min(QR_POLL_SECONDS, remaining))
            except TimeoutError:
                probed = await self._qr_probe(client, state)
                if probed:
                    return probed
                state['session'] = client.session.save()
                return {'pending': True, 'state': state}
            except SessionPasswordNeededError:
                state['session'] = client.session.save()
                return {'password_required': True, 'state': state}
            except RPCError as exc:
                raise TelegramAccountError(_telegram_code_error(exc)) from None
            return self._authorized(client, user)
        finally:
            await client.disconnect()

    async def _qr_probe(self, client, state):
        """The outcome of a QR scan that no poll saw live, or None while nobody scanned.

        UpdateLoginToken reaches only a connection that is open at scan time, and short
        polls are disconnected most of the time. An accepted QR leaves this auth key either
        authorized or waiting for the cloud password: then every call fails with
        SESSION_PASSWORD_NEEDED. (is_user_authorized() hides that difference and caches
        False for the rest of the poll.)"""
        from telethon.errors import RPCError, SessionPasswordNeededError
        from telethon.tl.functions.users import GetUsersRequest
        from telethon.tl.types import InputUserSelf
        try:
            users = await client(GetUsersRequest([InputUserSelf()]))
        except SessionPasswordNeededError:
            state['session'] = client.session.save()
            return {'password_required': True, 'state': state}
        except RPCError:
            return None
        return self._authorized(client, users[0]) if users else None

    def verify_code(self, state, code):
        return self._run(self._verify_code(state, code))

    async def _verify_code(self, state, code):
        from telethon import TelegramClient
        from telethon.errors import (FloodWaitError, PhoneCodeExpiredError,
                                     PhoneCodeInvalidError, SessionPasswordNeededError)
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(state['session']), self.api_id, self.api_hash)
        try:
            await client.connect()
            try:
                user = await client.sign_in(phone=state['phone'], code=code,
                                            phone_code_hash=state['phone_code_hash'])
            except SessionPasswordNeededError:
                state['session'] = client.session.save()
                return {'password_required': True, 'state': state}
            except (PhoneCodeInvalidError, PhoneCodeExpiredError):
                raise TelegramAccountError('Код неверный или истёк. Запросите новый код.') from None
            except FloodWaitError as exc:
                raise TelegramAccountError(f'Telegram ограничил частоту. Повторите через {exc.seconds} секунд.') from None
            return self._authorized(client, user)
        finally:
            await client.disconnect()

    def verify_password(self, state, password):
        return self._run(self._verify_password(state, password))

    async def _verify_password(self, state, password):
        from telethon import TelegramClient
        from telethon.errors import FloodWaitError, PasswordHashInvalidError, RPCError
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(state['session']), self.api_id, self.api_hash)
        try:
            await client.connect()
            try:
                user = await client.sign_in(password=password)
            except PasswordHashInvalidError:
                raise TelegramAccountError('Неверный облачный пароль Telegram.') from None
            except FloodWaitError as exc:
                raise TelegramAccountError(f'Telegram ограничил частоту. Повторите через {exc.seconds} секунд.') from None
            except RPCError:
                raise TelegramAccountError('Telegram не принял вход. Начните подключение заново.') from None
            return self._authorized(client, user)
        finally:
            await client.disconnect()

    @staticmethod
    def _authorized(client, user):
        name = ' '.join(x for x in (getattr(user, 'first_name', ''), getattr(user, 'last_name', '')) if x).strip()
        return {'password_required': False, 'session': client.session.save(),
                'telegram_user_id': user.id, 'display_name': name or 'Telegram-аккаунт'}

    def dialogs(self, session_string):
        return self._run(self._dialogs(session_string))

    async def _dialogs(self, session_string):
        from telethon import TelegramClient, utils
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(session_string), self.api_id, self.api_hash)
        result = []
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise TelegramAccountError('Telegram-сессия отозвана. Подключите аккаунт заново.')
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if not dialog.is_group or getattr(entity, 'broadcast', False) or not getattr(entity, 'megagroup', False):
                    continue
                result.append({'peer_id': utils.get_peer_id(entity), 'title': (dialog.name or 'Группа')[:300],
                               'username': getattr(entity, 'username', None),
                               'kind': 'supergroup'})
                if len(result) >= 1000:
                    break
            return result
        finally:
            await client.disconnect()

    def logout(self, session_string):
        return self._run(self._logout(session_string))

    async def _logout(self, session_string):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(session_string), self.api_id, self.api_hash)
        try:
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
                return True
            return False
        finally:
            await client.disconnect()


def _end_transaction(db):
    """Close the implicit read transaction before a slow Telegram call.

    psycopg opens a transaction on the first SELECT; holding it across a network call
    leaves the session idle in transaction (it pins a pooler slot and is killed by
    idle_in_transaction_session_timeout)."""
    db.commit()


def auth_record(db, user_id, cipher):
    row = db.execute('SELECT * FROM telegram_connection_auth WHERE user_id=?', (user_id,)).fetchone()
    if not row or row['expires_at'] <= datetime.now(timezone.utc) or row['attempts'] >= 5:
        raise TelegramAccountError('Время подключения истекло. Запросите новый код.')
    return row, cipher.decrypt(row['state_cipher'])


def begin_connection(db, user_id, phone, cipher, gateway):
    if not PHONE.fullmatch(phone):
        raise TelegramAccountError('Введите номер в международном формате, например +79991234567.')
    _end_transaction(db)
    state = gateway.begin(phone)
    now = datetime.now(timezone.utc)
    with db:
        db.execute('''INSERT INTO telegram_connection_auth(user_id,state_cipher,stage,attempts,expires_at,updated_at)
            VALUES(?,?,'code',0,?,now()) ON CONFLICT(user_id) DO UPDATE SET
            state_cipher=excluded.state_cipher,stage='code',attempts=0,expires_at=excluded.expires_at,updated_at=now()''',
            (user_id, cipher.encrypt(state), now + AUTH_TTL))
        db.execute('''INSERT INTO telegram_connections(user_id,phone_hint,status,updated_at)
            VALUES(?,?,'pending',now()) ON CONFLICT(user_id) DO UPDATE SET
            phone_hint=excluded.phone_hint,status='pending',last_error='',updated_at=now()''',
            (user_id, masked_phone(phone)))
    return state.get('delivery', 'через Telegram')


def resend_connection(db, user_id, cipher, gateway):
    row, state = auth_record(db, user_id, cipher)
    if row['stage'] != 'code':
        raise TelegramAccountError('Повторная отправка кода сейчас недоступна.')
    if not state.get('next_delivery'):
        raise TelegramAccountError('Telegram не предложил резервный способ доставки. Начните подключение позже.')
    wait = int(state.get('resend_after', 0) - time.time())
    if wait > 0:
        raise TelegramAccountError(f'Резервный способ станет доступен через {wait} с.')
    _end_transaction(db)
    state = gateway.resend(state)
    with db:
        db.execute('''UPDATE telegram_connection_auth SET state_cipher=?,expires_at=?,updated_at=now()
            WHERE user_id=?''', (cipher.encrypt(state), datetime.now(timezone.utc) + AUTH_TTL, user_id))
    return state.get('delivery', 'через Telegram')


def begin_qr_connection(db, user_id, cipher, gateway):
    _end_transaction(db)
    state = gateway.begin_qr()
    state.setdefault('deadline', (datetime.now(timezone.utc) + AUTH_TTL).isoformat())
    expires = datetime.fromisoformat(state['deadline'])
    with db:
        db.execute('''INSERT INTO telegram_connection_auth(user_id,state_cipher,stage,attempts,expires_at,updated_at)
            VALUES(?,?,'qr',0,?,now()) ON CONFLICT(user_id) DO UPDATE SET
            state_cipher=excluded.state_cipher,stage='qr',attempts=0,
            expires_at=excluded.expires_at,updated_at=now()''',
            (user_id, cipher.encrypt(state), expires))
        db.execute('''INSERT INTO telegram_connections(user_id,status,updated_at)
            VALUES(?,'pending',now()) ON CONFLICT(user_id) DO UPDATE SET
            status='pending',last_error='',updated_at=now()''', (user_id,))
    return state


def qr_connection_state(db, user_id, cipher):
    row, state = auth_record(db, user_id, cipher)
    if row['stage'] != 'qr':
        raise TelegramAccountError('QR-подключение не запущено.')
    return state


def wait_qr_connection(db, user_id, cipher, gateway):
    # An approved Telegram session remains valid after the visual QR token expires.
    # Let the gateway check that durable authorization before rejecting an old token.
    row = db.execute('SELECT * FROM telegram_connection_auth WHERE user_id=?', (user_id,)).fetchone()
    if row and row['stage'] == 'password' and row['expires_at'] > datetime.now(timezone.utc):
        # Another poll or tab already saw the scan: send this page to the password form too.
        return 'password', 0
    if not row or row['stage'] != 'qr':
        raise TelegramAccountError('QR-подключение не запущено.')
    state = cipher.decrypt(row['state_cipher'])
    _end_transaction(db)
    result = gateway.wait_qr(state)
    # Polls may overlap (two tabs, retries): never let a stale 'pending' overwrite a
    # row that another poll already moved to 'password' or replaced with a new flow.
    if result.get('pending'):
        with db:
            db.execute("""UPDATE telegram_connection_auth SET state_cipher=?,updated_at=now()
                WHERE user_id=? AND stage='qr' AND state_cipher=?""",
                       (cipher.encrypt(result['state']), user_id, row['state_cipher']))
        return ('refreshed' if result.get('refreshed') else 'pending'), 0
    if result.get('password_required'):
        with db:
            db.execute("""UPDATE telegram_connection_auth SET state_cipher=?,stage='password',
                attempts=0,expires_at=?,updated_at=now() WHERE user_id=? AND stage='qr'""",
                       (cipher.encrypt(result['state']), datetime.now(timezone.utc) + AUTH_TTL, user_id))
        return 'password', 0
    return 'active', _finish(db, user_id, result, cipher, gateway)


def _finish(db, user_id, result, cipher, gateway):
    with db:
        db.execute('''INSERT INTO telegram_connections
            (user_id,telegram_user_id,display_name,session_cipher,status,last_connected_at,updated_at)
            VALUES(?,?,?,?,'active',now(),now()) ON CONFLICT(user_id) DO UPDATE SET
            telegram_user_id=excluded.telegram_user_id,display_name=excluded.display_name,
            session_cipher=excluded.session_cipher,status='active',last_connected_at=now(),last_error='',updated_at=now()''',
            (user_id, result['telegram_user_id'], result['display_name'], cipher.encrypt({'session': result['session']})))
        db.execute('DELETE FROM telegram_connection_auth WHERE user_id=?', (user_id,))
    try:
        dialogs = gateway.dialogs(result['session'])
    except Exception as exc:
        with db:
            db.execute("UPDATE telegram_connections SET last_error=?,updated_at=now() WHERE user_id=?",
                       ('Не удалось обновить список групп: ' + type(exc).__name__, user_id))
        return 0
    with db:
        for dialog in dialogs:
            db.execute('''INSERT INTO user_telegram_dialogs(user_id,peer_id,title,username,kind)
                VALUES(?,?,?,?,?) ON CONFLICT(user_id,peer_id) DO UPDATE SET title=excluded.title,
                username=excluded.username,kind=excluded.kind,updated_at=now()''',
                (user_id, dialog['peer_id'], dialog['title'], dialog['username'], dialog['kind']))
    return len(dialogs)


def verify_code(db, user_id, code, cipher, gateway):
    if not re.fullmatch(r'\d{4,8}', code or ''):
        raise TelegramAccountError('Введите код из сообщения Telegram.')
    row, state = auth_record(db, user_id, cipher)
    if row['stage'] != 'code':
        raise TelegramAccountError('Сейчас требуется облачный пароль Telegram.')
    with db:
        db.execute('UPDATE telegram_connection_auth SET attempts=attempts+1,updated_at=now() WHERE user_id=?', (user_id,))
    result = gateway.verify_code(state, code)
    if result.get('password_required'):
        with db:
            db.execute("UPDATE telegram_connection_auth SET state_cipher=?,stage='password',attempts=0,updated_at=now() WHERE user_id=?",
                       (cipher.encrypt(result['state']), user_id))
        return 'password', 0
    return 'active', _finish(db, user_id, result, cipher, gateway)


def verify_password(db, user_id, password, cipher, gateway):
    if not password or len(password) > 256:
        raise TelegramAccountError('Введите облачный пароль Telegram.')
    row, state = auth_record(db, user_id, cipher)
    if row['stage'] != 'password':
        raise TelegramAccountError('Сначала подтвердите код Telegram.')
    with db:
        db.execute('UPDATE telegram_connection_auth SET attempts=attempts+1,updated_at=now() WHERE user_id=?', (user_id,))
    return _finish(db, user_id, gateway.verify_password(state, password), cipher, gateway)


def sync_dialogs(db, user_id, cipher, gateway):
    row = db.execute("SELECT session_cipher FROM telegram_connections WHERE user_id=? AND status='active'", (user_id,)).fetchone()
    if not row:
        raise TelegramAccountError('Telegram-аккаунт не подключён.')
    _end_transaction(db)
    dialogs = gateway.dialogs(cipher.decrypt(row['session_cipher'])['session'])
    with db:
        for dialog in dialogs:
            db.execute('''INSERT INTO user_telegram_dialogs(user_id,peer_id,title,username,kind)
                VALUES(?,?,?,?,?) ON CONFLICT(user_id,peer_id) DO UPDATE SET title=excluded.title,
                username=excluded.username,kind=excluded.kind,updated_at=now()''',
                (user_id, dialog['peer_id'], dialog['title'], dialog['username'], dialog['kind']))
        db.execute("UPDATE telegram_connections SET last_connected_at=now(),last_error='',updated_at=now() WHERE user_id=?", (user_id,))
    return len(dialogs)


def disconnect(db, user_id, cipher, gateway):
    row = db.execute('SELECT session_cipher FROM telegram_connections WHERE user_id=?', (user_id,)).fetchone()
    remote = False
    _end_transaction(db)
    if row and row['session_cipher']:
        try:
            remote = gateway.logout(cipher.decrypt(row['session_cipher'])['session'])
        except Exception:
            remote = False
    with db:
        db.execute('DELETE FROM telegram_connection_auth WHERE user_id=?', (user_id,))
        db.execute("UPDATE telegram_connections SET session_cipher='',status='revoked',updated_at=now() WHERE user_id=?", (user_id,))
        db.execute('UPDATE user_telegram_dialogs SET enabled=false,updated_at=now() WHERE user_id=?', (user_id,))
    return remote


def personal_message_link(entity, message_id):
    username = (getattr(entity, 'username', None) or '').strip()
    if username:
        return f'https://t.me/{username}/{message_id}'
    return f'https://t.me/c/{entity.id}/{message_id}'


def _load_monitor_targets(db):
    connections = [dict(row) for row in db.execute("""SELECT user_id,session_cipher,updated_at FROM telegram_connections
        WHERE status='active' AND session_cipher<>''""").fetchall()]
    enabled = {(row['user_id'], row['peer_id']) for row in db.execute(
        'SELECT user_id,peer_id FROM user_telegram_dialogs WHERE enabled=true').fetchall()}
    return connections, enabled


def store_personal_message(db, user_id, chat_id, entity, message, config):
    """Ingest one group message. Synchronous DB work only: never awaits."""
    from .app import ingest
    from .core import Lead, UTC
    # Re-checked inside the write transaction: the manager's cache may be up to one cycle old.
    source = db.execute('''SELECT title,username FROM user_telegram_dialogs
        WHERE user_id=? AND peer_id=? AND enabled=true''', (user_id, chat_id)).fetchone()
    if not source:
        return False
    text = message.message.strip()
    title = next((line.strip() for line in text.splitlines() if len(line.strip()) >= 8), text)[:220]
    lead = Lead(source='Telegram · ' + source['title'],
                url=personal_message_link(entity, message.id), title=title, text=text,
                published=message.date.astimezone(UTC).isoformat(), budget_text=text)
    ingest(db, lead, config, origin_user_id=user_id)
    db.execute('''UPDATE user_telegram_dialogs SET last_message_at=now(),updated_at=now()
        WHERE user_id=? AND peer_id=?''', (user_id, chat_id))
    return True


async def handle_personal_message(event, user_id, store, enabled, config):
    """Telegram work first (no DB held), then one short DB transaction on the DB thread."""
    if event.is_private or not event.is_group or not event.message.message:
        return False
    # Selected groups are cached by the manager: messages from other groups cost no DB call.
    if (user_id, event.chat_id) not in enabled:
        return False
    entity = await event.get_chat()
    if getattr(entity, 'broadcast', False) or not getattr(entity, 'megagroup', False):
        return False
    chat_id, message = event.chat_id, event.message
    return await store.run(lambda db: store_personal_message(db, user_id, chat_id, entity, message, config))


MANAGER_INTERVAL = 15
DISCONNECT_TIMEOUT = 10


def _log(text):
    print(text, flush=True)


def _report(exc, **tags):
    """Owner alert + Sentry for an unexpected manager failure; never breaks the caller."""
    try:
        from .observability import capture_exception
        capture_exception(exc, component='personal_monitors', **tags)
    except Exception:
        pass


def is_session_revoked(exc):
    """Only a revoked/unauthorized session disables a connection; everything else is retried."""
    from .telegram_monitor import is_unauthorized
    return isinstance(exc, TelegramAccountError) or is_unauthorized(exc)


async def _disconnect_quietly(client, user_id):
    try:
        await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)
    except Exception as exc:
        from .telegram_monitor import safe_error
        _log(f'Личное Telegram-подключение {user_id}: отключение не удалось ({safe_error(exc)}).')


async def serve_personal_client(client, user_id, on_ready=None):
    """One connection attempt. The client is always disconnected: on errors, revoked sessions and cancel."""
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise TelegramAccountError('Telegram-сессия отозвана.')
        if on_ready:
            try:
                await on_ready()
            except Exception as exc:  # bookkeeping only: a DB hiccup must not drop a live connection
                _log(f'Личное Telegram-подключение {user_id}: статус не обновлён ({type(exc).__name__}).')
        await client.run_until_disconnected()
    finally:
        await _disconnect_quietly(client, user_id)


async def keep_personal_connection(user_id, attempt, record_error, *, sleep=asyncio.sleep,
                                   clock=time.monotonic, log=_log):
    """Run attempt() forever with exponential backoff (5 s -> 5 min, reset after a long healthy run).

    A revoked session propagates (the manager then sets status='error'); network errors, disconnects and
    FloodWait only record last_error and retry, so the user never has to reconnect after a blip."""
    from .telegram_monitor import (HEALTHY_RUN_SECONDS, MAX_RETRY_SECONDS, MIN_RETRY_SECONDS,
                                   flood_wait_seconds, safe_error)
    delay = MIN_RETRY_SECONDS
    while True:
        started = clock()
        try:
            await attempt()
            error = None
        except Exception as exc:
            if is_session_revoked(exc):
                raise
            error = exc
        if clock()-started >= HEALTHY_RUN_SECONDS:
            delay = MIN_RETRY_SECONDS
        flood = flood_wait_seconds(error) if error is not None else None
        if flood is not None:
            wait = max(MIN_RETRY_SECONDS, flood+1)
        else:
            wait, delay = delay, min(delay*2, MAX_RETRY_SECONDS)
        reason = safe_error(error) if error is not None else 'соединение закрыто'
        log(f'Личное Telegram-подключение {user_id}: {reason}; переподключение через {wait} с.')
        try:
            await record_error(user_id, 'Временная ошибка соединения, переподключение: '
                               + (type(error).__name__ if error is not None else 'соединение закрыто'))
        except Exception as exc:
            log(f'Личное Telegram-подключение {user_id}: ошибка не сохранена ({type(exc).__name__}).')
        await sleep(wait)


async def run_forever(iteration, *, interval=MANAGER_INTERVAL, sleep=asyncio.sleep, log=_log, report=_report):
    """Call iteration() every `interval` seconds; an exception is logged and reported, never fatal."""
    while True:
        try:
            await iteration()
        except Exception as exc:
            from .telegram_monitor import safe_error
            log('Личные Telegram-подключения: сбой цикла менеджера (' + safe_error(exc) + '), повтор через '
                + str(interval) + ' с.')
            report(exc)
        await sleep(interval)


def run_personal_monitors(db_target, config, store=None):
    """Keep one event-only Telethon client per active encrypted connection."""
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    api_id, api_hash = telegram_credentials()
    cipher = SessionCipher(os.environ.get('TELEGRAM_SESSION_ENCRYPTION_KEY', ''))
    store = store or MonitorStore(db_target)
    enabled = set()

    def record_error(user_id, text):
        # updated_at is the manager's version stamp: touching it would restart the task and reset backoff.
        def work(db):
            db.execute('UPDATE telegram_connections SET last_error=? WHERE user_id=?', (text, user_id))
        return store.run(work)

    def clear_error(user_id):
        def work(db):
            db.execute("UPDATE telegram_connections SET last_error='' WHERE user_id=? AND last_error<>''", (user_id,))
        return store.run(work)

    async def connection_attempt(user_id, session_string):
        client = TelegramClient(StringSession(session_string), api_id, api_hash)

        @client.on(events.NewMessage(incoming=True))
        async def live(event):
            await handle_personal_message(event, user_id, store, enabled, config)

        await serve_personal_client(client, user_id, on_ready=lambda: clear_error(user_id))

    async def connection_task(user_id, session_string):
        await keep_personal_connection(user_id, lambda: connection_attempt(user_id, session_string), record_error)

    def mark_error(user_id, error):
        def work(db):
            db.execute("""UPDATE telegram_connections SET status='error',last_error=?,updated_at=now()
                WHERE user_id=?""", ('Соединение остановлено: ' + type(error).__name__, user_id))
        return store.run(work)

    tasks = {}

    async def sync_once():
        try:
            rows, selected = await store.run(_load_monitor_targets)
        except Exception as exc:
            # Database unavailable: keep the running clients and retry on the next cycle.
            _log('Личные Telegram-подключения: база недоступна (' + type(exc).__name__ + '), повтор через 15 с.')
            return
        enabled.clear()
        enabled.update(selected)
        wanted = {}
        for row in rows:
            try:
                wanted[row['user_id']] = (str(row['updated_at']), cipher.decrypt(row['session_cipher'])['session'])
            except TelegramAccountError:
                continue
        for user_id in list(tasks):
            if tasks[user_id][1].done():
                error = tasks[user_id][1].exception() if not tasks[user_id][1].cancelled() else None
                del tasks[user_id]
                if error:
                    try:
                        await mark_error(user_id, error)
                    except Exception as exc:
                        _log('Личные Telegram-подключения: статус ошибки не сохранён (' + type(exc).__name__ + ').')
                    wanted.pop(user_id, None)
        for user_id, (version, session_string) in wanted.items():
            current = tasks.get(user_id)
            if current and current[0] == version and not current[1].done():
                continue
            if current:
                current[1].cancel()
            task = asyncio.create_task(connection_task(user_id, session_string))
            tasks[user_id] = (version, task)
        for user_id in list(tasks):
            if user_id not in wanted:
                tasks[user_id][1].cancel()
                del tasks[user_id]

    try:
        asyncio.run(run_forever(sync_once))
    finally:
        store.close()
