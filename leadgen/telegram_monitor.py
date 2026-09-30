"""Read new messages from joined public Telegram groups in real time."""
import asyncio
import getpass
import os
import re
from datetime import datetime
from pathlib import Path

from .core import Lead, UTC

ROOT = Path(__file__).resolve().parent


def _telethon():
    try:
        from telethon import TelegramClient
        return TelegramClient
    except ImportError:
        raise ValueError('Установите зависимости: pip install -r requirements.txt') from None


def credentials():
    try:
        api_id = int(os.environ.get('TELEGRAM_API_ID',''))
    except ValueError:
        api_id = 0
    api_hash = os.environ.get('TELEGRAM_API_HASH','').strip()
    phone = os.environ.get('TELEGRAM_PHONE','').strip()
    if not api_id or not api_hash or not phone:
        raise ValueError('Добавьте TELEGRAM_API_ID, TELEGRAM_API_HASH и TELEGRAM_PHONE в .env')
    return api_id,api_hash,phone


def setup_session():
    TelegramClient = _telethon()
    try:
        api_id,api_hash,phone=credentials()
    except ValueError:
        api_id_text=input('Telegram API ID с my.telegram.org: ').strip()
        api_hash=getpass.getpass('Telegram API Hash (ввод скрыт): ').strip()
        phone=input('Телефон аккаунта в международном формате: ').strip()
        if not api_id_text.isdigit() or not re.fullmatch(r'[0-9a-fA-F]{32}',api_hash) or not re.fullmatch(r'\+\d{8,15}',phone):
            raise ValueError('Неверный API ID, API Hash или номер телефона')
        api_id=int(api_id_text)
        env_path=ROOT.parent/'.env'; lines=env_path.read_text().splitlines() if env_path.exists() else []
        values={'TELEGRAM_API_ID':str(api_id),'TELEGRAM_API_HASH':api_hash,'TELEGRAM_PHONE':phone}
        found=set(); output=[]
        for line in lines:
            key=line.split('=',1)[0].strip() if '=' in line else ''
            if key in values: output.append(key+'='+values[key]);found.add(key)
            else: output.append(line)
        output += [key+'='+value for key,value in values.items() if key not in found]
        env_path.write_text('\n'.join(output)+'\n');os.chmod(env_path,0o600)
        os.environ.update(values)
    session=str(ROOT/'data'/'telegram_reader')
    async def login():
        client=TelegramClient(session,api_id,api_hash)
        await client.start(phone=phone)
        me=await client.get_me()
        print(f'Сессия Telegram подключена: {me.first_name or "аккаунт"}.')
        await client.disconnect()
    asyncio.run(login())


def message_link(username,message_id):
    return f'https://t.me/{username}/{message_id}'


def to_lead(row,msg):
    text=(msg.message or '').strip()
    title=next((x.strip() for x in text.splitlines() if len(x.strip())>=8),text)[:220]
    return Lead(source='Telegram: '+row['title'],url=message_link(row['username'],msg.id),
                title=title,text=text,published=msg.date.astimezone(UTC).isoformat(),budget_text=text)


def realtime_group_username(event,chat):
    """Return a public group username; reject users, private chats and channels."""
    if not getattr(event,'is_group',False) or getattr(event,'is_private',False):
        return None
    if getattr(chat,'broadcast',False):
        return None
    username=(getattr(chat,'username',None) or '').strip().lower()
    return username or None


def run_monitor(db_path,config):
    """Handle only new-message events; never poll chat history or resolve the source list."""
    from .app import db_open, ingest, env_load
    env_load(); TelegramClient=_telethon();api_id,api_hash,_=credentials()
    db_target=db_path
    session=str(ROOT/'data'/'telegram_reader')
    async def monitor():
        client=TelegramClient(session,api_id,api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError('Сначала выполните setup-telegram')
        print('Мониторинг Telegram запущен в реальном времени без чтения истории.',flush=True)
        from telethon import events
        @client.on(events.NewMessage(incoming=True))
        async def live(event):
            chat=event.chat;username=realtime_group_username(event,chat)
            if not username:return
            live_db=db_open(db_target)
            try:
                row=live_db.execute("SELECT * FROM telegram_sources WHERE username=? AND enabled=1 AND status='active'",(username,)).fetchone()
                if row and event.message.message:
                    with live_db:
                        ingest(live_db,to_lead(row,event.message),config)
                        live_db.execute('UPDATE telegram_sources SET last_message_id=?,last_message_at=?,check_reason=? WHERE username=?',
                                        (event.message.id,datetime.now(UTC).isoformat(),'получено в реальном времени',username))
            finally:
                live_db.close()
        await client.run_until_disconnected()
    asyncio.run(monitor())
