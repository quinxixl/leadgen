"""/admin/ai: prompt versions, sandbox, journal and quality of the AI lead scoring (platform admins only)."""
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from . import ai_prompts as ap

# Sandbox runs spend Groq tokens: per admin and hour.
SANDBOX_LIMIT=(30,3600)
SANDBOX_MAX_LEADS=10
SANDBOX_MAX_TEXT=6000
RESCORE_MAX=200
JOURNAL_LIMIT=100
SCORE_STATUSES={'scored':'Оценён','skipped':'Отсеян правилами','failed':'Ошибка','pending':'В очереди'}
RULE_STATUSES={'ready':'Подходит','review':'На проверке','rejected':'Отсеян','duplicate':'Дубль','sent':'Отправлен',
               'sending':'Отправляется','uncertain':'Отправка не подтверждена'}


# --- sandbox ----------------------------------------------------------------------------------------
def _content(raw):
    try:return json.loads(raw)['choices'][0]['message']['content']
    except (KeyError,IndexError,TypeError,ValueError):
        return raw.decode('utf-8','replace')[:20000] if isinstance(raw,bytes) else str(raw)[:20000]


def _pretty(text):
    try:return json.dumps(json.loads(text),ensure_ascii=False,indent=2)
    except (TypeError,ValueError):return text


def _raw_topics(content):
    """Topics exactly as the model returned them, to show the names the contract dropped."""
    try:items=json.loads(content).get('items')
    except (AttributeError,TypeError,ValueError):return {}
    result={}
    for item in items if isinstance(items,list) else []:
        if isinstance(item,dict) and isinstance(item.get('id'),int) and isinstance(item.get('topics'),list):
            result[item['id']]=[str(value) for value in item['topics']][:10]
    return result


def run_scoring(items,prompt,examples,api_key,opener,timeout=None):
    """One scoring request in the sandbox: parsed results, raw answer, tokens, latency and contract check."""
    from .ai_scoring import ScoringError,build_request,parse_response,post_groq
    out={'prompt':prompt,'results':{},'usage':{},'raw':'','error':'','valid':False,'missing':[],'dropped':{}}
    started=time.monotonic()
    try:
        raw=post_groq(build_request(items,prompt['model'],examples,prompt),api_key,opener,timeout)
        content=_content(raw);out['raw']=_pretty(content)
        results,usage=parse_response(raw,[lead_id for lead_id,_ in items])
        out.update(results=results,usage=usage)
        out['missing']=[lead_id for lead_id,_ in items if lead_id not in results]
        for lead_id,names in _raw_topics(content).items():
            kept=set(results.get(lead_id,{}).get('topics',[]))
            dropped=[name for name in names if name not in kept]
            if dropped:out['dropped'][lead_id]=dropped
        out['valid']=not out['missing']
        if out['missing']:out['error']='Нет корректного ответа по схеме для: '+', '.join(map(str,out['missing']))
    except ScoringError as exc:out['error']=str(exc)
    out['latency_ms']=int((time.monotonic()-started)*1000)
    return out


def run_offers(lead,profile_services,portfolio,settings,prompt,api_key,opener):
    from .ai_offers import AIOfferError,build_request,parse_offer_response,post_offer_request
    from .drafts import apply_draft_rules
    out={'prompt':prompt,'drafts':{},'usage':{},'raw':'','error':'','valid':False}
    started=time.monotonic()
    try:
        body,_=build_request(lead,profile_services,portfolio,settings,prompt['model'],None,prompt)
        raw=post_offer_request(body,api_key,opener)
        out['raw']=_pretty(_content(raw))
        drafts,usage=parse_offer_response(raw)
        out.update(drafts=apply_draft_rules(drafts,settings),usage=usage,valid=True)
    except AIOfferError as exc:out['error']=str(exc)
    out['latency_ms']=int((time.monotonic()-started)*1000)
    return out


