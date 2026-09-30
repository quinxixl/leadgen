import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from leadgen.core import Lead, UTC, budget, classify, message, lead_profile
from leadgen.adapters import parse
from leadgen.app import db_open, ingest, deliver, telegram, notification_chat_ids
from leadgen.product import eligible_for_user, lead_buttons
import socket
import ssl
from urllib.error import URLError

NOW = datetime.now(UTC)
CONFIG = {'min_budget':5000,'max_age_hours':72}

def lead(**changes):
    values = dict(source='test',url='https://example.org/1',title='Нужен Telegram бот',
                  text='Нужно создать бота для записи клиентов',published=NOW.isoformat(),budget_text='10 000 ₽')
    return Lead(**(values | changes))

class Filters(unittest.TestCase):
    def test_budget_formats(self):
        for text,expected in [('5 000 ₽',5000),('5к',5000),('5 тыс. руб',5000),('20–70 тыс рублей',20000),
                              ('5.000 руб',5000),('5,5к',5500),('от 10000 рублей',10000),('Бюджет: 25000-35000 рублей',25000)]:
            with self.subTest(text=text):
                self.assertEqual(budget(text)[0],expected)

    def test_ambiguous_budget(self):
        for text in ['до 10000 ₽','10000 ₽/месяц','5000 рублей в час','7000 $','договорная','срок 5000 дней','2000 ₽ + 10000 ₽','20 000–5 000 ₽']:
            with self.subTest(text=text):
                self.assertIsNone(budget(text)[0])

    def test_threshold(self):
        self.assertEqual(classify(lead(budget_text='4999 ₽'))[0],'rejected')
        self.assertEqual(classify(lead(budget_text='5000 ₽'))[0],'ready')

    def test_reject_noise(self):
        for text in ['Ищу заказы на разработку ботов','Предлагаю услуги разработки сайтов','Вакансия: разработчик ботов',
                     'Заработай 17000 рублей, создав бота по пошаговой инструкции']:
            with self.subTest(text=text):
                self.assertEqual(classify(lead(text=text))[0],'rejected')

    def test_old_or_unknown_date(self):
        self.assertEqual(classify(lead(published=(NOW-timedelta(days=4)).isoformat()))[0],'rejected')
        self.assertEqual(classify(lead(published=None))[0],'review')

    def test_hh_is_separate(self):
        self.assertEqual(classify(lead(kind='vacancy',budget_text='100000 ₽'))[0],'review')

    def test_other_service(self):
        self.assertEqual(classify(lead(title='Нужен дизайнер логотипа',text='Нарисовать логотип'))[0],'rejected')

    def test_message_limit(self):
        self.assertLess(len(message(lead(text='<b>x</b>'*3000),'Причина')),4096)

    def test_temperature_and_summary(self):
        item=lead(text='Срочно нужен Telegram бот для записи клиентов. Срок 7 дней. Бюджет 50 000 ₽. Писать @owner',budget_text='50 000 ₽')
        profile=lead_profile(item)
        self.assertEqual(profile['temperature'],'горячий')
        self.assertGreaterEqual(profile['score'],70)
        self.assertIn('Нужно:',profile['summary'])
        self.assertIn('50 000 ₽',profile['summary'])

    def test_seller_is_cold(self):
        profile=lead_profile(lead(text='Предлагаю услуги разработки сайтов, ищу клиентов',budget_text=''))
        self.assertEqual(profile['temperature'],'холодный')

    def test_implicit_business_problem_is_possible_need(self):
        item=lead(title='Теряем заявки',text='Менеджеры вручную переносят заявки с сайта в таблицу, постоянно теряем клиентов',budget_text='')
        state,reason,tags=classify(item)
        self.assertEqual(state,'review')
        self.assertTrue(reason.startswith('возможная потребность:'))
        self.assertIn('Автоматизации',tags)
        self.assertIn('CRM',tags)

    def test_user_can_toggle_unknown_budget_separately(self):
        item=lead(budget_text='')
        prefs={'show_without_budget':False,'show_possible_needs':True}
        self.assertFalse(eligible_for_user(item,CONFIG,prefs)[0])
        prefs['show_without_budget']=True
        self.assertTrue(eligible_for_user(item,CONFIG,prefs)[0])

    def test_product_card_has_feedback_pipeline_and_reply_actions(self):
        markup=json.dumps(lead_buttons(42,'https://example.org/lead'),ensure_ascii=False)
        for value in ('feedback:fit:42','feedback:ad:42','pipeline:menu:42','reply:42'):
            self.assertIn(value,markup)

