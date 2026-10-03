"""Web routes for a user's own Telegram connection."""
from flask import abort, current_app, flash, g, redirect, render_template, request

from .rate_limit import RateLimited, hit

from .telegram_accounts import (SessionCipher, TelegramAccountError, TelethonGateway,
                                begin_connection, disconnect, sync_dialogs,
                                verify_code, verify_password)


def register_telegram_routes(app, db):
    def services():
        key = current_app.config.get('TELEGRAM_CIPHER_KEY', '')
        if not key:
            raise TelegramAccountError('Администратор ещё не настроил шифрование Telegram-сессий.')
        factory = current_app.config.get('TELEGRAM_GATEWAY_FACTORY', TelethonGateway)
        return SessionCipher(key), factory()

    @app.get('/app/telegram')
    def telegram_account():
        connection = db().execute('SELECT * FROM telegram_connections WHERE user_id=?',
                                  (g.user['id'],)).fetchone()
        auth = db().execute('SELECT stage,attempts,expires_at FROM telegram_connection_auth WHERE user_id=?',
                            (g.user['id'],)).fetchone()
        dialogs = db().execute('''SELECT * FROM user_telegram_dialogs WHERE user_id=?
            ORDER BY enabled DESC,title LIMIT 1000''', (g.user['id'],)).fetchall()
        configured = bool(current_app.config.get('TELEGRAM_CIPHER_KEY'))
        return render_template('telegram_account.html', connection=connection, auth=auth,
                               dialogs=dialogs, configured=configured)

    @app.post('/app/telegram/connect')
    def telegram_connect():
        phone = request.form.get('phone', '').strip()
        try:
            # Each request makes Telegram send a code to that number: cap it per account and per number.
            hit(db(), 'tg-code-user', g.user['id'], 3, 3600)
            hit(db(), 'tg-code-phone', phone, 3, 3600)
            cipher, gateway = services()
            begin_connection(db(), g.user['id'], phone, cipher, gateway)
            flash('Код отправлен приложением Telegram. Введите его ниже.')
        except (TelegramAccountError, RateLimited) as exc:
            flash(str(exc))
        return redirect('/app/telegram')

    @app.post('/app/telegram/code')
    def telegram_code():
        try:
            cipher, gateway = services()
            stage, count = verify_code(db(), g.user['id'], request.form.get('code', '').strip(), cipher, gateway)
            if stage == 'password':
                flash('На аккаунте включена двухэтапная защита. Введите облачный пароль.')
            else:
                flash(f'Аккаунт подключён. Найдено групп: {count}. Выберите нужные источники.')
        except TelegramAccountError as exc:
            flash(str(exc))
        return redirect('/app/telegram')

    @app.post('/app/telegram/password')
    def telegram_password():
        try:
            cipher, gateway = services()
            count = verify_password(db(), g.user['id'], request.form.get('password', ''), cipher, gateway)
            flash(f'Аккаунт подключён. Найдено групп: {count}. Выберите нужные источники.')
        except TelegramAccountError as exc:
            flash(str(exc))
        return redirect('/app/telegram')

    @app.post('/app/telegram/sync')
    def telegram_sync():
        try:
            cipher, gateway = services()
            count = sync_dialogs(db(), g.user['id'], cipher, gateway)
            flash(f'Список групп обновлён: {count}. История сообщений не читалась.')
        except TelegramAccountError as exc:
            flash(str(exc))
        return redirect('/app/telegram')

    @app.post('/app/telegram/groups')
    def telegram_groups():
        selected = request.form.getlist('groups')
        if len(selected) > 1000 or any(not value.lstrip('-').isdigit() for value in selected):
            abort(400)
        selected = [int(value) for value in selected]
        with db():
            db().execute('UPDATE user_telegram_dialogs SET enabled=false,updated_at=now() WHERE user_id=?',
                         (g.user['id'],))
            for peer_id in selected:
                db().execute('''UPDATE user_telegram_dialogs SET enabled=true,updated_at=now()
                    WHERE user_id=? AND peer_id=?''', (g.user['id'], peer_id))
        flash(f'Источники сохранены. Выбрано групп: {len(selected)}.')
        return redirect('/app/telegram')

    @app.post('/app/telegram/disconnect')
    def telegram_disconnect():
        try:
            cipher, gateway = services()
            remote = disconnect(db(), g.user['id'], cipher, gateway)
            message = 'Подключение удалено с сервера.'
            if remote:
                message += ' Сессия также завершена в Telegram.'
            else:
                message += ' Проверьте активные сеансы в настройках Telegram и завершите этот сеанс вручную.'
            flash(message)
        except TelegramAccountError as exc:
            flash(str(exc))
        return redirect('/app/telegram')