def side_by_side(jobs):
    """Run the draft and the active prompt at once: two sequential LLM calls could outlive the web timeout."""
    with ThreadPoolExecutor(max_workers=max(1,len(jobs))) as pool:
        futures={name:pool.submit(job) for name,job in jobs.items()}
        return {name:future.result() for name,future in futures.items()}


def sandbox_leads(db,ids=(),source='',with_feedback=False,last=5):
    """Leads for a sandbox run: explicit ids, else the newest ones matching the filters."""
    if ids:
        rows=db.execute('''SELECT id,payload FROM leads WHERE id = ANY(?) ORDER BY id DESC LIMIT ?''',
                        (list(ids)[:SANDBOX_MAX_LEADS],SANDBOX_MAX_LEADS)).fetchall()
    else:
        rows=db.execute('''SELECT l.id,l.payload FROM leads l WHERE l.status<>'duplicate'
            AND (?='' OR l.source=?)
            AND (NOT ? OR EXISTS (SELECT 1 FROM lead_feedback f WHERE f.lead_id=l.id))
            ORDER BY l.id DESC LIMIT ?''',(source,source,bool(with_feedback),max(1,min(SANDBOX_MAX_LEADS,last)))).fetchall()
    return rows


def lead_from_payload(payload):
    from .core import load_lead
    try:return load_lead(payload)
    except (TypeError,ValueError):return None


def feedback_labels(db,lead_ids):
    if not lead_ids:return {}
    rows=db.execute('''SELECT lead_id,string_agg(DISTINCT label,',' ORDER BY label) AS labels FROM lead_feedback
        WHERE lead_id = ANY(?) GROUP BY lead_id''',(list(lead_ids),)).fetchall()
    return {row['lead_id']:row['labels'].split(',') for row in rows}


def runtime_active(db,task):
    """Uncached: the admin sees the version that is active right now."""
    row=ap.current_active(db,task)
    return ap.as_runtime(row) if row and row['model'] in ap.MODELS else ap.factory(task)


# --- journal and quality ------------------------------------------------------------------------------
def parse_day(value):
    try:return date.fromisoformat(value).isoformat() if value else ''
    except ValueError:return ''


def journal_filters(args):
    from .ai_scoring import CATEGORIES
    from .core import TOPICS
    topic=args.get('topic','');category=args.get('category','');status=args.get('status','')
    return {'topic':topic if topic in TOPICS else '','category':category if category in CATEGORIES else '',
            'status':status if status in SCORE_STATUSES else '','source':args.get('source','')[:300],
            'date_from':parse_day(args.get('date_from','')),'date_to':parse_day(args.get('date_to','')),
            'feedback':args.get('feedback')=='1'}


def journal(db,filters,limit=JOURNAL_LIMIT):
    where=['TRUE'];params=[]
    if filters['topic']:where.append('s.topics @> ?::jsonb');params.append(json.dumps([filters['topic']],ensure_ascii=False))
    if filters['category']:where.append('s.category=?');params.append(filters['category'])
    if filters['status']:where.append('s.status=?');params.append(filters['status'])
    if filters['source']:where.append("l.source=?");params.append(filters['source'])
    if filters['date_from']:where.append("s.updated_at >= (?::date AT TIME ZONE 'Europe/Moscow')");params.append(filters['date_from'])
    if filters['date_to']:
        where.append("s.updated_at < ((?::date + 1) AT TIME ZONE 'Europe/Moscow')");params.append(filters['date_to'])
    if filters['feedback']:where.append('EXISTS (SELECT 1 FROM lead_feedback f WHERE f.lead_id=s.lead_id)')
    rows=db.execute(f'''SELECT s.lead_id,s.status,s.score,s.category,s.topics,s.summary,s.urgency,s.budget_amount,
            s.budget_currency,s.model,s.prompt_id,s.input_tokens,s.output_tokens,s.error,s.updated_at,
            l.status AS rule_status,l.reason AS rule_reason,l.payload,p.version AS prompt_version,
            (SELECT string_agg(DISTINCT f.label,',' ORDER BY f.label) FROM lead_feedback f WHERE f.lead_id=s.lead_id) AS feedback
        FROM lead_ai_scores s JOIN leads l ON l.id=s.lead_id LEFT JOIN ai_prompts p ON p.id=s.prompt_id
        WHERE {' AND '.join(where)} ORDER BY s.updated_at DESC,s.lead_id DESC LIMIT ?''',params+[limit]).fetchall()
    result=[]
    for row in rows:
        item=dict(row);lead=lead_from_payload(row['payload'])
        item['title']=(lead.title or lead.text or '')[:160] if lead else '';item['source']=lead.source if lead else ''
        item['url']=lead.url if lead else ''
        topics_value=item['topics'] if isinstance(item['topics'],list) else json.loads(item['topics'] or '[]')
        item['topics']=topics_value;item['feedback']=item['feedback'].split(',') if item['feedback'] else []
        result.append(item)
    return result


