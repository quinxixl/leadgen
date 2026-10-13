"""YooKassa payments: checkout with proration, activation, notifications, autorenewal and refunds.

Plans are activated only from a payment object fetched from the YooKassa API (a verified notification,
the return-page status check or the API response itself), idempotently per payment row.
CLI for the hourly timer: python -m leadgen.payments renew
"""
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from . import yookassa
from .billing import (MSK, PAID_PLANS, PERIODS, PLAN_NAMES, add_months, format_until, quote,
                      subscription_active, write_plan)
from .observability import capture_exception

MONTHS = {'month': 1, 'year': 12}
KOPECK = Decimal('0.01')
MIN_CHARGE = Decimal('1.00')
# Renewal attempts relative to the end of the paid period: a day before, at the end, two days after.
RENEW_SCHEDULE = (timedelta(hours=-24), timedelta(0), timedelta(days=2))
RENEW_WINDOW = timedelta(hours=24)
# After a failed renewal the paid plan keeps working until the last attempt resolves (with a margin
# for a missed timer run); the job then moves the account to Free.
GRACE = timedelta(days=3)
# A repeated click on the same checkout reuses the open payment instead of creating a second one.
PENDING_REUSE = timedelta(minutes=30)
# Pending renewal rows are re-checked against the API after this long.
PENDING_RECHECK = timedelta(minutes=10)
# The saved payment method can no longer be charged: stop retrying at once.
FATAL_REASONS = {'permission_revoked', 'card_expired', 'payment_method_restricted', 'fraud_suspected',
                 'country_forbidden', 'issuer_unavailable_forever'}
REASONS = {'insufficient_funds': 'недостаточно средств', 'card_expired': 'истёк срок действия карты',
           'permission_revoked': 'отозвано разрешение на списание', 'expired_on_confirmation': 'платёж не подтверждён вовремя',
           'canceled_by_merchant': 'платёж отменён', '3d_secure_failed': 'не пройдена проверка 3-D Secure',
           'call_issuer': 'банк отклонил платёж', 'payment_method_limit_exceeded': 'превышен лимит по карте',
           'payment_method_restricted': 'операции по карте ограничены', 'fraud_suspected': 'банк заподозрил мошенничество',
           'general_decline': 'банк отклонил платёж', 'country_forbidden': 'оплата картой этой страны недоступна',
           'issuer_unavailable': 'банк временно недоступен', 'internal_timeout': 'технический сбой'}
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
SITE = 'Сигналид'
DOWNGRADE_ERROR = ('Понизить тариф нельзя. Можно отменить подписку — тариф будет действовать '
                   'до конца оплаченного срока.')


class PaymentError(ValueError):
    """A user-facing checkout problem."""


# --- money and proration ------------------------------------------------------------------------
def money(value):
    return Decimal(str(value)).quantize(KOPECK, rounding=ROUND_HALF_UP)


def amount_str(value):
    return f'{money(value):.2f}'


def plan_price(code, period, founder_price=None):
    """Full price of a paid plan for one billing period, from PLANS (founder price and annual discount applied)."""
    return money(quote(code, period, founder_price)[0])


