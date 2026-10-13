"""Apply versioned web additions after the existing bot migrations.

Run order
---------
1. A session-level advisory lock (key 734297159) serialises deploys for the
   whole run, including the no-transaction phase.
2. All regular migrations run in ONE transaction (atomic: all or nothing) with
   a short ``lock_timeout`` so a deploy never queues an ACCESS EXCLUSIVE lock
   behind a long reader and stalls production traffic.  If the batch fails with
   ``LockNotAvailable`` (SQLSTATE 55P03) it is rolled back completely and the
   whole batch is retried with exponential backoff and jitter.  Any other error
   is raised at once.  On every lock failure the sessions holding locks on
   ``leadgen`` tables are logged (best effort; this never hides the original
   error).
3. Files whose first line is exactly ``-- leadgen:no-transaction`` are skipped
   by the batch and run after it commits, on an autocommit connection, one
   statement at a time (``CREATE INDEX CONCURRENTLY`` cannot run inside a
   transaction block, and psycopg wraps a multi-statement string in one).
   Each statement gets its own lock retry.  The file is recorded in
   ``web_schema_versions`` only after all of its statements succeed, so a
   failed file is re-run in full on the next deploy: its statements must be
   idempotent (``CREATE INDEX CONCURRENTLY IF NOT EXISTS ...``).
   These files run after every transactional migration of the same deploy, so a
   later transactional migration must not depend on them.

No-transaction file constraints
-------------------------------
Statements are split on a ``;`` that ends a line.  Do not put such a ``;``
inside a string, dollar-quoted body or comment; chunks that contain only
comments or whitespace are skipped.

Failed concurrent index builds
------------------------------
A failed or cancelled ``CREATE INDEX CONCURRENTLY`` leaves an INVALID index,
which ``IF NOT EXISTS`` would then silently keep.  Before each such statement
the index named in it is looked up in ``pg_index``; if it exists and
``indisvalid`` is false it is dropped with ``DROP INDEX CONCURRENTLY IF
EXISTS`` and built again.  After the build the index must exist and be valid,
otherwise the run fails loudly.  The advisory lock keeps two deploys from
racing on this; do not build the same index by hand while a deploy runs.

Timeouts (env vars, defaults in brackets; values are Postgres durations)
------------------------------------------------------------------------
LEADGEN_MIGRATE_LOCK_TIMEOUT [15s]       lock_timeout of the transactional batch
LEADGEN_MIGRATE_STATEMENT_TIMEOUT [120s] statement_timeout of that batch
LEADGEN_MIGRATE_CONCURRENT_LOCK_TIMEOUT [60s]
    lock_timeout for no-transaction statements.  Their locks (SHARE UPDATE
    EXCLUSIVE, waits for older transactions) do not block normal reads and
    writes, so they may wait longer than the batch.
LEADGEN_MIGRATE_CONCURRENT_STATEMENT_TIMEOUT [0]
    statement_timeout for no-transaction statements; 0 disables it so that
    building an index on the large leads table is not killed by the
    platform's 120s default.  lock_timeout still bounds lock waits.
LEADGEN_MIGRATE_LOCK_ATTEMPTS [5]        attempts per batch / per statement
LEADGEN_MIGRATE_RETRY_BASE_SECONDS [2]   first backoff; doubles every retry
LEADGEN_MIGRATE_RETRY_MAX_SECONDS [30]   backoff cap (jitter: 50-100%)
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import random
import re
import time
from .app import env_load
from .database import db_open


MIGRATIONS=('20260930143416_web_projects_crm.sql','20260930193638_lead_reminders.sql',
            '20261001041250_personal_telegram_connections.sql',
            '20261001061514_team_workspaces_and_assignments.sql',
            '20261001062736_lead_feedback_and_tags.sql',
            '20261001063156_api_and_webhook_integrations.sql',
            '20261001064130_assignment_notifications.sql',
            '20261001064937_project_reply_profiles.sql',
            '20261001094118_ai_offer_generations.sql',
            '20261003120000_subscription_trials.sql',
            '20261005070330_telegram_replies.sql',
            '20261005153000_telegram_qr_login.sql',
            '20261005180000_expand_service_topics.sql',
            '20261007120000_onboarding_foundation.sql',
            '20261010120000_mini_crm_pipeline.sql',
            '20261010130000_ai_lead_scores.sql',
            '20261010140000_free_tier_and_founder_price.sql',
            '20261011120000_user_feedback.sql',
            '20261011150000_disable_unjoined_sources.sql',
            '20261012120000_plan_requests.sql',
            '20261012130000_ai_prompt_admin.sql',
            '20261013120000_yookassa_payments.sql',
            '20261013140000_user_delivery_cursor.sql',
            '20261013150000_leads_query_indexes.sql',
            '20261014130000_user_leads_fk_indexes.sql')
MIGRATIONS_DIR=Path(__file__).resolve().parent.parent/'supabase'/'migrations'
NO_TRANSACTION_MARKER='-- leadgen:no-transaction'
ADVISORY_LOCK_KEY=734297159
LOCK_NOT_AVAILABLE='55P03'
_DURATION=re.compile(r'\d+(?:\.\d+)?\s*(?:us|ms|s|min|h|d)?')
_IDENT=r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_CONCURRENT_INDEX=re.compile(r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                             r'('+_IDENT+r'(?:\s*\.\s*'+_IDENT+r')?)\s+ON\b',re.I)
_LOCK_HOLDERS_SQL=r'''SELECT a.pid,a.state,a.application_name,
    date_trunc('second',now()-a.xact_start)::text AS xact_age,
    left(regexp_replace(coalesce(a.query,''),'\s+',' ','g'),200) AS query
FROM pg_stat_activity a
WHERE a.pid<>pg_backend_pid() AND a.pid IN (
    SELECT l.pid FROM pg_locks l
    JOIN pg_class c ON c.oid=l.relation JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE l.granted AND n.nspname='leadgen'
      AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database()))
ORDER BY a.xact_start NULLS LAST LIMIT 10'''


@dataclass(frozen=True)
class Settings:
    attempts:int=5
    retry_base:float=2.0
    retry_max:float=30.0
    lock_timeout:str='15s'
    statement_timeout:str='120s'
    concurrent_lock_timeout:str='60s'
    concurrent_statement_timeout:str='0'

    @classmethod
    def from_env(cls,env=None):
        env=os.environ if env is None else env
        def value(name,default,cast):
            raw=env.get('LEADGEN_MIGRATE_'+name,'').strip()
            if not raw:return default
            try:result=cast(raw)
            except ValueError:raise ValueError('Некорректное значение LEADGEN_MIGRATE_'+name+': '+raw) from None
            if isinstance(result,(int,float)) and result<0:raise ValueError('LEADGEN_MIGRATE_'+name+' не может быть отрицательным')
            return result
        def duration(raw):
            if not _DURATION.fullmatch(raw):raise ValueError(raw)
            return raw
        settings=cls(value('LOCK_ATTEMPTS',cls.attempts,int),value('RETRY_BASE_SECONDS',cls.retry_base,float),
                     value('RETRY_MAX_SECONDS',cls.retry_max,float),value('LOCK_TIMEOUT',cls.lock_timeout,duration),
                     value('STATEMENT_TIMEOUT',cls.statement_timeout,duration),
                     value('CONCURRENT_LOCK_TIMEOUT',cls.concurrent_lock_timeout,duration),
                     value('CONCURRENT_STATEMENT_TIMEOUT',cls.concurrent_statement_timeout,duration))
        if settings.attempts<1:raise ValueError('LEADGEN_MIGRATE_LOCK_ATTEMPTS должен быть не меньше 1')
        return settings


@dataclass(frozen=True)
class Migration:
    name:str
    sql:str
    checksum:str
    no_transaction:bool


def is_no_transaction(sql):
    return sql.split('\n',1)[0].rstrip('\r')==NO_TRANSACTION_MARKER


def split_statements(sql):
    """Split on ';' at end of line; drop chunks that are only comments/blank."""
    statements=[]
    for chunk in re.split(r';[ \t]*(?:\r?\n|$)',sql):
        meaningful=[line for line in chunk.splitlines() if line.strip() and not line.strip().startswith('--')]
        if meaningful:statements.append(chunk.strip())
    return statements


def concurrent_index_name(statement):
    code='\n'.join(line for line in statement.splitlines() if not line.strip().startswith('--'))
    match=_CONCURRENT_INDEX.match(code.strip())
    return re.sub(r'\s*\.\s*','.',match.group(1)) if match else None


def load_migrations(root=MIGRATIONS_DIR,names=MIGRATIONS):
    migrations=[]
    for name in names:
        sql=(Path(root)/name).read_text()
        migrations.append(Migration(name,sql,hashlib.sha256(sql.encode()).hexdigest(),is_no_transaction(sql)))
    return migrations


def is_lock_error(exc):
    return getattr(exc,'sqlstate',None)==LOCK_NOT_AVAILABLE


def with_lock_retry(action,label,settings,on_lock_error=None,sleep=time.sleep,rand=random.random):
    """Run action(); on LockNotAvailable retry up to settings.attempts in total."""
    for attempt in range(1,settings.attempts+1):
        try:return action()
        except Exception as exc:
            if not is_lock_error(exc):raise
            reason=(str(exc).strip().splitlines() or [type(exc).__name__])[0]
            if on_lock_error:
                try:on_lock_error()
                except Exception:pass
            if attempt>=settings.attempts:
                print(f'Блокировка не получена ({label}): {reason}. Попытки исчерпаны ({attempt}/{settings.attempts}).',flush=True)
                raise
            delay=min(settings.retry_max,settings.retry_base*2**(attempt-1))*(0.5+0.5*rand())
            print(f'Блокировка не получена ({label}): {reason}. Попытка {attempt}/{settings.attempts}, '
                  f'повтор через {delay:.1f} с.',flush=True)
            sleep(delay)


def log_lock_holders(connection):
    """Best effort: print sessions holding locks on leadgen tables."""
    try:
        rows=connection.execute(_LOCK_HOLDERS_SQL).fetchall()
        if not rows:print('Сессии с блокировками на таблицах leadgen не найдены.',flush=True)
        else:print('Сессии с блокировками на таблицах leadgen (старейшие транзакции первыми):',flush=True)
        for row in rows:
            print(f"  pid={row['pid']} state={row['state']} app={row['application_name']} "
                  f"xact_age={row['xact_age']} query={row['query']}",flush=True)
    except Exception as exc:
        print('Не удалось получить список блокирующих сессий: '+type(exc).__name__,flush=True)
    finally:
        try:
            if not connection.autocommit:connection.rollback()
        except Exception:pass


def apply_transactional(db,migrations,settings):
    """Apply regular migrations in one transaction; return pending no-transaction ones."""
    pending,applied=[],[]
    with db:
        db.execute("SELECT set_config('lock_timeout',?,true)",(settings.lock_timeout,))
        db.execute("SELECT set_config('statement_timeout',?,true)",(settings.statement_timeout,))
        db.execute('''CREATE TABLE IF NOT EXISTS leadgen.web_schema_versions
            (name text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())''')
        db.execute('ALTER TABLE leadgen.web_schema_versions ENABLE ROW LEVEL SECURITY')
        db.execute('REVOKE ALL ON leadgen.web_schema_versions FROM public,anon,authenticated')
        for migration in migrations:
            old=db.execute('SELECT checksum FROM leadgen.web_schema_versions WHERE name=?',(migration.name,)).fetchone()
            if old:
                if old['checksum']!=migration.checksum:raise RuntimeError('Изменена уже применённая миграция: '+migration.name)
                continue
            if migration.no_transaction:
                pending.append(migration);continue
            db.connection.execute(migration.sql,prepare=False)
            db.execute('INSERT INTO leadgen.web_schema_versions(name,checksum) VALUES(?,?)',(migration.name,migration.checksum))
            applied.append(migration.name)
    for name in applied:print('Применена миграция: '+name,flush=True)
    return pending


def _index_valid(connection,name):
    row=connection.execute('SELECT i.indisvalid AS valid FROM pg_index i WHERE i.indexrelid=to_regclass(%s)',(name,)).fetchone()
    return None if row is None else bool(row['valid'])


def run_concurrent_statement(connection,statement):
    name=concurrent_index_name(statement)
    if name and _index_valid(connection,name) is False:
        print(f'Индекс {name} невалиден (прерванная сборка CONCURRENTLY): удаление и пересоздание.',flush=True)
        connection.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}',prepare=False)
    connection.execute(statement,prepare=False)
    if name and _index_valid(connection,name) is not True:
        raise RuntimeError(f'Индекс {name} отсутствует или невалиден после CREATE INDEX CONCURRENTLY')


def apply_no_transaction(db,pending,settings,sleep=time.sleep):
    if not pending:return
    connection=db.connection
    connection.autocommit=True
    connection.execute("SELECT set_config('lock_timeout',%s,false)",(settings.concurrent_lock_timeout,))
    connection.execute("SELECT set_config('statement_timeout',%s,false)",(settings.concurrent_statement_timeout,))
    for migration in pending:
        statements=split_statements(migration.sql)
        for number,statement in enumerate(statements,1):
            with_lock_retry(lambda statement=statement:run_concurrent_statement(connection,statement),
                            f'{migration.name}, выражение {number}/{len(statements)}',settings,
                            on_lock_error=lambda:log_lock_holders(connection),sleep=sleep)
        connection.execute('INSERT INTO leadgen.web_schema_versions(name,checksum) VALUES(%s,%s) ON CONFLICT (name) DO NOTHING',
                           (migration.name,migration.checksum))
        print('Применена миграция (без транзакции): '+migration.name,flush=True)


def _release(connection):
    for action in (lambda:connection.rollback(),lambda:setattr(connection,'autocommit',True),
                   lambda:connection.execute('RESET lock_timeout'),lambda:connection.execute('RESET statement_timeout'),
                   lambda:connection.execute('SELECT pg_advisory_unlock(%s)',(ADVISORY_LOCK_KEY,))):
        try:action()
        except Exception:pass


def run(db,migrations,settings,sleep=time.sleep):
    connection=db.connection
    connection.autocommit=True
    lock=connection.execute('SELECT pg_try_advisory_lock(%s) AS acquired',(ADVISORY_LOCK_KEY,)).fetchone()
    if not lock['acquired']:
        raise RuntimeError('Другой процесс уже применяет миграции. Остановите старый run-контейнер и повторите команду.')
    try:
        connection.autocommit=False
        pending=with_lock_retry(lambda:apply_transactional(db,migrations,settings),'транзакционный пакет миграций',
                                settings,on_lock_error=lambda:log_lock_holders(connection),sleep=sleep)
        apply_no_transaction(db,pending,settings,sleep)
    finally:_release(connection)


def migrate():
    env_load()
    settings=Settings.from_env()
    migrations=load_migrations()
    print('Подключение к Supabase Postgres…',flush=True)
    db=db_open()
    try:
        print('Подключение установлено. Проверка блокировки миграций…',flush=True)
        run(db,migrations,settings)
        print('Схема веб-кабинета готова.',flush=True)
    finally:db.close()


if __name__=='__main__':migrate()
