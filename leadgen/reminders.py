"""Durable reminders. Ambiguous Telegram sends require an explicit reschedule."""
import json
from datetime import datetime, timedelta, timezone


def schedule(db,user_id,lead_id,due_at,note=''):
    now=datetime.now(timezone.utc)
    if due_at.tzinfo is None or not now < due_at <= now+timedelta(days=366):
        raise ValueError('Выберите время в будущем, не дальше чем через год.')
    if len(note)>500:raise ValueError('Заметка не должна превышать 500 символов.')
    with db:
        owner=db.execute("SELECT 1 FROM user_leads WHERE user_id=? AND lead_id=? AND delivery_status<>'filtered'",(user_id,lead_id)).fetchone()
        if not owner:raise ValueError('Лид не найден.')
        # A send already in flight cannot safely be replaced.
        row=db.execute('''INSERT INTO lead_reminders(user_id,lead_id,due_at,note) VALUES(?,?,?,?)
            ON CONFLICT(user_id,lead_id) DO UPDATE SET due_at=excluded.due_at,note=excluded.note,
            status='pending',updated_at=now() WHERE lead_reminders.status<>'sending' RETURNING lead_id''',
            (user_id,lead_id,due_at,note)).fetchone()
        if not row:raise ValueError('Напоминание уже отправляется. Повторите позже.')
        db.execute("INSERT INTO lead_activity(user_id,lead_id,kind,detail) VALUES(?,?,'note',?)",
            (user_id,lead_id,'Напоминание назначено на '+due_at.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')+('. '+note if note else '')))


def cancel(db,user_id,lead_id):
    with db:
        row=db.execute("UPDATE lead_reminders SET status='cancelled',updated_at=now() WHERE user_id=? AND lead_id=? AND status IN ('pending','uncertain') RETURNING lead_id",(user_id,lead_id)).fetchone()
        if row:db.execute("INSERT INTO lead_activity(user_id,lead_id,kind,detail) VALUES(?,?,'note','Напоминание отменено')",(user_id,lead_id))
    return bool(row)


def deliver_due(db,sender,limit=5):
    sent=0
    with db:
        db.execute("UPDATE lead_reminders SET status='uncertain',updated_at=now() WHERE status='sending' AND updated_at<now()-interval '5 minutes'")
    for _ in range(limit):
        with db:
            row=db.execute('''SELECT r.*,u.telegram_chat_id,l.payload FROM lead_reminders r
                JOIN app_users u ON u.id=r.user_id JOIN leads l ON l.id=r.lead_id
                WHERE r.status='pending' AND r.due_at<=now() AND u.status='active'
                ORDER BY r.due_at FOR UPDATE OF r SKIP LOCKED LIMIT 1''').fetchone()
            if not row:break
            db.execute("UPDATE lead_reminders SET status='sending',updated_at=now() WHERE user_id=? AND lead_id=?",(row['user_id'],row['lead_id']))
        lead=json.loads(row['payload'])
        text='Напоминание по лиду\n\n'+lead['title'][:500]+'\n'+row['note']+'\n\n'+lead['url']
        try:
            sender('sendMessage',{'chat_id':row['telegram_chat_id'],'text':text[:3900],'disable_web_page_preview':True})
        except Exception:
            with db:db.execute("UPDATE lead_reminders SET status='uncertain',updated_at=now() WHERE user_id=? AND lead_id=?",(row['user_id'],row['lead_id']))
        else:
            with db:db.execute("UPDATE lead_reminders SET status='sent',updated_at=now() WHERE user_id=? AND lead_id=?",(row['user_id'],row['lead_id']))
            sent+=1
    return sent


def register_routes(app,db):
    from flask import abort,flash,g,redirect,request

    @app.post('/app/leads/<int:lid>/reminder')
    def reminder(lid):
        owner=db().execute("SELECT 1 FROM user_leads WHERE user_id=? AND lead_id=? AND delivery_status<>'filtered'",(g.user['id'],lid)).fetchone()
        if not owner:abort(404)
        if request.form.get('action')=='cancel':
            flash('Напоминание отменено.' if cancel(db(),g.user['id'],lid) else 'Напоминание уже отправлено или отправляется.')
        else:
            try:
                due=datetime.fromisoformat(request.form.get('due',''))
                if due.tzinfo:raise ValueError('Укажите местное время МСК.')
                schedule(db(),g.user['id'],lid,due.replace(tzinfo=timezone(timedelta(hours=3))),request.form.get('note','').strip())
                flash('Напоминание сохранено. Оно придёт в Telegram.')
            except ValueError:flash('Укажите время в будущем (МСК), не дальше чем через год. Заметка — до 500 символов. Если напоминание отправляется, повторите позже.')
        return redirect('/app/leads/'+str(lid))
