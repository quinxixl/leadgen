"""Telegram Mini App (/tg): sign-in by initData, onboarding, compact lead feed, lead card and replies.

Access rules (session, workspace role, subscription, terms) are enforced in web.protect(); these routes
only re-check the role for writes, exactly like the cabinet."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request

from . import telegram_replies as replies
from .accounts import accept_terms, register_user
from .billing import subscription_active
from .core import TOPICS, TOPIC_CATEGORIES, Lead, enrich
from .product import FEEDBACK, PIPELINE
from .rate_limit import RateLimited, hit
from .teams import EDIT_LEADS, EDIT_SETTINGS, require_role
from .telegram_accounts import SessionCipher, TelegramAccountError
from .tg_auth import validate_init_data
from .web import owner_lead, safe_next, save_preferences, start_user_session, update_lead
from .web_auth import begin_handoff

# Statuses offered as one-tap buttons on the lead card (a subset of PIPELINE).
TG_STATUSES = ('saved', 'working', 'contacted', 'discussing', 'won', 'lost')
WORKING = ('working', 'contacted', 'discussing', 'meeting', 'proposal')
FEED_FILTERS = {'new': 'Новые', 'work': 'В работе', 'fit': 'Подходят'}
# 👎 reasons: every FEEDBACK label except the positive one.
DISLIKE_REASONS = {key: label for key, label in FEEDBACK.items() if key != 'fit'}
BUDGET_PRESETS = (0, 5000, 10000, 30000, 50000, 100000)
PAGE = 20


def start_target(start_param):
    """Where a Mini App opened by t.me/<bot>/<app>?startapp=… should land."""
    value = start_param or ''
    if value.startswith('lead_') and value[5:].isdigit() and len(value) <= 25:
        return '/tg/leads/' + value[5:]
    return {'billing': '/tg/billing', 'start': '/tg/start', 'settings': '/tg/settings'}.get(value, '/tg/feed')


def tg_next(value):
    """A Mini App page to return to after sign-in; never the boot page itself, which would loop."""
    value = safe_next(value, '/tg')
    return value if value.startswith('/tg/') and not value.startswith('/tg/auth') else ''


def onboarding_url(step, target='/tg/feed'):
    query = {'step': step}
    if target and target != '/tg/feed':
        query['next'] = target
    return '/tg/start?' + urlencode(query)


def register_tg_routes(app, db):
    def public_base():
        return current_app.config.get('WEB_PUBLIC_URL') or request.host_url.rstrip('/')

    def handoff_url(user_id, version, target):
        token = begin_handoff(db(), user_id, version)
        return public_base() + '/login/handoff?' + urlencode({'t': token, 'next': target})

    def reply_services():
        return (SessionCipher(current_app.config.get('TELEGRAM_CIPHER_KEY', '')),
                current_app.config.get('TELEGRAM_REPLY_GATEWAY_FACTORY', replies.ReplyGateway)())

    def preferences():
        return db().execute('SELECT * FROM user_preferences WHERE user_id=?', (g.owner_id,)).fetchone()

    @app.get('/tg')
    def tg_boot():
        target = tg_next(request.args.get('next', ''))
        if g.user:
            return redirect(target or '/tg/feed')
        return render_template('tg/boot.html', target=target, bot=current_app.config.get('BOT_USERNAME', ''))

    @app.post('/tg/auth')
    def tg_auth():
        # The body is a bearer credential: it is never logged or echoed back.
        def fail(message, code):
            return jsonify(ok=False, error=message), code
        try:
            hit(db(), 'tg-auth-ip', request.remote_addr or '', 60, 600)
        except RateLimited as exc:
            return fail(str(exc), 429)
        token = current_app.config.get('TELEGRAM_BOT_TOKEN', '')
        if not token:
            return fail('Мини-приложение ещё не настроено администратором.', 503)
        try:
            data = validate_init_data(request.form.get('init_data', ''), token)
        except ValueError:
            return fail('Не удалось подтвердить вход через Telegram. Закройте мини-приложение и откройте его снова.', 401)
        actor = data['user']
        query = 'SELECT id,status,session_version,terms_accepted_at FROM app_users WHERE telegram_user_id=?'
        user = db().execute(query, (actor['id'],)).fetchone()
        if user and user['status'] != 'active':
            return fail('Аккаунт заблокирован. Напишите в поддержку.', 403)
        if not user:
            # Consent is not implied by opening the app: /tg/start asks for it first.
            register_user(db(), actor, actor['id'])
            user = db().execute(query, (actor['id'],)).fetchone()
        start_user_session(db(), user['id'])
        target = tg_next(request.form.get('next', '')) or start_target(data['start_param'])
        if not user['terms_accepted_at']:
            target = onboarding_url(1, target)
        result = {'ok': True, 'next': target}
        if request.form.get('handoff') and user['terms_accepted_at']:
            result['handoff'] = handoff_url(user['id'], user['session_version'], '/app')
        return jsonify(result)

    @app.post('/tg/handoff')
    def tg_handoff():
        target = safe_next(request.form.get('next', '/app'), '/app') or '/app'
        try:
            hit(db(), 'tg-handoff', g.user['id'], 20, 600)
        except RateLimited as exc:
            return jsonify(ok=False, error=str(exc)), 429
        return jsonify(ok=True, url=handoff_url(g.user['id'], g.user['session_version'], target))

    @app.route('/tg/start', methods=['GET', 'POST'])
    def tg_start():
        target = tg_next(request.values.get('next', '')) or '/tg/feed'
        accepted = bool(g.user['terms_accepted_at'])
        prefs = preferences()
        try:
            step = int(request.values.get('step', '1'))
        except ValueError:
            abort(400)
        step = 1 if not accepted else min(4, max(2, step))
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'consent':
                if not request.form.get('consent'):
                    flash('Чтобы продолжить, примите оферту и согласие на обработку персональных данных.', 'error')
                    return redirect(onboarding_url(1, target))
                accept_terms(db(), g.user['id'])
                # Accounts configured earlier in the bot skip straight to what they opened.
                return redirect(target if prefs['topics'] else onboarding_url(2, target))
            if not accepted:
                return redirect(onboarding_url(1, target))
            require_role(*EDIT_SETTINGS)
            if action == 'services':
                selected = request.form.getlist('topics')
                if not set(selected) <= set(TOPICS):
                    abort(400)
                if not selected:
                    flash('Выберите хотя бы одну услугу.', 'error')
                    return redirect(onboarding_url(2, target))
                save_preferences(db(), g.owner_id, topics=selected)
                return redirect(onboarding_url(3, target))
            if action == 'budget':
                budget = request.form.get('min_budget', '')
                if not budget.isdigit() or int(budget) not in BUDGET_PRESETS:
                    abort(400)
                save_preferences(db(), g.owner_id, min_budget=int(budget),
                                 show_without_budget=bool(request.form.get('no_budget')))
                return redirect(onboarding_url(4, target))
            if action == 'launch':
                save_preferences(db(), g.owner_id, monitoring_active=True)
                flash('Мониторинг запущен. Новые заказы придут в бот и появятся здесь.', 'success')
                return redirect(target)
            abort(400)
        return render_template('tg/onboarding.html', step=step, target=target, prefs=prefs,
                               topic_categories=TOPIC_CATEGORIES, presets=BUDGET_PRESETS)

    @app.get('/tg/feed')
    def tg_feed():
        kind = request.args.get('f', '')
        before = request.args.get('before', '')
        if kind and kind not in FEED_FILTERS or before and (not before.isdigit() or len(before) > 18):
            abort(400)
        where = "ul.user_id=? AND ul.delivery_status<>'filtered'"
        args = [g.owner_id]
        if kind == 'new':
            where += " AND ul.pipeline_status IN ('saved','viewed')"
        elif kind == 'work':
            where += ' AND ul.pipeline_status IN (' + ','.join('?' for _ in WORKING) + ')'
            args += list(WORKING)
        elif kind == 'fit':
            where += " AND f.label='fit'"
        if before:
            where += ' AND l.id<?'
            args.append(int(before))
        # Keyset pagination: no count(*) and no OFFSET on a growing table.
        rows = db().execute('''SELECT l.id,l.payload,ul.pipeline_status,f.label AS feedback
            FROM user_leads ul JOIN leads l ON l.id=ul.lead_id
            LEFT JOIN lead_feedback f ON f.user_id=ul.user_id AND f.lead_id=ul.lead_id
            WHERE ''' + where + ' ORDER BY l.id DESC LIMIT ?', args + [PAGE + 1]).fetchall()
        more = len(rows) > PAGE
        items = [dict(row) | {'lead': enrich(Lead(**json.loads(row['payload'])))} for row in rows[:PAGE]]
        return render_template('tg/feed.html', items=items, kind=kind, filters=FEED_FILTERS,
                               next_before=items[-1]['id'] if more else None, before=before)

    @app.route('/tg/leads/<int:lid>', methods=['GET', 'POST'])
    def tg_lead(lid):
        row = owner_lead(db(), g.owner_id, lid)
        if not row:
            abort(404)
        if request.method == 'POST':
            require_role(*EDIT_LEADS)
            action = request.form.get('action')
            amount = str(row['deal_amount']) if row['deal_amount'] is not None else ''
            if action == 'status':
                status = request.form.get('status')
                if status not in TG_STATUSES:
                    abort(400)
                update_lead(db(), g.owner_id, lid, row, status, amount, row['feedback'] or '')
                flash('Статус: ' + PIPELINE[status] + '.', 'success')
            elif action == 'feedback':
                label = request.form.get('feedback', '')
                if label and label not in FEEDBACK:
                    abort(400)
                update_lead(db(), g.owner_id, lid, row, row['pipeline_status'], amount, label)
                flash('Оценка сохранена — отбор станет точнее.' if label else 'Оценка снята.', 'success')
            elif action == 'remind':
                from .reminders import schedule
                try:
                    schedule(db(), g.owner_id, lid, datetime.now(timezone.utc) + timedelta(hours=1))
                except ValueError as exc:
                    flash(str(exc), 'error')
                else:
                    flash('Напомним в Telegram через час.', 'success')
            else:
                abort(400)
            return redirect(f'/tg/leads/{lid}')
        lead = enrich(Lead(**json.loads(row['payload'])))
        try:
            replies.source_message(lead.url)
            replyable = True
        except TelegramAccountError:
            replyable = False
        reminder = db().execute('SELECT due_at,status FROM lead_reminders WHERE user_id=? AND lead_id=?',
                                (g.owner_id, lid)).fetchone()
        activity = db().execute('''SELECT kind,detail,created_at FROM lead_activity WHERE user_id=? AND lead_id=?
            ORDER BY id DESC LIMIT 5''', (g.owner_id, lid)).fetchall()
        return render_template('tg/lead.html', row=row, lead=lead, statuses=TG_STATUSES, reasons=DISLIKE_REASONS,
                               reminder=reminder, activity=activity, replyable=replyable,
                               can_edit=g.role in EDIT_LEADS)

    @app.route('/tg/leads/<int:lid>/reply', methods=['GET', 'POST'])
    def tg_reply(lid):
        require_role(*EDIT_LEADS)
        try:
            replies.authorized_lead(db(), g.user['id'], g.owner_id, lid)
        except TelegramAccountError:
            abort(404)
        row = owner_lead(db(), g.owner_id, lid)
        if not row:
            abort(404)
        lead = enrich(Lead(**json.loads(row['payload'])))
        account = db().execute("""SELECT display_name FROM telegram_connections
            WHERE user_id=? AND status='active' AND session_cipher<>''""", (g.user['id'],)).fetchone()
        from .ai_offers import latest_for_lead
        from .drafts import reply_drafts
        profile = preferences()
        variants = {}
        offer = latest_for_lead(db(), g.owner_id, lid, None)
        if offer and isinstance(offer['drafts'], dict):
            variants.update({'ai:' + name: ('AI · ' + name, text) for name, text in offer['drafts'].items()})
        templates = reply_drafts(lead, profile['profile_services'], profile['portfolio'], None)
        variants.update({'tpl:' + name: (name, text) for name, text in templates.items()})
        chosen = request.values.get('v', '')
        if chosen not in variants:
            chosen = next(iter(variants), '')
        mode = request.values.get('mode', 'dm')
        if mode not in replies.MODES:
            mode = 'dm'
        body = variants[chosen][1] if chosen else ''
        error = ''
        if request.method == 'POST':
            body = request.form.get('body', '')
            if not account:
                error = 'Подключите свой Telegram на сайте, чтобы отвечать от своего имени.'
            else:
                try:
                    cipher, gateway = reply_services()
                    prepared = replies.prepare(db(), g.user['id'], g.owner_id, lid, mode, body, cipher, gateway)
                    return redirect('/tg/replies/' + prepared['id'])
                except TelegramAccountError as exc:
                    error = str(exc)
        return render_template('tg/reply.html', lid=lid, lead=lead, account=account, variants=variants,
                               chosen=chosen, mode=mode, body=body, error=error, modes=replies.MODES)

    @app.route('/tg/replies/<token>', methods=['GET', 'POST'])
    def tg_confirm(token):
        require_role(*EDIT_LEADS)
        try:
            row = replies.get_reply(db(), token, g.user['id'], g.owner_id)
        except TelegramAccountError:
            abort(404)
        if request.method == 'POST':
            action = request.form.get('action')
            if action not in ('send', 'cancel'):
                abort(400)
            try:
                if action == 'cancel':
                    replies.cancel(db(), token, g.user['id'], g.owner_id)
                    flash('Отклик отменён.')
                else:
                    cipher, gateway = reply_services()
                    row = replies.confirm(db(), token, g.user['id'], g.owner_id, cipher, gateway)
                    flash(replies.STATUSES[row['status']], 'success' if row['status'] == 'sent' else 'error')
            except TelegramAccountError as exc:
                flash(str(exc), 'error')
            return redirect('/tg/replies/' + quote(token))
        return render_template('tg/confirm.html', reply=row, modes=replies.MODES, statuses=replies.STATUSES)

    @app.route('/tg/settings', methods=['GET', 'POST'])
    def tg_settings():
        if request.method == 'POST':
            require_role(*EDIT_SETTINGS)
            budget = request.form.get('min_budget', '')
            selected = request.form.getlist('topics')
            if not budget.isdigit() or int(budget) > 100000000 or not set(selected) <= set(TOPICS):
                abort(400)
            save_preferences(db(), g.owner_id, min_budget=int(budget), topics=selected,
                             show_without_budget=bool(request.form.get('no_budget')),
                             show_possible_needs=bool(request.form.get('possible')),
                             monitoring_active=bool(request.form.get('active')))
            flash('Настройки сохранены.', 'success')
            return redirect('/tg/settings')
        return render_template('tg/settings.html', prefs=preferences(), topic_categories=TOPIC_CATEGORIES,
                               presets=BUDGET_PRESETS, can_edit=g.role in EDIT_SETTINGS)

    @app.get('/tg/billing')
    def tg_billing():
        return render_template('tg/billing.html', active=subscription_active(g.subscription))
