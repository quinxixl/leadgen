"""Subscription plans, quotas and access checks. YooKassa payments live in leadgen/payments.py."""
import os
from datetime import date, datetime, timedelta, timezone


# Free plan: leads reach the user this many minutes after they are found.
FREE_LEAD_DELAY_MINUTES = 60
# Paying for a year at once costs this much less than twelve monthly payments.
ANNUAL_DISCOUNT_PERCENT = 20
# Beta founders keep «Старт» at this monthly price for as long as the subscription lasts.
FOUNDER_START_PRICE = 690
# Early-access accounts use «Старт» for free until this date (midnight Moscow time), then fall back to Free.
DEFAULT_BETA_ENDS_AT = '2026-11-15'
MSK = timezone(timedelta(hours=3))


def beta_ends_at():
    """End of the free beta «Старт» from BETA_ENDS_AT (ISO date), midnight Moscow time."""
    raw = os.environ.get('BETA_ENDS_AT', '').strip()
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        day = date.fromisoformat(DEFAULT_BETA_ENDS_AT)
    return datetime(day.year, day.month, day.day, tzinfo=MSK)


def _ends_at(row):
    ends_at = row['ends_at'] if row and 'ends_at' in row.keys() else None
    if isinstance(ends_at, str):
        ends_at = datetime.fromisoformat(ends_at)
    return ends_at


def beta_free_start(row):
    """The account uses «Старт» for free as an early-access beta member."""
    ends_at = _ends_at(row)
    return bool(row and ends_at is not None and row['status'] == 'active' and row['plan_code'] == 'start'
                and ends_at.astimezone(MSK).date() == beta_ends_at().date())


def beta_label(row, now=None):
    """«Старт бесплатно до 15.11.2026» while the beta period runs, otherwise None."""
    if not beta_free_start(row) or not subscription_active(row, now):
        return None
    return f'Старт бесплатно до {beta_ends_at():%d.%m.%Y}'


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


