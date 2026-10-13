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
# Public chats the shared reader account is subscribed to. The reader receives messages only from
# these chats, so they are the only enabled sources; the rest of the imported catalog is never read.
JOINED = 'joined'
JOINED_REASON = 'подписан служебный аккаунт'
NOT_JOINED_REASON = 'служебный аккаунт не подписан'


def _metadata(value):
    try: data = json.loads(value or '{}')
    except (TypeError, ValueError): return {}
    return data if isinstance(data, dict) else {}


def joined_metadata(value, kind='group'):
    """Metadata with the joined marker; returns the same string when it is already marked."""
    data = _metadata(value)
    if data.get('joined') is True and data.get('kind') == kind and value:
        return value
    return json.dumps(data | {'joined': True, 'kind': kind}, ensure_ascii=False)


def is_joined(selection_group, metadata, segment=None):
    return JOINED in (selection_group, segment) or _metadata(metadata).get('joined') is True


def monitor_enabled(status, selection_group, metadata, segment=None):
    """Enabled means read: an active chat the shared service account follows. Activity alone is not enough."""
    return status == 'active' and is_joined(selection_group, metadata, segment)


def read_count(db):
    """Chats the shared service account reads in real time (what the cabinet catalog lists)."""
    return db.execute("SELECT count(*) AS total FROM telegram_sources WHERE enabled=1 AND status='active'").fetchone()['total']


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
            metadata = json.dumps({'services':item.get('services',''),'offer':item.get('offer',''),
                        'risks':item.get('risks',''),'source_rows':item.get('sources',[])},ensure_ascii=False)
            old = db.execute('SELECT segment,selection_group,metadata FROM telegram_sources WHERE username=?',(username,)).fetchone()
            if old and is_joined(old['selection_group'],old['metadata'],old['segment']):
                # A re-import must not drop the marker of a chat the service account follows.
                metadata = joined_metadata(metadata,_metadata(old['metadata']).get('kind','group'))
            db.execute('''INSERT INTO telegram_sources
                (username,url,title,segment,priority,selection_group,original_members,status,metadata)
                VALUES(?,?,?,?,?,?,?,'pending',?)
                ON CONFLICT(username) DO UPDATE SET title=excluded.title,segment=excluded.segment,
                priority=excluded.priority,selection_group=excluded.selection_group,
                original_members=excluded.original_members,metadata=excluded.metadata''',
                (username,'https://t.me/'+match.group(1),item['title'],item['segment'],item['priority'],
                 item['sheet'],item.get('members'),metadata))
            counts['imported'] += 1
            bucket='potential' if item['sheet']=='Потенциальные клиенты' else 'partners' if item['sheet']=='Партнеры и заказы' else 'review'
            counts[bucket] += 1
    return counts


def apply_monitor_policy(db):
    """enabled=1 only for active chats the service account follows; everything else stays disabled.

    Members, online audience and the workbook segment no longer enable a source: the reader gets
    messages only from joined chats, so an unjoined catalog row would look active but never be read."""
    rows=db.execute('SELECT username,segment,selection_group,metadata,status,enabled FROM telegram_sources').fetchall();enabled=0
    with db:
        for row in rows:
            allow=monitor_enabled(row['status'],row['selection_group'],row['metadata'],row['segment'])
            if allow and not row['enabled']:
                db.execute('UPDATE telegram_sources SET enabled=1 WHERE username=?',(row['username'],))
            elif row['enabled'] and not allow:
                if row['status']=='active':
                    db.execute('UPDATE telegram_sources SET enabled=0,check_reason=? WHERE username=?',
                               (NOT_JOINED_REASON,row['username']))
                else:
                    db.execute('UPDATE telegram_sources SET enabled=0 WHERE username=?',(row['username'],))
            enabled+=int(allow)
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
    query = ("SELECT username,segment,selection_group,metadata FROM telegram_sources "
             "WHERE status IN ('pending','retry','unavailable') ORDER BY priority,original_members DESC")
    rows = [dict(r) for r in db.execute(query + (" LIMIT ?" if limit else ''),(limit,) if limit else ())]
    counts = {}
    def check(row):
        result=inspect_public_page(row['username'])
        time.sleep(.5)
        return row,result
    started=time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for i,(row,result) in enumerate(pool.map(check,rows),1):
            status=result['status']; now=datetime.now(UTC).isoformat(); username=row['username']
            # A live public page does not mean the service account reads the chat: joined chats only.
            enabled=monitor_enabled(status,row['selection_group'],row['metadata'],row['segment'])
            reason=NOT_JOINED_REASON if status=='active' and not enabled else result['reason']
            with db:
                db.execute('''UPDATE telegram_sources SET title=COALESCE(?,title),members=?,online=?,
                    status=?,enabled=?,checked_at=?,last_message_at=?,check_reason=? WHERE username=?''',
                    (result.get('title'),result.get('members'),result.get('online'),status,int(enabled),
                     now,result.get('last_message_at'),reason,username))
            counts[status]=counts.get(status,0)+1
            if i%250==0:
                print(json.dumps({'checked':i,'total':len(rows),'counts':counts},ensure_ascii=False),flush=True)
    return {'checked':len(rows),'counts':counts,'enabled':apply_monitor_policy(db),'seconds':round(time.monotonic()-started,1)}


def registry_stats(db):
    return {r['status']:r['total'] for r in db.execute('SELECT status,count(*) AS total FROM telegram_sources GROUP BY status')}
