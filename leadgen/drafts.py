"""Three factual reply drafts without inventing experience or sending messages."""
import re

from .core import topics


def _clean(value, limit):
    value = re.sub(r'\s+', ' ', value or '').strip()
    if len(value) <= limit:
        return value
    return value[:limit-1].rsplit(' ',1)[0] + '…'


def reply_drafts(lead, profile_services='', portfolio=''):
    services = ', '.join(topics(lead.title+' '+lead.text)) or _clean(profile_services,120) or 'вашей задаче'
    task = _clean(lead.title or lead.text,220)
    proof = _clean(portfolio,280)
    question = 'Подскажите, какой результат нужен в первую очередь и к какому сроку?'
    drafts = {
        'Короткий': f'Здравствуйте! Могу помочь по направлению «{services}». {question}',
        'Экспертный': (f'Здравствуйте! Вижу задачу: {task}. Предлагаю сначала уточнить текущий процесс, '
                       f'ожидаемый результат и ограничения, затем зафиксировать этапы, сроки и оценку стоимости. {question}'),
        'Дружелюбный': (f'Здравствуйте! Увидел ваш запрос про {services}. Давайте коротко разберём задачу и поймём, '
                         f'какой вариант подойдёт лучше. {question}')
    }
    if proof:
        drafts['Экспертный'] += ' Из указанного в моём профиле: ' + proof
    return {name:_clean(text,1500) for name,text in drafts.items()}
