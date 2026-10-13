"""Paid plan requests (payments are not integrated) and manual plan management in /admin."""
import os
from datetime import date, datetime

from .billing import (MSK, PAID_PLANS, PERIODS, PLAN_CODES, PLAN_NAMES, beta_ends_at, find_user,
                      format_until, period_end, quote, set_plan)
from .observability import capture_exception

STATUSES = {'new': 'Новая', 'done': 'Выполнена', 'rejected': 'Отклонена'}
# Per user: re-clicks update the open request, so a few attempts an hour are plenty.
REQUEST_LIMIT = (5, 3600)
SENT_MESSAGE = 'Заявка отправлена — мы свяжемся с вами в Telegram в течение дня.'
TERMS = {'month': '+1 месяц', 'year': '+1 год', 'beta': 'До конца беты', 'forever': 'Бессрочно', 'date': 'До даты'}


def money(value):
    return '{:,}'.format(value).replace(',', ' ')


def save_request(db, user_id, code, period, founder_price=None):
    """Create the user's open request or update it; returns (row, created)."""
    if code not in PAID_PLANS or period not in PERIODS:
        raise ValueError('Выберите платный тариф и период.')
    price, founder = quote(code, period, founder_price)
    with db:
        row = db.execute('''INSERT INTO plan_requests(user_id,plan_code,billing_period,price,founder_price)
            VALUES(?,?,?,?,?) ON CONFLICT(user_id) WHERE status='new' DO UPDATE SET plan_code=excluded.plan_code,
            billing_period=excluded.billing_period,price=excluded.price,founder_price=excluded.founder_price,
            updated_at=now() RETURNING *,(xmax=0) AS created''', (user_id, code, period, price, founder)).fetchone()
    return row, row['created']


def open_request(db, user_id):
    return db.execute("SELECT * FROM plan_requests WHERE user_id=? AND status='new'", (user_id,)).fetchone()


def open_requests(db, limit=100):
    return db.execute('''SELECT r.*,u.display_name,u.username,u.telegram_user_id FROM plan_requests r
        JOIN app_users u ON u.id=r.user_id WHERE r.status='new' ORDER BY r.created_at LIMIT ?''', (limit,)).fetchall()


def describe(row, user, created):
    who = f"id {user['id']}"
    if user['username']:who += f" · @{user['username']}"
    if user['display_name']:who += f" · {user['display_name']}"
    who += f" · Telegram ID {user['telegram_user_id']}"
    price = f"{money(row['price'])} ₽ за {PERIODS[row['billing_period']]}"
    if row['founder_price']:price += ' (цена участника беты)'
    lines = ['💳 ' + ('Новая заявка на тариф' if created else 'Заявка на тариф обновлена'), 'Пользователь: ' + who,
             f"Тариф: «{PLAN_NAMES[row['plan_code']]}», {PERIODS[row['billing_period']]}", 'Цена: ' + price]
    base = os.environ.get('WEB_PUBLIC_URL', '').strip().rstrip('/')
    if base:lines.append('Выдать: ' + base + '/admin#plan-requests')
    return '\n'.join(lines)


def notify_owner(sender, text):
    """Best effort: the request is already saved; a failed send is reported, never raised."""
    from .feedback import admin_chat_ids
    for chat in admin_chat_ids():
        try:sender('sendMessage', {'chat_id': chat, 'text': text, 'disable_web_page_preview': True})
        except Exception as exc:
            capture_exception(exc, component='plan_requests')
            print('Уведомление о заявке на тариф: ' + type(exc).__name__, flush=True)


def notify_user(sender, user, code, ends_at):
    text = f'Тариф «{PLAN_NAMES[code]}» подключён {format_until(ends_at)}. Спасибо, что вы с Сигналид!'
    try:sender('sendMessage', {'chat_id': str(user['telegram_chat_id']), 'text': text})
    except Exception as exc:
        capture_exception(exc, component='plan_requests', user_id=user['id'])
        print('Сообщение о подключении тарифа: ' + type(exc).__name__, flush=True)
        return False
    return True


def grant(db, request_id, admin_id, period=None):
    """Apply an open request for one month or one year; returns (request, user, ends_at) or None."""
    with db:
        row = db.execute("SELECT * FROM plan_requests WHERE id=? AND status='new' FOR UPDATE", (request_id,)).fetchone()
        if not row:return None
        period = period if period in PERIODS else row['billing_period']
        current = db.execute('SELECT status,plan_code,ends_at FROM subscriptions WHERE user_id=?', (row['user_id'],)).fetchone()
        ends_at = period_end(current, row['plan_code'], 12 if period == 'year' else 1)
        db.execute("UPDATE plan_requests SET status='done',handled_at=now(),handled_by=?,updated_at=now() WHERE id=?",
                   (admin_id, request_id))
    try:
        # Founder price is set only when the request quoted it; otherwise the stored value stays.
        set_plan(db, row['user_id'], row['plan_code'], ends_at, period, True if row['founder_price'] else None,
                 changed_by=admin_id, source='request', note=f'Заявка #{request_id}')
    except Exception:
        with db:db.execute("UPDATE plan_requests SET status='new',handled_at=NULL,handled_by=NULL WHERE id=?", (request_id,))
        raise
    user = db.execute('SELECT * FROM app_users WHERE id=?', (row['user_id'],)).fetchone()
    return row, user, ends_at


