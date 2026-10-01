"""Owner-scoped bulk CRM actions and tags."""
import re

from flask import abort, flash, g, redirect, request

from .product import FEEDBACK, PIPELINE
from .teams import EDIT_LEADS, require_role


TAG = re.compile(r'[^\x00-\x1f\x7f]{1,40}$')


def register_lead_actions(app, db):
    def selected_leads():
        raw = list(dict.fromkeys(request.form.getlist('leads')))
        if not raw or len(raw)>100 or any(not item.isdigit() for item in raw):abort(400)
        ids=[int(item) for item in raw]
        placeholders=','.join('?' for _ in ids)
        rows=db().execute("SELECT lead_id FROM user_leads WHERE user_id=? AND delivery_status<>'filtered' AND lead_id IN ("+
                          placeholders+')',[g.owner_id]+ids).fetchall()
        if len(rows)!=len(ids):abort(404)
        return ids

    @app.post('/app/leads/bulk')
    def bulk_leads():
        require_role(*EDIT_LEADS)
        ids=selected_leads();action=request.form.get('action','')
        with db():
            if action.startswith('status:'):
                status=action[7:]
                if status not in PIPELINE:abort(400)
                for lead_id in ids:
                    db().execute('UPDATE user_leads SET pipeline_status=?,updated_at=now() WHERE user_id=? AND lead_id=?',
                                 (status,g.owner_id,lead_id))
                    db().execute("INSERT INTO lead_activity(user_id,lead_id,kind,detail) VALUES(?,?,'status',?)",
                                 (g.owner_id,lead_id,PIPELINE[status]))
                label='Статус изменён'
            elif action.startswith('feedback:'):
                feedback=action[9:]
                if feedback not in FEEDBACK:abort(400)
                for lead_id in ids:
                    db().execute('''INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,?)
                        ON CONFLICT(user_id,lead_id) DO UPDATE SET label=excluded.label,updated_at=now()''',
                        (g.owner_id,lead_id,feedback))
                    db().execute("INSERT INTO lead_activity(user_id,lead_id,kind,detail) VALUES(?,?,'feedback',?)",
                                 (g.owner_id,lead_id,FEEDBACK[feedback]))
                label='Оценка сохранена'
            elif action.startswith('assign:'):
                assignee=action[7:]
                if assignee and not assignee.isdigit():abort(400)
                if assignee:
                    member=db().execute('SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?',
                                        (g.workspace['id'],int(assignee))).fetchone()
                    if not member:abort(400)
                for lead_id in ids:
                    if assignee:
                        db().execute('''INSERT INTO lead_assignments
                            (workspace_id,owner_user_id,lead_id,assignee_user_id,assigned_by) VALUES(?,?,?,?,?)
                            ON CONFLICT(workspace_id,lead_id) DO UPDATE SET assignee_user_id=excluded.assignee_user_id,
                            assigned_by=excluded.assigned_by,assigned_at=now(),notification_status='pending',
                            notification_error='',notification_updated_at=now()''',
                            (g.workspace['id'],g.owner_id,lead_id,int(assignee),g.user['id']))
                    else:
                        db().execute('DELETE FROM lead_assignments WHERE workspace_id=? AND lead_id=?',
                                     (g.workspace['id'],lead_id))
                label='Ответственный изменён'
            elif action=='tag':
                tag=request.form.get('tag','').strip()
                if not TAG.fullmatch(tag):abort(400)
                for lead_id in ids:
                    db().execute('''INSERT INTO lead_tags(user_id,lead_id,tag) VALUES(?,?,?)
                        ON CONFLICT DO NOTHING''',(g.owner_id,lead_id,tag))
                label='Тег добавлен'
            else:abort(400)
        flash(f'{label} для лидов: {len(ids)}.')
        return redirect('/app/leads')

    @app.post('/app/leads/<int:lead_id>/tags')
    def lead_tag(lead_id):
        require_role(*EDIT_LEADS)
        owner=db().execute("SELECT 1 FROM user_leads WHERE user_id=? AND lead_id=? AND delivery_status<>'filtered'",
                           (g.owner_id,lead_id)).fetchone()
        if not owner:abort(404)
        tag=request.form.get('tag','').strip()
        if not TAG.fullmatch(tag):abort(400)
        with db():
            if request.form.get('action')=='remove':
                db().execute('DELETE FROM lead_tags WHERE user_id=? AND lead_id=? AND tag=?',(g.owner_id,lead_id,tag))
                message='Тег удалён.'
            else:
                db().execute('INSERT INTO lead_tags(user_id,lead_id,tag) VALUES(?,?,?) ON CONFLICT DO NOTHING',
                             (g.owner_id,lead_id,tag));message='Тег добавлен.'
        flash(message);return redirect('/app/leads/'+str(lead_id))
