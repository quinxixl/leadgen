"""Project settings and notes; authorization always comes from the session."""
import json
from flask import abort, flash, g, redirect, render_template, request
from .core import TOPICS


def register_projects(app, db):
    @app.route('/app/projects',methods=['GET','POST'])
    def projects_page():
        if request.method=='POST':
            from .teams import EDIT_SETTINGS,require_role
            require_role(*EDIT_SETTINGS)
            name=request.form.get('name','').strip()
            if not name or len(name)>100:abort(400)
            with db():
                # Lock the owner, so parallel requests cannot exceed the limit.
                db().execute('SELECT id FROM workspaces WHERE id=? FOR UPDATE',(g.workspace['id'],))
                count=db().execute('SELECT count(*) AS n FROM projects WHERE workspace_id=?',(g.workspace['id'],)).fetchone()['n']
                if count>=20:abort(400,description='Можно создать не более 20 проектов.')
                row=db().execute('''INSERT INTO projects(user_id,workspace_id,name,topics,enabled)
                    VALUES(?,?,?,?::jsonb,false) RETURNING id''',
                    (g.owner_id,g.workspace['id'],name,json.dumps(list(TOPICS)))).fetchone()
            return redirect('/app/projects/'+str(row['id']))
        rows=db().execute('''SELECT p.*,count(pl.lead_id) AS matches FROM projects p
            LEFT JOIN project_leads pl ON pl.project_id=p.id AND pl.user_id=p.user_id
            WHERE p.workspace_id=? GROUP BY p.id ORDER BY p.id''',(g.workspace['id'],)).fetchall()
        return render_template('projects.html',rows=rows)

    @app.route('/app/projects/<int:pid>',methods=['GET','POST'])
    def project_page(pid):
        project=db().execute('SELECT * FROM projects WHERE id=? AND workspace_id=?',(pid,g.workspace['id'])).fetchone()
        if not project:abort(404)
        if request.method=='POST':
            from .teams import EDIT_SETTINGS,require_role
            require_role(*EDIT_SETTINGS)
            name=request.form.get('name','').strip();budget=request.form.get('min_budget','');score=request.form.get('min_score','')
            topics=request.form.getlist('topics')
            if not name or len(name)>100 or not budget.isdigit() or not score.isdigit():abort(400)
            if int(budget)>100000000 or int(score)>100 or not topics or not set(topics)<=set(TOPICS):abort(400)
            def lines(field):
                words=list(dict.fromkeys(x.strip() for x in request.form.get(field,'').splitlines() if x.strip()))
                if len(words)>100 or any(len(x)>200 for x in words):abort(400)
                return json.dumps(words,ensure_ascii=False)
            def reply_lines(field):
                words=list(dict.fromkeys(x.strip() for x in request.form.get(field,'').splitlines() if x.strip()))
                if len(words)>30 or any(len(x)>300 for x in words):abort(400)
                return json.dumps(words,ensure_ascii=False)
            reply_length=request.form.get('reply_max_length',str(project['reply_max_length']))
            if not reply_length.isdigit() or not 200<=int(reply_length)<=1500:abort(400)
            with db():
                db().execute('''UPDATE projects SET name=?,description=?,keywords=?::jsonb,stop_words=?::jsonb,
                    topics=?::jsonb,source_names=?::jsonb,min_budget=?,min_score=?,show_without_budget=?,
                    show_possible_needs=?,enabled=?,reply_sender=?,reply_offer=?,reply_proof=?::jsonb,
                    reply_question=?,reply_signature=?,forbidden_phrases=?::jsonb,reply_max_length=?,
                    updated_at=now() WHERE id=? AND user_id=?''',
                    (name,request.form.get('description','')[:3000],lines('keywords'),lines('stop_words'),json.dumps(topics),
                    lines('source_names'),int(budget),int(score),bool(request.form.get('no_budget')),
                    bool(request.form.get('possible')),bool(request.form.get('enabled')),
                    request.form.get('reply_sender','').strip()[:120],request.form.get('reply_offer','').strip()[:1000],
                    reply_lines('reply_proof'),request.form.get('reply_question','').strip()[:300],
                    request.form.get('reply_signature','').strip()[:300],reply_lines('forbidden_phrases'),
                    int(reply_length),pid,g.owner_id))
                db().execute("DELETE FROM user_leads WHERE user_id=? AND delivery_status='filtered'",(g.owner_id,))
            flash('Фильтры проекта сохранены. Они применяются при доставке лидов.');return redirect(request.path)
        return render_template('project.html',project=project,topics=TOPICS)

    @app.post('/app/leads/<int:lid>/notes')
    def add_note(lid):
        from .teams import EDIT_LEADS,require_role
        require_role(*EDIT_LEADS)
        row=db().execute("SELECT 1 FROM user_leads WHERE user_id=? AND lead_id=? AND delivery_status<>'filtered'",(g.owner_id,lid)).fetchone()
        if not row:abort(404)
        note=request.form.get('note','').strip()
        if not note or len(note)>4000:abort(400)
        with db():
            db().execute("INSERT INTO lead_activity(user_id,lead_id,kind,detail) VALUES(?,?,'note',?)",(g.owner_id,lid,note))
            from .integrations import enqueue_lead_event
            enqueue_lead_event(db(),g.owner_id,lid,'lead.updated')
        flash('Заметка сохранена.');return redirect('/app/leads/'+str(lid))
