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
    intent: str = 'direct'

    def data(self):
        return asdict(self)

def normalize(text):
    return re.sub(r'\s+', ' ', text.casefold().replace('ё', 'е').replace('\u200b', '')).strip()

TOPIC_GROUPS = (
    ('Разработка и IT', (
        ('Сайты', r'сайт|лендинг|лендос|landing|wordpress|вордпресс|tilda|тильд|taplink|таплинк|самопис|веб[ -]?(?:разработ|прилож)|интернет[ -]магазин|верстк'),
        ('Боты', r'\bбот(?:а|ов|ы|у|ом)?\b|чат[ -]?бот|chatbot|aiogram|telegram.{0,15}mini[ -]?app|телеграм.{0,15}мини[ -]?апп|обработчик.{0,15}лид|систем.{0,15}лояльност'),
        ('Мобильные приложения', r'мобильн.{0,25}прилож|прилож.{0,25}(?:android|ios|телефон|смартфон)|android|\bios\b|flutter|react native|app\s?store|google\s?play'),
        ('Программирование и ПО', r'программист|программирован|программное обеспеч|разработк.{0,20}(?:по|сервис|платформ)|backend|frontend|fullstack|бэкенд|фронтенд|скрипт|десктопн.{0,15}прилож|микросервис'),
        ('Автоматизации', r'автоматизац|\bn8n\b|make\.com|zapier|парсер|парсинг|webhook|вебхук|интеграц.{0,20}(?:api|сервис|систем)|связать.{0,25}(?:сервис|систем)'),
        ('CRM', r'\bcrm\b|amocrm|битрикс\s?24|bitrix\s?24|амо(?:срм|crm)|retailcrm|мегаплан|мои?\s?склад'),
        ('Нейросети и AI', r'нейросет|искусственн.{0,12}интеллект|\bai\b|machine learning|машинн.{0,12}обучен|\bllm\b|\brag\b|chatgpt|gpt[- ]?[34o5]|компьютерн.{0,12}зрени'),
        ('Аналитика данных и BI', r'аналитик.{0,12}данн|анализ данн|\bbi\b|power\s?bi|tableau|дашборд|dashboard|визуализац.{0,12}данн|data analyst|data engineer'),
        ('DevOps и серверы', r'\bdevops\b|системн.{0,12}администратор|настро(?:ить|йка).{0,20}сервер|\bvps\b|docker|kubernetes|nginx|ci[ /-]?cd|депло[йи]|облачн.{0,15}инфраструктур'),
        ('Кибербезопасность', r'кибербезопас|информационн.{0,12}безопас|пентест|penetration test|аудит.{0,12}безопас|защит.{0,15}(?:сайт|сервер|данн)|поиск уязвимост'),
    )),
    ('Дизайн и медиа', (
        ('Графический дизайн', r'графическ.{0,12}дизайн|дизайнер|баннер|креатив|полиграф|макет.{0,15}(?:листов|буклет|баннер|плакат)|оформлен.{0,15}соцсет'),
        ('UI/UX-дизайн', r'ui[ /-]?ux|ux[ /-]?ui|дизайн.{0,15}(?:интерфейс|сайт|прилож)|прототип.{0,15}(?:сайт|прилож)|\bfigma\b|юзабилити'),
        ('Логотипы и брендинг', r'логотип|фирменн.{0,12}стил|брендинг|айдентик|брендбук|гайдлайн.{0,12}бренд'),
        ('Презентации', r'презентац|pitch[ -]?deck|питч[ -]?дек|слайды|оформить.{0,15}(?:кп|коммерческ.{0,8}предлож)'),
        ('Видеомонтаж', r'видеомонтаж|монтаж.{0,15}видео|смонтировать|видео.{0,15}ролик|youtube.{0,15}ролик|ютуб.{0,15}ролик|reels|рилс|shorts'),
        ('Motion-дизайн и анимация', r'motion[ -]?дизайн|моушн|анимац|2d[ /-]?анимац|explainer video|инфографик.{0,10}видео'),
        ('3D и визуализация', r'\b3d\b|3д[ -]?(?:модел|визуал)|рендер|визуализац.{0,15}(?:интерьер|экстерьер|товар)|blender|cinema\s?4d'),
        ('Фото и ретушь', r'фотограф|фотосъем|фотосесси|предметн.{0,10}съем|ретуш|обработк.{0,12}фото'),
        ('Аудио и озвучка', r'озвучк|диктор|аудиомонтаж|монтаж.{0,12}подкаст|звукореж|сведени.{0,10}(?:трек|аудио)|саунд[ -]?дизайн'),
    )),
    ('Маркетинг и продажи', (
        ('Маркетинг', r'маркетолог|маркетингов|стратеги.{0,12}продвиж|исследовани.{0,12}рынк|анализ.{0,12}конкурент|медиаплан'),
        ('SMM', r'\bsmm\b|смм|ведени.{0,15}соцсет|контент[ -]?план|telegram[ -]?канал.{0,15}(?:вести|продвиж)|продвижен.{0,12}соцсет'),
        ('Таргетированная реклама', r'таргетолог|таргетированн.{0,12}реклам|настроить.{0,12}таргет|реклам.{0,12}(?:vk|вконтакте|mytarget|facebook|instagram)'),
        ('Контекстная реклама', r'контекстн.{0,12}реклам|яндекс[ .]?директ|google ads|гугл эдс|директолог|настроить.{0,15}(?:директ|контекст)'),
        ('SEO', r'\bseo\b|сео|поисков.{0,12}продвиж|продвижен.{0,15}(?:яндекс|google|гугл)|семантическ.{0,10}ядр|техническ.{0,10}seo'),
        ('Копирайтинг и контент', r'копирайт|редактор.{0,15}текст|написать.{0,20}(?:текст|стать|пост|сценар)|контент[ -]?менеджер|текст.{0,20}(?:для сайта|лендинг|рассылк|поста)'),
        ('Email-маркетинг', r'email[ -]?маркет|e-mail[ -]?маркет|емейл[ -]?маркет|почтов.{0,12}рассылк|цепочк.{0,12}писем|рассылк.{0,15}(?:unisender|sendpulse)'),
        ('Репутация и PR', r'управлени.{0,12}репутац|\bserm\b|pr[ -]?(?:менеджер|кампан)|пиар|публикац.{0,15}сми|удалить.{0,12}отзыв'),
        ('Лидогенерация и продажи', r'лидогенерац|лидген|поиск.{0,12}клиент|назначени.{0,12}встреч|отдел продаж|скрипт.{0,12}продаж|воронк.{0,12}продаж|холодн.{0,12}(?:звон|рассыл)'),
    )),
    ('Бизнес-услуги', (
        ('Юридические услуги', r'юрист|юридическ|адвокат|правов.{0,12}(?:анализ|консультац|сопровожд)|составить.{0,15}(?:договор|претензи|иск)|проверить.{0,15}договор|регистрац.{0,12}(?:ооо|ип|товарн.{0,8}знак)'),
        ('Бухгалтерия и налоги', r'бухгалтер|бухгалтерск.{0,12}учет|налогов.{0,12}(?:консультац|отчетност|декларац)|сдать.{0,12}отчетност|1с[ :.-]?(?:бухгалтер|зарплат|предприяти)'),
        ('HR и рекрутинг', r'рекрутер|рекрутинг|hr[ -]?(?:специалист|менеджер|агентств)|подбор.{0,12}(?:персонал|сотрудник|команд)|найти.{0,12}(?:сотрудник|разработчик|менеджер)|закрыть.{0,12}ваканси'),
        ('Переводы и локализация', r'переводчик|перевести.{0,18}(?:текст|документ|сайт|прилож)|перевод.{0,15}(?:английск|китайск|немецк|испанск|французск)|локализац.{0,15}(?:сайт|прилож|игр)'),
        ('Бизнес-консалтинг', r'бизнес[ -]?консульт|управленческ.{0,12}консалт|бизнес[ -]?план|финансов.{0,12}модел|юнит[ -]?экономик|оптимизац.{0,12}бизнес[ -]?процесс'),
        ('Онлайн-курсы и методология', r'методолог|онлайн[ -]?курс|образовательн.{0,12}программ|учебн.{0,12}программ|упаковк.{0,12}курс|продюсер.{0,12}(?:курс|эксперт)'),
    )),
)
TOPICS = {name: regex for _, group in TOPIC_GROUPS for name, regex in group}
TOPIC_CATEGORIES = tuple((section, tuple(name for name, _ in group)) for section, group in TOPIC_GROUPS)
DEMAND = r'нуж(?:ен|на|но|ны)|ищ(?:у|ем|ут)|требуется|необходимо|заказ|сделать|создать|разработать|настроить|доработать|внедрить|кто может|помогите|посоветуйте.{0,20}(?:разработ|специалист|подрядчик)|есть задача|нужна помощь'
SELLER = r'предлага(?:ю|ем) услуги|оказыва(?:ю|ем) услуги|ищу заказы|ищу клиентов|ищу работу|рассматриваю вакансии|открыт.{0,12}к предложениям|возьму.{0,12}(?:заказ|проект)|сделаю.{0,30}(?:сайт|бот)|разрабатываю.{0,30}(?:сайт|бот|прилож)|мои услуги|наши услуги|мое портфолио|наше портфолио|что я делаю|ищу партнера по поиску'
SELLER_ACTION = r'(?<!кто\s)(?<!если\s)\b(?:помогу|поможем|сделаю|сделаем|создадим|разработаем|настроим|внедрим|подключим|интегрируем|автоматизируем|продвинем|нарисую|смонтирую|озвучу|переведу|проконсультирую|составлю.{0,15}договор|выведем.{0,18}в топ|упакуем|запустим)\b'
SELLER_IDENTITY = r'\b(?:мы|наша команда|команда специалистов)\b.{0,80}\b(?:делаем|создаем|разрабатыва(?:ем|ет)|настраива(?:ем|ет)|внедря(?:ем|ет)|продвига(?:ем|ет)|оказыва(?:ем|ет)|рисуем|монтируем|консультируем|сопровождаем)\b'
SELLER_CTA = r'пишите.{0,20}(?:лс|личк|директ|обсуд)|обращайтесь|закажите|оставьте заявку|бесплатн.{0,20}(?:разбор|консультац|аудит)|готов(?:ы|а)? помочь|работаем под ключ'
JOBS = r'ваканси|#job\b|#hiring\b|full[ -]?time|фулл[ -]?тайм|полный рабочий день|\b5/2\b|в штат|з/п|зарплат|оклад'
SERVICE_JOB_REQUEST = r'нуж(?:ен|на)\s+(?:рекрутер|hr|эйчар)|ищ(?:у|ем)\s+(?:рекрутер|hr|эйчар)|(?:расчет|начислен).{0,15}зарплат'
ADS = r'заработал|заработай|пошагов.{0,15}инструкц|бесплатн.{0,15}(?:курс|вебинар)|обучим|курс по|розыгрыш'
URGENCY = r'срочн|как можно скорее|в ближайш|до (?:завтра|конца недели)|горит|asap'
DEADLINE = r'(?:срок|дедлайн|готово|запуск).{0,35}(?:\d{1,2}[./]\d{1,2}|дн|недел|месяц|завтра)'
CONTACT = r'@[a-zA-Z][\w]{3,}|https?://t\.me/[\w+/-]+|(?:телефон|whatsapp|ватсап|связь|лс|личк)'
POSSIBLE_NEEDS = {
    'Сайты': r'нет сайта|сайт.{0,25}(?:устарел|не принос|не работает|медлен)|низк.{0,15}конверси.{0,20}сайт',
    'Боты': r'однотипн.{0,20}вопрос|клиент.{0,25}(?:долго жд|не получают ответ)|поддержк.{0,25}не успева',
    'Мобильные приложения': r'клиент.{0,25}(?:личн.{0,10}кабинет|с телефона)|мобильн.{0,15}верси.{0,20}не хватает',
    'Автоматизации': r'вручн.{0,35}(?:перенос|копир|обрабаты|свод|заполня)|рутин.{0,25}(?:занима|отнима)|дублиру.{0,20}данн|постоянн.{0,20}ошиб',
    'CRM': r'(?:теря|пропада).{0,25}(?:заявк|лид|клиент)|(?:заявк|лид).{0,25}(?:теря|пропада)|нет единой базы|менеджер.{0,35}(?:таблиц|excel|эксел)',
    'Графический дизайн': r'визуал.{0,20}(?:устарел|разрознен)|материал.{0,20}выглядят непрофессионально',
    'Логотипы и брендинг': r'нет фирменного стиля|бренд.{0,20}(?:не узнают|выглядит устаревш)',
    'Видеомонтаж': r'есть отснят.{0,15}материал|не успева.{0,20}монтировать',
    'Маркетинг': r'реклам.{0,20}не окупа|нет заявок|стоимость лида.{0,15}(?:растет|вырос)',
    'SEO': r'сайт.{0,20}не виден в поиске|позици.{0,15}упали|нет органическ.{0,12}трафик',
    'Юридические услуги': r'получил.{0,12}(?:претензи|иск)|нужно ответить.{0,12}претензи|нарушил.{0,12}условия договор',
    'Бухгалтерия и налоги': r'не сдал.{0,12}отчетност|требовани.{0,12}налогов|учет.{0,20}не ведется',
    'HR и рекрутинг': r'не можем.{0,15}найти сотрудник|не закрыва.{0,15}ваканси|долго ищем.{0,15}(?:специалист|сотрудник)',
}