def _moment(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def subscription_active(row, now=None):
    """A paid, trial or free period is running. Free rows never end.

    After a failed autorenewal (status past_due) the paid plan keeps working until grace_until."""
    if not row or (row['status'] not in ACTIVE_STATUSES and row['status'] != 'past_due'):
        return False
    keys = row.keys()
    ends_at = _moment(row['ends_at']) if 'ends_at' in keys else None
    grace = _moment(row['grace_until']) if 'grace_until' in keys else None
    if ends_at is None:
        return row['status'] != 'past_due'
    if grace is not None and grace > ends_at:
        ends_at = grace
    return ends_at > (now or datetime.now(timezone.utc))


def effective_plan(row, now=None):
    """Plan whose limits apply now: an ended trial or paid period falls back to Free instead of closing access."""
    if not subscription_active(row, now):
        return 'free'
    code = row['plan_code'] if 'plan_code' in row.keys() else None
    return code if code in LIMITS else 'free'


def plan_limits(code):
    return LIMITS.get(code, LIMITS['free'])


# The public API and webhooks come with every running non-free plan: paid, trial, beta «Старт» and service accounts.
INTEGRATION_PLANS = frozenset(LIMITS) - {'free'}


def plan_includes_integrations(code):
    return code in INTEGRATION_PLANS


def _env_ids(*names):
    return {value.strip() for name in names for value in os.environ.get(name, '').split(',') if value.strip()}


def unlimited_telegram_ids():
    """Platform admins (WEB_ADMIN_TELEGRAM_IDS) and service accounts (UNLIMITED_TELEGRAM_IDS)."""
    return _env_ids('WEB_ADMIN_TELEGRAM_IDS', 'UNLIMITED_TELEGRAM_IDS')


def is_unlimited(telegram_user_id):
    return telegram_user_id is not None and str(telegram_user_id) in unlimited_telegram_ids()


SERVICE_PLAN = 'team'


def service_subscription(row=None):
    """Subscription view of an admin or service account: the top plan without an end date.

    Computed on the fly; the stored subscription row is left as it is."""
    keys = row.keys() if row else ()
    return {'status': 'active', 'plan_code': SERVICE_PLAN, 'ends_at': None, 'service': True,
            'founder_price': row['founder_price'] if 'founder_price' in keys else None,
            'billing_period': (row['billing_period'] if 'billing_period' in keys else None) or 'month'}


def is_service(row):
    return isinstance(row, dict) and row.get('service') is True


def owner_subscription(db, owner_id):
    """Subscription whose plan applies to the owner's workspace; admin and service accounts get «Команда»."""
    row = db.execute('''SELECT s.status,s.plan_code,s.ends_at,s.founder_price,s.billing_period,s.provider,s.starts_at,
            s.grace_until,s.autorenew,s.payment_method_title,s.scheduled_plan,s.scheduled_period,u.telegram_user_id
        FROM app_users u LEFT JOIN subscriptions s ON s.user_id=u.id WHERE u.id=?''', (owner_id,)).fetchone()
    if row and is_unlimited(row['telegram_user_id']):
        return service_subscription(row)
    if not row or row['status'] is None:
        return None
    return row


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
    return f" AND {column} <= now()-(? * interval '1 minute')", [minutes]


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
    ends_at = _ends_at(row)
    if ends_at is None:
        return None
    return max(0, (ends_at - (now or datetime.now(timezone.utc))).days)


def founder_signups_open():
    """New registrations get the founder price lock while the beta lasts; BETA_FOUNDER_OPEN=0 ends it."""
    return os.environ.get('BETA_FOUNDER_OPEN', '1').strip() != '0'


def support_contact():
    """Telegram username for sales and support, without @."""
    return os.environ.get('SUPPORT_TELEGRAM', '').strip().lstrip('@')


# --- plan management: manual grants and YooKassa activations ------------------------------------
PAID_PLANS = tuple(plan['code'] for plan in PLANS if plan['price'])
PLAN_CODES = tuple(plan['code'] for plan in PLANS)
PERIODS = {'month': 'месяц', 'year': 'год'}


def plan_by_code(code):
    return next((plan for plan in PLANS if plan['code'] == code), None)


def quote(code, period, founder_price=None):
    """(price for the whole period in rubles, founder price applied) for a paid plan request."""
    plan = plan_by_code(code)
    if not plan or not plan['price'] or period not in PERIODS:
        raise ValueError('Неизвестный тариф или период.')
    founder = code == 'start' and founder_price is not None
    monthly = founder_price if founder else plan['price']
    return (monthly if period == 'month' else annual_monthly_price(monthly) * 12), founder


def add_months(moment, months):
    """Same day and time `months` later; the 31st becomes the last day of a shorter month."""
    import calendar
    month = moment.month - 1 + months
    year, month = moment.year + month // 12, month % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def period_end(current, code, months, now=None):
    """New end date: a running period of the same plan is extended, otherwise counted from now."""
    now = now or datetime.now(timezone.utc)
    start = now
    if current and current['plan_code'] == code and subscription_active(current, now) and _ends_at(current):
        start = _ends_at(current)
    return add_months(start, months)


def format_until(ends_at):
    return f'до {ends_at.astimezone(MSK):%d.%m.%Y}' if ends_at else 'без ограничения срока'


def find_user(db, ident):
    """app_users row by internal id, Telegram id or @username; ValueError when absent or ambiguous."""
    ident = (ident or '').strip()
    if not ident:
        raise ValueError('Укажите пользователя: id, Telegram ID или @username.')
    if ident.startswith('@') or not ident.lstrip('#').isdigit():
        rows = db.execute('SELECT * FROM app_users WHERE lower(username)=lower(?)', (ident.lstrip('@'),)).fetchall()
    else:
        number = int(ident.lstrip('#'))
        rows = db.execute('SELECT * FROM app_users WHERE id=? OR telegram_user_id=?', (number, number)).fetchall()
    if not rows:
        raise ValueError('Пользователь не найден: ' + ident)
    if len(rows) > 1:
        raise ValueError('Неоднозначно: ' + ident + ' совпадает с несколькими пользователями. Укажите @username.')
    return rows[0]


def set_plan(db, user_id, code, ends_at, period='month', founder=None, changed_by=None, source='admin', note=''):
    """Store a plan granted by hand and log the change. founder: None keeps, True/False sets the founder price."""
    with db:
        return write_plan(db, user_id, code, ends_at, period, founder, changed_by, source, note)


def write_plan(db, user_id, code, ends_at, period='month', founder=None, changed_by=None, source='admin', note='',
               provider='manual'):
    """set_plan without its own transaction, for callers that update more rows atomically.

    A manual grant also ends autorenewal and clears a scheduled change and a grace period."""
    if code not in PLAN_CODES:
        raise ValueError('Неизвестный тариф.')
    if period not in PERIODS:
        raise ValueError('Неизвестный период.')
    if code == 'free':
        ends_at = None
    old = db.execute('SELECT plan_code,ends_at,founder_price FROM subscriptions WHERE user_id=? FOR UPDATE',
                     (user_id,)).fetchone()
    if founder is None:
        founder_value = old['founder_price'] if old else None
    else:
        founder_value = FOUNDER_START_PRICE if founder else None
    db.execute('''INSERT INTO subscriptions(user_id,status,plan_code,ends_at,billing_period,founder_price,provider,updated_at)
        VALUES(?,'active',?,?,?,?,?,now()) ON CONFLICT(user_id) DO UPDATE SET status='active',
        plan_code=excluded.plan_code,ends_at=excluded.ends_at,billing_period=excluded.billing_period,
        founder_price=excluded.founder_price,provider=excluded.provider,grace_until=NULL,
        scheduled_plan=NULL,scheduled_period=NULL,
        autorenew=CASE WHEN excluded.provider='manual' THEN false ELSE subscriptions.autorenew END,
        updated_at=now()''',
               (user_id, code, ends_at, period, founder_value, provider))
    db.execute('''INSERT INTO plan_changes(user_id,changed_by,source,old_plan,old_ends_at,new_plan,new_ends_at,
        billing_period,founder_price,note) VALUES(?,?,?,?,?,?,?,?,?,?)''',
               (user_id, changed_by, source, old['plan_code'] if old else None, old['ends_at'] if old else None,
                code, ends_at, period, founder_value, (note or '')[:500]))
    return ends_at


def main(argv=None, db_factory=None):
    """Emergency CLI: python -m leadgen.billing set-plan --user @name --plan pro --months 1"""
    import argparse
    parser = argparse.ArgumentParser(prog='python -m leadgen.billing', description='Ручное управление тарифами')
    sub = parser.add_subparsers(dest='command', required=True)
    cmd = sub.add_parser('set-plan', help='выдать тариф пользователю')
    cmd.add_argument('--user', required=True, help='id, Telegram ID или @username')
    cmd.add_argument('--plan', required=True, choices=PLAN_CODES)
    term = cmd.add_mutually_exclusive_group()
    term.add_argument('--months', type=int, help='срок в месяцах (продлевает текущий срок того же тарифа)')
    term.add_argument('--forever', action='store_true', help='без даты окончания')
    term.add_argument('--until', help='дата окончания YYYY-MM-DD (полночь МСК)')
    cmd.add_argument('--founder', choices=('on', 'off'), help='цена основателя для «Старт»')
    cmd.add_argument('--note', default='')
    args = parser.parse_args(argv)
    if args.plan != 'free' and args.months is None and not args.forever and not args.until:
        parser.error('укажите --months, --until или --forever')
    if args.months is not None and not 1 <= args.months <= 120:
        parser.error('--months: от 1 до 120')
    if db_factory is None:
        from .app import env_load
        from .database import db_open
        env_load()
        db_factory = db_open
    db = db_factory()
    try:
        user = find_user(db, args.user)
        current = db.execute('SELECT status,plan_code,ends_at FROM subscriptions WHERE user_id=?', (user['id'],)).fetchone()
        if args.forever or args.plan == 'free':
            ends_at = None
        elif args.until:
            day = date.fromisoformat(args.until)
            ends_at = datetime(day.year, day.month, day.day, tzinfo=MSK)
        else:
            ends_at = period_end(current, args.plan, args.months)
        period = 'year' if args.months and args.months % 12 == 0 else 'month'
        set_plan(db, user['id'], args.plan, ends_at, period,
                 None if args.founder is None else args.founder == 'on', source='cli', note=args.note)
    except ValueError as exc:
        print('Ошибка: ' + str(exc))
        return 1
    finally:
        db.close()
    name = user['display_name'] or ('@' + user['username'] if user['username'] else 'без имени')
    print(f"Пользователь id {user['id']} ({name}): тариф «{PLAN_NAMES[args.plan]}» {format_until(ends_at)}.")
    return 0


DEFAULT_LEGAL_ENTITY = "ИП Маленюк Никита Романович, ОГРНИП 325911200139330, ИНН 910924258281"


def legal_entity():
    """Seller details for the footer and documents, e.g. «ИП Иванов И. И., ИНН 000000000000»."""
    value = os.environ.get('LEGAL_ENTITY', '').strip()
    if not value or value == '0':
        return DEFAULT_LEGAL_ENTITY
    return value


if __name__ == "__main__":
    raise SystemExit(main())
