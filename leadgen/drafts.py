"""Three factual reply drafts without inventing experience or sending messages."""
import re

from .core import topics


def _clean(value, limit):
    value = re.sub(r'\s+', ' ', value or '').strip()
    if len(value) <= limit:
        return value
    return value[:limit-1].rsplit(' ',1)[0] + '…'


def _items(value):
    if isinstance(value,list):
        return [_clean(item,300) for item in value if _clean(item,300)]
    return [_clean(item,300) for item in str(value or '').splitlines() if _clean(item,300)]


def _remove_forbidden(text, phrases):
    for phrase in phrases:
        text=re.sub(re.escape(phrase),'',text,flags=re.IGNORECASE)
    return re.sub(r'\s+([,.!?])',r'\1',re.sub(r'\s{2,}',' ',text)).strip(' ,')


def reply_drafts(lead, profile_services='', portfolio='', settings=None):
    settings=settings or {}
    services = ', '.join(topics(lead.title+' '+lead.text)) or _clean(profile_services,120) or 'вашей задаче'
    task = _clean(lead.title or lead.text,220)
    sender=_clean(settings.get('reply_sender',''),120)
    offer=_clean(settings.get('reply_offer',''),500)
    proofs=_items(settings.get('reply_proof')) or _items(portfolio)[:1]
    proof='; '.join(proofs[:3])
    question=_clean(settings.get('reply_question',''),300) or 'Подскажите, какой результат нужен в первую очередь и к какому сроку?'
    signature=_clean(settings.get('reply_signature',''),300)
    introduction=f' Пишет {sender}.' if sender else ''
    offer_sentence=(offer.rstrip('.!?')+'.') if offer else f'Могу помочь по направлению «{services}».'
    drafts = {
        'Короткий': f'Здравствуйте!{introduction} {offer_sentence} {question}',
        'Экспертный': (f'Здравствуйте!{introduction} Вижу задачу: {task}. {offer_sentence} Предлагаю сначала уточнить текущий процесс, '
                       f'ожидаемый результат и ограничения, затем зафиксировать этапы, сроки и оценку стоимости. {question}'),
        'Дружелюбный': (f'Здравствуйте!{introduction} Увидел ваш запрос про {services}. {offer_sentence} Давайте коротко разберём задачу и поймём, '
                         f'какой вариант подойдёт лучше. {question}')
    }
    if proof:
        drafts['Экспертный'] += ' Подтверждённые примеры: ' + proof + '.'
    if signature:
        drafts={name:text+' '+signature for name,text in drafts.items()}
    forbidden=_items(settings.get('forbidden_phrases'))
    try:limit=max(200,min(1500,int(settings.get('reply_max_length',1500))))
    except (TypeError,ValueError):limit=1500
    return {name:_clean(_remove_forbidden(text,forbidden),limit) for name,text in drafts.items()}
