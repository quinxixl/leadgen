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
        # roll back to what is running, not to a checkout that a failed update may have moved ahead
        self.assertIn('cp .leadfinder-deployed-sha /root/leadfinder-rollback.sha',before)
        self.assertNotIn('rev-parse HEAD > /root/leadfinder-rollback.sha',text)
        self.assertIn('ls -ld leadgen/data',after)
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

    def test_installer_lets_the_deploy_group_list_container_data(self):
        text=Path('deploy/install-auto-update.sh').read_text()
        self.assertIn('DATA_DIR="$APP_DIR/leadgen/data"',text)
        steps=['mkdir -p "$DATA_DIR"','chown 10001:"$DEPLOY_USER" "$DATA_DIR"','chmod 0750 "$DATA_DIR"']
        positions=[text.index(step) for step in steps]
        self.assertEqual(positions,sorted(positions))
        self.assertGreater(positions[0],text.index('-prune -o ! -user'))     # after the checkout chown, which skips data
        for line in text.splitlines():                                     # the directory only, never the session inside
            if 'DATA_DIR' in line:
                self.assertNotRegex(line,r'(chown|chmod|chgrp) -R|find ')


DOCKERIGNORE_GLOB=re.compile(r'[*?\[\\]')


def dockerignore():
    return [line for line in Path('.dockerignore').read_text().splitlines() if line and not line.startswith('#')]


class BuildContext(unittest.TestCase):
    def test_exceptions_are_plain_paths_so_buildkit_skips_container_data(self):
        # BuildKit (fsutil) skips excluded directories only while no "!" pattern has a glob before a trailing /**.
        # One such pattern makes `docker compose build` open leadgen/data and fail as leadfinder-deploy.
        for pattern in dockerignore():
            if pattern.startswith('!'):
                self.assertNotRegex(pattern[1:].removesuffix('/**'),DOCKERIGNORE_GLOB,pattern)
        self.assertEqual(dockerignore()[0],'*')
        self.assertEqual(dockerignore()[-1],'leadgen/data')

    def test_every_module_is_listed_and_nothing_else_from_leadgen_root(self):
        listed={p[1:] for p in dockerignore() if re.fullmatch(r'!leadgen/[^/]+\.py',p)}
        self.assertEqual(listed,{p.as_posix() for p in Path('leadgen').glob('*.py')})
        for pattern in dockerignore():
            if pattern.startswith('!leadgen/'):
                self.assertTrue(pattern in {'!leadgen/'+name for name in ('sources.json','web_templates/**','web_static/**')}
                                or pattern[1:] in listed,pattern)

    def test_dockerfile_sources_are_in_the_context(self):
        patterns=dockerignore()
        for source in ('requirements.txt','leadgen/sources.json'):
            self.assertIn('!'+source,patterns)
        for directory in ('leadgen/web_templates','leadgen/web_static','supabase/migrations'):
            self.assertIn('!'+directory+'/**',patterns)
        self.assertIn('COPY leadgen/*.py leadgen/',Path('Dockerfile').read_text())


def nginx_site():
    """The site config with comments stripped, so directives mentioned in comments do not count."""
    text=Path('deploy/nginx/signalid.ru.conf').read_text()
    return '\n'.join(line.split('#',1)[0].rstrip() for line in text.splitlines())


def nginx_block(config,header):
    """The `header { ... }` block, braces balanced."""
    start=config.index(header+' {')
    depth=0
    for index in range(start,len(config)):
        depth+={'{':1,'}':-1}.get(config[index],0)
        if config[index]=='}' and depth==0:return config[start:index+1]
    raise AssertionError(header)


