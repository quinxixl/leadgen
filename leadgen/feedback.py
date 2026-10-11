"""Product feedback (bot, cabinet, Mini App), beta surveys and owner notifications."""
import os

KINDS={'idea':'Идея','bug':'Ошибка','other':'Другое'}
SOURCES={'bot':'Бот','web':'Сайт','miniapp':'Mini App'}
STATUSES={'new':'Новый','seen':'Просмотрен','done':'Готово'}
MAX_TEXT=4000
# Survey key -> days after registration.
SURVEYS={'d7':7,'d21':21}
DISAPPOINTMENT={'very':'Очень','somewhat':'Немного','not':'Не расстроюсь'}
PRICES={'0':'0','lt500':'до 500','500_1000':'500–1000','1000_2000':'1000–2000','gt2000':'больше 2000'}
SURVEY_Q1='Насколько вы расстроитесь, если Сигналид перестанет работать?'
SURVEY_Q2='Сколько вы готовы платить в месяц?'
SURVEY_Q3='Что бы вы улучшили? Напишите одним сообщением или нажмите «Пропустить».'
WEB_LIMIT=(5,3600)


def save(db,user_id,kind,text,source,context='',workspace_id=None):
    if kind not in KINDS:raise ValueError('Выберите тип обращения.')
    if source not in SOURCES:raise ValueError('unknown source')
    text=(text or '').strip()
    if not text:raise ValueError('Напишите текст обращения.')
    if len(text)>MAX_TEXT:raise ValueError(f'Текст не должен превышать {MAX_TEXT} символов.')
    with db:
        return db.execute('''INSERT INTO feedback(user_id,workspace_id,kind,text,context,source)
            VALUES(?,?,?,?,?,?) RETURNING id''',(user_id,workspace_id,kind,text,(context or '')[:200],source)).fetchone()['id']


def set_status(db,feedback_id,status):
    if status not in STATUSES:raise ValueError('unknown status')
    with db:
        row=db.execute('UPDATE feedback SET status=?,updated_at=now() WHERE id=? RETURNING id',(status,feedback_id)).fetchone()
    return bool(row)


def recent(db,limit=100):
    return db.execute('''SELECT f.*,u.display_name,u.telegram_user_id FROM feedback f
        JOIN app_users u ON u.id=f.user_id
        WHERE f.kind<>'survey' OR f.disappointment IS NOT NULL
        ORDER BY f.created_at DESC,f.id DESC LIMIT ?''',(limit,)).fetchall()


def admin_chat_ids():
    """FEEDBACK_CHAT_ID (a chat or a list), else the platform admins' private chats."""
    raw=os.environ.get('FEEDBACK_CHAT_ID','').strip() or os.environ.get('WEB_ADMIN_TELEGRAM_IDS','')
    return [value.strip() for value in raw.split(',') if value.strip().lstrip('-').isdigit()]


def describe(row):
    who=(row['display_name'] or 'Пользователь')+f" (id {row['user_id']})"
    if row['kind']=='survey':
        lines=[f"📊 Опрос беты ({row['survey']}) — {who}",
               SURVEY_Q1+' '+DISAPPOINTMENT.get(row['disappointment'] or '','—'),
               SURVEY_Q2+' '+PRICES.get(row['price'] or '','—')]
        if row['text']:lines.append('Комментарий: '+row['text'])
    else:
        lines=[f"💬 {KINDS.get(row['kind'],row['kind'])} · {SOURCES.get(row['source'],row['source'])} — {who}"]
        if row['context']:lines.append('Где: '+row['context'])
        lines.append('');lines.append(row['text'])
    return '\n'.join(lines)[:3900]


def deliver_notifications(db,sender,limit=5):
    """At most once per row: the claim is committed before the send, a failed send is only logged."""
    chats=admin_chat_ids()
    if not chats:return 0
    sent=0
    for _ in range(limit):
        with db:
            row=db.execute('''SELECT f.*,u.display_name FROM feedback f JOIN app_users u ON u.id=f.user_id
                WHERE f.notified_at IS NULL AND f.updated_at>now()-interval '2 days'
                  AND (f.kind<>'survey' OR f.answered_at IS NOT NULL
                       -- The optional comment never came: report the two answers an hour later.
                       OR (f.price IS NOT NULL AND f.updated_at<now()-interval '1 hour'))
                ORDER BY f.id FOR UPDATE OF f SKIP LOCKED LIMIT 1''').fetchone()
            if not row:break
            db.execute('UPDATE feedback SET notified_at=now() WHERE id=?',(row['id'],))
        text=describe(row)
        for chat in chats:
            try:sender('sendMessage',{'chat_id':chat,'text':text,'disable_web_page_preview':True})
            except Exception as exc:print('Уведомление об обратной связи: '+type(exc).__name__,flush=True)
            else:sent+=1
    return sent


def survey_markup(survey,step):
    if step=='a':
        rows=[[{'text':label,'callback_data':f'sv:{survey}:a:{key}'} for key,label in DISAPPOINTMENT.items()]]
    elif step=='p':
        items=[{'text':label,'callback_data':f'sv:{survey}:p:{key}'} for key,label in PRICES.items()]
        rows=[items[:3],items[3:]]
    else:
        rows=[[{'text':'Пропустить','callback_data':f'sv:{survey}:skip'}]]
    return {'inline_keyboard':rows}


