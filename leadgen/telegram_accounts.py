"""Encrypted personal Telegram sessions and explicit group selection."""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken


PHONE = re.compile(r'^\+[1-9]\d{7,14}$')
AUTH_TTL = timedelta(minutes=10)


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

    def _run(self, awaitable):
        try:
            return asyncio.run(asyncio.wait_for(awaitable, timeout=30))
        except TimeoutError:
            raise TelegramAccountError('Telegram не ответил за 30 секунд. Повторите попытку.') from None

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
        from telethon.errors import PasswordHashInvalidError
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(state['session']), self.api_id, self.api_hash)
        try:
            await client.connect()
            try:
                user = await client.sign_in(password=password)
            except PasswordHashInvalidError:
                raise TelegramAccountError('Неверный облачный пароль Telegram.') from None
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


def auth_record(db, user_id, cipher):
    row = db.execute('SELECT * FROM telegram_connection_auth WHERE user_id=?', (user_id,)).fetchone()
    if not row or row['expires_at'] <= datetime.now(timezone.utc) or row['attempts'] >= 5:
        raise TelegramAccountError('Время подключения истекло. Запросите новый код.')
    return row, cipher.decrypt(row['state_cipher'])


def begin_connection(db, user_id, phone, cipher, gateway):
    if not PHONE.fullmatch(phone):
        raise TelegramAccountError('Введите номер в международном формате, например +79991234567.')
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
    state = gateway.resend(state)
    with db:
        db.execute('''UPDATE telegram_connection_auth SET state_cipher=?,expires_at=?,updated_at=now()
            WHERE user_id=?''', (cipher.encrypt(state), datetime.now(timezone.utc) + AUTH_TTL, user_id))
    return state.get('delivery', 'через Telegram')


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


def run_personal_monitors(db_target, config):
    """Keep one event-only Telethon client per active encrypted connection."""
    from .app import db_open, ingest
    from .core import Lead, UTC
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    api_id, api_hash = telegram_credentials()
    cipher = SessionCipher(os.environ.get('TELEGRAM_SESSION_ENCRYPTION_KEY', ''))

    async def connection_task(user_id, session_string):
        client = TelegramClient(StringSession(session_string), api_id, api_hash)

        @client.on(events.NewMessage(incoming=True))
        async def live(event):
            if event.is_private or not event.is_group or not event.message.message:
                return
            live_db = db_open(db_target)
            try:
                source = live_db.execute('''SELECT title,username FROM user_telegram_dialogs
                    WHERE user_id=? AND peer_id=? AND enabled=true''',
                    (user_id, event.chat_id)).fetchone()
                if not source:
                    return
                entity = await event.get_chat()
                if getattr(entity, 'broadcast', False) or not getattr(entity, 'megagroup', False):
                    return
                text = event.message.message.strip()
                title = next((line.strip() for line in text.splitlines() if len(line.strip()) >= 8), text)[:220]
                lead = Lead(source='Telegram · ' + source['title'],
                            url=personal_message_link(entity, event.message.id), title=title, text=text,
                            published=event.message.date.astimezone(UTC).isoformat(), budget_text=text)
                with live_db:
                    ingest(live_db, lead, config, origin_user_id=user_id)
                    live_db.execute('''UPDATE user_telegram_dialogs SET last_message_at=now(),updated_at=now()
                        WHERE user_id=? AND peer_id=?''', (user_id, event.chat_id))
            finally:
                live_db.close()

        await client.connect()
        if not await client.is_user_authorized():
            raise TelegramAccountError('Telegram-сессия отозвана.')
        await client.run_until_disconnected()

    async def manager():
        tasks = {}
        while True:
            live_db = db_open(db_target)
            try:
                rows = live_db.execute("""SELECT user_id,session_cipher,updated_at FROM telegram_connections
                    WHERE status='active' AND session_cipher<>''""").fetchall()
            finally:
                live_db.close()
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
                        error_db = db_open(db_target)
                        try:
                            with error_db:
                                error_db.execute("""UPDATE telegram_connections SET status='error',last_error=?,updated_at=now()
                                    WHERE user_id=?""", ('Соединение остановлено: ' + type(error).__name__, user_id))
                        finally:
                            error_db.close()
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
            await asyncio.sleep(15)

    asyncio.run(manager())
