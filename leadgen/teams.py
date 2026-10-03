"""Workspace membership, invitations, roles and lead assignments."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import abort, flash, g, redirect, render_template, request, session, url_for


ROLES = {'owner':'Владелец','admin':'Администратор','manager':'Менеджер','viewer':'Наблюдатель'}
EDIT_LEADS = {'owner','admin','manager'}
EDIT_SETTINGS = {'owner','admin'}


def ensure_workspace(db, user):
    name = ((user['display_name'] or 'Моя команда') + ' — команда')[:100]
    with db:
        db.execute('''INSERT INTO workspaces(owner_user_id,name) VALUES(?,?)
            ON CONFLICT(owner_user_id) DO NOTHING''', (user['id'], name))
        workspace = db.execute('SELECT id FROM workspaces WHERE owner_user_id=?', (user['id'],)).fetchone()
        db.execute("""INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,'owner')
            ON CONFLICT(workspace_id,user_id) DO NOTHING""", (workspace['id'], user['id']))
    return workspace['id']


def load_workspace(db, user, selected=None):
    ensure_workspace(db, user)
    rows = db.execute('''SELECT w.id,w.name,w.owner_user_id,m.role
        FROM workspace_members m JOIN workspaces w ON w.id=m.workspace_id
        WHERE m.user_id=? ORDER BY (m.role='owner') DESC,w.name''', (user['id'],)).fetchall()
    chosen = next((row for row in rows if str(row['id']) == str(selected)), rows[0])
    return rows, chosen


def require_role(*allowed):
    if g.role not in allowed:
        abort(403)


def can_manage_member(actor_role, target_role, new_role=None):
    if target_role == 'owner':
        return False
    if actor_role == 'owner':
        return new_role != 'owner'
    return actor_role == 'admin' and target_role in ('manager','viewer') and new_role in (None,'manager','viewer')


def register_team_routes(app, db):
    @app.post('/app/workspace')
    def switch_workspace():
        workspace_id = request.form.get('workspace_id','')
        if not workspace_id.isdigit():abort(400)
        row = db().execute('SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?',
                           (int(workspace_id), g.user['id'])).fetchone()
        if not row:abort(403)
        session['workspace_id'] = int(workspace_id)
        return redirect(request.form.get('next','/app') if request.form.get('next','').startswith('/app') else '/app')

    @app.get('/app/team')
    def team_page():
        members = db().execute('''SELECT m.user_id,m.role,m.joined_at,u.display_name,u.telegram_user_id
            FROM workspace_members m JOIN app_users u ON u.id=m.user_id
            WHERE m.workspace_id=? ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1
            WHEN 'manager' THEN 2 ELSE 3 END,u.display_name''', (g.workspace['id'],)).fetchall()
        invites = []
        if g.role in EDIT_SETTINGS:
            invites = db().execute('''SELECT id,role,expires_at,revoked,accepted_at FROM team_invites
                WHERE workspace_id=? ORDER BY id DESC LIMIT 30''', (g.workspace['id'],)).fetchall()
        invite_path = session.pop('new_invite_path', None)
        return render_template('team.html', members=members, invites=invites,
                               invite_path=invite_path, roles=ROLES)

    @app.post('/app/team/name')
    def team_name():
        require_role(*EDIT_SETTINGS)
        name = request.form.get('name','').strip()
        if not name or len(name)>100:abort(400)
        with db():db().execute('UPDATE workspaces SET name=?,updated_at=now() WHERE id=?', (name,g.workspace['id']))
        flash('Название команды сохранено.')
        return redirect('/app/team')

    @app.post('/app/team/invite')
    def team_invite():
        require_role(*EDIT_SETTINGS)
        role = request.form.get('role','')
        if role not in ('admin','manager','viewer') or (g.role=='admin' and role=='admin'):
            abort(403)
        token = secrets.token_urlsafe(32)
        with db():
            db().execute('''INSERT INTO team_invites(workspace_id,token_hash,role,invited_by,expires_at)
                VALUES(?,?,?,?,?)''', (g.workspace['id'],hashlib.sha256(token.encode()).hexdigest(),role,
                                      g.user['id'],datetime.now(timezone.utc)+timedelta(days=7)))
        session['new_invite_path'] = url_for('join_team',token=token)
        flash('Одноразовая ссылка создана. Она действует 7 дней.')
        return redirect('/app/team')

    @app.post('/app/team/invites/<int:invite_id>/revoke')
    def revoke_invite(invite_id):
        require_role(*EDIT_SETTINGS)
        with db():
            row=db().execute('''UPDATE team_invites SET revoked=true WHERE id=? AND workspace_id=?
                AND accepted_at IS NULL RETURNING id''',(invite_id,g.workspace['id'])).fetchone()
        if not row:abort(404)
        flash('Приглашение отозвано.')
        return redirect('/app/team')

    @app.route('/app/team/join/<token>',methods=['GET','POST'])
    def join_team(token):
        if len(token)>100:abort(404)
        digest=hashlib.sha256(token.encode()).hexdigest()
        invite=db().execute('''SELECT i.*,w.name FROM team_invites i JOIN workspaces w ON w.id=i.workspace_id
            WHERE i.token_hash=?''',(digest,)).fetchone()
        valid=bool(invite and not invite['revoked'] and not invite['accepted_at'] and
                   invite['expires_at']>datetime.now(timezone.utc))
        if request.method=='POST':
            if not valid:abort(400)
            with db():
                locked=db().execute('''SELECT * FROM team_invites WHERE token_hash=? FOR UPDATE''',(digest,)).fetchone()
                if not locked or locked['revoked'] or locked['accepted_at'] or locked['expires_at']<=datetime.now(timezone.utc):
                    abort(400)
                db().execute('''INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,?)
                    ON CONFLICT(workspace_id,user_id) DO NOTHING''',(locked['workspace_id'],g.user['id'],locked['role']))
                db().execute('UPDATE team_invites SET accepted_by=?,accepted_at=now() WHERE id=?',
                             (g.user['id'],locked['id']))
            session['workspace_id']=invite['workspace_id']
            flash('Вы присоединились к команде.')
            return redirect('/app')
        return render_template('join_team.html',invite=invite,valid=valid,roles=ROLES)

    @app.post('/app/team/members/<int:user_id>')
    def change_member(user_id):
        require_role(*EDIT_SETTINGS)
        target=db().execute('SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?',
                            (g.workspace['id'],user_id)).fetchone()
        if not target:abort(404)
        action=request.form.get('action')
        if action=='remove':
            if not can_manage_member(g.role,target['role']):abort(403)
            with db():db().execute('DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?',
                                   (g.workspace['id'],user_id))
            flash('Участник удалён из команды.')
        else:
            role=request.form.get('role','')
            if role not in ('admin','manager','viewer') or not can_manage_member(g.role,target['role'],role):abort(403)
            with db():db().execute('UPDATE workspace_members SET role=? WHERE workspace_id=? AND user_id=?',
                                   (role,g.workspace['id'],user_id))
            flash('Роль участника изменена.')
        return redirect('/app/team')

    @app.post('/app/leads/<int:lead_id>/assign')
    def assign_lead(lead_id):
        require_role(*EDIT_LEADS)
        owner=db().execute("SELECT 1 FROM user_leads WHERE user_id=? AND lead_id=? AND delivery_status<>'filtered'",
                           (g.owner_id,lead_id)).fetchone()
        if not owner:abort(404)
        assignee=request.form.get('assignee','')
        with db():
            if not assignee:
                db().execute('DELETE FROM lead_assignments WHERE workspace_id=? AND lead_id=?',
                             (g.workspace['id'],lead_id))
                label='Ответственный снят'
            else:
                if not assignee.isdigit():abort(400)
                member=db().execute('SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?',
                                    (g.workspace['id'],int(assignee))).fetchone()
                if not member:abort(400)
                db().execute('''INSERT INTO lead_assignments
                    (workspace_id,owner_user_id,lead_id,assignee_user_id,assigned_by) VALUES(?,?,?,?,?)
                    ON CONFLICT(workspace_id,lead_id) DO UPDATE SET assignee_user_id=excluded.assignee_user_id,
                    assigned_by=excluded.assigned_by,assigned_at=now(),notification_status='pending',
                    notification_error='',notification_updated_at=now()''',
                    (g.workspace['id'],g.owner_id,lead_id,int(assignee),g.user['id']))
                person=db().execute('SELECT display_name FROM app_users WHERE id=?',(int(assignee),)).fetchone()
                label='Ответственный: '+person['display_name']
                db().execute("INSERT INTO lead_activity(user_id,lead_id,kind,detail) VALUES(?,?,'assignment',?)",
                             (g.owner_id,lead_id,label))
            from .integrations import enqueue_lead_event
            enqueue_lead_event(db(),g.owner_id,lead_id,'lead.updated')
        flash(label+'.')
        return redirect('/app/leads/'+str(lead_id))