def send_surveys(db,sender,limit=20):
    """Day 7 and day 21 after signup, once per user per survey; the row is the send marker."""
    sent=0
    for survey,days in SURVEYS.items():
        # A day-7 survey is pointless for long-time users; they get the day-21 one instead.
        upper=" AND u.registered_at>now()-interval '21 days'" if survey=='d7' else ''
        rows=db.execute(f'''SELECT u.id,u.telegram_chat_id FROM app_users u
            WHERE u.status='active' AND u.registered_at<=now()-make_interval(days=>?){upper}
              AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.user_id=u.id AND f.survey=?)
              AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.user_id=u.id AND f.kind='survey'
                              AND f.created_at>now()-interval '3 days')
            ORDER BY u.id LIMIT ?''',(days,survey,limit)).fetchall()
        for row in rows:
            with db:
                claimed=db.execute('''INSERT INTO feedback(user_id,kind,survey,source) VALUES(?,'survey',?,'bot')
                    ON CONFLICT(user_id,survey) WHERE survey IS NOT NULL DO NOTHING RETURNING id''',(row['id'],survey)).fetchone()
            if not claimed:continue
            text=('Небольшой опрос участников беты — три вопроса, меньше минуты.\n\n'+SURVEY_Q1)
            try:sender('sendMessage',{'chat_id':str(row['telegram_chat_id']),'text':text,'reply_markup':survey_markup(survey,'a')})
            except Exception as exc:print('Опрос беты: '+type(exc).__name__,flush=True)
            else:sent+=1
    return sent


def survey_answer(db,user_id,survey,field,value):
    """Store one structured answer; returns False when the survey was never sent to this user."""
    allowed={'disappointment':DISAPPOINTMENT,'price':PRICES}
    if survey not in SURVEYS or field not in allowed or value not in allowed[field]:return False
    with db:
        row=db.execute(f'''UPDATE feedback SET {field}=?,updated_at=now() WHERE user_id=? AND survey=?
            RETURNING id''',(value,user_id,survey)).fetchone()
    return bool(row)


def survey_finish(db,user_id,survey,text=''):
    with db:
        row=db.execute('''UPDATE feedback SET text=?,answered_at=coalesce(answered_at,now()),updated_at=now()
            WHERE user_id=? AND survey=? RETURNING id''',((text or '').strip()[:MAX_TEXT],user_id,survey)).fetchone()
    return bool(row)


def register_feedback_routes(app,db):
    from urllib.parse import urlsplit
    from flask import abort,flash,g,redirect,render_template,request
    from .rate_limit import RateLimited,hit
    from .web import is_platform_admin

    def page_context():
        """Path of the page the form was opened from; never a foreign URL or a query string."""
        path=urlsplit(request.referrer or '').path
        return path[:200] if path.startswith(('/app','/tg')) and not path.endswith('/feedback') else ''

    def submit(source):
        kind=request.form.get('kind','');text=request.form.get('text','')
        try:
            if kind not in KINDS or not text.strip() or len(text.strip())>MAX_TEXT:
                raise ValueError(f'Выберите тип и напишите текст (до {MAX_TEXT} символов).')
            hit(db(),'feedback',g.user['id'],*WEB_LIMIT)
            save(db(),g.user['id'],kind,text,source,request.form.get('context','')[:200],
                 (g.get('workspace') or {}).get('id'))
        except RateLimited as exc:return False,str(exc)
        except ValueError as exc:return False,str(exc)
        return True,'Спасибо! Сообщение отправлено команде Сигналид.'

    @app.route('/app/feedback',methods=['GET','POST'])
    def web_feedback():
        if request.method=='POST':
            ok,message=submit('web');flash(message)
            if ok:return redirect('/app/feedback')
            return render_template('feedback.html',kinds=KINDS,context=request.form.get('context','')[:200],
                                   kind=request.form.get('kind',''),text=request.form.get('text','')[:MAX_TEXT]),400
        return render_template('feedback.html',kinds=KINDS,context=page_context(),kind='idea',text='')

    @app.route('/tg/feedback',methods=['GET','POST'])
    def tg_feedback():
        if request.method=='POST':
            ok,message=submit('miniapp');flash(message,'success' if ok else 'error')
            if ok:return redirect('/tg/feedback')
            return render_template('tg/feedback.html',kinds=KINDS,context=request.form.get('context','')[:200],
                                   kind=request.form.get('kind',''),text=request.form.get('text','')[:MAX_TEXT]),400
        return render_template('tg/feedback.html',kinds=KINDS,context=page_context(),kind='idea',text='')

    @app.post('/admin/feedback/<int:fid>')
    def admin_feedback_status(fid):
        if not is_platform_admin(g.user):abort(403)
        status=request.form.get('status','')
        if status not in STATUSES:abort(400)
        if not set_status(db(),fid,status):abort(404)
        return redirect('/admin#feedback')
