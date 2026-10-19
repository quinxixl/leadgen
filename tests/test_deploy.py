import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


DB_UP='up -d --no-recreate --wait --wait-timeout 120 db'


class Updater(unittest.TestCase):
    def test_migration_failure_does_not_mark_deployed_and_retry_works(self):
        script=Path('deploy/leadfinder-update.sh').resolve()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);bin_dir=root/'bin';bin_dir.mkdir();(root/'leadgen').mkdir()
            (root/'leadgen/web_migrate.py').touch()
            (bin_dir/'git').write_text('#!/bin/sh\nif [ "$1" = rev-parse ]; then echo abc123; fi\n')
            (bin_dir/'docker').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$LEADFINDER_DIR/calls"\ncase "$*" in *web_migrate*) exit "${FAIL_MIGRATION:-0}";; esac\n')
            for file in bin_dir.iterdir():file.chmod(0o755)
            env=os.environ|{'LEADFINDER_DIR':name,'LEADFINDER_WEB':'1','PATH':str(bin_dir)+':'+os.environ['PATH'],'FAIL_MIGRATION':'1'}
            self.assertNotEqual(subprocess.run(['sh',str(script)],env=env,capture_output=True).returncode,0)
            self.assertFalse((root/'.leadfinder-deployed-sha').exists())
            failed=(root/'calls').read_text()
            self.assertIn(DB_UP,failed)                     # the database may start...
            self.assertNotIn('--remove-orphans',failed)     # ...but the app is not restarted
            env['FAIL_MIGRATION']='0'
            self.assertEqual(subprocess.run(['sh',str(script)],env=env,capture_output=True).returncode,0)
            self.assertEqual((root/'.leadfinder-deployed-sha').read_text().strip(),'abc123')
            calls=(root/'calls').read_text()
            self.assertIn('-f compose.yaml -f compose.web.yaml',calls)
            self.assertIn('--wait',calls)
            self.assertEqual(subprocess.run(['sh',str(script)],env=env,capture_output=True).returncode,0)
            self.assertEqual(calls,(root/'calls').read_text())

    def run_updater(self,web):
        script=Path('deploy/leadfinder-update.sh').resolve()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);bin_dir=root/'bin';bin_dir.mkdir();(root/'leadgen').mkdir()
            (root/'leadgen/web_migrate.py').touch()
            (bin_dir/'git').write_text('#!/bin/sh\nif [ "$1" = rev-parse ]; then echo abc123; fi\n')
            (bin_dir/'docker').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$LEADFINDER_DIR/calls"\n')
            for file in bin_dir.iterdir():file.chmod(0o755)
            env=os.environ|{'LEADFINDER_DIR':name,'LEADFINDER_WEB':web,'PATH':str(bin_dir)+':'+os.environ['PATH']}
            self.assertEqual(subprocess.run(['sh',str(script)],env=env,capture_output=True).returncode,0)
            return (root/'calls').read_text().splitlines()

    def test_database_is_healthy_before_migrations_and_never_recreated(self):
        for web,files,apps in (('0','-f compose.yaml ','leadfinder'),('1','-f compose.yaml -f compose.web.yaml ','leadfinder web')):
            calls=self.run_updater(web)
            db=calls.index('compose '+files+DB_UP)
            migrate=next(i for i,call in enumerate(calls) if 'web_migrate' in call)
            app=next(i for i,call in enumerate(calls) if '--remove-orphans' in call)
            self.assertLess(db,migrate,web);self.assertLess(migrate,app,web)
            self.assertIn('run --rm --no-deps leadfinder',calls[migrate])
            # Only the app containers are (re)created: --no-deps and an explicit service list without db.
            self.assertTrue(calls[app].endswith('--no-deps --wait --wait-timeout 180 '+apps),calls[app])
            self.assertFalse(any(re.search(r'\bup\b',call) and ' db' in call and '--no-recreate' not in call for call in calls))


def service_block(text,name):
    """The lines of one top-level service of a compose file, up to the next top-level key."""
    lines=text.splitlines();start=lines.index('  '+name+':')
    block=[]
    for line in lines[start+1:]:
        if line.strip() and not line.startswith('   ') and not line.lstrip().startswith('#'):break
        block.append(line)
    return '\n'.join(block)


def compose_image():
    return re.search(r'image: (postgres:\S+)',Path('compose.yaml').read_text()).group(1)


def runbook():
    text=Path('DEPLOY.md').read_text()
    return text[text.index('## 16.'):text.index('## Типичные ошибки')]


