"""Subscription plans and access checks. Payment provider integration is not connected yet."""
import os
from datetime import datetime, timezone


# Prices are monthly, in rubles. Change them here; the landing and billing pages read this list.
PLANS = [
    {'code': 'start', 'name': 'Старт', 'price': 1490, 'tagline': 'Фрилансеру, который ищет первые заказы',
     'features': ['1 проект с фильтрами', 'Уведомления в Telegram', 'Биржи и каталог Telegram-чатов',
                  'Воронка и напоминания', '10 AI-откликов в день']},
    {'code': 'pro', 'name': 'Профи', 'price': 3490, 'tagline': 'Для тех, кто живёт на входящих заказах',
     'features': ['До 5 проектов', 'Свои Telegram-группы', 'Аналитика спроса и источников',
                  'Экспорт в Excel и CSV', '30 AI-откликов в день'], 'featured': True},
    {'code': 'team', 'name': 'Команда', 'price': 7990, 'tagline': 'Агентствам и студиям',
     'features': ['До 20 проектов', 'До 10 участников и роли', 'Назначение ответственных',
                  'API и webhook в вашу CRM', '100 AI-откликов в день']},
]
PLAN_NAMES = {plan['code']: plan['name'] for plan in PLANS} | {
    'trial': 'Пробный период', 'stub': 'Ранний доступ'}
ACTIVE_STATUSES = ('stub_active', 'trial', 'active')
TRIAL_DAYS = 7


def subscription_active(row, now=None):
    if not row or row['status'] not in ACTIVE_STATUSES:
        return False
    ends_at = row['ends_at'] if 'ends_at' in row.keys() else None
    if ends_at is None:
        return True
    if isinstance(ends_at, str):
        ends_at = datetime.fromisoformat(ends_at)
    return ends_at > (now or datetime.now(timezone.utc))


def days_left(row, now=None):
    ends_at = row['ends_at'] if row and 'ends_at' in row.keys() else None
    if ends_at is None:
        return None
    if isinstance(ends_at, str):
        ends_at = datetime.fromisoformat(ends_at)
    return max(0, (ends_at - (now or datetime.now(timezone.utc))).days)


def support_contact():
    """Telegram username for sales and support, without @."""
    return os.environ.get('SUPPORT_TELEGRAM', '').strip().lstrip('@')


DEFAULT_LEGAL_ENTITY = "ИП Маленюк Никита Романович, ОГРНИП 325911200139330, ИНН 910924258281"


def legal_entity():
    """Seller details for the footer and documents, e.g. «ИП Иванов И. И., ИНН 000000000000»."""
    value = os.environ.get('LEGAL_ENTITY', '').strip()
    if not value or value == '0':
        return DEFAULT_LEGAL_ENTITY
    return value
