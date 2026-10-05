"""Compose, preview and explicitly confirm a reply from the signed-in user's account."""
from flask import abort, current_app, flash, g, redirect, render_template, request

from . import telegram_replies as replies
from .telegram_accounts import SessionCipher, TelegramAccountError
from .teams import EDIT_LEADS, require_role


def register_reply_routes(app, db):
    def services():
        return (SessionCipher(current_app.config.get('TELEGRAM_CIPHER_KEY', '')),
                current_app.config.get('TELEGRAM_REPLY_GATEWAY_FACTORY', replies.ReplyGateway)())

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
        if request.method == 'POST':
            try:
                cipher, gateway = services()
                row = replies.prepare(db(), g.user['id'], g.owner_id, lead_id, mode, text, cipher, gateway)
                return redirect('/app/replies/' + row['id'])
            except TelegramAccountError as exc:
                error = str(exc)
        account = db().execute("SELECT display_name FROM telegram_connections WHERE user_id=? AND status='active'",
                               (g.user['id'],)).fetchone()
        history = db().execute('''SELECT * FROM telegram_replies WHERE actor_id=? AND owner_id=? AND lead_id=?
            ORDER BY created_at DESC LIMIT 10''', (g.user['id'], g.owner_id, lead_id)).fetchall()
        return render_template('reply.html', lead=lead, lead_id=lead_id, body=text, mode=mode,
                               error=error, account=account, history=history, modes=replies.MODES,
                               statuses=replies.STATUSES, reply=None)

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