def journal_sources(db):
    rows=db.execute('''SELECT DISTINCT l.source AS source FROM lead_ai_scores s JOIN leads l ON l.id=s.lead_id
        WHERE s.updated_at>now()-interval '30 days' ORDER BY 1 LIMIT 200''').fetchall()
    return [row['source'] for row in rows if row['source']]


def agreement(db,days):
    """Share of user labels that agree with the AI category: fit↔service_request, ad/competitor↔ad, job/vacancy↔job."""
    from .ai_scoring import FEEDBACK_CATEGORY
    rows=db.execute('''SELECT f.label,s.category,count(*) AS n FROM lead_feedback f JOIN lead_ai_scores s ON s.lead_id=f.lead_id
        WHERE s.status='scored' AND f.label = ANY(?) AND f.updated_at>now()-(? * interval '1 day')
        GROUP BY f.label,s.category''',(list(FEEDBACK_CATEGORY),days)).fetchall()
    total=sum(row['n'] for row in rows)
    agree=sum(row['n'] for row in rows if FEEDBACK_CATEGORY[row['label']]==row['category'])
    return {'days':days,'total':total,'agree':agree,'rate':round(100*agree/total) if total else None,
            'pairs':sorted(([row['label'],row['category'],row['n']] for row in rows),key=lambda pair:-pair[2])}


def usage_by_day(db,days=14):
    scoring=db.execute('''SELECT (updated_at AT TIME ZONE 'Europe/Moscow')::date AS day,
            coalesce(sum(input_tokens),0) AS input_tokens,coalesce(sum(output_tokens),0) AS output_tokens,
            count(*) FILTER (WHERE status='scored') AS scored,count(*) FILTER (WHERE status='failed') AS failed
        FROM lead_ai_scores WHERE updated_at>now()-(? * interval '1 day') GROUP BY 1''',(days,)).fetchall()
    offers=db.execute('''SELECT (created_at AT TIME ZONE 'Europe/Moscow')::date AS day,
            coalesce(sum(input_tokens+output_tokens),0) AS tokens,count(*) AS generations
        FROM ai_offer_generations WHERE created_at>now()-(? * interval '1 day') GROUP BY 1''',(days,)).fetchall()
    merged={}
    for row in scoring:merged.setdefault(row['day'],{}).update(dict(row))
    for row in offers:merged.setdefault(row['day'],{}).update(offer_tokens=row['tokens'],generations=row['generations'])
    return [dict({'day':day,'input_tokens':0,'output_tokens':0,'scored':0,'failed':0,'offer_tokens':0,'generations':0},**values)
            for day,values in sorted(merged.items(),reverse=True)]


