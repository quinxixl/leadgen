"""Web routes for a user's own Telegram connection."""
import io
from datetime import datetime, timezone

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request, Response

from .rate_limit import RateLimited, hit

from .telegram_accounts import (SessionCipher, TelegramAccountError, TelethonGateway,
                                begin_connection, begin_qr_connection, disconnect,
                                qr_connection_state, resend_connection, sync_dialogs,
                                verify_code, verify_password, wait_qr_connection)


CHAT_SPHERES = (
    ('Фриланс и заказы', ('фриланс', 'freelance', 'заказ', 'работа', 'ваканс', 'job', 'удален', 'исполнитель', 'подработ')),
    ('Разработка', ('разработ', 'програм', 'frontend', 'backend', 'fullstack', 'python', 'javascript', 'php',
                    'веб', 'web', 'сайт', 'tilda', 'wordpress', 'кодинг')),
    ('Telegram и боты', ('telegram', 'телеграм', 'боты', 'чат-бот', 'chatbot')),
    ('Автоматизация и CRM', ('автоматизац', 'интеграц', 'crm', 'срм', 'amo', 'битрикс', 'bitrix',
                            'no-code', 'nocode', 'n8n', 'make.com')),
    ('Мобильные приложения', ('мобильн', 'ios', 'android', 'flutter', 'react native', 'приложени')),
    ('Дизайн', ('дизайн', 'design', 'figma', 'ux', 'ui', 'график', 'иллюстратор', 'брендинг')),
    ('Видео и монтаж', ('монтаж', 'видео', 'video', 'reels', 'рилс', 'motion', 'youtube', 'ютуб')),
    ('Маркетинг и продвижение', ('маркетинг', 'marketing', 'smm', 'таргет', 'seo', 'реклама',
                                'трафик', 'контент', 'копирайт', 'продвиж')),
    ('Юриспруденция', ('юрист', 'юридическ', 'адвокат', 'право', 'legal')),
    ('Бухгалтерия', ('бухгалтер', 'налог', 'отчётност', 'отчетност')),
    ('HR и рекрутинг', ('рекрутер', 'рекрутинг', 'hr', 'подбор персонала')),
    ('Переводы', ('переводчик', 'переводы', 'localization', 'локализац')),
    ('Бизнес', ('бизнес', 'предприним', 'стартап', 'startup', 'b2b', 'digital')),
)


def chat_spheres(title, username=''):
    """Classify a dialog by public metadata only; keep every matching sphere."""
    value = f'{title or ""} {username or ""}'.casefold()
    matches = [name for name, words in CHAT_SPHERES if any(word in value for word in words)]
    return matches or ['Другое']


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
        auth = db().execute('SELECT stage,attempts,expires_at,state_cipher FROM telegram_connection_auth WHERE user_id=?',
                            (g.user['id'],)).fetchone()
        if auth and auth['expires_at'] <= datetime.now(timezone.utc):
            # A stale QR/code flow must not keep rendering a dead QR image and polling.
            auth = None
        dialogs = db().execute('''SELECT * FROM user_telegram_dialogs WHERE user_id=?
            ORDER BY enabled DESC,title LIMIT 1000''', (g.user['id'],)).fetchall()
        dialogs = [dict(row) | {'spheres': chat_spheres(row['title'], row['username'])} for row in dialogs]
        present = {sphere for row in dialogs for sphere in row['spheres']}
        sphere_order = [name for name, _ in CHAT_SPHERES] + ['Другое']
        available_spheres = [name for name in sphere_order if name in present]
        configured = bool(current_app.config.get('TELEGRAM_CIPHER_KEY'))
        qr_url = ''
        if auth and auth['stage'] == 'qr' and configured:
            try:
                qr_url = SessionCipher(current_app.config['TELEGRAM_CIPHER_KEY']).decrypt(auth['state_cipher']).get('url', '')
            except TelegramAccountError:
                qr_url = ''
        return render_template('telegram_account.html', connection=connection, auth=auth,
                               dialogs=dialogs, configured=configured, qr_url=qr_url,
                               available_spheres=available_spheres)

    @app.post('/app/telegram/qr/start')
    def telegram_qr_start():
        try:
            hit(db(), 'tg-qr-user', g.user['id'], 5, 3600)
            cipher, gateway = services()
            begin_qr_connection(db(), g.user['id'], cipher, gateway)
            flash('QR-код создан. Отсканируйте его в Telegram через «Настройки → Устройства».')
        except (TelegramAccountError, RateLimited) as exc:
            flash(str(exc))
        return redirect('/app/telegram')

    @app.get('/app/telegram/qr.svg')
    def telegram_qr_image():
        try:
            cipher, _ = services()
            state = qr_connection_state(db(), g.user['id'], cipher)
            import qrcode
            import qrcode.image.svg
            image = qrcode.make(state['url'], image_factory=qrcode.image.svg.SvgPathImage,
                                box_size=8, border=3)
            output = io.BytesIO()
            image.save(output)
            return Response(output.getvalue(), mimetype='image/svg+xml',
                            headers={'Cache-Control': 'no-store'})
        except TelegramAccountError as exc:
            return Response(str(exc), status=410, mimetype='text/plain')

    @app.post('/app/telegram/qr/wait')
    def telegram_qr_wait():
        try:
            # Every poll opens an MTProto connection; the page polls every ~1.5 s.
            hit(db(), 'tg-qr-wait', g.user['id'], 60, 60)
        except RateLimited:
            return jsonify(status='retry'), 429
        try:
            cipher, gateway = services()
            stage, count = wait_qr_connection(db(), g.user['id'], cipher, gateway)
            if stage == 'refreshed':
                return jsonify(status=stage, url=qr_connection_state(db(), g.user['id'], cipher).get('url', ''))
            return jsonify(status=stage, count=count)
        except TelegramAccountError as exc:
            return jsonify(status='error', message=str(exc)), 409

    @app.post('/app/telegram/connect')
    def telegram_connect():
        phone = request.form.get('phone', '').strip()
        try:
            # Each request makes Telegram send a code to that number: cap it per account and per number.
            hit(db(), 'tg-code-user', g.user['id'], 3, 3600)
            hit(db(), 'tg-code-phone', phone, 3, 3600)
            cipher, gateway = services()
            delivery = begin_connection(db(), g.user['id'], phone, cipher, gateway)
            flash(f'Код отправлен {delivery}. Введите его ниже. Не запрашивайте код повторно сразу.')
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

    @app.post('/app/telegram/resend')
    def telegram_resend():
        try:
            cipher, gateway = services()
            delivery = resend_connection(db(), g.user['id'], cipher, gateway)
            flash(f'Новый код отправлен {delivery}. Используйте только последний полученный код.')
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
        selected = list(dict.fromkeys(int(value) for value in selected))
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
