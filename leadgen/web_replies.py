"""Compose, preview and explicitly confirm a reply from the signed-in user's account."""
from flask import abort, current_app, flash, g, redirect, render_template, request

from . import telegram_replies as replies
from .telegram_accounts import SessionCipher, TelegramAccountError
from .teams import EDIT_LEADS, require_role

DRAFT_KINDS = {'ai': 'AI-оффер', 'tpl': 'Быстрый шаблон'}


def register_reply_routes(app, db):
    def services():
        return (SessionCipher(current_app.config.get('TELEGRAM_CIPHER_KEY', '')),
                current_app.config.get('TELEGRAM_REPLY_GATEWAY_FACTORY', replies.ReplyGateway)())

    def prefill(lead, lead_id, value):
        """Text of ?draft=ai:<variant> or ?draft=tpl:<variant>, built like on the lead page."""
        kind, _, name = value.partition(':')
        if kind not in DRAFT_KINDS or not name:
            return ''
        project_id = request.args.get('project', '')
        project = None
        if project_id:
            if not project_id.isdigit():
                abort(400)
            project = db().execute('''SELECT p.* FROM project_leads pl JOIN projects p
                ON p.id=pl.project_id AND p.user_id=pl.user_id
                WHERE pl.lead_id=? AND p.id=? AND p.workspace_id=?''',
                (lead_id, int(project_id), g.workspace['id'])).fetchone()
            if not project:
                abort(404)
        if kind == 'ai':
            from .ai_offers import latest_for_lead
            row = latest_for_lead(db(), g.owner_id, lead_id, project['id'] if project else None)
            drafts = row['drafts'] if row else {}
        else:
            from .core import Lead
            from .drafts import reply_drafts
            profile = db().execute('SELECT profile_services,portfolio FROM user_preferences WHERE user_id=?',
                                   (g.owner_id,)).fetchone()
            drafts = reply_drafts(Lead(**lead), profile['profile_services'], profile['portfolio'],
                                  dict(project) if project else None)
        text = drafts.get(name, '') if isinstance(drafts, dict) else ''
        return text if isinstance(text, str) else ''

    @app.route('/app/leads/<int:lead_id>/reply', methods=['GET', 'POST'])
    def compose_reply(lead_id):
        require_role(*EDIT_LEADS)
        try:
            lead = replies.authorized_lead(db(), g.user['id'], g.owner_id, lead_id)
        except TelegramAccountError:
            abort(404)
        text = request.form.get('body', '')
        mode = request.form.get('mode', 'dm')
        error = ''
        draft_name = ''
        if request.method == 'POST':
            try:
                cipher, gateway = services()
                row = replies.prepare(db(), g.user['id'], g.owner_id, lead_id, mode, text, cipher, gateway)
                return redirect('/app/replies/' + row['id'])
            except TelegramAccountError as exc:
                error = str(exc)
        elif request.args.get('draft'):
            text = prefill(lead, lead_id, request.args['draft'][:200])
            if text:
                kind, _, name = request.args['draft'].partition(':')
                draft_name = DRAFT_KINDS[kind] + ' «' + name + '»'
        account = db().execute("SELECT display_name FROM telegram_connections WHERE user_id=? AND status='active'",
                               (g.user['id'],)).fetchone()
        replies.recover_stuck(db())
        history = db().execute('''SELECT * FROM telegram_replies WHERE actor_id=? AND owner_id=? AND lead_id=?
            ORDER BY created_at DESC LIMIT 10''', (g.user['id'], g.owner_id, lead_id)).fetchall()
        return render_template('reply.html', lead=lead, lead_id=lead_id, body=text, mode=mode,
                               error=error, account=account, history=history, modes=replies.MODES,
                               statuses=replies.STATUSES, reply=None, draft_name=draft_name)

    @app.route('/app/replies/<token>', methods=['GET', 'POST'])
    def confirm_reply(token):
        require_role(*EDIT_LEADS)
        try:
            row = replies.get_reply(db(), token, g.user['id'], g.owner_id)
        except TelegramAccountError:
            abort(404)
        if request.method == 'POST':
            action = request.form.get('action')
            try:
                if action == 'cancel':
                    replies.cancel(db(), token, g.user['id'], g.owner_id)
                elif action == 'send':
                    cipher, gateway = services()
                    row = replies.confirm(db(), token, g.user['id'], g.owner_id, cipher, gateway)
                    flash(replies.STATUSES[row['status']])
                else:
                    abort(400)
            except TelegramAccountError as exc:
                flash(str(exc))
            return redirect('/app/replies/' + token)
        return render_template('reply.html', reply=row, lead_id=row['lead_id'], modes=replies.MODES,
                               statuses=replies.STATUSES)