class SelfHostedDatabase(unittest.TestCase):
    def test_db_service_is_pinned_private_and_healthchecked(self):
        compose=Path('compose.yaml').read_text();db=service_block(compose,'db')
        self.assertRegex(db,r'image: postgres:\d+\.\d+-bookworm@sha256:[0-9a-f]{64}\n')
        self.assertNotIn('ports:',db)                      # internal compose network only
        self.assertIn('restart: unless-stopped',db)
        self.assertIn('"pg_isready"',db)
        self.assertIn('env_file: .env.db',db)              # the superuser password never reaches the app containers
        self.assertIn('pgdata:/var/lib/postgresql/data',db)
        self.assertIn('./deploy/postgres/init:/docker-entrypoint-initdb.d:ro',db)
        self.assertIn('./backups:/backups:ro',db)
        self.assertIn('oom_score_adj: -800',db)
        self.assertIn('max-size: 10m',db)
        for setting in ('max_connections=50','shared_buffers=512MB','effective_cache_size=2GB','work_mem=8MB',
                        'pg_stat_statements.track_utility=off','log_parameter_max_length=0',
                        'log_parameter_max_length_on_error=0'):
            self.assertIn('- '+setting,db)
        # App timeouts live on the app role (init script), not server-wide.
        self.assertNotIn('idle_in_transaction_session_timeout',db)
        self.assertNotIn('statement_timeout',db)
        self.assertIn('\nvolumes:\n  pgdata:\n',compose)

    def test_app_services_wait_for_healthy_db_and_are_memory_capped(self):
        for path,name,limit in (('compose.yaml','leadfinder','768m'),('compose.web.yaml','web','512m')):
            block=service_block(Path(path).read_text(),name)
            self.assertIn('depends_on:\n      db:\n        condition: service_healthy',block,path)
            self.assertIn('mem_limit: '+limit,block,path)

    def test_init_creates_every_role_schema_and_extension_the_migrations_reference(self):
        path='deploy/postgres/init/10-leadgen.sh';init=Path(path).read_text()
        self.assertTrue(os.access(path,os.X_OK))
        sql='\n'.join(file.read_text() for file in sorted(Path('supabase/migrations').glob('*.sql')))
        roles=set()
        for match in re.finditer(r'\b(?:revoke|grant)\b[^;]*?\b(?:from|to)\s+([a-z_,\s]+?)\s*;',sql,re.I|re.S):
            roles|={role.strip().lower() for role in match.group(1).split(',')}
        self.assertEqual(roles-{'public'},{'anon','authenticated'})
        self.assertIn("ARRAY['anon','authenticated']",init)
        self.assertIn('extensions.gin_trgm_ops',sql)
        self.assertIn('CREATE SCHEMA IF NOT EXISTS extensions',init)
        for extension in set(re.findall(r'create extension if not exists (\w+)',sql,re.I)):
            self.assertIn('CREATE EXTENSION IF NOT EXISTS '+extension+' WITH SCHEMA extensions',init)
        for function in set(re.findall(r'function (public\.\w+)\(',sql)):self.assertIn(function+'()',init)
        self.assertIn('NOSUPERUSER',init)
        self.assertEqual(subprocess.run(['sh','-n',path]).returncode,0)

    def test_init_keeps_secrets_off_argv_and_least_privilege(self):
        init=Path('deploy/postgres/init/10-leadgen.sh').read_text()
        self.assertIn('\\getenv app_password LEADGEN_DB_PASSWORD',init)
        self.assertNotIn('-v app_password',init)
        self.assertNotIn('pg_read_all_stats',init)
        self.assertIn('ALTER SCHEMA public OWNER TO postgres',init)
        self.assertIn('REVOKE ALL ON SCHEMA public FROM PUBLIC',init)
        self.assertIn('GRANT USAGE ON SCHEMA public TO %I',init)
        self.assertIn('reject="host all $POSTGRES_USER all reject"',init)
        self.assertIn('/^host all all all /i',init)      # before the image's generic scram rule
        self.assertIn('pg_stat_statements_reset()',init)

    def test_app_role_timeouts_stay_below_the_delivery_cursor_lag(self):
        init=Path('deploy/postgres/init/10-leadgen.sh').read_text()
        def seconds(name):
            return int(re.search(r"SET "+name+r" = %L', :'app_user', '(\d+)s'",init).group(1))
        statement,idle=seconds('statement_timeout'),seconds('idle_in_transaction_session_timeout')
        self.assertEqual((statement,idle),(120,60))       # what Supabase enforced and the code was written for
        product=Path('leadgen/product.py').read_text()
        self.assertIn('statement_timeout is 120 s, idle_in_transaction 60 s',product)
        from leadgen.product import cursor_lag_seconds
        from unittest.mock import patch
        with patch.dict(os.environ,{'LEAD_CURSOR_LAG_SECONDS':''}):self.assertGreater(cursor_lag_seconds(),statement+idle)

    def test_db_secrets_file_is_ignored_and_documented(self):
        ignored=Path('.gitignore').read_text().split()
        self.assertIn('.env.db',ignored);self.assertIn('backups/',ignored)
        example=Path('.env.db.example').read_text()
        for name in ('POSTGRES_PASSWORD=','LEADGEN_DB_USER=','LEADGEN_DB_PASSWORD='):self.assertIn(name,example)


