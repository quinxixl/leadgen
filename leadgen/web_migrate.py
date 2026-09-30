"""Apply versioned web additions atomically after the existing bot migrations."""
import hashlib
from pathlib import Path
from .app import env_load
from .database import db_open


def migrate():
    env_load()
    db=db_open()
    try:
        with db:
            db.execute('SELECT pg_advisory_xact_lock(734297159)')
            db.execute('''CREATE TABLE IF NOT EXISTS leadgen.web_schema_versions
                (name text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())''')
            db.execute('ALTER TABLE leadgen.web_schema_versions ENABLE ROW LEVEL SECURITY')
            db.execute('REVOKE ALL ON leadgen.web_schema_versions FROM public,anon,authenticated')
            root=Path(__file__).resolve().parent.parent/'supabase'/'migrations'
            for name in ('20260930143416_web_projects_crm.sql',):
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
