"""Admin-managed AI prompt versions (platform admins only; users never see prompts).

An admin edits only the instructions text, picks a model from MODELS and sets the temperature and a few
numeric params. The output contract — JSON schema, categories, the allowed service names, field limits and
the untrusted-data rule — lives in code (ai_scoring.CONTRACT, ai_offers.CONTRACT) and is appended to every
request and enforced when parsing, so an edited prompt cannot break parsing. Without an active version the
runtime uses the prompt constants in code: the «factory» version."""
import hashlib
import json
import time

TASKS={'scoring':'Оценка объявлений','offers':'AI-отклики'}
# Groq models an admin may choose. schema: strict json_schema output is supported (otherwise JSON mode
# plus the contract text); reasoning: the model accepts reasoning_effort.
MODELS={
    'openai/gpt-oss-20b':{'label':'GPT-OSS 20B','schema':True,'reasoning':True},
    'openai/gpt-oss-120b':{'label':'GPT-OSS 120B','schema':True,'reasoning':True},
    'llama-3.3-70b-versatile':{'label':'Llama 3.3 70B Versatile','schema':False,'reasoning':False},
    'llama-3.1-8b-instant':{'label':'Llama 3.1 8B Instant','schema':False,'reasoning':False},
}
# Models set through the environment keep the behaviour they had before the allowlist existed.
DEFAULT_CAPS={'schema':True,'reasoning':True}
MAX_INSTRUCTIONS=8000
MAX_NOTE=500
TEMPERATURE_MAX=1.0
# Per task: key -> (label, minimum, maximum). Empty means «as in the server settings».
PARAMS={
    'scoring':{'batch':('Лидов в одном запросе',1,10),
               'daily_limit':('Оценок в сутки (не больше лимита сервера)',0,100000),
               'fewshot':('Примеров из разметки пользователей',0,5)},
    'offers':{'max_tokens':('Максимум токенов ответа',600,4000)},
}
CACHE_TTL=60
PAUSE_KEY='ai:scoring_paused'
STATUSES={'draft':'Черновик','active':'Активна','archived':'В архиве'}

_cache={}


def clear_cache():
    _cache.clear()


def model_caps(model):
    return MODELS.get(model,DEFAULT_CAPS)


def _cached(key,loader):
    now=time.monotonic();hit=_cache.get(key)
    if hit and hit[0]>now:return hit[1]
    value=loader();_cache[key]=(now+CACHE_TTL,value)
    return value


def _rollback(db):
    try:db.connection.rollback()
    except Exception:pass


def factory(task):
    """The code version: what runs when no version is active."""
    if task=='scoring':
        from .ai_scoring import FACTORY_INSTRUCTIONS,FACTORY_TEMPERATURE,scoring_model
        instructions,model,temperature=FACTORY_INSTRUCTIONS,scoring_model(),FACTORY_TEMPERATURE
    elif task=='offers':
        from .ai_offers import FACTORY_INSTRUCTIONS,FACTORY_TEMPERATURE,configured_model
        instructions,model,temperature=FACTORY_INSTRUCTIONS,configured_model(),FACTORY_TEMPERATURE
    else:raise ValueError('unknown task')
    return {'id':None,'version':0,'task':task,'instructions':instructions,'model':model,
            'temperature':temperature,'params':{},'source':'factory'}


def clean_params(task,params):
    """Known numeric params within bounds; anything else is dropped."""
    if isinstance(params,str):
        try:params=json.loads(params)
        except ValueError:params={}
    result={}
    for key,(_,low,high) in PARAMS.get(task,{}).items():
        value=params.get(key) if isinstance(params,dict) else None
        if isinstance(value,bool) or not isinstance(value,(int,float)):continue
        result[key]=max(low,min(high,int(value)))
    return result


def as_runtime(row):
    return {'id':row['id'],'version':row['version'],'task':row['task'],'instructions':row['instructions'],
            'model':row['model'],'temperature':float(row['temperature']),
            'params':clean_params(row['task'],row['params']),'source':'db'}