class TelegramDiagnostics(unittest.TestCase):
    def test_errors_hide_token_and_distinguish_setup(self):
        token = '123:' + 'a'*30
        cases = [(URLError(socket.gaierror(-2,'secret '+token)), 'DNS'),
                 (URLError(ssl.SSLCertVerificationError(1,'secret '+token)), 'SSL'),
                 (TimeoutError('secret '+token), 'время ожидания'),
                 (ValueError('secret '+token), 'ValueError')]
        for error, expected in cases:
            with self.subTest(expected=expected), patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':token}), patch('leadgen.app.fetch',side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    telegram('getMe',{})
                self.assertIn(expected,str(caught.exception))
                self.assertNotIn(token,str(caught.exception))
                self.assertIn('настройка не завершена',str(caught.exception))

    def test_bad_token_fails_before_network(self):
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'не токен'}), patch('leadgen.app.fetch') as fetch:
            with self.assertRaises(ValueError):telegram('getMe',{})
            fetch.assert_not_called()

class Parsing(unittest.TestCase):
    def test_telegram_time_and_contact(self):
        source={'name':'test','kind':'telegram','url':'https://t.me/s/test'}
        body='<div class="tgme_widget_message" data-post="test/1"><div class="tgme_widget_message_text">Нужен бот. Бюджет 5000 ₽ <a href="https://t.me/contact">Контакт</a></div><time datetime="2026-09-14T10:00:00+00:00"></time></div>'
        result=parse(source,body)[0]
        self.assertEqual(result.url,'https://t.me/test/1')
        self.assertIn('https://t.me/contact',result.text)
        self.assertEqual(result.published,'2026-09-14T10:00:00+00:00')

    def test_layout_failure(self):
        with self.assertRaises(ValueError):
            parse({'kind':'fl','name':'FL','url':'https://www.fl.ru/'},'<html>Captcha</html>')

    def test_closed_tender(self):
        self.assertEqual(parse({'kind':'workspace','name':'WS','url':'https://workspace.ru/'},'<div data-tender-card><span>Завершён</span></div>'),[])

class Delivery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=db_open(Path(self.tmp.name)/'test.sqlite')
        self.env=patch.dict(os.environ,{'TELEGRAM_CHAT_ID':'123'})
        self.env.start()

    def tearDown(self):
        self.env.stop();self.db.close();self.tmp.cleanup()

    def test_repeat_and_cross_source_dedupe(self):
        with self.db:
            ingest(self.db,lead(),CONFIG)
            ingest(self.db,lead(),CONFIG)
            ingest(self.db,lead(source='second',url='https://example.org/2'),CONFIG)
        self.assertEqual(self.db.execute("SELECT count(*) FROM leads WHERE status='ready'").fetchone()[0],1)

    def test_send_once_after_rescan(self):
        calls=[]
        with self.db:ingest(self.db,lead(),CONFIG)
        with patch('leadgen.app.time.sleep'):
            deliver(self.db,CONFIG,lambda *args:calls.append(args))
            with self.db:ingest(self.db,lead(),CONFIG)
            deliver(self.db,CONFIG,lambda *args:calls.append(args))
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][1]['chat_id'],'123')

    def test_additional_notification_recipient(self):
        calls=[]
        with patch.dict(os.environ,{'TELEGRAM_CHAT_ID':'123','TELEGRAM_NOTIFY_CHAT_IDS':'456, 123'}):
            self.assertEqual(notification_chat_ids(),['123','456'])
            with self.db:ingest(self.db,lead(),CONFIG)
            with patch('leadgen.app.time.sleep'):
                deliver(self.db,CONFIG,lambda *args:calls.append(args))
        self.assertEqual([call[1]['chat_id'] for call in calls],['123','456'])

    def test_uncertain_transport_is_not_retried(self):
        with self.db:ingest(self.db,lead(),CONFIG)
        def fail(*args):raise RuntimeError('Telegram transport error; delivery may be uncertain')
        deliver(self.db,CONFIG,fail)
        with self.db:ingest(self.db,lead(),CONFIG)
        self.assertEqual(self.db.execute('SELECT status FROM leads').fetchone()[0],'uncertain')

    def test_rate_limit_stays_queued(self):
        with self.db:ingest(self.db,lead(),CONFIG)
        def fail(*args):raise RuntimeError('Telegram HTTP 429')
        deliver(self.db,CONFIG,fail)
        row=self.db.execute('SELECT status,next_attempt FROM leads').fetchone()
        self.assertEqual(row['status'],'ready')
        self.assertGreater(row['next_attempt'],0)

    def test_old_queue_not_sent(self):
        with self.db:ingest(self.db,lead(),CONFIG)
        with patch('leadgen.app.classify',return_value=('rejected','вне окна свежести',[])):
            deliver(self.db,CONFIG,lambda *args:self.fail('Should not send stale lead'))
        self.assertEqual(self.db.execute('SELECT status FROM leads').fetchone()[0],'rejected')

    def test_notify_good_request_without_budget_when_enabled(self):
        config=CONFIG|{'notify_without_budget':True}
        item=lead(text='Нужен Telegram бот для обработки заявок, есть подробное ТЗ. Писать @owner',budget_text='')
        with self.db:ingest(self.db,item,config)
        self.assertEqual(self.db.execute('SELECT status FROM leads').fetchone()[0],'ready')
        calls=[]
        deliver(self.db,config,lambda *args:calls.append(args))
        self.assertEqual(len(calls),1)

if __name__=='__main__':unittest.main()
