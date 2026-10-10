"""Subscription plans, quotas and access checks. Payment provider integration is not connected yet."""
import os
from datetime import datetime, timezone


# Free plan: leads reach the user this many minutes after they are found.
FREE_LEAD_DELAY_MINUTES = 60
# Paying for a year at once costs this much less than twelve monthly payments.
ANNUAL_DISCOUNT_PERCENT = 20
# Beta founders keep «Старт» at this monthly price for as long as the subscription lasts.
FOUNDER_START_PRICE = 690
TRIAL_DAYS = 7


def annual_monthly_price(price):
    """Monthly equivalent with the annual discount, whole rubles."""
    return price * (100 - ANNUAL_DISCOUNT_PERCENT) // 100


# Prices are monthly, in rubles. Change them here; the landing, pricing and billing pages read this list.
# Limits: projects per workspace, workspace members including the owner, AI drafts per 24 hours,
# and the lead delivery delay in minutes.
PLANS = [
    {'code': 'free', 'name': 'Бесплатный', 'price': 0, 'tagline': 'Попробовать поиск заказов без оплаты',
     'limits': {'projects': 1, 'members': 1, 'drafts': 3, 'delay_minutes': FREE_LEAD_DELAY_MINUTES},
     'features': ['1 проект с фильтрами', f'Заказы с задержкой ~{FREE_LEAD_DELAY_MINUTES} минут',
                  'Уведомления в Telegram', '3 AI-отклика в день']},
    {'code': 'start', 'name': 'Старт', 'price': 990, 'tagline': 'Фрилансеру, который ищет первые заказы',
     'limits': {'projects': 1, 'members': 1, 'drafts': 10, 'delay_minutes': 0},
     'features': ['1 проект с фильтрами', 'Заказы сразу после публикации', 'Биржи и каталог Telegram-чатов',
                  'Воронка и напоминания', '10 AI-откликов в день']},
    {'code': 'pro', 'name': 'Профи', 'price': 1990, 'tagline': 'Для тех, кто живёт на входящих заказах',
     'limits': {'projects': 5, 'members': 1, 'drafts': 30, 'delay_minutes': 0},
     'features': ['До 5 проектов', 'Свои Telegram-группы', 'Аналитика спроса и источников',
                  'Экспорт в Excel и CSV', '30 AI-откликов в день'], 'featured': True},
    {'code': 'team', 'name': 'Команда', 'price': 4990, 'tagline': 'Агентствам и студиям',
     'limits': {'projects': 20, 'members': 10, 'drafts': 100, 'delay_minutes': 0},
     'features': ['До 20 проектов', 'До 10 участников и роли', 'Назначение ответственных',
                  '100 AI-откликов в день']},
]
for _plan in PLANS:
    _plan['annual_month_price'] = annual_monthly_price(_plan['price'])
    _plan['annual_price'] = _plan['annual_month_price'] * 12
del _plan

PLAN_NAMES = {plan['code']: plan['name'] for plan in PLANS} | {
    'trial': 'Пробный период', 'stub': 'Ранний доступ'}
# The trial (and the legacy early access) open the «Профи» limits without a delay.
LIMITS = {plan['code']: plan['limits'] for plan in PLANS}
LIMITS['trial'] = LIMITS['stub'] = LIMITS['pro']
ACTIVE_STATUSES = ('stub_active', 'trial', 'active')


def subscription_active(row, now=None):
    """A paid, trial or free period is running. Free rows never end."""
    if not row or row['status'] not in ACTIVE_STATUSES:
        return False
    ends_at = row['ends_at'] if 'ends_at' in row.keys() else None
    if ends_at is None:
        return True
    if isinstance(ends_at, str):
        ends_at = datetime.fromisoformat(ends_at)
    return ends_at > (now or datetime.now(timezone.utc))


def effective_plan(row, now=None):
    """Plan whose limits apply now: an ended trial or paid period falls back to Free instead of closing access."""
    if not subscription_active(row, now):
        return 'free'
    code = row['plan_code'] if 'plan_code' in row.keys() else None
    return code if code in LIMITS else 'free'


def plan_limits(code):
    return LIMITS.get(code, LIMITS['free'])


def owner_subscription(db, owner_id):
    return db.execute('''SELECT status,plan_code,ends_at,founder_price,billing_period
        FROM subscriptions WHERE user_id=?''', (owner_id,)).fetchone()


def owner_plan(db, owner_id):
    """Effective plan of a workspace owner; team members work under the owner's plan."""
    return effective_plan(owner_subscription(db, owner_id))


LIMIT_LABELS = {'projects': 'проектов', 'members': 'участников в команде', 'drafts': 'AI-откликов в день'}


def limit_message(kind, code):
    """User-facing explanation of a reached quota with the cheapest plan that lifts it."""
    limit = plan_limits(code)[kind]
    name = PLAN_NAMES.get(code, code)
    if kind == 'members' and limit <= 1:
        text = f'На тарифе «{name}» работа в команде недоступна.'
    else:
        text = f'На тарифе «{name}» доступно {LIMIT_LABELS[kind]}: {limit}.'
    upgrade = next((plan for plan in PLANS if plan['limits'][kind] > limit), None)
    if upgrade:
        text += f" На тарифе «{upgrade['name']}» — до {upgrade['limits'][kind]}. Сменить тариф можно в разделе «Подписка»."
    return text


def delay_clause(minutes, column='l.first_seen'):
    """SQL condition and parameters that hide leads younger than the plan's delivery delay."""
    if not minutes:
        return '', []
    return f" AND {column}::timestamptz <= now()-(? * interval '1 minute')", [minutes]


def plan_summary(code):
    """One line with the limits of a plan for the bot and notices."""
    limits = plan_limits(code)
    parts = [f"проектов: {limits['projects']}", f"AI-откликов в день: {limits['drafts']}"]
    if limits['members'] > 1:
        parts.append(f"участников: до {limits['members']}")
    parts.append(f"заказы с задержкой ~{limits['delay_minutes']} мин" if limits['delay_minutes']
                 else 'заказы сразу после публикации')
    return ', '.join(parts)


def days_left(row, now=None):
    ends_at = row['ends_at'] if row and 'ends_at' in row.keys() else None
    if ends_at is None:
        return None
    if isinstance(ends_at, str):
        ends_at = datetime.fromisoformat(ends_at)
    return max(0, (ends_at - (now or datetime.now(timezone.utc))).days)


def founder_signups_open():
    """New registrations get the founder price lock while the beta lasts; BETA_FOUNDER_OPEN=0 ends it."""
    return os.environ.get('BETA_FOUNDER_OPEN', '1').strip() != '0'


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