class Runbook(unittest.TestCase):
    def test_client_image_matches_server_image(self):
        text=runbook()
        self.assertIn('\nPGIMG='+compose_image()+'\n',text)
        self.assertNotRegex(text,r'docker run --rm (?:-\S+ )*postgres:')   # always $PGIMG, one place to update
        self.assertIn("pg_dump \"$PGURL\" --schema=leadgen --no-owner --no-privileges -Fc",text)

    def test_old_database_url_never_on_a_command_line(self):
        text=runbook()
        for line in text.splitlines():
            if 'docker run' in line:
                self.assertIn('-e PGURL',line);self.assertNotIn('DATABASE_URL',line)
        self.assertIn('unset PGURL',text)
        self.assertNotIn('OLD_DATABASE_URL',text)
        self.assertNotIn('backups/env-',text)                 # the old .env copy lives in /root, not with dumps
        self.assertIn('install -m 600 .env /root/env-supabase.bak',text)

    def test_move_steps_run_as_root_and_installer_comes_last(self):
        text=runbook();move=text[text.index('### Переезд'):text.index('### Откат')]
        before,after=move.split('**11.',1)
        for forbidden in ('sudo -u','chown','install -m 600 -o','install -d -m 700 -o'):
            self.assertNotIn(forbidden,before.split('```',1)[1])
        self.assertNotIn('install-auto-update.sh',before.replace('**Не запускайте `deploy/install-auto-update.sh` до шага 11.**',''))
        self.assertIn('install-auto-update.sh',after)
        self.assertIn('systemctl stop leadfinder-update.timer leadfinder-update.service',before)
        self.assertIn('systemctl is-active leadfinder-update.timer leadfinder-update.service',before)
        self.assertIn('git -C /opt/leadfinder rev-parse HEAD > /root/leadfinder-rollback.sha',before)
        self.assertIn('LEADGEN_MIGRATE_STATEMENT_TIMEOUT=2h',before)
        self.assertEqual(before.count('vacuumdb -U postgres -d leadgen --analyze-in-stages'),2)   # after restore and after migrations
        self.assertIn('exec db pg_restore -U leadgen -d leadgen --no-owner --no-privileges --single-transaction --exit-on-error /backups/',before)
        self.assertNotIn('pg_restore -U leadgen -d leadgen --no-owner --no-privileges --single-transaction --exit-on-error <',text)
        self.assertIn("grep -E 'POLICY|EVENT TRIGGER|EXTENSION'",before)

    def test_rollback_and_restore_are_documented_safely(self):
        text=runbook()
        rollback=text[text.index('### Откат'):text.index('### Восстановление')]
        self.assertIn('checkout --detach "$(cat /root/leadfinder-rollback.sha)"',rollback)
        self.assertIn('install -m 600 /root/env-supabase.bak .env',rollback)
        self.assertNotRegex(rollback,r'\ndocker compose[^\n]*--remove-orphans')   # old code would remove the db container
        restore=text[text.index('### Восстановление'):text.index('### Обслуживание')]
        self.assertLess(restore.index('RENAME TO leadgen_old'),restore.index('pg_restore'))
        self.assertLess(restore.index('pg_restore'),restore.index('DROP SCHEMA leadgen_old'))
        self.assertIn('up -d --force-recreate --wait db',text)       # how db config changes are applied


