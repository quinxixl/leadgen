"""Project filters shared by Telegram delivery and the web portal."""
import copy
import json
from .core import normalize


def as_list(value):
    return value if isinstance(value,list) else json.loads(value or '[]')


def project_eligibility(lead,base,project):
    from .product import eligible_for_user
    text=normalize(lead.title+' '+lead.text)
    if not project['enabled']:return False,'проект приостановлен'
    stop=as_list(project['stop_words'])
    if any(normalize(word) in text for word in stop):return False,'стоп-слово проекта'
    words=as_list(project['keywords'])
    if words and not any(normalize(word) in text for word in words):return False,'нет ключевых слов проекта'
    sources=as_list(project['source_names'])
    if sources and lead.source not in sources:return False,'источник не выбран в проекте'
    config=copy.deepcopy(base)
    config['min_budget']=project['min_budget'];config['topics']=as_list(project['topics'])
    config['notify_without_budget']=False
    allowed,reason=eligible_for_user(lead,config,project)
    if allowed and lead.score<project['min_score']:return False,'оценка ниже порога проекта'
    return allowed,reason
