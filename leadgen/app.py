"""python -m leadgen.app scan|run|status|setup. Sending requires --send."""
import argparse
import getpass
import json
import os
import re
import socket
import ssl
from pathlib import Path
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from .adapters import collect, fetch, parse
from .core import Lead, UTC, classify, fingerprint, message, enrich
from .database import db_open, database_url

ROOT = Path(__file__).resolve().parent

def env_load():
    path = ROOT.parent / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k,v = line.split('=',1)
                os.environ.setdefault(k.strip(),v.strip())

def telegram(method, payload):
    token = os.environ.get('TELEGRAM_BOT_TOKEN','')
    if not token:
        raise ValueError('TELEGRAM_BOT_TOKEN is missing')
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', token):
        raise ValueError('Неверный формат токена: вставьте только токен BotFather, без кавычек и пробелов')
    try:
        data = json.loads(fetch('https://api.telegram.org/bot'+token+'/'+method,
                          json.dumps(payload).encode(),{'Content-Type':'application/json'}))
    except HTTPError as exc:
        # Do not log the URL: it contains the token.
        raise RuntimeError(f'Telegram HTTP {exc.code}') from None
    except Exception as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        if isinstance(reason, ssl.SSLCertVerificationError):
            detail = 'не удалось проверить SSL-сертификат; проверьте SSL_CERT_FILE и доверенные сертификаты Python'
        elif isinstance(reason, socket.gaierror):
            detail = 'DNS не нашёл api.telegram.org; проверьте интернет и настройки DNS'
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            detail = 'истекло время ожидания соединения; проверьте доступ к api.telegram.org'
        elif isinstance(reason, ConnectionError):
            detail = 'соединение разорвано или отклонено; проверьте доступ к api.telegram.org'
        elif isinstance(exc, json.JSONDecodeError):
            detail = 'получен ответ не в формате Telegram API; проверьте прокси или сетевой фильтр'
        else:
            # Exception text can contain a URL with the token. Only expose the type.
            detail = 'ошибка соединения (' + type(reason).__name__ + ')'
        suffix = '; результат отправки неизвестен' if method == 'sendMessage' else '; настройка не завершена'
        raise RuntimeError('Telegram: ' + detail + suffix) from None
    if not data.get('ok'):
        raise RuntimeError('Telegram rejected request')
    return data['result']

def ingest(db, lead, config):
    status,reason = classify_for_config(lead,config)
    fp = fingerprint(lead)
    existing = db.execute('SELECT status FROM leads WHERE url=?',(lead.url,)).fetchone()
    if existing and existing['status'] in ('sent','uncertain','sending'):
        return
    duplicate = db.execute("SELECT url FROM leads WHERE fingerprint=? AND url<>? AND status IN ('ready','sent','sending','uncertain') LIMIT 1",(fp,lead.url)).fetchone()
    if duplicate:
        status,reason = 'duplicate','совпадает с '+duplicate['url']
    db.execute('''INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen)
                  VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                  fingerprint=excluded.fingerprint,payload=excluded.payload,
                  status=excluded.status,reason=excluded.reason''',
               (lead.url,fp,json.dumps(lead.data(),ensure_ascii=False),status,reason,datetime.now(UTC).isoformat()))

def classify_for_config(lead,config):
    enrich(lead,config['min_budget'])
    status, reason, _ = classify(lead,config['min_budget'],config['max_age_hours'])
    if (config.get('notify_without_budget') and status == 'review' and
            reason in ('бюджет не указан','указан только потолок бюджета') and lead.score >= 45):
        status, reason = 'ready', f'{lead.temperature} лид; бюджет нужно уточнить'
    status, reason = apply_preferences(lead, config, status, reason)
    return status,reason

