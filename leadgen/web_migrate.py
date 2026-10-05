"""Apply versioned web additions atomically after the existing bot migrations."""
import hashlib
from pathlib import Path
from .app import env_load
from .database import db_open


def migrate():
    env_load()
    print('Подключение к Supabase Postgres…',flush=True)
    db=db_open()
    try:
        print('Подключение установлено. Проверка блокировки миграций…',flush=True)
        with db:
            lock=db.execute('SELECT pg_try_advisory_xact_lock(734297159) AS acquired').fetchone()
            if not lock['acquired']:
                raise RuntimeError('Другой процесс уже применяет миграции. Остановите старый run-контейнер и повторите команду.')
            db.execute("SET LOCAL lock_timeout = '15s'")
            db.execute("SET LOCAL statement_timeout = '120s'")
            db.execute('''CREATE TABLE IF NOT EXISTS leadgen.web_schema_versions
                (name text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())''')
            db.execute('ALTER TABLE leadgen.web_schema_versions ENABLE ROW LEVEL SECURITY')
            db.execute('REVOKE ALL ON leadgen.web_schema_versions FROM public,anon,authenticated')
            root=Path(__file__).resolve().parent.parent/'supabase'/'migrations'
            for name in ('20260930143416_web_projects_crm.sql','20260930193638_lead_reminders.sql',
                         '20261001041250_personal_telegram_connections.sql',
                         '20261001061514_team_workspaces_and_assignments.sql',
                         '20261001062736_lead_feedback_and_tags.sql',
                         '20261001063156_api_and_webhook_integrations.sql',
                         '20261001064130_assignment_notifications.sql',
                         '20261001064937_project_reply_profiles.sql',
                         '20261001094118_ai_offer_generations.sql',
                         '20261003120000_subscription_trials.sql',
                         '20261005070330_telegram_replies.sql',
                         '20261005153000_telegram_qr_login.sql'):
                sql=(root/name).read_text();checksum=hashlib.sha256(sql.encode()).hexdigest()
                old=db.execute('SELECT checksum FROM leadgen.web_schema_versions WHERE name=?',(name,)).fetchone()
                if old:
                    if old['checksum']!=checksum:raise RuntimeError('Изменена уже применённая миграция: '+name)
                    continue
                db.connection.execute(sql,prepare=False)
                db.execute('INSERT INTO leadgen.web_schema_versions(name,checksum) VALUES(?,?)',(name,checksum))
                print('Применена миграция: '+name,flush=True)
        print('Схема веб-кабинета готова.',flush=True)
    finally:db.close()


if __name__=='__main__':migrate()