def active_prompt(db,task):
    """Active version for the task, else the factory one. Cached ~60 s per process; never raises.
    Call it outside a transaction: a failed lookup (table not migrated yet) rolls the connection back."""
    def load():
        if db is None or getattr(db,'backend','')!='postgres':return None
        try:
            row=db.execute("SELECT * FROM ai_prompts WHERE task=? AND status='active'",(task,)).fetchone()
        except Exception:
            _rollback(db);return None
        return dict(row) if row else None
    row=_cached(('prompt',task),load)
    if row and row['model'] in MODELS:return as_runtime(row)
    return factory(task)


def paused(db):
    def load():
        if db is None or getattr(db,'backend','')!='postgres':return False
        try:return bool(db.execute('SELECT 1 FROM settings WHERE key=?',(PAUSE_KEY,)).fetchone())
        except Exception:
            _rollback(db);return False
    return _cached(('paused',),load)


def pause_info(db):
    row=db.execute('SELECT value FROM settings WHERE key=?',(PAUSE_KEY,)).fetchone()
    if not row:return None
    try:return json.loads(row['value'])
    except ValueError:return {}


def set_paused(db,on,admin_id=None):
    from datetime import datetime,timezone
    with db:
        if on:
            db.execute('''INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                (PAUSE_KEY,json.dumps({'by':admin_id,'at':datetime.now(timezone.utc).isoformat()})))
        else:db.execute('DELETE FROM settings WHERE key=?',(PAUSE_KEY,))
    clear_cache()


def contract_hash(task):
    if task=='scoring':
        from .ai_scoring import CONTRACT,response_schema
    else:
        from .ai_offers import CONTRACT,response_schema
    return hashlib.sha256((CONTRACT+json.dumps(response_schema(),sort_keys=True,ensure_ascii=False)).encode()).hexdigest()


def content_hash(task,instructions,model,temperature,params):
    """What a sandbox test vouches for; the code contract is part of it, so a contract change needs a new test."""
    payload={'task':task,'instructions':instructions,'model':model,'temperature':f'{float(temperature):.2f}',
             'params':clean_params(task,params),'contract':contract_hash(task)}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def row_hash(row):
    return content_hash(row['task'],row['instructions'],row['model'],row['temperature'],row['params'])


def test_current(row):
    """The draft passed the sandbox and has not changed since."""
    return bool(row['test_passed_at'] and row['content_hash'] and row['content_hash']==row_hash(row))


def validate(task,form):
    """Form fields -> clean values; ValueError with a message for the admin."""
    if task not in TASKS:raise ValueError('Неизвестная задача.')
    instructions=(form.get('instructions') or '').replace('\r\n','\n').strip()
    if not instructions:raise ValueError('Напишите инструкции.')
    if len(instructions)>MAX_INSTRUCTIONS:raise ValueError(f'Инструкции длиннее {MAX_INSTRUCTIONS} символов.')
    model=(form.get('model') or '').strip()
    if model not in MODELS:raise ValueError('Выберите модель из списка.')
    raw=(form.get('temperature') or '0').strip().replace(',','.')
    try:temperature=round(float(raw),2)
    except ValueError:raise ValueError('Температура — число от 0 до 1.') from None
    if not 0<=temperature<=TEMPERATURE_MAX:raise ValueError('Температура — число от 0 до 1.')
    params={}
    for key,(label,low,high) in PARAMS[task].items():
        value=(form.get('param_'+key) or '').strip()
        if not value:continue
        if not value.isdigit() or not low<=int(value)<=high:raise ValueError(f'«{label}»: целое от {low} до {high}.')
        params[key]=int(value)
    note=(form.get('note') or '').strip()
    if len(note)>MAX_NOTE:raise ValueError(f'Комментарий длиннее {MAX_NOTE} символов.')
    return {'instructions':instructions,'model':model,'temperature':temperature,'params':params,'note':note}


def _lock(db,task):
    db.execute('SELECT pg_advisory_xact_lock(734297160,hashtext(?))',(task,))


def create_draft(db,task,data,admin_id):
    with db:
        _lock(db,task)
        version=db.execute('SELECT coalesce(max(version),0)+1 AS v FROM ai_prompts WHERE task=?',(task,)).fetchone()['v']
        row=db.execute('''INSERT INTO ai_prompts(task,version,instructions,model,temperature,params,status,note,created_by)
            VALUES(?,?,?,?,?,?::jsonb,'draft',?,?) RETURNING id''',
            (task,version,data['instructions'],data['model'],data['temperature'],json.dumps(data['params']),
             data['note'],admin_id)).fetchone()
    return row['id']


def update_draft(db,prompt_id,data):
    """Only drafts are editable; any change voids the sandbox test."""
    with db:
        row=db.execute('''UPDATE ai_prompts SET instructions=?,model=?,temperature=?,params=?::jsonb,note=?,
                test_passed_at=NULL,content_hash=NULL,updated_at=now()
            WHERE id=? AND status='draft' RETURNING id''',
            (data['instructions'],data['model'],data['temperature'],json.dumps(data['params']),data['note'],prompt_id)).fetchone()
    return bool(row)


def mark_tested(db,prompt_id,tested_hash):
    """Record a passed sandbox run for exactly the content that was run."""
    with db:
        row=db.execute('''UPDATE ai_prompts SET test_passed_at=now(),content_hash=?
            WHERE id=? AND status='draft' RETURNING id''',(tested_hash,prompt_id)).fetchone()
    return bool(row)


def discard_draft(db,prompt_id):
    with db:
        row=db.execute("UPDATE ai_prompts SET status='archived',updated_at=now() WHERE id=? AND status='draft' RETURNING id",
                       (prompt_id,)).fetchone()
    return bool(row)


def activate(db,prompt_id):
    """Make a version live. A version that was never live must have passed the sandbox after its last
    change; a previously active version (rollback) is activated as is. Returns the activated row."""
    with db:
        row=db.execute('SELECT * FROM ai_prompts WHERE id=?',(prompt_id,)).fetchone()
        if not row:raise LookupError(prompt_id)
        _lock(db,row['task'])
        row=db.execute('SELECT * FROM ai_prompts WHERE id=? FOR UPDATE',(prompt_id,)).fetchone()
        if row['status']=='active':raise ValueError('Эта версия уже активна.')
        if row['model'] not in MODELS:raise ValueError('Модель этой версии больше не поддерживается.')
        if row['activated_at'] is None:
            if not row['test_passed_at'] or not row['content_hash']:
                raise ValueError('Сначала проверьте черновик в песочнице.')
            if row['content_hash']!=row_hash(row):
                raise ValueError('Черновик изменился после проверки — проверьте его в песочнице ещё раз.')
        db.execute("UPDATE ai_prompts SET status='archived',updated_at=now() WHERE task=? AND status='active'",(row['task'],))
        row=db.execute("""UPDATE ai_prompts SET status='active',activated_at=now(),updated_at=now()
            WHERE id=? RETURNING *""",(prompt_id,)).fetchone()
    clear_cache()
    return row


def reset_to_factory(db,task):
    """Archive the active version: the code prompt applies again."""
    with db:
        _lock(db,task)
        row=db.execute("UPDATE ai_prompts SET status='archived',updated_at=now() WHERE task=? AND status='active' RETURNING id",
                       (task,)).fetchone()
    clear_cache()
    return bool(row)


def get(db,prompt_id):
    return db.execute('SELECT * FROM ai_prompts WHERE id=?',(prompt_id,)).fetchone()


def versions(db,task,limit=50):
    return db.execute('''SELECT p.*,u.display_name AS author,u.username AS author_username FROM ai_prompts p
        LEFT JOIN app_users u ON u.id=p.created_by WHERE p.task=? ORDER BY p.version DESC LIMIT ?''',(task,limit)).fetchall()


def current_active(db,task):
    return db.execute("SELECT * FROM ai_prompts WHERE task=? AND status='active'",(task,)).fetchone()
