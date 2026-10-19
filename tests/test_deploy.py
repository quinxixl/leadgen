import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


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
            self.assertIn('up -d --wait --wait-timeout 120 db',failed)   # the database may start...
            self.assertNotIn('--remove-orphans',failed)                   # ...but the app is not restarted
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

    def test_database_is_healthy_before_migrations_in_both_modes(self):
        for web,files in (('0','-f compose.yaml '),('1','-f compose.yaml -f compose.web.yaml ')):
            calls=self.run_updater(web)
            db=calls.index('compose '+files+'up -d --wait --wait-timeout 120 db')
            migrate=next(i for i,call in enumerate(calls) if 'web_migrate' in call)
            app=next(i for i,call in enumerate(calls) if '--remove-orphans' in call)
            self.assertLess(db,migrate,web);self.assertLess(migrate,app,web)
            self.assertIn('run --rm --no-deps leadfinder',calls[migrate])


def service_block(text,name):
    """The lines of one top-level service of a compose file, up to the next top-level key."""
    lines=text.splitlines();start=lines.index('  '+name+':')
    block=[]
    for line in lines[start+1:]:
        if line.strip() and not line.startswith('   ') and not line.lstrip().startswith('#'):break
        block.append(line)
    return '\n'.join(block)


class SelfHostedDatabase(unittest.TestCase):
    def test_db_service_is_pinned_private_and_healthchecked(self):
        compose=Path('compose.yaml').read_text();db=service_block(compose,'db')
        self.assertRegex(db,r'image: postgres:\d+\.\d+-bookworm\n')
        self.assertNotIn('ports:',db)                      # internal compose network only
        self.assertIn('restart: unless-stopped',db)
        self.assertIn('"pg_isready"',db)
        self.assertIn('env_file: .env.db',db)              # the superuser password never reaches the app containers
        self.assertIn('pgdata:/var/lib/postgresql/data',db)
        self.assertIn('./deploy/postgres/init:/docker-entrypoint-initdb.d:ro',db)
        self.assertIn('max-size: 10m',db)
        for setting in ('max_connections=50','shared_buffers=512MB','effective_cache_size=2GB','work_mem=8MB'):
            self.assertIn('- '+setting,db)
        self.assertIn('\nvolumes:\n  pgdata:\n',compose)

    def test_app_services_wait_for_healthy_db(self):
        for path,name in (('compose.yaml','leadfinder'),('compose.web.yaml','web')):
            block=service_block(Path(path).read_text(),name)
            self.assertIn('depends_on:\n      db:\n        condition: service_healthy',block,path)

    def test_runbook_clients_match_server_image(self):
        image=re.search(r'image: (postgres:\S+)',Path('compose.yaml').read_text()).group(1)
        deploy=Path('DEPLOY.md').read_text()
        tags=re.findall(r'docker run --rm (?:-\S+ )*(postgres:\S+)',deploy)
        self.assertTrue(tags)
        for tag in tags:self.assertEqual(tag,image)
        self.assertIn('--schema=leadgen --no-owner --no-privileges -Fc',deploy)

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

    def test_db_secrets_file_is_ignored_and_documented(self):
        ignored=Path('.gitignore').read_text().split()
        self.assertIn('.env.db',ignored);self.assertIn('backups/',ignored)
        example=Path('.env.db.example').read_text()
        for name in ('POSTGRES_PASSWORD=','LEADGEN_DB_USER=','LEADGEN_DB_PASSWORD='):self.assertIn(name,example)


class Backups(unittest.TestCase):
    def run_backup(self,root,dump='PGDMP archive',fail_list=False):
        bin_dir=root/'bin';bin_dir.mkdir(exist_ok=True)
        (bin_dir/'docker').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$LEADFINDER_DIR/calls"\n'
            'case "$*" in *pg_dump*) printf "%s" "$FAKE_DUMP";; *pg_restore*) cat >/dev/null; exit "$FAIL_LIST";; esac\n')
        (bin_dir/'docker').chmod(0o755)
        env=os.environ|{'LEADFINDER_DIR':str(root),'PATH':str(bin_dir)+':'+os.environ['PATH'],
                        'FAKE_DUMP':dump,'FAIL_LIST':'1' if fail_list else '0'}
        return subprocess.run(['sh','deploy/leadfinder-backup.sh'],env=env,capture_output=True,text=True)

    def old_backup(self,backups,name='leadgen-20000101T000000Z.dump'):
        path=backups/name;path.write_text('old');os.utime(path,(0,0))
        return path

    def test_backup_is_verified_kept_private_and_old_ones_pruned(self):
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
            self.assertIn('pg_restore --list',calls)

    def test_failed_backup_keeps_old_ones_and_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);backups=root/'backups';backups.mkdir();old=self.old_backup(backups)
            self.assertNotEqual(self.run_backup(root,fail_list=True).returncode,0)
            self.assertEqual(list(backups.iterdir()),[old])
            self.assertNotEqual(self.run_backup(root,dump='').returncode,0)
            self.assertEqual(list(backups.iterdir()),[old])

    def test_backup_unit_is_hardened_and_installed(self):
        unit=Path('deploy/leadfinder-backup.service').read_text()
        for line in ('User=leadfinder-deploy','SupplementaryGroups=docker','NoNewPrivileges=yes','PrivateTmp=yes',
                     'ProtectSystem=strict','ReadWritePaths=/opt/leadfinder/backups /var/lib/leadfinder-deploy'):
            self.assertIn(line,unit)
        self.assertNotIn('User=root',unit)
        self.assertIn('Persistent=true',Path('deploy/leadfinder-backup.timer').read_text())
        installer=Path('deploy/install-auto-update.sh').read_text()
        directory=installer.index('install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR/backups"')
        self.assertLess(directory,installer.index('systemctl enable --now leadfinder-backup.timer'))
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
