"""Conservative rules: uncertain budget and intent are held for review."""
import hashlib
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

UTC = timezone.utc

@dataclass
class Lead:
    source: str
    url: str
    title: str
    text: str
    published: str | None
    budget_text: str = ''
    trusted_order: bool = False
    kind: str = 'order'
    temperature: str = ''
    score: int = 0
    summary: str = ''
    score_reason: str = ''

    def data(self):
        return asdict(self)

def normalize(text):
    return re.sub(r'\s+', ' ', text.casefold().replace('ё', 'е').replace('\u200b', '')).strip()

TOPICS = {
    'Сайты': r'сайт|лендинг|лендос|landing|wordpress|вордпресс|tilda|тильд|taplink|таплинк|самопис|веб[ -]?(?:разработ|прилож)|интернет[ -]магазин|верстк',
    'Боты': r'\bбот(?:а|ов|ы|у|ом)?\b|чат[ -]?бот|chatbot|aiogram|telegram.{0,15}mini[ -]?app|телеграм.{0,15}мини[ -]?апп|обработчик.{0,15}лид|систем.{0,15}лояльност',
    'Мобильные приложения': r'мобильн.{0,25}прилож|прилож.{0,25}(?:android|ios|телефон|смартфон)|android|\bios\b|flutter|react native|app\s?store|google\s?play',
    'Автоматизации': r'автоматизац|\bn8n\b|make\.com|zapier|парсер|парсинг|webhook|вебхук|интеграц.{0,20}(?:api|сервис|систем)|связать.{0,25}(?:сервис|систем)',
    'CRM': r'\bcrm\b|amocrm|битрикс\s?24|bitrix\s?24|амо(?:срм|crm)|retailcrm|мегаплан|мои?\s?склад',
}
DEMAND = r'нуж(?:ен|на|но|ны)|ищ(?:у|ем|ут)|требуется|необходимо|заказ|сделать|создать|разработать|настроить|доработать|внедрить|кто может|посоветуйте.{0,20}(?:разработ|специалист|подрядчик)|есть задача|нужна помощь'
SELLER = r'предлагаю услуги|оказываю услуги|ищу заказы|ищу клиентов|возьму.{0,12}(?:заказ|проект)|сделаю.{0,30}(?:сайт|бот)|разрабатываю.{0,30}(?:сайт|бот|прилож)|мои услуги|мое портфолио|что я делаю|ищу партнера по поиску'
JOBS = r'ваканси|#job\b|#hiring\b|full[ -]?time|фулл[ -]?тайм|полный рабочий день|\b5/2\b|в штат|з/п|зарплат|оклад'
ADS = r'заработал|заработай|пошагов.{0,15}инструкц|бесплатн.{0,15}(?:курс|вебинар)|обучим|курс по|розыгрыш'
URGENCY = r'срочн|как можно скорее|в ближайш|до (?:завтра|конца недели)|горит|asap'
DEADLINE = r'(?:срок|дедлайн|готово|запуск).{0,35}(?:\d{1,2}[./]\d{1,2}|дн|недел|месяц|завтра)'
CONTACT = r'@[a-zA-Z][\w]{3,}|https?://t\.me/[\w+/-]+|(?:телефон|whatsapp|ватсап|связь|лс|личк)'

def topics(text):
    t = normalize(text)
    return [name for name, regex in TOPICS.items() if re.search(regex, t)]

def budget(text):
    """Return conservative lower bound, status. Never treat hourly/monthly as a project."""
    t = normalize(text)
    if re.search(r'/\s*(?:час|ч\b|мес)|в час|за час|почас|в месяц|ежемесяч|за месяц', t):
        return None, 'ставка за время'
    if re.search(r'\$|€|usd|eur|доллар|евро|тенге|грн|₴', t):
        return None, 'другая валюта'
    number = r'\d+(?:[ .]\d{3})*(?:[,.]\d+)?'
    unit = r'(?:тыс\.?|[кk])'
    currency = r'(?:₽|руб(?:лей|ля|ль|\.)?|р\.)'
    pattern = rf'(?<![\w\d])(?:(от|до)\s*)?({number})\s*({unit})?\s*(?:[-–—]\s*({number})\s*({unit})?\s*)?({currency})?(?!\w)'
    amounts = []
    for m in re.finditer(pattern, t):
        prefix, first, u1, second, u2, cur = m.groups()
        if not (u1 or u2 or cur):
            continue
        def val(n, u):
            return int(float(n.replace(' ', '').replace(',', '.').replace('.', '') if re.fullmatch(r'\d{1,3}(?:\.\d{3})+', n) else n.replace(' ', '').replace(',', '.')) * (1000 if u else 1))
        low = val(first, u1 or (u2 if second else None))
        high = val(second, u2 or u1) if second else low
        if prefix == 'до':
            return None, 'указан только потолок бюджета'
        if high < low:
            return None, 'неоднозначный диапазон'
        amounts.append(low)
    if not amounts:
        return None, 'бюджет не указан'
    if len(amounts) > 1:
        return None, 'несколько сумм: нужна проверка'
    return amounts[0], 'RUB за проект'