def scan(db, config, fixtures=False):
    for source in config['sources']:
        if not source['enabled']:
            continue
        try:
            if fixtures:
                fixture = 'fl_page' if source['id'] == 'fl' else source['id']
                leads = parse(source,(ROOT/'research'/(fixture+'.html')).read_bytes())
            else:
                leads = collect(source)
            with db:
                for lead in leads:
                    ingest(db,lead,config)
                db.execute('''INSERT INTO health(source,checked,ok,count,error) VALUES(?,?,?,?,?)
                    ON CONFLICT(source) DO UPDATE SET checked=excluded.checked,ok=excluded.ok,
                    count=excluded.count,error=excluded.error''',
                    (source['name'],datetime.now(UTC).isoformat(),1,len(leads),None))
            print(f"{source['name']}: прочитано {len(leads)}",flush=True)
        except Exception as exc:
            with db:
                db.execute('''INSERT INTO health(source,checked,ok,count,error) VALUES(?,?,?,?,?)
                    ON CONFLICT(source) DO UPDATE SET checked=excluded.checked,ok=excluded.ok,
                    count=excluded.count,error=excluded.error''',
                    (source['name'],datetime.now(UTC).isoformat(),0,0,type(exc).__name__))
            print(f"{source['name']}: ошибка {type(exc).__name__}; см. status",flush=True)

def apply_preferences(lead, config, status, reason):
    from .core import topics
    if 'topics' in config and not set(topics(lead.title+' '+lead.text)).intersection(config['topics']):
        return 'rejected', 'направление выключено'
    enabled = {s['name'] for s in config.get('sources',[]) if s['enabled']}
    if 'sources' in config and lead.source not in enabled:
        return 'rejected', 'источник выключен'
    return status, reason

def send_lead(lead, reason, chat_id, sender=telegram):
    return sender('sendMessage',{'chat_id':chat_id,'text':message(lead,reason),
        'link_preview_options':{'is_disabled':True},
        'reply_markup':{'inline_keyboard':[[{'text':'Открыть оригинал','url':lead.url}]]}})

def notification_chat_ids(db=None):
    if db and db.backend=='postgres':
        rows=db.execute('SELECT chat_id FROM notification_recipients WHERE enabled=true ORDER BY can_manage DESC,created_at').fetchall()
        if rows:return [str(row['chat_id']) for row in rows]
    values = [os.environ.get('TELEGRAM_CHAT_ID','')]
    values += re.split(r'[\s,;]+',os.environ.get('TELEGRAM_NOTIFY_CHAT_IDS','').strip())
    result=[]
    for value in values:
        if not value:
            continue
        if not re.fullmatch(r'-?[0-9]+',value):
            raise ValueError('TELEGRAM_NOTIFY_CHAT_IDS должен содержать Telegram ID через запятую')
        if value not in result:
            result.append(value)
    return result

def deliver(db, config, sender=telegram, limit=20):
    chat_ids = notification_chat_ids(db)
    if not chat_ids:
        raise ValueError('TELEGRAM_CHAT_ID is missing; run setup')
    # An interrupted in-flight request cannot safely be assumed unsent.
    with db:
        db.execute("UPDATE leads SET status='uncertain', reason='Сбой во время отправки; проверьте Telegram' WHERE status='sending'")
    rows = db.execute("SELECT * FROM leads WHERE status='ready' AND next_attempt<=? ORDER BY first_seen LIMIT ?",(time.time(),limit)).fetchall()
    for row in rows:
        lead = Lead(**json.loads(row['payload']))
        status,reason = classify_for_config(lead,config)
        if status != 'ready':
            with db:
                db.execute('UPDATE leads SET status=?,reason=? WHERE url=?',(status,reason,lead.url))
            continue
        with db:
            db.execute("UPDATE leads SET status='sending', attempts=attempts+1 WHERE url=?",(lead.url,))
        try:
            for chat_id in chat_ids:
                send_lead(lead,reason,chat_id,sender)
        except RuntimeError as exc:
            retry = 'HTTP 429' in str(exc)
            with db:
                db.execute('UPDATE leads SET status=?,reason=?,next_attempt=? WHERE url=?',
                           ('ready' if retry else 'uncertain',str(exc),time.time()+min(3600,300*2**min(row['attempts'],4)),lead.url))
            print('Отправка не завершена; состояние сохранено. Проверьте status.',flush=True)
            break
        else:
            with db:
                db.execute("UPDATE leads SET status='sent', sent_at=? WHERE url=?",(datetime.now(UTC).isoformat(),lead.url))
            time.sleep(1.1)

def status(db):
    counts = {row['status']:row['total'] for row in db.execute('SELECT status,count(*) AS total FROM leads GROUP BY status')}
    registry = {row['status']:row['total'] for row in db.execute('SELECT status,count(*) AS total FROM telegram_sources GROUP BY status')}
    enabled = db.execute('SELECT count(*) AS total FROM telegram_sources WHERE enabled=1').fetchone()['total']
    print(json.dumps({'counts':counts,'sources':[dict(r) for r in db.execute('SELECT * FROM health')],
                      'telegram_registry':registry,'telegram_enabled':enabled},ensure_ascii=False,indent=2))