def summary(db):
    from .ai_scoring import USED_TODAY,daily_limit,scoring_configured
    counts=db.execute('''SELECT count(*) FILTER (WHERE status='pending') AS pending,
            count(*) FILTER (WHERE status='failed' AND updated_at>now()-interval '1 day') AS failed_day,
            count(*) FILTER (WHERE status='failed' AND updated_at>now()-interval '7 days') AS failed_week
        FROM lead_ai_scores''').fetchone()
    errors=db.execute('''SELECT error,count(*) AS n FROM lead_ai_scores WHERE status='failed' AND error<>''
        AND updated_at>now()-interval '7 days' GROUP BY error ORDER BY n DESC LIMIT 5''').fetchall()
    return {'used':db.execute(USED_TODAY).fetchone()['n'],'limit':daily_limit(runtime_active(db,'scoring')),
            'pending':counts['pending'],'failed_day':counts['failed_day'],'failed_week':counts['failed_week'],
            'errors':errors,'configured':scoring_configured(),'paused':ap.pause_info(db),
            'week':agreement(db,7),'month':agreement(db,30),'days':usage_by_day(db)}


def rescore(db,lead_ids=(),last=0):
    """Put leads back in the scoring queue; the scorer re-runs the active prompt on them."""
    if lead_ids:ids=sorted({int(value) for value in lead_ids})[:RESCORE_MAX]
    else:
        ids=[row['id'] for row in db.execute("SELECT id FROM leads WHERE status<>'duplicate' ORDER BY id DESC LIMIT ?",
                                             (max(1,min(RESCORE_MAX,int(last))),)).fetchall()]
    if not ids:return 0
    with db:
        rows=db.execute('''INSERT INTO lead_ai_scores(lead_id,status)
            SELECT id,'pending' FROM leads WHERE id = ANY(?) AND status<>'duplicate'
            ON CONFLICT(lead_id) DO UPDATE SET status='pending',score=NULL,category=NULL,topics='[]'::jsonb,
                budget_amount=NULL,budget_currency=NULL,urgency=NULL,summary='',error='',attempts=0,updated_at=now()
            RETURNING lead_id''',(ids,)).fetchall()
    return len(rows)


