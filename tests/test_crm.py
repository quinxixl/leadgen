"""Mini-CRM: statuses, legacy aliases, deal amount and note, follow-up reminders, pipeline and results."""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen import crm
from leadgen.product import ProductController, crm_buttons

ENV = {'WEB_PUBLIC_URL': 'https://signalid.test', 'MINIAPP_ENABLED': ''}


def buttons(markup):
    return [button for row in (markup or {}).get('inline_keyboard', []) for button in row]


class Pure(unittest.TestCase):
    def test_normalize_maps_legacy_and_rejects_unknown(self):
        self.assertEqual(list(crm.PIPELINE), ['new', 'replied', 'in_progress', 'won', 'lost', 'archived'])
        for legacy, current in {'saved': 'new', 'viewed': 'new', 'contacted': 'replied', 'working': 'in_progress',
                                'discussing': 'in_progress', 'meeting': 'in_progress', 'proposal': 'in_progress',
                                'not_fit': 'archived', 'won': 'won', 'lost': 'lost'}.items():
            self.assertEqual(crm.normalize(legacy), current, legacy)
        for bad in ('', None, 'nope', 'WON'):
            self.assertIsNone(crm.normalize(bad))

    def test_status_keyboard_marks_current_and_offers_day_reminders(self):
        data = [b['callback_data'] for b in buttons(crm_buttons(7, 'won'))]
        self.assertEqual(data, ['status:replied:7', 'status:in_progress:7', 'status:won:7', 'status:lost:7',
                                'status:new:7', 'status:archived:7', 'remind:7:1', 'remind:7:3', 'card:7'])
        self.assertTrue(buttons(crm_buttons(7, 'won'))[2]['text'].startswith('✓ '))

    def test_results_text(self):
        text = crm.results_text({'month': 50000, 'total': 125000, 'won': 3, 'won_month': 1, 'replied': 6, 'conversion': 50.0})
        self.assertIn('₽50 000 за месяц / ₽125 000 всего', text)
        self.assertIn('Сделок: 3', text)
        self.assertIn('50.0%', text)
        self.assertIn('пока нет откликов', crm.results_text({'month': 0, 'total': 0, 'won': 0, 'won_month': 0,
                                                             'replied': 0, 'conversion': None}))