def setup():
    path = ROOT.parent/'.env'
    if path.exists():
        raise ValueError('.env already exists; edit it locally to change settings')
    token = getpass.getpass('Токен от @BotFather (ввод скрыт): ').strip()
    os.environ['TELEGRAM_BOT_TOKEN'] = token
    me = telegram('getMe',{})
    print('Откройте https://t.me/'+me['username']+' и отправьте /start.')
    input('После этого нажмите Enter: ')
    updates = telegram('getUpdates',{'timeout':0,'limit':100})
    chats = {}
    for update in updates:
        msg = update.get('message',{})
        chat = msg.get('chat',{})
        if chat.get('type') == 'private':
            chats[str(chat['id'])] = chat.get('username') or chat.get('first_name','')
    for cid,name in chats.items():
        print(cid,name)
    chosen = input('Введите ID своего чата из списка: ').strip()
    if chosen not in chats:
        raise ValueError('Chat not in list; send /start and repeat setup')
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:
        f.write('TELEGRAM_BOT_TOKEN='+token+'\nTELEGRAM_CHAT_ID='+chosen+'\nTELEGRAM_NOTIFY_CHAT_IDS=\n')
    print('Подключение сохранено в .env. Уведомления пока не запущены.')

def main():
    env_load()
    parser = argparse.ArgumentParser(description='IT-лиды от 5 000 ₽')
    parser.add_argument('command',choices=['scan','run','bot','status','export','setup','import-sources','verify-sources','setup-telegram','monitor-telegram'])
    parser.add_argument('--send',action='store_true',help='Отправлять подходящие объявления в ваш Telegram')
    parser.add_argument('--fixtures',action='store_true',help='Проверка на ранее сохранённых страницах, без сети')
    parser.add_argument('--db',type=Path,help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fixtures and (args.send or args.command in ('run','bot')):
        parser.error('Fixtures support only scan without sending')
    if args.command == 'setup':
        setup(); return
    if args.command == 'setup-telegram':
        from .telegram_monitor import setup_session
        setup_session(); return
    config = json.loads((ROOT/'sources.json').read_text())
    if config['poll_seconds'] < 60 or config['min_budget'] < 0:
        raise ValueError('Invalid config')
    if (args.send or args.command=='bot') and not all(os.environ.get(k) for k in ('TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID')):
        parser.error('Сначала выполните setup для подключения бота')
    db = db_open(args.db if args.db else database_url())
    try:
        if args.command in ('scan','run','bot') and db.backend=='postgres':
            locked=db.execute('SELECT pg_try_advisory_lock(734297154) AS locked').fetchone()['locked']
            if not locked:raise ValueError('Бот уже запущен в другом процессе')
        if args.command == 'import-sources':
            from .source_registry import import_selection
            source = ROOT.parent/'outputs'/'chat-selection-20260928'/'data.json'
            print(json.dumps(import_selection(db,source),ensure_ascii=False,indent=2)); return
        if args.command == 'verify-sources':
            from .source_registry import verify_pending
            print(json.dumps(verify_pending(db),ensure_ascii=False,indent=2)); return
        if args.command == 'monitor-telegram':
            from .telegram_monitor import run_monitor
            run_monitor(db.target,config); return
        if args.command == 'bot':
            from .bot import run_bot
            run_bot(db,config)
            return
        if args.command == 'status':
            status(db); return
        if args.command == 'export':
            for row in db.execute("SELECT payload,status,reason FROM leads WHERE status IN ('ready','review','sent','uncertain')"):
                print(json.dumps({**json.loads(row['payload']),'status':row['status'],'reason':row['reason']},ensure_ascii=False))
            return
        while True:
            scan(db,config,args.fixtures)
            if args.send:
                deliver(db,config)
            status(db)
            if args.command == 'scan':
                break
            time.sleep(config['poll_seconds'])
    finally:
        db.close()

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Остановлено.')
    except Exception as exc:
        # No traceback or secret-bearing request URLs in user-facing logs.
        print(f'Ошибка: {type(exc).__name__}: {exc}' if isinstance(exc,(ValueError,RuntimeError)) else f'Ошибка: {type(exc).__name__}')
        raise SystemExit(1)