# --- routes -------------------------------------------------------------------------------------------
def register_ai_admin_routes(app,db):
    from flask import abort,current_app,flash,g,redirect,render_template,request
    from .rate_limit import RateLimited,hit
    from .web import is_platform_admin

    def admin_only():
        if not is_platform_admin(g.user):abort(403)

    def task_arg(value):
        if value not in ap.TASKS:abort(400)
        return value

    def param_defaults(task):
        if task=='scoring':
            from .ai_scoring import batch_size,daily_limit,fewshot_count
            return {'batch':batch_size(),'daily_limit':daily_limit(),'fewshot':fewshot_count()}
        from .ai_offers import FACTORY_MAX_TOKENS
        return {'max_tokens':FACTORY_MAX_TOKENS}

    def contract(task):
        if task=='scoring':
            from .ai_scoring import CONTRACT
        else:
            from .ai_offers import CONTRACT
        return CONTRACT

    def editor_from(task,source,row=None):
        """Values for the editor: a draft being edited, a version to copy, the active one or the factory."""
        if row is not None:
            return {'instructions':row['instructions'],'model':row['model'],'temperature':float(row['temperature']),
                    'params':ap.clean_params(task,row['params']),'note':row['note'] if 'note' in row.keys() else '',
                    'source':source}
        base=ap.factory(task)
        model=base['model'] if base['model'] in ap.MODELS else next(iter(ap.MODELS))
        return {'instructions':base['instructions'],'model':model,'temperature':base['temperature'],'params':{},
                'note':'','source':source}

    def render(task,editor=None,sandbox=None,status=200):
        rows=ap.versions(db(),task)
        active=ap.current_active(db(),task)
        drafts=[row for row in rows if row['status']=='draft']
        if editor is None:
            edit=request.args.get('edit','');copy=request.args.get('from','')
            row=ap.get(db(),int(edit)) if edit.isdigit() else None
            if row and row['task']==task and row['status']=='draft':editor=editor_from(task,'edit',row)|{'id':row['id'],'version':row['version']}
            elif copy=='factory':editor=editor_from(task,'factory')
            elif copy.isdigit() and (source:=ap.get(db(),int(copy))) and source['task']==task:
                editor=editor_from(task,'copy',source)|{'copied':source['version']}
            elif active:editor=editor_from(task,'copy',active)|{'copied':active['version']}
            else:editor=editor_from(task,'factory')
        chosen=request.values.get('draft','')
        sandbox_draft=next((row for row in drafts if str(row['id'])==chosen),drafts[0] if drafts else None)
        sources=[] if task!='scoring' else journal_sources(db())
        from .product import FEEDBACK
        return render_template('ai_admin.html',task=task,tasks=ap.TASKS,rows=rows,active=active,drafts=drafts,
            factory=ap.factory(task),contract=contract(task),models=ap.MODELS,params=ap.PARAMS[task],
            param_defaults=param_defaults(task),editor=editor,max_instructions=ap.MAX_INSTRUCTIONS,
            statuses=ap.STATUSES,test_current=ap.test_current,sandbox=sandbox,sandbox_draft=sandbox_draft,
            sandbox_sources=sources,sandbox_max=SANDBOX_MAX_LEADS,paused=ap.pause_info(db()),
            runtime=runtime_active(db(),task),feedback_labels=FEEDBACK,
            form=request.form if request.method=='POST' else {}),status

    @app.get('/admin/ai')
    def ai_admin():
        admin_only()
        return render(task_arg(request.args.get('task','scoring')))

    @app.post('/admin/ai/prompts')
    def ai_create_prompt():
        admin_only();task=task_arg(request.form.get('task',''))
        try:data=ap.validate(task,request.form)
        except ValueError as exc:
            flash(str(exc))
            return render(task,editor=_editor_values(task,request.form),status=400)
        prompt_id=ap.create_draft(db(),task,data,g.user['id'])
        flash('Черновик сохранён. Проверьте его в песочнице перед активацией.')
        return redirect(f'/admin/ai?task={task}&draft={prompt_id}#sandbox')

    def _editor_values(task,form,row=None):
        values={'instructions':form.get('instructions',''),'model':form.get('model',''),
                'temperature':form.get('temperature',''),'note':form.get('note',''),'source':'edit' if row else 'form',
                'params':{key:form.get('param_'+key,'') for key in ap.PARAMS[task]}}
        if row:values.update(id=row['id'],version=row['version'])
        return values

    def prompt_row(prompt_id):
        row=ap.get(db(),prompt_id)
        if not row:abort(404)
        return row

    @app.post('/admin/ai/prompts/<int:prompt_id>')
    def ai_update_prompt(prompt_id):
        admin_only();row=prompt_row(prompt_id);task=row['task']
        if row['status']!='draft':
            flash('Изменять можно только черновик: создайте новый на основе этой версии.')
            return redirect(f'/admin/ai?task={task}')
        try:data=ap.validate(task,request.form)
        except ValueError as exc:
            flash(str(exc))
            return render(task,editor=_editor_values(task,request.form,row),status=400)
        ap.update_draft(db(),prompt_id,data)
        flash(f"Черновик v{row['version']} сохранён. После изменений нужна новая проверка в песочнице.")
        return redirect(f'/admin/ai?task={task}&draft={prompt_id}#sandbox')

    @app.post('/admin/ai/prompts/<int:prompt_id>/activate')
    def ai_activate_prompt(prompt_id):
        admin_only();row=prompt_row(prompt_id)
        try:activated=ap.activate(db(),prompt_id)
        except ValueError as exc:flash(str(exc))
        else:flash(f"Версия v{activated['version']} активна. Процессы подхватят её в течение минуты.")
        return redirect(f"/admin/ai?task={row['task']}")

    @app.post('/admin/ai/prompts/<int:prompt_id>/discard')
    def ai_discard_prompt(prompt_id):
        admin_only();row=prompt_row(prompt_id)
        if ap.discard_draft(db(),prompt_id):flash(f"Черновик v{row['version']} перемещён в архив.")
        return redirect(f"/admin/ai?task={row['task']}")

    @app.post('/admin/ai/reset')
    def ai_reset_prompt():
        admin_only();task=task_arg(request.form.get('task',''))
        if ap.reset_to_factory(db(),task):flash('Активная версия снята: работает заводской промпт из кода.')
        else:flash('Заводской промпт уже используется.')
        return redirect(f'/admin/ai?task={task}')

    @app.post('/admin/ai/pause')
    def ai_pause():
        admin_only()
        on=request.form.get('paused')=='1'
        ap.set_paused(db(),on,g.user['id'])
        flash('AI-оценка лидов приостановлена: новые лиды уходят по правилам без ожидания оценки.' if on
              else 'AI-оценка лидов возобновлена.')
        back=request.form.get('next','')
        return redirect(back if back in ('/admin/ai','/admin/ai/journal') else '/admin/ai')

    @app.post('/admin/ai/sandbox')
    def ai_sandbox():
        admin_only();task=task_arg(request.form.get('task',''))
        draft_id=request.form.get('draft','')
        draft_row=ap.get(db(),int(draft_id)) if draft_id.isdigit() else None
        if draft_row and (draft_row['task']!=task or draft_row['status']!='draft'):draft_row=None
        api_key=os.environ.get('GROQ_API_KEY','').strip()
        if not api_key:
            flash('GROQ_API_KEY не задан на сервере: песочница недоступна.');return render(task,status=400)
        opener=current_app.config.get('GROQ_OPENER',urllib.request.urlopen)
        prompts={'active':runtime_active(db(),task)}
        if draft_row:prompts['draft']=ap.as_runtime(draft_row)
        try:
            prepared=_prepare_scoring(prompts) if task=='scoring' else _prepare_offers(prompts)
        except ValueError as exc:
            flash(str(exc));return render(task,status=400)
        try:hit(db(),'ai_sandbox',g.user['id'],*SANDBOX_LIMIT)
        except RateLimited as exc:
            flash(str(exc));return render(task,status=429)
        sandbox=prepared['run'](api_key,opener)
        if draft_row:
            draft_run=sandbox['runs']['draft']
            if draft_run['valid']:
                ap.mark_tested(db(),draft_row['id'],ap.row_hash(draft_row))
                flash(f"Черновик v{draft_row['version']} прошёл проверку: ответ соответствует схеме. Его можно активировать.")
            else:flash(f"Черновик v{draft_row['version']} не прошёл проверку: {draft_run['error'] or 'ответ не соответствует схеме'}.")
        return render(task,sandbox=sandbox)

    def _prepare_scoring(prompts):
        from .ai_scoring import feedback_examples,fewshot_count,request_timeout
        from .core import Lead
        text=request.form.get('text','').strip()
        if text:
            if len(text)>SANDBOX_MAX_TEXT:raise ValueError(f'Текст длиннее {SANDBOX_MAX_TEXT} символов.')
            items=[(1,Lead(source='Песочница',url='',title='',text=text,published=None))]
            meta={1:{'title':text[:160],'source':'Вставленный текст','feedback':[]}}
        else:
            raw_ids=request.form.get('lead_ids','').replace(',',' ').split()
            if any(not value.isdigit() for value in raw_ids):raise ValueError('ID лидов — числа через запятую.')
            last=request.form.get('last','5')
            rows=sandbox_leads(db(),[int(value) for value in raw_ids],request.form.get('source','')[:300],
                               request.form.get('with_feedback')=='1',int(last) if last.isdigit() else 5)
            items=[];meta={}
            labels=feedback_labels(db(),[row['id'] for row in rows])
            for row in rows:
                lead=lead_from_payload(row['payload'])
                if not lead:continue
                items.append((row['id'],lead))
                meta[row['id']]={'title':(lead.title or lead.text or '')[:160],'source':lead.source,
                                 'feedback':labels.get(row['id'],[])}
            if not items:raise ValueError('Вставьте текст объявления или выберите лиды: по фильтрам ничего не найдено.')
        ids=[lead_id for lead_id,_ in items]
        examples={name:feedback_examples(db(),fewshot_count(prompt),exclude=ids) for name,prompt in prompts.items()}
        timeout=min(25,request_timeout())

        def run(api_key,opener):
            runs=side_by_side({name:(lambda name=name,prompt=prompt:run_scoring(items,prompt,examples[name],api_key,opener,timeout))
                               for name,prompt in prompts.items()})
            return {'task':'scoring','runs':runs,'items':[(lead_id,meta[lead_id]) for lead_id in ids]}
        return {'run':run}

    def _prepare_offers(prompts):
        from .core import ensure_enriched
        lead_id=request.form.get('lead_id','').strip()
        if not lead_id.isdigit():raise ValueError('Укажите ID лида.')
        row=db().execute('SELECT id,payload FROM leads WHERE id=?',(int(lead_id),)).fetchone()
        lead=lead_from_payload(row['payload']) if row else None
        if not lead:raise ValueError('Лид не найден.')
        lead=ensure_enriched(lead)
        project_id=request.form.get('project_id','').strip();settings=None;services=portfolio=''
        if project_id:
            if not project_id.isdigit():raise ValueError('ID проекта — число.')
            project=db().execute('SELECT * FROM projects WHERE id=?',(int(project_id),)).fetchone()
            if not project:raise ValueError('Проект не найден.')
            settings=dict(project)
            profile=db().execute('SELECT profile_services,portfolio FROM user_preferences WHERE user_id=?',
                                 (project['user_id'],)).fetchone()
            if profile:services,portfolio=profile['profile_services'] or '',profile['portfolio'] or ''

        def run(api_key,opener):
            runs=side_by_side({name:(lambda prompt=prompt:run_offers(lead,services,portfolio,settings,prompt,api_key,opener))
                               for name,prompt in prompts.items()})
            return {'task':'offers','runs':runs,'lead':{'id':row['id'],'title':(lead.title or lead.text or '')[:200],
                    'source':lead.source},'project':settings['name'] if settings and 'name' in settings else project_id}
        return {'run':run}

    @app.get('/admin/ai/journal')
    def ai_journal():
        admin_only()
        from .ai_scoring import CATEGORIES,CATEGORY_LABELS
        from .core import TOPICS
        from .product import FEEDBACK
        filters=journal_filters(request.args)
        return render_template('ai_journal.html',tasks=ap.TASKS,rows=journal(db(),filters),filters=filters,
            summary=summary(db()),topics=list(TOPICS),categories=CATEGORIES,category_labels=CATEGORY_LABELS,
            score_statuses=SCORE_STATUSES,rule_statuses=RULE_STATUSES,sources=journal_sources(db()),feedback_labels=FEEDBACK,
            rescore_max=RESCORE_MAX,paused=ap.pause_info(db()))

    @app.post('/admin/ai/rescore')
    def ai_rescore():
        admin_only()
        ids=request.form.getlist('lead_id')
        if request.form.get('mode')=='selected':
            if not ids:flash('Отметьте лиды для повторной оценки.');return redirect('/admin/ai/journal')
            if any(not value.isdigit() for value in ids):abort(400)
            count=rescore(db(),ids)
        else:
            last=request.form.get('last','')
            if not last.isdigit() or not 1<=int(last)<=RESCORE_MAX:
                flash(f'Укажите количество от 1 до {RESCORE_MAX}.');return redirect('/admin/ai/journal')
            count=rescore(db(),last=int(last))
        flash(f'В очередь на повторную оценку: {count}. Оценка идёт в фоне по активному промпту.')
        return redirect('/admin/ai/journal')
