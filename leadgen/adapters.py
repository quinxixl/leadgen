import json
import os
import re
import ssl
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from .core import Lead, UTC

MSK = timezone(timedelta(hours=3))
MONTHS = 'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()

def fetch(url, data=None, headers=None):
    cafile = os.getenv('SSL_CERT_FILE')
    if not cafile and sys.platform == 'darwin' and os.path.exists('/etc/ssl/cert.pem'):
        cafile = '/etc/ssl/cert.pem'
    request = urllib.request.Request(url, data=data, headers=headers or {'User-Agent': 'ITLeadFinder/0.1'})
    with urllib.request.urlopen(request, timeout=30, context=ssl.create_default_context(cafile=cafile)) as response:
        body = response.read(4_000_001)
        if len(body) > 4_000_000:
            raise ValueError('Response too large')
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
        raise ValueError('No listing elements: source layout changed or access blocked')
    return result

def collect(source):
    if source['kind'] != 'hh':
        return parse(source,fetch(source['url']))
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