def _dt(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _get(row, key, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    return row[key] if key in row.keys() else default


def paid_running(current, now):
    """The current period was paid through YooKassa and is running (not in a failed-renewal grace)."""
    return bool(current and _get(current, 'provider') == 'yookassa' and _get(current, 'status') == 'active'
                and _get(current, 'plan_code') in PAID_PLANS and _get(current, 'ends_at') is not None
                and subscription_active(current, now))


def change_kind(current_plan, current_period, plan, period):
    """How a running YooKassa-paid plan/period may change: 'same', 'upgrade' or 'downgrade' (not allowed).

    Upgrades only: a strictly higher plan with the same or a longer period, or the same plan month→year.
    A lower or equal plan with the same period and any year→month change are downgrades."""
    if plan == current_plan and period == current_period:
        return 'same'
    rank, current_rank = PAID_PLANS.index(plan), PAID_PLANS.index(current_plan)
    longer = current_period == 'month' and period == 'year'
    if (period == current_period and rank > current_rank) or (longer and rank >= current_rank):
        return 'upgrade'
    return 'downgrade'


def plan_options(current, now):
    """Per paid plan and period, what the billing cards offer: {'pro': {'month': 'same', 'year': 'upgrade'}, ...}.

    None when nothing was paid through YooKassa for the running period: every paid plan can be bought."""
    if not paid_running(current, now):
        return None
    current_plan, current_period = current['plan_code'], _get(current, 'billing_period') or 'month'
    return {plan: {period: change_kind(current_plan, current_period, plan, period) for period in PERIODS}
            for plan in PAID_PLANS}


def proration(current, plan, period, now, paid=None, founder_price=None):
    """What switching to plan/period costs now. Pure: no database, no clock.

    current: subscription row (status, plan_code, billing_period, ends_at, provider) or None.
    paid: the succeeded YooKassa payment covering the current period, as
        {'value': rubles applied to the period, 'period_start', 'period_end'}, or None.
    Returns kind (initial | upgrade | same), price, credit, amount to charge now and starts_at/ends_at
    of the new period. A downgrade of a running paid period raises PaymentError: the only way down is
    cancelling the subscription (autorenewal off), after which the plan runs out and the account goes Free."""
    if plan not in PAID_PLANS or period not in PERIODS:
        raise PaymentError('Выберите платный тариф и период.')
    price = plan_price(plan, period, founder_price)
    months = MONTHS[period]
    result = {'plan': plan, 'period': period, 'price': price, 'credit': money(0), 'amount': price,
              'starts_at': now, 'ends_at': add_months(now, months), 'kind': 'initial'}
    if not paid_running(current, now):
        # Trial, Free, beta or a manual grant: nothing was paid for the current period.
        return result
    current_plan, current_period = current['plan_code'], _get(current, 'billing_period') or 'month'
    current_end = _dt(current['ends_at'])
    if plan == current_plan and period == current_period:
        return result | {'kind': 'same', 'amount': money(0), 'starts_at': current_end, 'ends_at': current_end}
    if change_kind(current_plan, current_period, plan, period) != 'upgrade':
        raise PaymentError(DOWNGRADE_ERROR)
    credit = money(0)
    if paid and paid.get('period_end') is not None and abs((_dt(paid['period_end']) - current_end).total_seconds()) < 1:
        start, end = _dt(paid['period_start']), _dt(paid['period_end'])
        length = (end - start).total_seconds()
        remaining = max(0.0, min(length, (end - now).total_seconds()))
        if length > 0:
            credit = money(max(Decimal(0), money(paid['value'])) * Decimal(remaining) / Decimal(length))
    amount = max(MIN_CHARGE, price - credit)
    return result | {'kind': 'upgrade', 'credit': price - amount, 'amount': amount}


def preview_json(result):
    """JSON-ready proration preview for the billing UI."""
    plan, period = result['plan'], result['period']
    data = {'ok': True, 'kind': result['kind'], 'plan': plan, 'plan_name': PLAN_NAMES[plan], 'period': period,
            'period_name': PERIODS[period], 'currency': 'RUB', 'price': amount_str(result['price']),
            'credit': amount_str(result['credit']), 'amount': amount_str(result['amount']),
            'starts_at': result['starts_at'].isoformat(), 'ends_at': result['ends_at'].isoformat()}
    if result['kind'] == 'same':
        data['message'] = 'Этот тариф уже подключён.'
    return data


# --- database helpers --------------------------------------------------------------------------
def subscription(db, owner_id):
    return db.execute('SELECT * FROM subscriptions WHERE user_id=?', (owner_id,)).fetchone()


def covering_payment(db, owner_id, period_end):
    """Last succeeded payment whose period ends with the current one, as {'value', 'period_start', 'period_end'}.

    value = amount charged + credit carried over − refunds: what the period is actually worth."""
    if period_end is None:
        return None
    row = db.execute('''SELECT p.amount,p.credit,p.period_start,p.period_end,
            COALESCE((SELECT sum(r.amount) FROM payment_refunds r WHERE r.payment_id=p.id AND r.status IN ('pending','succeeded')),0) AS refunded
        FROM payments p WHERE p.user_id=? AND p.status='succeeded' AND p.period_end=? AND p.period_start IS NOT NULL
        ORDER BY p.paid_at DESC NULLS LAST LIMIT 1''', (owner_id, period_end)).fetchone()
    if not row:
        return None
    return {'value': money(row['amount']) + money(row['credit']) - money(row['refunded']),
            'period_start': row['period_start'], 'period_end': row['period_end']}


def preview(db, owner_id, plan, period, now=None):
    now = now or datetime.now(timezone.utc)
    current = subscription(db, owner_id)
    paid = covering_payment(db, owner_id, current['ends_at']) if current else None
    return proration(current, plan, period, now, paid, current['founder_price'] if current else None)


def normalize_email(value):
    email = (value or '').strip()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise PaymentError('Укажите e-mail для чека, например name@example.ru.')
    return email


def receipt(email, description, amount):
    """54-ФЗ receipt for «Чеки от ЮKassa»: one service item equal to the payment amount."""
    try:
        vat_code = int(os.environ.get('YOOKASSA_VAT_CODE', '1').strip() or 1)
    except ValueError:
        vat_code = 1
    data = {'customer': {'email': email},
            'items': [{'description': description[:128], 'quantity': '1.00',
                       'amount': {'value': amount_str(amount), 'currency': 'RUB'}, 'vat_code': vat_code,
                       'payment_mode': 'full_payment', 'payment_subject': 'service'}]}
    tax = os.environ.get('YOOKASSA_TAX_SYSTEM_CODE', '').strip()
    if tax.isdigit():
        data['tax_system_code'] = int(tax)
    return data


def describe(code, period):
    return f"{SITE}: тариф «{PLAN_NAMES[code]}» на 1 {PERIODS[period]}"[:128]


def payment_body(row, return_url=None):
    """Request body for a payment row; deterministic so an unknown outcome can be re-sent with the same key."""
    description = describe(row['plan_code'], row['billing_period'])
    body = {'amount': {'value': amount_str(row['amount']), 'currency': 'RUB'}, 'capture': True,
            'description': description,
            'metadata': {'payment_row_id': str(row['id']), 'owner_id': str(row['user_id']), 'kind': row['kind'],
                         'plan': row['plan_code'], 'period': row['billing_period']}}
    if row['email']:
        body['receipt'] = receipt(row['email'], description, row['amount'])
    if row['kind'] == 'renewal':
        body['payment_method_id'] = row['payment_method_id']
    else:
        body['save_payment_method'] = True
        body['confirmation'] = {'type': 'redirect', 'return_url': return_url}
    return body


# --- checkout ----------------------------------------------------------------------------------
def checkout(db, client, user, owner_id, plan, period, email, consent, return_url, now=None):
    """Start a payment for a new plan/period or an upgrade (downgrades raise PaymentError in preview).

    Returns {'status': 'redirect', 'confirmation_url', 'payment_id', 'preview'}.
    return_url gets ?payment=<our payment id> appended."""
    now = now or datetime.now(timezone.utc)
    if plan not in PAID_PLANS or period not in PERIODS:
        raise PaymentError('Выберите платный тариф и период.')
    email = normalize_email(email)
    if not consent:
        raise PaymentError('Подтвердите согласие на автопродление подписки по условиям оферты.')
    result = preview(db, owner_id, plan, period, now)
    with db:
        db.execute('UPDATE app_users SET email=? WHERE id=?', (email, user['id']))
    if result['kind'] == 'same':
        raise PaymentError('Этот тариф уже подключён. Продление произойдёт автоматически, если включено автопродление.')
    with db:
        reuse = db.execute('''SELECT * FROM payments WHERE user_id=? AND status='pending' AND kind=? AND plan_code=?
            AND billing_period=? AND amount=? AND confirmation_url IS NOT NULL AND created_at>? AND email=?
            ORDER BY created_at DESC LIMIT 1''',
                           (owner_id, result['kind'], plan, period, result['amount'], now - PENDING_REUSE, email)).fetchone()
        db.execute('UPDATE subscriptions SET autorenew_consent_at=?,updated_at=now() WHERE user_id=?', (now, owner_id))
    if reuse:
        return {'status': 'redirect', 'confirmation_url': reuse['confirmation_url'], 'payment_id': str(reuse['id']),
                'preview': result}
    row_id = str(uuid.uuid4())
    metadata = {'autorenew_consent_at': now.isoformat(), 'price': amount_str(result['price'])}
    with db:
        row = db.execute('''INSERT INTO payments(id,user_id,created_by,kind,plan_code,billing_period,amount,credit,email,metadata)
            VALUES(?,?,?,?,?,?,?,?,?,?::jsonb) RETURNING *''',
                         (row_id, owner_id, user['id'], result['kind'], plan, period, result['amount'], result['credit'],
                          email, json.dumps(metadata))).fetchone()
    separator = '&' if '?' in return_url else '?'
    try:
        obj = client.create_payment(payment_body(row, f'{return_url}{separator}payment={row_id}'), row_id)
    except yookassa.YooKassaError as exc:
        capture_exception(exc, component='payments', user_id=owner_id)
        with db:
            db.execute('''UPDATE payments SET status='failed',cancellation_reason=?,updated_at=now() WHERE id=? AND status='pending' ''',
                       (str(exc)[:300], row_id))
        raise PaymentError('Не удалось создать платёж. Попробуйте ещё раз через минуту.') from None
    url = (obj.get('confirmation') or {}).get('confirmation_url')
    with db:
        db.execute('UPDATE payments SET yookassa_payment_id=?,confirmation_url=?,updated_at=now() WHERE id=?',
                   (obj.get('id'), url, row_id))
    if obj.get('status') in ('succeeded', 'canceled') or not url:
        process_payment(db, obj, now)
        if not url:
            raise PaymentError('Платёж отклонён. Попробуйте другой способ оплаты.')
    return {'status': 'redirect', 'confirmation_url': url, 'payment_id': row_id, 'preview': result}


# --- activation --------------------------------------------------------------------------------
def _amount_matches(row, obj):
    amount = obj.get('amount') or {}
    try:
        return amount.get('currency') == 'RUB' and money(amount.get('value')) == money(row['amount'])
    except Exception:
        return False


def _lock_row(db, obj):
    row = db.execute('SELECT * FROM payments WHERE yookassa_payment_id=? FOR UPDATE', (obj.get('id'),)).fetchone()
    if row:
        return row
    row_id = (obj.get('metadata') or {}).get('payment_row_id')
    try:
        row_id = str(uuid.UUID(str(row_id)))
    except ValueError:
        return None
    # The create call succeeded but its id was not stored (crash between the API call and the update).
    return db.execute('SELECT * FROM payments WHERE id=? AND yookassa_payment_id IS NULL FOR UPDATE', (row_id,)).fetchone()


def process_payment(db, obj, now=None):
    """Apply a payment object fetched from the YooKassa API. Idempotent: a final row is never processed twice.

    Returns {'event': activated | canceled | retry | expired | lapsing, 'row', ...} or None when nothing changed."""
    now = now or datetime.now(timezone.utc)
    status = obj.get('status')
    with db:
        row = _lock_row(db, obj)
        # Final rows are never processed twice. A 'failed' row without a YooKassa id (create call with an
        # unknown outcome) may still turn out paid and is activated then.
        if not row or row['status'] in ('succeeded', 'canceled') or (row['status'] == 'failed' and row['yookassa_payment_id']):
            return None
        if status == 'succeeded':
            if not _amount_matches(row, obj):
                capture_exception(RuntimeError('YooKassa amount mismatch'), component='payments', user_id=row['user_id'])
                return None
            return _activate(db, row, obj, now)
        if status == 'canceled':
            details = obj.get('cancellation_details') or {}
            db.execute('''UPDATE payments SET status='canceled',yookassa_payment_id=?,cancellation_party=?,
                cancellation_reason=?,updated_at=now() WHERE id=?''',
                       (obj.get('id'), details.get('party'), details.get('reason'), row['id']))
            if row['kind'] == 'renewal':
                return _renewal_failed(db, row, details.get('reason'), now)
            return {'event': 'canceled', 'row': row, 'reason': details.get('reason')}
    return None


def _activate(db, row, obj, now):
    start = _dt(row['period_start']) if row['kind'] == 'renewal' and row['period_start'] else now
    end = add_months(start, MONTHS[row['billing_period']])
    method = obj.get('payment_method') or {}
    saved = method.get('id') if method.get('saved') is True and method.get('id') else None
    title = None
    if saved:
        card = method.get('card') or {}
        title = (method.get('title') or (f"{card.get('card_type') or 'Карта'} *{card['last4']}" if card.get('last4') else '')
                 or 'Сохранённый способ оплаты')[:100]
    db.execute('''UPDATE payments SET status='succeeded',yookassa_payment_id=?,paid_at=?,period_start=?,period_end=?,
        payment_method_id=COALESCE(?,payment_method_id),updated_at=now() WHERE id=?''',
               (obj.get('id'), now, start, end, saved, row['id']))
    write_plan(db, row['user_id'], row['plan_code'], end, row['billing_period'], None, row['created_by'],
               source='payment', note=f"ЮKassa {obj.get('id')}: {amount_str(row['amount'])} ₽ ({row['kind']})",
               provider='yookassa')
    db.execute('''UPDATE subscriptions SET starts_at=?,
        payment_method_id=COALESCE(?,payment_method_id),payment_method_title=COALESCE(?,payment_method_title),
        autorenew=CASE WHEN ? THEN true ELSE autorenew END,updated_at=now() WHERE user_id=?''',
               (start, saved, title, bool(saved), row['user_id']))
    return {'event': 'activated', 'row': row, 'ends_at': end, 'starts_at': start}


def _expire(db, owner_id, note):
    write_plan(db, owner_id, 'free', None, 'month', None, None, source='payment', note=note, provider='yookassa')
    db.execute('UPDATE subscriptions SET autorenew=false,updated_at=now() WHERE user_id=?', (owner_id,))


def _renewal_failed(db, row, reason, now):
    """A renewal attempt failed: schedule the next try, or stop and move to Free after the last one."""
    sub = db.execute('SELECT * FROM subscriptions WHERE user_id=? FOR UPDATE', (row['user_id'],)).fetchone()
    period_start = _dt(row['period_start'])
    if not sub or sub['ends_at'] is None or abs((_dt(sub['ends_at']) - period_start).total_seconds()) >= 1:
        return {'event': 'canceled', 'row': row, 'reason': reason}
    failures = db.execute('''SELECT count(*) AS n FROM payments WHERE user_id=? AND kind='renewal' AND period_start=?
        AND status IN ('canceled','failed')''', (row['user_id'], period_start)).fetchone()['n']
    final = failures >= len(RENEW_SCHEDULE) or reason in FATAL_REASONS
    if final:
        if reason in FATAL_REASONS:
            db.execute('UPDATE subscriptions SET payment_method_id=NULL,payment_method_title=NULL WHERE user_id=?',
                       (row['user_id'],))
        if now >= period_start:
            _expire(db, row['user_id'], 'Автопродление не удалось' + (f': {reason}' if reason else ''))
            return {'event': 'expired', 'row': row, 'reason': reason}
        # The paid period is still running: it simply ends without renewal.
        db.execute('''UPDATE subscriptions SET autorenew=false,status='active',grace_until=NULL,updated_at=now()
            WHERE user_id=?''', (row['user_id'],))
        return {'event': 'lapsing', 'row': row, 'reason': reason, 'ends_at': period_start}
    db.execute('''UPDATE subscriptions SET status='past_due',grace_until=?,updated_at=now() WHERE user_id=?''',
               (period_start + GRACE, row['user_id']))
    return {'event': 'retry', 'row': row, 'reason': reason, 'next_at': period_start + RENEW_SCHEDULE[failures]}


# --- notifications -----------------------------------------------------------------------------
def reason_text(reason):
    return REASONS.get(reason or '', 'платёж отклонён')


def notify(db, sender, result):
    """Tell the subscriber (and the service owner, for income) what happened; best effort."""
    if not result or not sender:
        return
    row = result['row']
    user = db.execute('SELECT * FROM app_users WHERE id=?', (row['user_id'],)).fetchone()
    if not user:
        return
    name = PLAN_NAMES[row['plan_code']]
    event = result['event']
    if event == 'activated':
        if row['kind'] == 'renewal':
            text = (f"Подписка продлена: тариф «{name}» {format_until(result['ends_at'])}, "
                    f"списано {amount_str(row['amount'])} ₽. Управлять автопродлением можно в разделе «Подписка».")
        else:
            text = (f"Оплата прошла: тариф «{name}» подключён {format_until(result['ends_at'])}. "
                    'Автопродление можно отключить в разделе «Подписка». Спасибо, что вы с Сигналид!')
    elif event == 'retry':
        text = (f"Не удалось списать оплату за тариф «{name}»: {reason_text(result['reason'])}. "
                f"Повторим попытку {result['next_at'].astimezone(MSK):%d.%m в %H:%M МСК}. "
                'Проверьте карту или оплатите тариф в разделе «Подписка».')
    elif event == 'expired':
        text = (f"Не удалось продлить тариф «{name}»: {reason_text(result['reason'])}. Аккаунт переведён на бесплатный "
                'тариф. Подключить тариф снова можно в разделе «Подписка».')
    elif event == 'lapsing':
        text = (f"Автопродление тарифа «{name}» отключено: {reason_text(result['reason'])}. Тариф действует "
                f"{format_until(result['ends_at'])}. Продлить его можно в разделе «Подписка».")
    else:
        return
    try:
        sender('sendMessage', {'chat_id': str(user['telegram_chat_id']), 'text': text})
    except Exception as exc:
        capture_exception(exc, component='payments', user_id=user['id'])
        print('Сообщение об оплате: ' + type(exc).__name__, flush=True)
    if event == 'activated':
        from .plan_requests import notify_owner
        who = f"id {user['id']}" + (f" · @{user['username']}" if user['username'] else '')
        notify_owner(sender, f"💳 Оплата ЮKassa ({row['kind']}): {who}, «{name}», {PERIODS[row['billing_period']]}, "
                             f"{amount_str(row['amount'])} ₽")


# --- return page fallback ----------------------------------------------------------------------
def refresh(db, client, payment_id, owner_id, now=None, check=True):
    """Return-page check: re-fetch a pending payment of this owner from the API and apply it.

    Returns (payment row, process result or None); row is None for an unknown id."""
    try:
        payment_id = str(uuid.UUID(str(payment_id)))
    except ValueError:
        return None, None
    row = db.execute('SELECT * FROM payments WHERE id=? AND user_id=?', (payment_id, owner_id)).fetchone()
    result = None
    if check and row and row['status'] == 'pending' and row['yookassa_payment_id']:
        result = process_payment(db, client.get_payment(row['yookassa_payment_id']), now)
        row = db.execute('SELECT * FROM payments WHERE id=?', (payment_id,)).fetchone()
    return row, result


# --- webhook ----------------------------------------------------------------------------------
def handle_notification(db, client, data, now=None):
    """Process a YooKassa notification body (source IP already checked): the object is re-fetched from the API
    and only the fetched copy is trusted. Returns the process result for notifications, or None."""
    if not isinstance(data, dict) or data.get('type') != 'notification':
        raise ValueError('not a notification')
    event, obj = str(data.get('event') or ''), data.get('object') or {}
    object_id = obj.get('id') if isinstance(obj, dict) else None
    if not object_id or not isinstance(object_id, str) or len(object_id) > 64:
        raise ValueError('no object id')
    if event.startswith('payment.'):
        return process_payment(db, client.get_payment(object_id), now)
    if event.startswith('refund.'):
        process_refund(db, client.get_refund(object_id))
    return None


# --- autorenew --------------------------------------------------------------------------------
def set_autorenew(db, owner_id, enabled, consent=False, now=None):
    """Resume (autorenewal on: needs a saved payment method and consent) or cancel the subscription (autorenewal
    off: the plan stays until the paid period ends, then the account goes Free; the saved method is kept)."""
    now = now or datetime.now(timezone.utc)
    with db:
        sub = db.execute('SELECT * FROM subscriptions WHERE user_id=? FOR UPDATE', (owner_id,)).fetchone()
        if not sub:
            raise PaymentError('Подписка не найдена.')
        if enabled:
            if not consent:
                raise PaymentError('Подтвердите согласие на автопродление подписки по условиям оферты.')
            if not sub['payment_method_id'] or sub['provider'] != 'yookassa' or sub['plan_code'] not in PAID_PLANS:
                raise PaymentError('Автопродление включится после оплаты тарифа картой.')
            db.execute('UPDATE subscriptions SET autorenew=true,autorenew_consent_at=?,updated_at=now() WHERE user_id=?',
                       (now, owner_id))
        else:
            db.execute('UPDATE subscriptions SET autorenew=false,updated_at=now() WHERE user_id=?', (owner_id,))
    return enabled


# --- renewal job -------------------------------------------------------------------------------
def _renewal_row(db, sub, now):
    """Create the renewal payment row of the current period, or None if one is live or not due yet."""
    owner, period_start = sub['user_id'], _dt(sub['ends_at'])
    failures = db.execute('''SELECT count(*) AS n FROM payments WHERE user_id=? AND kind='renewal' AND period_start=?
        AND status IN ('canceled','failed')''', (owner, period_start)).fetchone()['n']
    if failures >= len(RENEW_SCHEDULE) or now < period_start + RENEW_SCHEDULE[failures]:
        return None
    # Always the current plan and period. scheduled_plan/scheduled_period (left from removed scheduled downgrades)
    # are ignored.
    plan, period = sub['plan_code'], sub['billing_period'] or 'month'
    amount = plan_price(plan, period, sub['founder_price'])
    email = db.execute('SELECT email FROM app_users WHERE id=?', (owner,)).fetchone()['email']
    if not email:
        email = db.execute('''SELECT email FROM payments WHERE user_id=? AND email IS NOT NULL
            ORDER BY created_at DESC LIMIT 1''', (owner,)).fetchone()
        email = email['email'] if email else None
    with db:
        # The partial unique index keeps one live or successful renewal per period: never a double charge.
        return db.execute('''INSERT INTO payments(id,user_id,kind,plan_code,billing_period,amount,credit,status,
                period_start,period_end,payment_method_id,email,metadata)
            VALUES(?,?,'renewal',?,?,?,0,'pending',?,?,?,?,?::jsonb) ON CONFLICT DO NOTHING RETURNING *''',
                          (str(uuid.uuid4()), owner, plan, period, amount, period_start,
                           add_months(period_start, MONTHS[period]), sub['payment_method_id'], email,
                           json.dumps({'attempt': failures + 1}))).fetchone()


def _charge(db, client, row, now):
    """Send a renewal payment (again, with the same Idempotence-Key = our row id) and apply the response."""
    try:
        obj = client.create_payment(payment_body(row), str(row['id']))
    except yookassa.YooKassaUnavailable as exc:
        # Unknown outcome: the row stays pending and is re-sent with the same key on the next run.
        capture_exception(exc, component='payments', user_id=row['user_id'])
        return None
    except yookassa.YooKassaError as exc:
        capture_exception(exc, component='payments', user_id=row['user_id'])
        with db:
            locked = db.execute("SELECT * FROM payments WHERE id=? AND status='pending' FOR UPDATE", (row['id'],)).fetchone()
            if not locked:
                return None
            db.execute('''UPDATE payments SET status='failed',cancellation_reason=?,updated_at=now() WHERE id=?''',
                       ((exc.code or str(exc))[:300], row['id']))
            return _renewal_failed(db, locked, exc.code, now)
    with db:
        db.execute('UPDATE payments SET yookassa_payment_id=?,updated_at=now() WHERE id=? AND yookassa_payment_id IS NULL',
                   (obj.get('id'), row['id']))
    return process_payment(db, obj, now)


def renew(db, client, sender=None, now=None):
    """Hourly: charge subscriptions ending within 24 hours, retry failures and expire exhausted ones.

    Returns a list of process results (one per state change)."""
    now = now or datetime.now(timezone.utc)
    results = []
    # Safety net: a failed renewal whose grace ran out without a final attempt (e.g. autorenew turned off meanwhile).
    for sub in db.execute("SELECT user_id,plan_code FROM subscriptions WHERE status='past_due' AND grace_until<=?",
                          (now,)).fetchall():
        with db:
            _expire(db, sub['user_id'], 'Льготный период после неудачного автопродления закончился')
    subs = db.execute('''SELECT * FROM subscriptions WHERE provider='yookassa' AND autorenew AND payment_method_id IS NOT NULL
        AND status IN ('active','past_due') AND plan_code IN ('start','pro','team') AND ends_at IS NOT NULL AND ends_at<=?
        ORDER BY ends_at''', (now + RENEW_WINDOW,)).fetchall()
    for sub in subs:
        try:
            live = db.execute('''SELECT * FROM payments WHERE user_id=? AND kind='renewal' AND period_start=?
                AND status IN ('pending','succeeded')''', (sub['user_id'], sub['ends_at'])).fetchone()
            if live and live['status'] == 'succeeded':
                continue
            if live:
                if live['yookassa_payment_id']:
                    if now - _dt(live['updated_at']) < PENDING_RECHECK:
                        continue
                    result = process_payment(db, client.get_payment(live['yookassa_payment_id']), now)
                else:
                    result = _charge(db, client, live, now)
            else:
                row = _renewal_row(db, sub, now)
                if not row:
                    continue
                result = _charge(db, client, row, now)
        except Exception as exc:
            db.connection.rollback()
            capture_exception(exc, component='payments', user_id=sub['user_id'])
            print(f"Автопродление id {sub['user_id']}: {type(exc).__name__}", flush=True)
            continue
        if result:
            results.append(result)
            notify(db, sender, result)
    return results


# --- refunds ----------------------------------------------------------------------------------
def refund(db, client, payment_id, amount=None, admin_id=None):
    """Refund a succeeded payment fully (amount None) or partly. Returns the refund row."""
    try:
        payment_id = str(uuid.UUID(str(payment_id)))
    except ValueError:
        raise PaymentError('Платёж не найден.') from None
    row = db.execute("SELECT * FROM payments WHERE id=? AND status='succeeded'", (payment_id,)).fetchone()
    if not row or not row['yookassa_payment_id']:
        raise PaymentError('Вернуть можно только успешный платёж.')
    refunded = db.execute('''SELECT COALESCE(sum(amount),0) AS total FROM payment_refunds
        WHERE payment_id=? AND status IN ('pending','succeeded')''', (payment_id,)).fetchone()['total']
    available = money(row['amount']) - money(refunded)
    try:
        value = available if amount in (None, '') else money(str(amount).replace(',', '.').replace(' ', ''))
    except Exception:
        raise PaymentError('Укажите сумму возврата в рублях, например 990.00.') from None
    if value <= 0 or value > available:
        raise PaymentError(f'Сумма возврата должна быть от 0.01 до {amount_str(available)} ₽.')
    refund_id = str(uuid.uuid4())
    with db:
        db.execute('INSERT INTO payment_refunds(id,payment_id,amount,created_by) VALUES(?,?,?,?)',
                   (refund_id, payment_id, value, admin_id))
    body = {'payment_id': row['yookassa_payment_id'], 'amount': {'value': amount_str(value), 'currency': 'RUB'},
            'description': ('Возврат: ' + describe(row['plan_code'], row['billing_period']))[:250]}
    if row['email']:
        body['receipt'] = receipt(row['email'], describe(row['plan_code'], row['billing_period']), value)
    try:
        obj = client.create_refund(body, refund_id)
    except yookassa.YooKassaError as exc:
        definite = not isinstance(exc, yookassa.YooKassaUnavailable)
        with db:
            if definite:
                db.execute("UPDATE payment_refunds SET status='failed',updated_at=now() WHERE id=?", (refund_id,))
        raise PaymentError('ЮKassa не приняла возврат: ' + str(exc)
                           if definite else 'ЮKassa не ответила; статус возврата уточнится по уведомлению.') from None
    status = obj.get('status') if obj.get('status') in ('pending', 'succeeded', 'canceled') else 'pending'
    with db:
        return db.execute('''UPDATE payment_refunds SET yookassa_refund_id=?,status=?,updated_at=now() WHERE id=?
            RETURNING *''', (obj.get('id'), status, refund_id)).fetchone()


def process_refund(db, obj):
    """Apply a refund object fetched from the API (refund.succeeded / canceled notifications)."""
    status = obj.get('status')
    if status not in ('succeeded', 'canceled'):
        return None
    with db:
        return db.execute('''UPDATE payment_refunds SET status=?,updated_at=now()
            WHERE yookassa_refund_id=? AND status<>? RETURNING *''', (status, obj.get('id'), status)).fetchone()


# --- admin helpers ----------------------------------------------------------------------------
def recent_payments(db, limit=50):
    return db.execute('''SELECT p.*,u.display_name,u.username,
            COALESCE((SELECT sum(r.amount) FROM payment_refunds r WHERE r.payment_id=p.id AND r.status IN ('pending','succeeded')),0)
                AS refunded
        FROM payments p JOIN app_users u ON u.id=p.user_id ORDER BY p.created_at DESC LIMIT ?''', (limit,)).fetchall()


# --- routes -------------------------------------------------------------------------------------
def billing_context(db, app):
    """Template variables for «Подписка» pages (cabinet and Mini App)."""
    from flask import current_app, g
    from .teams import EDIT_SETTINGS
    enabled = bool(current_app.config.get('YOOKASSA_CLIENT_FACTORY')) or yookassa.enabled()
    last = next_charge = options = None
    if enabled and g.get('owner_id'):
        last = db.execute('SELECT * FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT 1', (g.owner_id,)).fetchone()
        sub = subscription(db, g.owner_id)
        options = plan_options(sub, datetime.now(timezone.utc)) if sub else None
        if sub and sub['autorenew'] and sub['provider'] == 'yookassa' and sub['ends_at'] is not None:
            plan, period = sub['plan_code'], sub['billing_period'] or 'month'
            if plan in PAID_PLANS:
                next_charge = {'plan': plan, 'plan_name': PLAN_NAMES[plan], 'period': period, 'at': _dt(sub['ends_at']),
                               'amount': plan_price(plan, period, sub['founder_price'])}
    # A checkout started in the last hour and not confirmed yet: the page offers to check its status.
    open_payment = None
    if last and last['status'] == 'pending' and last['kind'] != 'renewal' and last['confirmation_url'] \
            and datetime.now(timezone.utc) - _dt(last['created_at']) < timedelta(hours=1):
        open_payment = last
    return {'payments_enabled': enabled, 'payment_email': (g.user['email'] if g.get('user') else None) or '',
            'can_pay': bool(g.get('workspace')) and g.get('role') in EDIT_SETTINGS, 'last_payment': last,
            'next_charge': next_charge, 'open_payment': open_payment, 'period_names': PERIODS, 'plan_options': options}


CHECKOUT_LIMIT = (10, 3600)


def client_ip(request):
    """Notification source address. Behind nginx (YOOKASSA_TRUST_PROXY=1, default) it comes from X-Real-IP;
    otherwise the socket peer is used and forwarded headers are ignored."""
    if os.environ.get('YOOKASSA_TRUST_PROXY', '1').strip() not in ('0', 'false', 'no', ''):
        real = request.headers.get('X-Real-IP', '').strip()
        return real or request.remote_addr
    original = request.environ.get('werkzeug.proxy_fix.orig') or {}
    return original.get('REMOTE_ADDR') or request.environ.get('REMOTE_ADDR')


def register_payment_routes(app, db):
    from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request
    from .rate_limit import RateLimited, hit
    from .teams import EDIT_SETTINGS
    from .web import is_platform_admin

    def client():
        factory = current_app.config.get('YOOKASSA_CLIENT_FACTORY')
        return factory() if factory else yookassa.Client()

    def enabled():
        return bool(current_app.config.get('YOOKASSA_CLIENT_FACTORY')) or yookassa.enabled()

    def sender():
        custom = current_app.config.get('TELEGRAM_SENDER')
        if custom:
            return custom
        from .app import telegram
        return telegram

    def wants_json():
        return 'application/json' in request.headers.get('Accept', '')

    def base(tg):
        return '/tg/billing' if tg else '/app/billing'

    def public_root():
        root = current_app.config.get('WEB_PUBLIC_URL') or ''
        return root.rstrip('/') or request.host_url.rstrip('/')

    def fail(message, back, code=400):
        if wants_json():
            return jsonify({'ok': False, 'error': message}), code
        flash(message, 'error')
        return redirect(back)

    def can_pay():
        return bool(g.workspace) and g.role in EDIT_SETTINGS

    def checkout_view(tg):
        back = base(tg)
        if not enabled():
            abort(404)
        if not can_pay():
            return fail('Подпиской управляет владелец или администратор команды.', back, 403)
        plan, period = request.form.get('plan', ''), request.form.get('period', 'month')
        if plan not in PAID_PLANS or period not in PERIODS:
            abort(400)
        try:
            hit(db(), 'checkout', g.user['id'], *CHECKOUT_LIMIT)
            result = checkout(db(), client(), g.user, g.owner_id, plan, period, request.form.get('email', '')[:254],
                              request.form.get('consent') in ('1', 'on', 'true', 'yes'), public_root() + back + '/return')
        except (RateLimited, PaymentError) as exc:
            return fail(str(exc), back)
        data = preview_json(result['preview']) | {'status': result['status'], 'confirmation_url': result['confirmation_url'],
                                                  'payment_id': result['payment_id']}
        if wants_json():
            return jsonify(data)
        return redirect(result['confirmation_url'], 303)

    def preview_view():
        if not enabled():
            abort(404)
        plan, period = request.args.get('plan', ''), request.args.get('period', 'month')
        if plan not in PAID_PLANS or period not in PERIODS:
            return jsonify({'ok': False, 'error': 'Выберите платный тариф и период.'}), 400
        try:
            result = preview(db(), g.owner_id, plan, period)
        except PaymentError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        return jsonify(preview_json(result))

    def return_view(tg):
        if not enabled():
            abort(404)
        payment_id = request.args.get('payment', '')[:64]
        try:
            row, result = refresh(db(), client(), payment_id, g.owner_id)
        except yookassa.YooKassaError as exc:
            # The API is unreachable: show the stored status; the notification will activate the plan.
            capture_exception(exc, component='payments')
            db().connection.rollback()
            row, result = refresh(db(), None, payment_id, g.owner_id, check=False)
        if not row:
            abort(404)
        notify(db(), sender(), result)
        sub = subscription(db(), g.owner_id)
        data = {'ok': True, 'payment_id': str(row['id']), 'status': row['status'], 'kind': row['kind'],
                'plan': row['plan_code'], 'plan_name': PLAN_NAMES[row['plan_code']], 'period': row['billing_period'],
                'amount': amount_str(row['amount']),
                'ends_at': row['period_end'].isoformat() if row['period_end'] else None,
                'reason': reason_text(row['cancellation_reason']) if row['status'] in ('canceled', 'failed') else None,
                'autorenew': bool(sub and sub['autorenew'])}
        if wants_json():
            return jsonify(data)
        template = 'tg/billing_return.html' if tg else 'billing_return.html'
        return render_template(template, payment=data, back=base(tg))

    def autorenew_view(tg):
        back = base(tg)
        if not can_pay():
            return fail('Подпиской управляет владелец или администратор команды.', back, 403)
        on = request.form.get('enabled') in ('1', 'on', 'true', 'yes')
        try:
            set_autorenew(db(), g.owner_id, on, request.form.get('consent') in ('1', 'on', 'true', 'yes'))
        except PaymentError as exc:
            return fail(str(exc), back)
        sub = subscription(db(), g.owner_id)
        until = f"{_dt(sub['ends_at']).astimezone(MSK):%d.%m.%Y}" if sub and sub['ends_at'] else None
        if on:
            message = 'Подписка возобновлена: продлим тариф в конце оплаченного периода.'
        else:
            message = 'Подписка отменена' + (f', тариф действует до {until}' if until else '') + '. Деньги больше не спишутся.'
        if wants_json():
            return jsonify({'ok': True, 'autorenew': on, 'message': message})
        flash(message, 'success')
        return redirect(back)

    @app.post('/app/billing/checkout')
    def billing_checkout():
        return checkout_view(False)

    @app.post('/tg/billing/checkout')
    def tg_billing_checkout():
        return checkout_view(True)

    @app.get('/app/billing/preview')
    def billing_preview():
        return preview_view()

    @app.get('/tg/billing/preview')
    def tg_billing_preview():
        return preview_view()

    @app.get('/app/billing/return')
    def billing_return():
        return return_view(False)

    @app.get('/tg/billing/return')
    def tg_billing_return():
        return return_view(True)

    @app.post('/app/billing/autorenew')
    def billing_autorenew():
        return autorenew_view(False)

    @app.post('/tg/billing/autorenew')
    def tg_billing_autorenew():
        return autorenew_view(True)

    @app.post('/webhooks/yookassa')
    def yookassa_webhook():
        if not enabled():
            abort(404)
        if not yookassa.trusted_ip(client_ip(request)):
            abort(403)
        try:
            result = handle_notification(db(), client(), request.get_json(silent=True))
        except ValueError:
            return jsonify({'ok': False}), 400
        except yookassa.YooKassaError as exc:
            # Not confirmed by the API right now: a non-200 answer makes YooKassa retry for 24 hours.
            capture_exception(exc, component='payments')
            return jsonify({'ok': False}), 503
        notify(db(), sender(), result)
        return jsonify({'ok': True})

    @app.post('/admin/payments/<payment_id>/refund')
    def admin_refund(payment_id):
        if not is_platform_admin(g.user):
            abort(403)
        try:
            row = refund(db(), client(), payment_id, request.form.get('amount', '').strip()[:20] or None, g.user['id'])
        except PaymentError as exc:
            flash(str(exc))
            return redirect('/admin#payments')
        flash(f"Возврат {amount_str(row['amount'])} ₽: " + {'succeeded': 'выполнен', 'canceled': 'отклонён'}.get(
            row['status'], 'в обработке') + '.')
        return redirect('/admin#payments')


# --- CLI ----------------------------------------------------------------------------------------
def main(argv=None, db_factory=None, client_factory=None, sender=None):
    import argparse
    parser = argparse.ArgumentParser(prog='python -m leadgen.payments', description='Платежи ЮKassa')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('renew', help='списать автопродления, срок которых истекает в ближайшие 24 часа')
    parser.parse_args(argv)
    if db_factory is None:
        from .app import env_load
        env_load()
    if client_factory is None and not yookassa.enabled():
        print('ЮKassa не настроена (YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY): автопродление пропущено.')
        return 0
    if db_factory is None:
        from .database import db_open
        db_factory = db_open
    if sender is None:
        from .app import telegram as sender
    db = db_factory()
    try:
        results = renew(db, (client_factory or yookassa.Client)(), sender)
    finally:
        db.close()
    counts = {}
    for result in results:
        counts[result['event']] = counts.get(result['event'], 0) + 1
    print('Автопродление: ' + (', '.join(f'{key}: {value}' for key, value in sorted(counts.items())) or 'нечего списывать'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