@unittest.skipUnless(fixtures.DSN, 'WEB_TEST_DATABASE_URL is not configured')
class CrmPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixtures.WebPostgres.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls): fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.sent = []
        self.ui = ProductController(self.db, {'min_budget': 5000, 'sources': []},
                                    lambda method, data: self.sent.append((method, data)))

    def tearDown(self): self.env.stop()

    def row(self, lid):
        return self.db.execute('SELECT * FROM user_leads WHERE lead_id=?', (lid,)).fetchone()

    def calls(self, method): return [data for name, data in self.sent if name == method]

    def text(self, value, uid=1):
        self.ui.handle({'message': {'from': {'id': uid}, 'chat': {'id': uid, 'type': 'private'}, 'text': value}})

    def callback(self, data, uid=1):
        self.ui.handle({'callback_query': {'id': 'cb', 'from': {'id': uid}, 'data': data,
                                           'message': {'message_id': 9, 'chat': {'id': uid, 'type': 'private'}}}})

    def test_new_leads_default_to_new_and_trigger_stamps_reply_and_win(self):
        lid = self.ids[0]
        self.assertEqual(self.row(lid)['pipeline_status'], 'new')
        with self.db:
            self.db.execute("UPDATE user_leads SET pipeline_status='replied' WHERE lead_id=?", (lid,))
        replied = self.row(lid)['replied_at']
        self.assertIsNotNone(replied)
        self.assertIsNone(self.row(lid)['won_at'])
        with self.db:
            self.db.execute("UPDATE user_leads SET pipeline_status='won',deal_amount=30000 WHERE lead_id=?", (lid,))
        self.assertIsNotNone(self.row(lid)['won_at'])
        self.assertEqual(self.row(lid)['replied_at'], replied)
        with self.db:
            self.db.execute("UPDATE user_leads SET pipeline_status='lost' WHERE lead_id=?", (lid,))
        self.assertIsNone(self.row(lid)['won_at'])
        with self.assertRaises(Exception):
            with self.db:
                self.db.execute("UPDATE user_leads SET pipeline_status='saved' WHERE lead_id=?", (lid,))

    def test_web_won_with_amount_and_note_feeds_results_and_pipeline(self):
        lid = self.ids[0]
        r = self.client.post(f'/app/leads/{lid}', data={'csrf': 'test-csrf', 'status': 'won', 'amount': '45000',
                                                        'feedback': '', 'crm_note': 'Предоплата 50%'})
        self.assertEqual(r.status_code, 302)
        row = self.row(lid)
        self.assertEqual((row['pipeline_status'], row['deal_amount'], row['crm_note']), ('won', 45000, 'Предоплата 50%'))
        details = [a['detail'] for a in self.db.execute('SELECT detail FROM lead_activity WHERE lead_id=?', (lid,))]
        self.assertIn('Сделка', details)
        self.assertIn('Заметка по сделке: Предоплата 50%', details)
        # The other owner's win must not leak into this owner's numbers.
        with self.db:
            self.db.execute("UPDATE user_leads SET pipeline_status='won',deal_amount=999999 WHERE lead_id=?", (self.ids[1],))
        data = crm.results(self.db, self.u)
        self.assertEqual((data['month'], data['total'], data['won'], data['replied'], data['conversion']),
                         (45000, 45000, 1, 1, 100.0))
        dashboard = self.client.get('/app').text
        self.assertIn('Через Сигналид вы заработали ₽45 000 за месяц / ₽45 000 всего', dashboard)
        self.assertNotIn('999 999', dashboard)
        board = self.client.get('/app/pipeline')
        self.assertEqual(board.status_code, 200)
        self.assertIn('Нужен сайт 0', board.text)
        self.assertIn('Предоплата 50%', board.text)
        self.assertNotIn('Нужен сайт 1', board.text)
        # Legacy status in links still works; unknown ones are rejected.
        self.assertEqual(self.client.get('/app/pipeline?status=working').status_code, 200)
        self.assertEqual(self.client.get('/app/leads?status=saved').status_code, 200)
        self.assertEqual(self.client.get('/app/pipeline?status=nope').status_code, 400)
        self.assertEqual(self.client.post(f'/app/leads/{lid}', data={'csrf': 'test-csrf', 'status': 'won',
                                                                     'crm_note': 'x' * 1001}).status_code, 400)

    def test_archive_is_hidden_from_board_and_viewer_reads_only(self):
        lid = self.ids[0]
        self.client.post(f'/app/leads/{lid}', data={'csrf': 'test-csrf', 'status': 'archived'})
        self.assertNotIn('Нужен сайт 0', self.client.get('/app/pipeline').text)
        self.assertIn('Нужен сайт 0', self.client.get('/app/pipeline?status=archived').text)
        with self.db:
            self.db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,'viewer')", (self.ws, self.other))
        with self.client.session_transaction() as state:
            state['user_id'] = self.other
            state['workspace_id'] = self.ws
        self.assertIn('Нужен сайт 0', self.client.get('/app/pipeline?status=archived').text)
        self.assertEqual(self.client.post(f'/app/leads/{lid}', data={'csrf': 'test-csrf', 'status': 'won'}).status_code, 403)
        self.assertEqual(self.client.post(f'/app/leads/{lid}/reminder', data={'csrf': 'test-csrf', 'in_days': '1'}).status_code, 403)

    def test_web_quick_reminder_in_days(self):
        lid = self.ids[0]
        self.assertEqual(self.client.post(f'/app/leads/{lid}/reminder', data={'csrf': 'test-csrf', 'in_days': '3'}).status_code, 302)
        due = self.db.execute('SELECT due_at FROM lead_reminders WHERE lead_id=?', (lid,)).fetchone()['due_at']
        self.assertAlmostEqual((due - datetime.now(timezone.utc)).total_seconds(), 3 * 86400, delta=120)
        self.assertEqual(self.client.post(f'/app/leads/{lid}/reminder', data={'csrf': 'test-csrf', 'in_days': '2'}).status_code, 400)
        self.assertEqual(self.client.post(f'/app/leads/{self.ids[1]}/reminder', data={'csrf': 'test-csrf', 'in_days': '1'}).status_code, 404)

    def test_bot_status_buttons_deal_amount_note_reminder_and_results(self):
        lid = self.ids[0]
        self.callback(f'crm:{lid}')
        markup = self.calls('editMessageReplyMarkup')[-1]['reply_markup']
        self.assertIn(f'status:won:{lid}', [b['callback_data'] for b in buttons(markup)])
        self.callback(f'status:won:{lid}')
        self.assertEqual(self.row(lid)['pipeline_status'], 'won')
        self.assertIn('сумму сделки', self.calls('sendMessage')[-1]['text'])
        self.text('70 000')
        self.assertEqual(self.row(lid)['deal_amount'], 70000)
        self.assertIn('заметку', self.calls('sendMessage')[-1]['text'])
        self.text('Сдаём 1 ноября')
        self.assertEqual(self.row(lid)['crm_note'], 'Сдаём 1 ноября')
        self.callback(f'remind:{lid}:1')
        due = self.db.execute('SELECT due_at FROM lead_reminders WHERE lead_id=?', (lid,)).fetchone()['due_at']
        self.assertAlmostEqual((due - datetime.now(timezone.utc)).total_seconds(), 86400, delta=120)
        self.text('/results')
        self.assertIn('₽70 000 за месяц / ₽70 000 всего', self.calls('sendMessage')[-1]['text'])
        # Another user's lead is not reachable; legacy keys still map onto the new pipeline.
        self.callback(f'status:won:{self.ids[1]}')
        self.assertEqual(self.row(self.ids[1])['pipeline_status'], 'new')
        self.callback(f'status:contacted:{lid}')
        self.assertEqual(self.row(lid)['pipeline_status'], 'replied')
        self.callback(f'remind:{lid}:2')
        self.assertIn(f'remind:{lid}:1', [b['callback_data'] for b in buttons(crm_buttons(lid))])

    def test_mini_app_deal_form_and_day_reminder(self):
        lid = self.ids[0]
        with self.db:
            self.db.execute('UPDATE app_users SET terms_accepted_at=now()')
        tg = self.app.test_client()
        with tg.session_transaction('/tg/feed') as state:
            state.update(user_id=self.u, csrf='tg-csrf')
        post = lambda **data: tg.post(f'/tg/leads/{lid}', data={'csrf': 'tg-csrf'} | data)
        self.assertEqual(post(action='status', status='won').status_code, 302)
        self.assertIn('Сохранить сделку', tg.get(f'/tg/leads/{lid}').text)
        self.assertEqual(post(action='deal', amount='12000', crm_note='Через месяц допработы').status_code, 302)
        row = self.row(lid)
        self.assertEqual((row['pipeline_status'], row['deal_amount'], row['crm_note']), ('won', 12000, 'Через месяц допработы'))
        self.assertEqual(post(action='deal', amount='-5').status_code, 400)
        self.assertEqual(post(action='remind', days='3').status_code, 302)
        due = self.db.execute('SELECT due_at FROM lead_reminders WHERE lead_id=?', (lid,)).fetchone()['due_at']
        self.assertAlmostEqual((due - datetime.now(timezone.utc)).total_seconds(), 3 * 86400, delta=120)
        self.assertEqual(post(action='remind', days='7').status_code, 400)
        self.assertEqual(tg.post(f'/tg/leads/{self.ids[1]}', data={'csrf': 'tg-csrf', 'action': 'deal', 'amount': '1'}).status_code, 404)
