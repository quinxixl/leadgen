import os
from pathlib import Path
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
            self.assertNotIn('up -d',(root/'calls').read_text())
            env['FAIL_MIGRATION']='0'
            self.assertEqual(subprocess.run(['sh',str(script)],env=env,capture_output=True).returncode,0)
            self.assertEqual((root/'.leadfinder-deployed-sha').read_text().strip(),'abc123')
            calls=(root/'calls').read_text()
            self.assertIn('-f compose.yaml -f compose.web.yaml',calls)
            self.assertIn('--wait',calls)
            self.assertEqual(subprocess.run(['sh',str(script)],env=env,capture_output=True).returncode,0)
            self.assertEqual(calls,(root/'calls').read_text())


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
