"""Read public Telegram messages through the user's MTProto session."""
import asyncio
import getpass
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .core import Lead, UTC

ROOT = Path(__file__).resolve().parent


def _telethon():
    try:
        from telethon import TelegramClient
        from telethon.errors import FloodWaitError
        return TelegramClient, FloodWaitError
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
    TelegramClient,_ = _telethon()
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


def run_monitor(db_path,config):
    """Cycle through active public sources. Joined chats also arrive in their normal history."""
    from .app import db_open, ingest, env_load
    env_load(); TelegramClient,FloodWaitError=_telethon();api_id,api_hash,_=credentials()
    db_target=db_path
    delay=max(1.0,float(config.get('telegram_source_delay_seconds',2.0)))
    session=str(ROOT/'data'/'telegram_reader')
    async def monitor():
        client=TelegramClient(session,api_id,api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError('Сначала выполните setup-telegram')
        print('Мониторинг Telegram запущен.',flush=True)
        from telethon import events
        @client.on(events.NewMessage)
        async def live(event):
            chat=await event.get_chat();username=(getattr(chat,'username',None) or '').lower()
            if not username:return
            live_db=db_open(db_target)
            row=live_db.execute("SELECT * FROM telegram_sources WHERE username=? AND enabled=1 AND status='active'",(username,)).fetchone()
            if row and event.message.message:
                with live_db:
                    ingest(live_db,to_lead(row,event.message),config)
                    live_db.execute('UPDATE telegram_sources SET last_message_id=?,last_message_at=?,check_reason=? WHERE username=?',
                                    (event.message.id,datetime.now(UTC).isoformat(),'получено в реальном времени',username))
            live_db.close()
        while True:
            db=db_open(db_target)
            rows=db.execute("SELECT * FROM telegram_sources WHERE enabled=1 AND status='active' ORDER BY priority,online DESC,members DESC").fetchall()
            for row in rows:
                try:
                    entity=await client.get_entity(row['username'])
                    messages=await client.get_messages(entity,limit=20,min_id=row['last_message_id'])
                    newest=row['last_message_id']
                    with db:
                        for msg in reversed(messages):
                            newest=max(newest,msg.id)
                            if msg.message:
                                ingest(db,to_lead(row,msg),config)
                        db.execute('UPDATE telegram_sources SET last_message_id=?,last_message_at=?,check_reason=? WHERE username=?',
                                   (newest,datetime.now(UTC).isoformat(),'сообщения прочитаны',row['username']))
                except FloodWaitError as exc:
                    print(f'Telegram ограничил частоту на {exc.seconds} с.',flush=True)
                    await asyncio.sleep(exc.seconds+1)
                except Exception as exc:
                    with db:
                        db.execute('UPDATE telegram_sources SET check_reason=? WHERE username=?',(type(exc).__name__,row['username']))
                await asyncio.sleep(delay)
            db.close()
            if not rows:
                await asyncio.sleep(60)
    asyncio.run(monitor())