class Nginx(unittest.TestCase):
    def test_https_http2_and_certbot_files(self):
        config=nginx_site()
        self.assertIn('listen 443 ssl http2;',config)
        self.assertIn('listen [::]:443 ssl http2 ipv6only=on;',config)
        self.assertNotIn('http2 on;',config)            # nginx 1.25.1+; the server runs 1.24
        for line in ('ssl_certificate /etc/letsencrypt/live/signalid.ru/fullchain.pem;',
                     'ssl_certificate_key /etc/letsencrypt/live/signalid.ru/privkey.pem;',
                     'include /etc/letsencrypt/options-ssl-nginx.conf;',
                     'ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;'):
            self.assertIn(line,config)
        # certbot's options-ssl-nginx.conf sets these; repeating one makes `nginx -t` fail.
        # Let's Encrypt has no OCSP any more, so no stapling either.
        for directive in ('ssl_session_cache','ssl_session_timeout','ssl_session_tickets','ssl_protocols','ssl_stapling'):
            self.assertNotRegex(config,rf'(?m)^\s*{directive}\b')

    def test_plain_http_redirects_both_names(self):
        config=nginx_site()
        redirect=nginx_block(config,'server')
        self.assertIn('listen 80;',redirect)
        self.assertIn('server_name signalid.ru www.signalid.ru;',redirect)
        self.assertIn('return 301 https://$host$request_uri;',redirect)
        self.assertNotIn('default_server',config)

    def test_gzip_and_rate_limits(self):
        config=nginx_site()
        for line in ('gzip on;','gzip_vary on;','gzip_proxied any;','server_tokens off;','client_max_body_size 2m;',
                     'proxy_read_timeout 60s;','limit_req_status 429;',
                     'limit_req_zone $binary_remote_addr zone=signalid_login:10m rate=10r/m;',
                     'limit_req_zone $binary_remote_addr zone=signalid_api:10m rate=60r/m;'):
            self.assertIn(line,config)
        for mime in ('text/css','application/javascript','image/svg+xml','application/json'):
            self.assertRegex(config,rf'gzip_types[^;]*{re.escape(mime)}')
        for location in ('location = /login','location = /tg/auth','location ~ ^/app/telegram/(connect|code|resend|password)$'):
            self.assertIn('limit_req zone=signalid_login',nginx_block(config,location))
        self.assertIn('limit_req zone=signalid_api',nginx_block(config,'location /api/'))
        self.assertNotIn('limit_req',nginx_block(config,'location /'))

    def test_proxy_sets_forwarded_host_and_keeps_upstream_connections(self):
        config=nginx_site()
        self.assertIn('proxy_set_header X-Forwarded-Host $host;',config)   # ProxyFix(x_host=1) trusts it
        self.assertNotIn('$http_x_forwarded_host',config)
        self.assertIn('proxy_set_header Connection "";',config)
        upstream=nginx_block(config,'upstream signalid_app')
        self.assertIn('server 127.0.0.1:8000;',upstream)
        self.assertIn('keepalive 16;',upstream)
        self.assertEqual(set(re.findall(r'proxy_pass (\S+);',config)),{'http://signalid_app'})

    def test_static_is_served_by_nginx_with_versioned_cache(self):
        config=nginx_site()
        static=nginx_block(config,'location /web_static/')
        self.assertIn('root /opt/leadfinder/leadgen;',static)
        self.assertIn('try_files $uri =404;',static)
        self.assertIn('access_log off;',static)
        self.assertIn('add_header Cache-Control $static_cache_control;',static)
        self.assertIn('add_header X-Content-Type-Options nosniff always;',static)
        cache=nginx_block(config,'map $arg_v $static_cache_control')
        self.assertIn('default "public, max-age=31536000, immutable";',cache)
        self.assertRegex(cache,r'""\s+"public, max-age=3600";')
        # The URL prefix and directory must be what Flask serves.
        self.assertIn("static_folder='web_static'",Path('leadgen/web.py').read_text())
        self.assertTrue(Path('leadgen/web_static/app.css').is_file())
        favicon=nginx_block(config,'location = /favicon.ico')
        self.assertIn('root /opt/leadfinder/leadgen/web_static;',favicon)
        self.assertIn('try_files /favicon.ico @app;',favicon)
        self.assertIn('max-age=86400',favicon)
        self.assertTrue(Path('leadgen/web_static/favicon.ico').is_file())