class Backups(unittest.TestCase):
    def run_backup(self,root,dump='PGDMP archive',fail_verify=False):
        bin_dir=root/'bin';bin_dir.mkdir(exist_ok=True)
        (bin_dir/'docker').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$LEADFINDER_DIR/calls"\n'
            'case "$*" in *pg_dump*) printf "%s" "$FAKE_DUMP";; *pg_restore*) exit "$FAIL_VERIFY";; esac\n')
        (bin_dir/'docker').chmod(0o755)
        env=os.environ|{'LEADFINDER_DIR':str(root),'PATH':str(bin_dir)+':'+os.environ['PATH'],
                        'FAKE_DUMP':dump,'FAIL_VERIFY':'1' if fail_verify else '0'}
        return subprocess.run(['sh','deploy/leadfinder-backup.sh'],env=env,capture_output=True,text=True)

    def old_backup(self,backups,name='leadgen-20000101T000000Z.dump'):
        path=backups/name;path.write_text('old');os.utime(path,(0,0))
        return path

    def test_backup_is_fully_read_kept_private_and_old_ones_pruned(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);backups=root/'backups';backups.mkdir()
            old=self.old_backup(backups);final=self.old_backup(backups,'supabase-final.dump')
            result=self.run_backup(root)
            self.assertEqual(result.returncode,0,result.stderr)
            dumps=[path for path in backups.glob('leadgen-*.dump')]
            self.assertEqual(len(dumps),1);self.assertNotEqual(dumps[0],old)
            self.assertTrue(final.exists())                    # only leadgen-*.dump files rotate
            self.assertEqual(dumps[0].stat().st_mode&0o777,0o600)
            calls=(root/'calls').read_text()
            self.assertIn('-f compose.yaml exec -T db pg_dump -U postgres -d leadgen --schema=leadgen -Fc',calls)
            # Full read of the archive inside the container, from the read-only /backups mount.
            self.assertIn('exec -T db pg_restore -f /dev/null /backups/'+dumps[0].name+'.partial',calls)

    def test_failed_backup_keeps_old_ones_and_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);backups=root/'backups';backups.mkdir();old=self.old_backup(backups)
            self.assertNotEqual(self.run_backup(root,fail_verify=True).returncode,0)
            self.assertEqual(list(backups.iterdir()),[old])
            self.assertNotEqual(self.run_backup(root,dump='').returncode,0)
            self.assertEqual(list(backups.iterdir()),[old])

    def test_backup_unit_is_hardened_alerts_on_failure_and_is_installed(self):
        unit=Path('deploy/leadfinder-backup.service').read_text()
        for line in ('User=leadfinder-deploy','SupplementaryGroups=docker','NoNewPrivileges=yes','PrivateTmp=yes',
                     'ProtectSystem=strict','ReadWritePaths=/opt/leadfinder/backups /var/lib/leadfinder-deploy',
                     'UMask=0077','PrivateDevices=yes','LockPersonality=yes','SystemCallArchitectures=native',
                     'CapabilityBoundingSet=\n','OnFailure=leadfinder-alert@%n.service'):
            self.assertIn(line,unit)
        self.assertNotIn('User=root',unit)
        self.assertIn('Persistent=true',Path('deploy/leadfinder-backup.timer').read_text())
        alert=Path('deploy/leadfinder-alert@.service').read_text()
        self.assertIn('python -m leadgen.observability --alert',alert)
        self.assertIn('User=leadfinder-deploy',alert)
        self.assertNotIn('TELEGRAM',alert.split('ExecStart=',1)[1].splitlines()[0])   # no secrets in argv
        installer=Path('deploy/install-auto-update.sh').read_text()
        directory=installer.index('install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR/backups"')
        self.assertLess(directory,installer.index('systemctl enable --now leadfinder-backup.timer'))
        self.assertIn('deploy/leadfinder-alert@.service" /etc/systemd/system/leadfinder-alert@.service',installer)
        self.assertEqual(subprocess.run(['sh','-n','deploy/leadfinder-backup.sh']).returncode,0)


class Units(unittest.TestCase):
    def test_units_run_as_deploy_user_with_hardening(self):
        for name in ('leadfinder-update.service','leadfinder-renew.service'):
            text=Path('deploy/'+name).read_text()
            for line in ('User=leadfinder-deploy','SupplementaryGroups=docker','NoNewPrivileges=yes',
                         'PrivateTmp=yes','ProtectSystem=strict','ReadWritePaths='):
                self.assertIn(line,text,name)
            self.assertNotIn('User=root',text)
        self.assertIn('ReadWritePaths=/opt/leadfinder ',Path('deploy/leadfinder-update.service').read_text())

    def test_installer_creates_user_idempotently_and_keeps_container_data(self):
        text=Path('deploy/install-auto-update.sh').read_text()
        self.assertIn('if ! id -u "$DEPLOY_USER"',text)
        self.assertIn('usermod -aG docker "$DEPLOY_USER"',text)
        self.assertIn('-path "$APP_DIR/leadgen/data" -prune',text)
        self.assertEqual(subprocess.run(['sh','-n','deploy/install-auto-update.sh']).returncode,0)
