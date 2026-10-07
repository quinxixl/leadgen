"""Explicit, durable Telegram replies shared by the web and bot interfaces."""
import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from .billing import subscription_active
from .telegram_accounts import SessionCipher, TelegramAccountError, TelethonGateway


MODES = {'dm': 'В личку автору', 'group': 'Ответом в группе'}
STATUSES = {'draft': 'Ожидает подтверждения', 'sending': 'Отправляется; не повторяйте отправку',
            'sent': 'Отправлено', 'failed': 'Не отправлено',
            'uncertain': 'Доставка не подтверждена — проверьте Telegram', 'cancelled': 'Отменено'}
LINK = re.compile(r'https://t\.me/(?:(?:s/)?([A-Za-z][A-Za-z0-9_]{3,})|c/([1-9][0-9]*))/([1-9][0-9]*)/?(?:\?[^#]*)?$')


def source_message(url):
    match = LINK.fullmatch(url or '')
    if not match:
        raise TelegramAccountError('Ответ доступен только для объявления со ссылкой на сообщение Telegram.')
    username, channel_id, message_id = match.groups()
    return username or int('-100' + channel_id), int(message_id)


class ReplyGateway(TelethonGateway):
    def _client(self, session):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        return TelegramClient(StringSession(session), self.api_id, self.api_hash,
                              request_retries=0, connection_retries=0, flood_sleep_threshold=0)

    async def _resolve(self, client, url, mode):
        from telethon import utils
        from telethon.tl.types import User
        source, message_id = source_message(url)
        if not await client.is_user_authorized():
            raise TelegramAccountError('Подключите Telegram-аккаунт заново в разделе «Аккаунт».')
        me = await client.get_me()
        if isinstance(source, int):
            # StringSession has no persistent entity cache for private groups.
            entity = None
            async for dialog in client.iter_dialogs():
                if utils.get_peer_id(dialog.entity) == source:
                    entity = dialog.entity
                    break
            if entity is None:
                raise TelegramAccountError('Ваш аккаунт не имеет доступа к исходной группе.')
        else:
            entity = await client.get_entity(source)
        message = await client.get_messages(entity, ids=message_id)
        if not message or not getattr(message, 'message', None):
            raise TelegramAccountError('Исходное объявление удалено или недоступно вашему аккаунту.')
        if mode == 'dm':
            recipient = await message.get_sender()
            if not isinstance(recipient, User) or recipient.bot or recipient.deleted:
                raise TelegramAccountError('Автор скрыт, пишет от имени канала или является ботом. Личный отклик недоступен.')
            if recipient.id == me.id:
                raise TelegramAccountError('Нельзя отправить отклик самому себе.')
            title = utils.get_display_name(recipient)
            if recipient.username:
                title += ' (@' + recipient.username + ')'
        else:
            if not getattr(entity, 'megagroup', False) or getattr(entity, 'broadcast', False):
                raise TelegramAccountError('Ответ в группе доступен для супергрупп. Комментарии к каналам пока не поддерживаются.')
            recipient, title = entity, entity.title
        info = {'account_id': me.id, 'sender': utils.get_display_name(me)[:200],
                'source_peer': utils.get_peer_id(entity), 'message_id': message_id,
                'recipient_id': utils.get_peer_id(recipient), 'recipient': title[:300]}
        return info, await client.get_input_entity(recipient)

    def preview(self, session, url, mode):
        try:
            return self._run(self._preview(session, url, mode))
        except TelegramAccountError:
            raise
        except Exception:
            raise TelegramAccountError('Не удалось проверить получателя. Проверьте доступ аккаунта к объявлению в Telegram.') from None

    async def _preview(self, session, url, mode):
        client = self._client(session)
        try:
            await client.connect()
            info, _ = await self._resolve(client, url, mode)
            return info
        finally:
            await client.disconnect()

    def send(self, session, url, mode, text, expected, random_id):
        # A timeout here is deliberately not translated into "try again". The budget
        # stays below gunicorn's 30 s worker timeout so the outcome is always recorded.
        awaitable = self._send(session, url, mode, text, expected, random_id)
        return asyncio.run(asyncio.wait_for(awaitable, timeout=self._timeout(awaitable)))

    async def _send(self, session, url, mode, text, expected, random_id):
        from telethon.errors import FloodWaitError, RPCError, ServerError
        from telethon.tl.functions.messages import SendMessageRequest
        from telethon.tl.types import InputReplyToMessage, InputPeerSelf
        client = self._client(session)
        try:
            await client.connect()
            actual, peer = await self._resolve(client, url, mode)
            if any(actual[key] != expected[key] for key in ('account_id', 'source_peer', 'message_id', 'recipient_id')):
                raise TelegramAccountError('Аккаунт или получатель изменился. Подготовьте отклик заново.')
            await client(SendMessageRequest(peer=peer, message=text, random_id=random_id,
                reply_to=InputReplyToMessage(actual['message_id']) if mode == 'group' else None,
                send_as=InputPeerSelf() if mode == 'group' else None, no_webpage=True))
        except FloodWaitError as exc:
            raise TelegramAccountError(f'Telegram ограничил отправку на {exc.seconds} сек. Сообщение не отправлено.') from None
        except ServerError:
            raise
        except RPCError:
            raise TelegramAccountError('Telegram отклонил отправку. Проверьте ограничения личных сообщений или права в группе.') from None
        finally:
            await client.disconnect()