def reject(db, request_id, admin_id):
    with db:
        row = db.execute('''UPDATE plan_requests SET status='rejected',handled_at=now(),handled_by=?,updated_at=now()
            WHERE id=? AND status='new' RETURNING id''', (admin_id, request_id)).fetchone()
    return bool(row)


def term_end(db, user_id, code, term, until=''):
    """End date for an admin quick term."""
    if term == 'forever' or code == 'free':return None
    if term == 'beta':return beta_ends_at()
    if term == 'date':
        try:day = date.fromisoformat(until)
        except ValueError:raise ValueError('Укажите дату окончания.') from None
        return datetime(day.year, day.month, day.day, tzinfo=MSK)
    if term in ('month', 'year'):
        current = db.execute('SELECT status,plan_code,ends_at FROM subscriptions WHERE user_id=?', (user_id,)).fetchone()
        return period_end(current, code, 12 if term == 'year' else 1)
    raise ValueError('Выберите срок.')


def admin_users(db, limit=100):
    return db.execute('''SELECT u.id,u.telegram_user_id,u.username,u.display_name,u.status,u.registered_at,u.last_seen_at,
            s.status AS sub_status,s.plan_code,s.ends_at,s.founder_price
        FROM app_users u LEFT JOIN subscriptions s ON s.user_id=u.id ORDER BY u.id DESC LIMIT ?''', (limit,)).fetchall()


def recent_changes(db, limit=30):
    return db.execute('''SELECT c.*,u.display_name,u.username,a.display_name AS admin_name FROM plan_changes c
        JOIN app_users u ON u.id=c.user_id LEFT JOIN app_users a ON a.id=c.changed_by
        ORDER BY c.created_at DESC,c.id DESC LIMIT ?''', (limit,)).fetchall()


def register_plan_routes(app, db):
    from flask import abort, current_app, flash, g, redirect, request
    from .rate_limit import RateLimited, hit
    from .web import is_platform_admin

    def sender():
        custom = current_app.config.get('TELEGRAM_SENDER')
        if custom:return custom
        from .app import telegram
        return telegram

    def submit(back):
        if not g.workspace or g.workspace['owner_user_id'] != g.user['id']:
            flash('Подпиской управляет владелец команды.', 'error');return redirect(back)
        code, period = request.form.get('plan', ''), request.form.get('period', 'month')
        if code not in PAID_PLANS or period not in PERIODS:abort(400)
        try:hit(db(), 'plan_request', g.user['id'], *REQUEST_LIMIT)
        except RateLimited as exc:
            flash(str(exc), 'error');return redirect(back)
        founder = g.subscription['founder_price'] if g.subscription else None
        row, created = save_request(db(), g.user['id'], code, period, founder)
        notify_owner(sender(), describe(row, g.user, created))
        flash(SENT_MESSAGE, 'success')
        return redirect(back)

    @app.post('/app/billing/request')
    def billing_request():
        return submit('/app/billing')

    @app.post('/tg/billing/request')
    def tg_billing_request():
        return submit('/tg/billing')

    def admin_only():
        if not is_platform_admin(g.user):abort(403)

    @app.post('/admin/plans')
    def admin_set_plan():
        admin_only()
        code, term = request.form.get('plan', ''), request.form.get('term', '')
        founder = {'on': True, 'off': False}.get(request.form.get('founder', 'keep'))
        if code not in PLAN_CODES or term not in TERMS:abort(400)
        try:
            user = find_user(db(), request.form.get('user', '')[:100])
            ends_at = term_end(db(), user['id'], code, term, request.form.get('until', '')[:10])
            set_plan(db(), user['id'], code, ends_at, 'year' if term == 'year' else 'month', founder,
                     changed_by=g.user['id'], source='admin', note=request.form.get('note', '')[:500])
        except ValueError as exc:
            flash(str(exc));return redirect('/admin#plans')
        flash(f"Пользователь id {user['id']}: тариф «{PLAN_NAMES[code]}» {format_until(ends_at)}.")
        return redirect('/admin#plans')

    @app.post('/admin/plan-requests/<int:rid>/grant')
    def admin_grant(rid):
        admin_only()
        period = request.form.get('period', '')
        if period and period not in PERIODS:abort(400)
        result = grant(db(), rid, g.user['id'], period or None)
        if not result:abort(404)
        row, user, ends_at = result
        delivered = notify_user(sender(), user, row['plan_code'], ends_at)
        flash(f"Заявка #{rid}: тариф «{PLAN_NAMES[row['plan_code']]}» выдан {format_until(ends_at)}."
              + ('' if delivered else ' Сообщение пользователю не доставлено.'))
        return redirect('/admin#plan-requests')

    @app.post('/admin/plan-requests/<int:rid>/reject')
    def admin_reject(rid):
        admin_only()
        if not reject(db(), rid, g.user['id']):abort(404)
        flash(f'Заявка #{rid} отклонена.')
        return redirect('/admin#plan-requests')

