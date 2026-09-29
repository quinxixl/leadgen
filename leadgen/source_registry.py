"""Import and verify public Telegram sources selected from the workbook analysis."""
import concurrent.futures
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError

from bs4 import BeautifulSoup

from .adapters import fetch
from .core import UTC

PUBLIC_USERNAME = re.compile(r'^https://t\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$', re.I)
NUMBER = re.compile(r'([\d\s.,]+)\s*(members?|subscribers?|участник\w*|подписчик\w*)', re.I)
ONLINE = re.compile(r'([\d\s.,]+)\s*(?:online|онлайн)', re.I)
DISQUALIFY = re.compile(r'Возможно сообщество отдельн|потребительск.{0,20}аудитори|Преимущественно вакансии|Обучение или материалы',re.I)


def _number(value):
    value = value.replace('\xa0','').replace(' ','').replace(',','').replace('.','')
    return int(value) if value.isdigit() else None


def import_selection(db, path: Path):
    data = json.loads(path.read_text())
    accepted = data['selected']
    counts = {'imported':0,'unsupported_links':0,'potential':0,'partners':0,'review':0}
    with db:
        for item in accepted:
            match = PUBLIC_USERNAME.match(item['url'])
            if not match:
                counts['unsupported_links'] += 1
                continue
            username = match.group(1).lower()
            metadata = {'services':item.get('services',''),'offer':item.get('offer',''),
                        'risks':item.get('risks',''),'source_rows':item.get('sources',[])}
            db.execute('''INSERT INTO telegram_sources
                (username,url,title,segment,priority,selection_group,original_members,status,metadata)
                VALUES(?,?,?,?,?,?,?,'pending',?)
                ON CONFLICT(username) DO UPDATE SET title=excluded.title,segment=excluded.segment,
                priority=excluded.priority,selection_group=excluded.selection_group,
                original_members=excluded.original_members,metadata=excluded.metadata''',
                (username,'https://t.me/'+match.group(1),item['title'],item['segment'],item['priority'],
                 item['sheet'],item.get('members'),json.dumps(metadata,ensure_ascii=False)))
            counts['imported'] += 1
            bucket='potential' if item['sheet']=='Потенциальные клиенты' else 'partners' if item['sheet']=='Партнеры и заказы' else 'review'
            counts[bucket] += 1
    return counts


def monitor_allowed(selection_group,metadata):
    if selection_group in ('Потенциальные клиенты','Партнеры и заказы'):
        return True
    try: risks=json.loads(metadata or '{}').get('risks','')
    except (TypeError,json.JSONDecodeError): return False
    return not DISQUALIFY.search(risks)


def apply_monitor_policy(db):
    rows=db.execute("SELECT username,selection_group,metadata,status FROM telegram_sources").fetchall(); enabled=0
    with db:
        for row in rows:
            username,group,metadata,status=(row['username'],row['selection_group'],row['metadata'],row['status'])
            allow=status=='active' and monitor_allowed(group,metadata)
            db.execute('UPDATE telegram_sources SET enabled=? WHERE username=?',(int(allow),username));enabled+=int(allow)
    return enabled


def inspect_public_page(username):
    url = 'https://t.me/'+username
    try:
        body = fetch(url)
    except HTTPError as exc:
        return {'status':'retry' if exc.code in (429,500,502,503,504) else 'unavailable','reason':f'HTTP {exc.code}'}
    except (URLError,TimeoutError) as exc:
        return {'status':'retry','reason':type(exc).__name__}
    soup = BeautifulSoup(body,'html.parser')
    title_node = soup.select_one('.tgme_page_title')
    extra_node = soup.select_one('.tgme_page_extra')
    action = ' '.join(x.get_text(' ',strip=True) for x in soup.select('.tgme_action_button_new, .tgme_page_action'))
    title = title_node.get_text(' ',strip=True) if title_node else ''
    extra = extra_node.get_text(' ',strip=True) if extra_node else ''
    members_m, online_m = NUMBER.search(extra), ONLINE.search(extra)
    members = _number(members_m.group(1)) if members_m else None
    online = _number(online_m.group(1)) if online_m else None
    lower = (action+' '+extra).casefold()
    is_group = bool(members_m and 'subscriber' not in members_m.group(2).casefold() and 'подписчик' not in members_m.group(2).casefold())
    is_channel = bool(members_m and not is_group)
    page_text=body.decode('utf-8','ignore').casefold()
    if 'username is not occupied' in page_text:
        return {'status':'unavailable','reason':'страница не существует'}
    if not title and 'send message' in lower:
        return {'status':'not_chat','reason':'ссылка ведёт на личный аккаунт'}
    if not title:
        return {'status':'retry','reason':'Telegram не вернул данные страницы'}
    if not is_group and not is_channel:
        return {'status':'not_chat','reason':'ссылка ведёт не на публичную группу или канал','title':title}
    result = {'title':title,'members':members,'online':online,'last_message_at':None}
    if is_group and online is not None and online > 0 and (members or 0) >= 50:
        return result | {'status':'active','reason':f'{online} онлайн при проверке'}
    if is_group:
        return result | {'status':'inactive','reason':'нет подтверждённой онлайн-аудитории или менее 50 участников'}
    # Public channels expose recent posts in /s/. Use the latest visible timestamp.
    try:
        preview = BeautifulSoup(fetch('https://t.me/s/'+username),'html.parser')
        times = [x.get('datetime') for x in preview.select('time[datetime]') if x.get('datetime')]
        latest = max(times) if times else None
        result['last_message_at'] = latest
        if latest and datetime.fromisoformat(latest) >= datetime.now(UTC)-timedelta(days=30):
            return result | {'status':'active','reason':'публикация за последние 30 дней'}
        return result | {'status':'inactive','reason':'нет публикаций за последние 30 дней'}
    except Exception as exc:
        return result | {'status':'retry','reason':type(exc).__name__}


def verify_pending(db, workers=14, limit=None):
    query = "SELECT username FROM telegram_sources WHERE status IN ('pending','retry','unavailable') ORDER BY priority,original_members DESC"
    rows = [(r['username'],r['selection_group']) for r in db.execute(query.replace('SELECT username','SELECT username,selection_group') + (" LIMIT ?" if limit else ''),(limit,) if limit else ())]
    counts = {}
    def check(row):
        username,selection_group=row
        result=inspect_public_page(username)
        time.sleep(.5)
        return username,selection_group,result
    started=time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for i,(username,selection_group,result) in enumerate(pool.map(check,rows),1):
            status=result['status']; now=datetime.now(UTC).isoformat()
            with db:
                db.execute('''UPDATE telegram_sources SET title=COALESCE(?,title),members=?,online=?,
                    status=?,enabled=?,checked_at=?,last_message_at=?,check_reason=? WHERE username=?''',
                    (result.get('title'),result.get('members'),result.get('online'),status,
                     int(status=='active' and selection_group in ('Потенциальные клиенты','Партнеры и заказы')),
                     now,result.get('last_message_at'),result['reason'],username))
            counts[status]=counts.get(status,0)+1
            if i%250==0:
                print(json.dumps({'checked':i,'total':len(rows),'counts':counts},ensure_ascii=False),flush=True)
    return {'checked':len(rows),'counts':counts,'enabled':apply_monitor_policy(db),'seconds':round(time.monotonic()-started,1)}


def registry_stats(db):
    return {r['status']:r['total'] for r in db.execute('SELECT status,count(*) AS total FROM telegram_sources GROUP BY status')}