def authorized_lead(db, actor_id, owner_id, lead_id):
    row = db.execute('''SELECT l.payload FROM leads l JOIN user_leads ul ON ul.lead_id=l.id
        JOIN app_users owner ON owner.id=ul.user_id
        JOIN app_users actor ON actor.id=?
        WHERE ul.user_id=? AND l.id=? AND ul.delivery_status<>'filtered'
          AND owner.status='active' AND actor.status='active'
          AND (actor.id=owner.id OR EXISTS (
            SELECT 1 FROM workspaces w JOIN workspace_members m ON m.workspace_id=w.id
            WHERE w.owner_user_id=owner.id AND m.user_id=actor.id AND m.role IN ('owner','admin','manager')))''',
        (actor_id, owner_id, lead_id)).fetchone()
    if not row:
        raise TelegramAccountError('Лид недоступен или у вас нет права отправлять отклики.')
    sub = db.execute('SELECT status,ends_at FROM subscriptions WHERE user_id=?', (owner_id,)).fetchone()
    if not subscription_active(sub):
        raise TelegramAccountError('Срок подписки закончился.')
    return json.loads(row['payload'])


def connection(db, actor_id):
    row = db.execute("SELECT * FROM telegram_connections WHERE user_id=? AND status='active' AND session_cipher<>''",
                     (actor_id,)).fetchone()
    if not row:
        raise TelegramAccountError('Подключите свой Telegram-аккаунт на сайте в разделе «Telegram».')
    return row


def prepare(db, actor_id, owner_id, lead_id, mode, text, cipher, gateway):
    text = (text or '').strip()
    if mode not in MODES or not text or len(text.encode('utf-16-le')) // 2 > 3000:
        raise TelegramAccountError('Выберите получателя и введите текст от 1 до 3000 символов.')
    lead = authorized_lead(db, actor_id, owner_id, lead_id)
    source_message(lead['url'])
    account = connection(db, actor_id)
    from .rate_limit import hit, RateLimited
    try:
        hit(db, 'telegram-reply-preview', actor_id, 20, 60)
    except RateLimited as exc:
        raise TelegramAccountError(str(exc)) from None
    info = gateway.preview(cipher.decrypt(account['session_cipher'])['session'], lead['url'], mode)
    if info['account_id'] != account['telegram_user_id']:
        raise TelegramAccountError('Аккаунт изменился. Подключите Telegram заново.')
    token = secrets.token_hex(16)
    with db:
        row = db.execute('''INSERT INTO telegram_replies
            (id,actor_id,owner_id,lead_id,mode,body,source_url,target_cipher,sender_label,recipient_label,
             account_id,random_id,expires_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *''',
            (token, actor_id, owner_id, lead_id, mode, text, lead['url'], cipher.encrypt(info),
             info['sender'], info['recipient'], info['account_id'], secrets.randbits(63) or 1,
             datetime.now(timezone.utc) + timedelta(minutes=15))).fetchone()
    return row