def classify(lead, minimum=5000, max_age_hours=72, now=None):
    now = now or datetime.now(UTC)
    text = normalize(lead.title + '\n' + lead.text)
    tags = topics(lead.title if lead.trusted_order else text)
    if not tags:
        return 'rejected', 'другая услуга', tags
    if lead.kind != 'order':
        return 'review', 'проектная вакансия: оплата не является бюджетом заказа', tags
    if re.search(SELLER, text) or re.search(ADS, text):
        return 'rejected', 'самореклама или обучение', tags
    if re.search(JOBS, text):
        return 'rejected', 'вакансия', tags
    if not lead.trusted_order and not re.search(DEMAND, text):
        return 'review', 'неясно, ищут ли исполнителя', tags
    try:
        date = datetime.fromisoformat(lead.published)
        if date.tzinfo is None:
            raise ValueError('no timezone')
        age = (now - date).total_seconds() / 3600
    except (TypeError, ValueError):
        return 'review', 'дата не подтверждена', tags
    if age < -1 or age > max_age_hours:
        return 'rejected', 'вне окна свежести', tags
    price, reason = budget(lead.budget_text)
    if price is None:
        return 'review', reason, tags
    if price < minimum:
        return 'rejected', 'бюджет ниже порога', tags
    return 'ready', f'{", ".join(tags)}; бюджет от {price:,} ₽'.replace(',', ' '), tags

def lead_profile(lead, minimum=5000):
    """Create a compact, deterministic brief and a transparent intent score."""
    raw = re.sub(r'\s+', ' ', (lead.title + ' ' + lead.text)).strip()
    text = normalize(raw)
    tags = topics(text)
    points, reasons = 0, []
    if tags:
        points += 20; reasons.append('подходящая услуга')
    if re.search(DEMAND, text):
        points += 25; reasons.append('явный запрос исполнителя')
    if re.search(SELLER, text) or re.search(ADS, text):
        points -= 45; reasons.append('похоже на рекламу исполнителя')
    if re.search(JOBS, text):
        points -= 35; reasons.append('признаки вакансии')
    price, price_reason = budget(lead.budget_text or raw)
    if price is not None:
        if price >= minimum:
            points += 20; reasons.append(f'бюджет от {price:,} ₽'.replace(',', ' '))
        else:
            points += 5; reasons.append('бюджет ниже выбранного порога')
    if re.search(URGENCY, text):
        points += 10; reasons.append('срочность')
    if re.search(DEADLINE, text):
        points += 8; reasons.append('указан срок')
    if re.search(CONTACT, raw, re.I):
        points += 7; reasons.append('есть способ связи')
    if len(text) >= 220:
        points += 5; reasons.append('есть детали задачи')
    points = max(0, min(100, points))
    temperature = 'горячий' if points >= 70 else 'тёплый' if points >= 45 else 'холодный'

    sentences = [s.strip(' •—-') for s in re.split(r'(?<=[.!?])\s+|\n+', raw) if len(s.strip()) >= 12]
    important = []
    for sentence in sentences:
        n = normalize(sentence)
        if any(re.search(rx, n) for rx in TOPICS.values()) or re.search(DEMAND+'|'+URGENCY+'|'+DEADLINE, n):
            if sentence not in important:
                important.append(sentence)
        if len(important) == 3:
            break
    if not important:
        important = sentences[:2] or [lead.title]
    need = ' '.join(important)
    if len(need) > 520:
        need = need[:517].rsplit(' ',1)[0] + '…'
    brief = f"Нужно: {need}\nНаправление: {', '.join(tags) if tags else 'не определено'}\nБюджет: "
    brief += (f'от {price:,} ₽'.replace(',', ' ') if price is not None else price_reason)
    return {'temperature':temperature,'score':points,'summary':brief,'reason':', '.join(reasons) or 'мало данных','topics':tags}

def enrich(lead, minimum=5000):
    profile = lead_profile(lead, minimum)
    lead.temperature = profile['temperature']
    lead.score = profile['score']
    lead.summary = profile['summary']
    lead.score_reason = profile['reason']
    return lead

def fingerprint(lead):
    # Keep budget and contact identity. Exact normalized copies across channels collapse.
    text = normalize(lead.title + ' ' + lead.text + ' ' + lead.budget_text)
    return hashlib.sha256(text.encode()).hexdigest()

def message(lead, reason):
    lead = enrich(lead)
    label = {'горячий':'🔥','тёплый':'🌤','холодный':'❄️'}.get(lead.temperature,'')
    # Plain text avoids HTML injection from untrusted listings.
    return (f'{label} {lead.temperature.capitalize()} лид · {lead.score}/100\n'
            f'{lead.source}\n\n{lead.summary}\n\n'
            f'Почему такая оценка: {lead.score_reason}\n'
            f'Фильтр: {reason}\nОпубликован: {lead.published or "не указано"}\n\n'
            f'Открыть заявку: {lead.url}')[:3900]