def topics(text):
    t = normalize(text)
    return [name for name, regex in TOPICS.items() if re.search(regex, t)]

def possible_need_topics(text):
    t = normalize(text)
    return [name for name,regex in POSSIBLE_NEEDS.items() if re.search(regex,t)]

def seller_offer(text):
    """High-confidence service offer, not a buyer describing their own problem."""
    t = normalize(text)
    if re.search(SELLER, t) or re.search(SELLER_IDENTITY, t):
        return True
    action = bool(re.search(SELLER_ACTION, t))
    cta = bool(re.search(SELLER_CTA, t))
    hashtags = re.findall(r'(?<!\w)#[\w-]+', t)
    promotional_list = len(hashtags) >= 3 or len(re.findall(r'[🔹✅✔️►•]', text)) >= 3
    # First-person promises are offers by themselves; CTA/list signals cover
    # short ads such as "SEO под ключ — пишите в ЛС".
    return action or (cta and promotional_list and bool(topics(t)))

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
    direct_tags = topics(lead.title if lead.trusted_order else text)
    need_tags = possible_need_topics(text)
    tags = list(dict.fromkeys(direct_tags+need_tags))
    if not tags:
        return 'rejected', 'другая услуга', tags
    if lead.kind != 'order':
        return 'review', 'проектная вакансия: оплата не является бюджетом заказа', tags
    if seller_offer(lead.title + '\n' + lead.text):
        return 'rejected', 'предложение услуг другого подрядчика', tags
    if re.search(ADS, text):
        return 'rejected', 'самореклама или обучение', tags
    if re.search(JOBS, text) and not re.search(SERVICE_JOB_REQUEST, text):
        return 'rejected', 'вакансия', tags
    try:
        date = datetime.fromisoformat(lead.published)
        if date.tzinfo is None:
            raise ValueError('no timezone')
        age = (now - date).total_seconds() / 3600
    except (TypeError, ValueError):
        return 'review', 'дата не подтверждена', tags
    if age < -1 or age > max_age_hours:
        return 'rejected', 'вне окна свежести', tags
    if not lead.trusted_order and not re.search(DEMAND, text) and need_tags:
        price,_=budget(lead.budget_text)
        if price is not None and price<minimum:return 'rejected','бюджет ниже порога',tags
        return 'review', 'возможная потребность: '+', '.join(need_tags), tags
    if not lead.trusted_order and not re.search(DEMAND, text):
        return 'review', 'неясно, ищут ли исполнителя', tags
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
    direct_tags = topics(text)
    need_tags = possible_need_topics(text)
    tags = list(dict.fromkeys(direct_tags+need_tags))
    points, reasons = 0, []
    if tags:
        points += 20; reasons.append('подходящая услуга')
    if re.search(DEMAND, text):
        points += 25; reasons.append('явный запрос исполнителя')
    elif need_tags:
        points += 18; reasons.append('описана проблема, которую можно решить услугой')
    if seller_offer(raw) or re.search(ADS, text):
        points -= 45; reasons.append('похоже на рекламу исполнителя')
    if re.search(JOBS, text) and not re.search(SERVICE_JOB_REQUEST, text):
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
    temperature = 'горячий' if points >= 80 else 'тёплый' if points >= 50 else 'холодный'

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
    lead.intent = 'direct' if re.search(DEMAND,normalize(lead.title+' '+lead.text)) else ('possible' if possible_need_topics(lead.title+' '+lead.text) else 'unclear')
    return lead

def fingerprint(lead):
    # Keep budget and contact identity. Exact normalized copies across channels collapse.
    text = normalize(lead.title + ' ' + lead.text + ' ' + lead.budget_text)
    return hashlib.sha256(text.encode()).hexdigest()

def message(lead, reason):
    lead = enrich(lead)
    label = {'горячий':'🔥','тёплый':'🌤','холодный':'❄️'}.get(lead.temperature,'')
    intent = '\nСигнал: возможная потребность, готовый заказ не подтверждён.' if lead.intent=='possible' else ''
    # Plain text avoids HTML injection from untrusted listings.
    return (f'{label} {lead.temperature.capitalize()} лид · {lead.score}/100\n'
            f'{lead.source}{intent}\n\n{lead.summary}\n\n'
            f'Почему такая оценка: {lead.score_reason}\n'
            f'Фильтр: {reason}\nОпубликован: {lead.published or "не указано"}\n\n'
            f'Открыть заявку: {lead.url}')[:3900]