STUCK_ERROR = 'Отправка была прервана. Проверьте переписку в Telegram перед новым откликом. Автоматического повтора не будет.'


def recover_stuck(db):
    """A process killed mid-send leaves 'sending'; surface it as 'uncertain', never resend."""
    with db:
        db.execute("""UPDATE telegram_replies SET status='uncertain',error=?,updated_at=now()
            WHERE status='sending' AND updated_at<now()-interval '5 minutes'""", (STUCK_ERROR,))


def get_reply(db, token, actor_id, owner_id):
    recover_stuck(db)
    row = db.execute('SELECT * FROM telegram_replies WHERE id=? AND actor_id=? AND owner_id=?',
                     (token, actor_id, owner_id)).fetchone()
    if not row:
        raise TelegramAccountError('Отклик не найден.')
    authorized_lead(db, actor_id, owner_id, row['lead_id'])
    return row


def confirm(db, token, actor_id, owner_id, cipher, gateway):
    row = get_reply(db, token, actor_id, owner_id)
    if row['status'] != 'draft':
        return row
    if row['expires_at'] <= datetime.now(timezone.utc):
        raise TelegramAccountError('Подтверждение истекло. Подготовьте отклик заново.')
    account = connection(db, actor_id)
    if account['telegram_user_id'] != row['account_id']:
        raise TelegramAccountError('Аккаунт изменился. Подготовьте отклик заново.')
    session = cipher.decrypt(account['session_cipher'])['session']
    expected = cipher.decrypt(row['target_cipher'])
    # Commit the single-use claim BEFORE any network write. Concurrent clicks and
    # crashed processes must never cause an automatic second send.
    with db:
        claimed = db.execute("""UPDATE telegram_replies SET status='sending',updated_at=now()
            WHERE id=? AND status='draft' AND expires_at>now() RETURNING id""", (token,)).fetchone()
    if not claimed:
        return get_reply(db, token, actor_id, owner_id)
    try:
        gateway.send(session, row['source_url'], row['mode'], row['body'], expected, row['random_id'])
        status, error = 'sent', ''
    except TelegramAccountError as exc:
        status, error = 'failed', str(exc)
    except Exception:
        status, error = 'uncertain', 'Проверьте переписку в Telegram перед новым откликом. Автоматического повтора не будет.'
    with db:
        db.execute('UPDATE telegram_replies SET status=?,error=?,updated_at=now() WHERE id=?', (status, error, token))
        if status == 'sent':
            db.execute("""UPDATE user_leads SET pipeline_status='contacted',updated_at=now()
                WHERE user_id=? AND lead_id=? AND pipeline_status IN ('saved','viewed','working')""", (owner_id, row['lead_id']))
            db.execute("INSERT INTO lead_activity(user_id,lead_id,kind,detail) VALUES(?,?,'note',?)",
                       (owner_id, row['lead_id'], 'Telegram: ' + MODES[row['mode']] + '. От: ' + row['sender_label'] +
                        '. Кому: ' + row['recipient_label'] + '\n' + row['body']))
    return get_reply(db, token, actor_id, owner_id)


def cancel(db, token, actor_id, owner_id):
    get_reply(db, token, actor_id, owner_id)
    with db:
        db.execute("UPDATE telegram_replies SET status='cancelled',updated_at=now() WHERE id=? AND status='draft'", (token,))
    return get_reply(db, token, actor_id, owner_id)
