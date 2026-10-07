"""Bot UX on a real isolated PostgreSQL with a fake Telegram sender: onboarding, cards, menu, reminders."""
import json
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen.core import TOPICS
from leadgen import product
from leadgen.product import (LATEST, MONITOR_OFF, MONITOR_ON, CABINET, HELP, SETUP, ProductController,
                             deliver_registered, notify_subscriptions)

CONFIG={'min_budget':5000,'max_age_hours':72,'sources':[]}
ENV={'WEB_PUBLIC_URL':'https://signalid.test','MINIAPP_ENABLED':'','SUPPORT_TELEGRAM':'signalid_help'}


def buttons(markup):
    return [button for row in (markup or {}).get('inline_keyboard',[]) for button in row]


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
class BotUX(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        with self.db:self.db.execute("DELETE FROM settings WHERE key LIKE 'first_run:%%' OR key LIKE 'subnote:%%'")
        self.env=patch.dict(os.environ,ENV);self.env.start()
        self.sent=[];self.ui=ProductController(self.db,CONFIG,lambda method,data:self.sent.append((method,data)))
        self.clicks=0

    def tearDown(self):self.env.stop()

    def text(self,value,uid=1):
        self.ui.handle({'message':{'from':{'id':uid,'first_name':'Тест'},'chat':{'id':uid,'type':'private'},'text':value}})

    def callback(self,data,uid=1,message_id=500):
        self.clicks+=1
        self.ui.handle({'callback_query':{'id':f'cb{self.clicks}','from':{'id':uid,'first_name':'Тест'},'data':data,
            'message':{'message_id':message_id,'chat':{'id':uid,'type':'private'}}}})

    def calls(self,method):return [data for name,data in self.sent if name==method]

    def prefs(self,uid):
        return self.db.execute('SELECT p.* FROM user_preferences p JOIN app_users u ON u.id=p.user_id WHERE u.telegram_user_id=?',(uid,)).fetchone()

    def add_lead(self,title,text,budget_text='',seen_hours=2,key=None):
        now=datetime.now(timezone.utc)
        self.lead_no=getattr(self,'lead_no',100)+1
        payload=dict(source='Telegram: test',url=f'https://t.me/business_chat/{self.lead_no}',title=title,text=text,
                     published=(now-timedelta(hours=1)).isoformat(),budget_text=budget_text)
        with self.db:
            return self.db.execute('INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen) VALUES(?,?,?,?,?,?) RETURNING id',
                (payload['url'],key or title,json.dumps(payload,ensure_ascii=False),'ready','test',
                 (now-timedelta(hours=seen_hours)).isoformat())).fetchone()['id']

    def test_registration_consent_wizard_and_first_leads(self):
        with self.db:self.db.execute('DELETE FROM leads')
        self.text('/start',uid=3)
        intro=self.calls('sendMessage')[-1]
        self.assertIn('https://signalid.test/terms',intro['text']);self.assertIn('https://signalid.test/privacy',intro['text'])
        self.assertIn('согласие',intro['text'])
        self.assertEqual(buttons(intro['reply_markup'])[0]['callback_data'],'register:confirm')
        self.callback('register:confirm',uid=3)
        user=self.db.execute('SELECT * FROM app_users WHERE telegram_user_id=3').fetchone()
        self.assertIsNotNone(user['terms_accepted_at'])
        self.assertEqual(self.prefs(3)['topics'],[])
        done,wizard=self.calls('sendMessage')[-2:]
        self.assertIn('Регистрация завершена',done['text'])
        self.assertEqual(done['reply_markup']['keyboard'][0][0]['text'],MONITOR_OFF)
        self.assertEqual([b['callback_data'] for b in buttons(wizard['reply_markup'])],['ob:g:0','ob:g:1','ob:g:2','ob:g:3'])
        sites,logos=list(TOPICS).index('Сайты'),list(TOPICS).index('Логотипы и брендинг')
        matching=[self.add_lead('Нужен сайт для стоматологии','Нужен сайт для стоматологии с онлайн-записью, бюджет 80 000 рублей','80 000 рублей'),
                  self.add_lead('Ищем разработчика лендинга','Ищем разработчика лендинга для курса, бюджет 40 000 рублей','40 000 рублей',5),
                  self.add_lead('Нужен логотип для кофейни','Нужен логотип и фирменный стиль для кофейни, бюджет 25 000 рублей','25 000 рублей',10),
                  self.add_lead('Требуется доработка сайта на WordPress','Требуется доработка сайта на WordPress, бюджет 20 000 рублей','20 000 рублей',20),
                  self.add_lead('Нужен интернет-магазин','Нужен интернет-магазин одежды под ключ, бюджет 150 000 рублей','150 000 рублей',30),
                  self.add_lead('Нужен брендбук для студии','Нужен брендбук и логотип для студии йоги, бюджет 60 000 рублей','60 000 рублей',40),
                  self.add_lead('Нужен сайт-визитка','Нужен сайт-визитка для юриста, бюджет 15 000 рублей','15 000 рублей',50)]
        old=self.add_lead('Нужен сайт для автосервиса','Нужен сайт для автосервиса, бюджет 50 000 рублей','50 000 рублей',100)
        other=self.add_lead('Нужен бухгалтер','Нужен бухгалтер на аутсорс, бюджет 30 000 рублей','30 000 рублей',3)
        self.callback('ob:g:0',uid=3)
        edit=self.calls('editMessageText')[-1]
        self.assertEqual(edit['message_id'],500);self.assertIn('Разработка и IT',edit['text'])
        self.callback(f'ob:t:0:{sites}',uid=3)
        self.assertEqual(self.prefs(3)['topics'],['Сайты'])
        self.assertIn('✅ Сайты',[b['text'] for b in buttons(self.calls('editMessageText')[-1]['reply_markup'])])
        self.callback(f'ob:t:0:{logos}',uid=3)
        self.assertEqual(self.prefs(3)['topics'],['Сайты'])
        self.callback('ob:dir',uid=3)
        step1=[b['text'] for b in buttons(self.calls('editMessageText')[-1]['reply_markup'])]
        self.assertIn('Разработка и IT · 1',step1);self.assertIn('Далее →',step1)
        self.callback('ob:g:1',uid=3);self.callback(f'ob:t:1:{logos}',uid=3)
        self.assertEqual(self.prefs(3)['topics'],['Сайты','Логотипы и брендинг'])
        self.callback('ob:budget',uid=3);self.callback('ob:b:10000',uid=3)
        self.assertEqual(self.prefs(3)['min_budget'],10000)
        self.assertIn('✅ от 10 000 ₽',[b['text'] for b in buttons(self.calls('editMessageText')[-1]['reply_markup'])])
        self.callback('ob:nb',uid=3)
        self.assertTrue(self.prefs(3)['show_without_budget'])
        self.callback('ob:launch',uid=3)
        self.assertIn('Бюджет: от 10 000 ₽',self.calls('editMessageText')[-1]['text'])
        self.assertFalse(self.prefs(3)['monitoring_active'])
        before=len(self.calls('sendMessage'))
        self.callback('ob:go',uid=3)
        self.assertTrue(self.prefs(3)['monitoring_active'])
        self.assertIn('Мониторинг запущен',self.calls('answerCallbackQuery')[-1]['text'])
        self.assertEqual(len(self.calls('answerCallbackQuery')),self.clicks)
        messages=self.calls('sendMessage')[before:]
        self.assertEqual(messages[0]['reply_markup']['keyboard'][0][0]['text'],MONITOR_ON)
        cards=messages[1:]
        self.assertEqual(len(cards),5)
        self.assertTrue(all(card['text'].startswith('🕘 Из недавних') for card in cards))
        scores=[int(re.search(r'· (\d+)/100',card['text']).group(1)) for card in cards]
        self.assertEqual(scores,sorted(scores,reverse=True))
        sent_ids={row['lead_id'] for row in self.db.execute("SELECT lead_id FROM user_leads WHERE user_id=? AND delivery_status='sent'",(user['id'],))}
        self.assertEqual(len(sent_ids),5);self.assertTrue(sent_ids<=set(matching))
        self.assertNotIn(old,sent_ids);self.assertNotIn(other,sent_ids)
        for card in cards:
            self.assertEqual(buttons(card['reply_markup'])[0]['url'].split('/app/leads/')[0],'https://signalid.test')
        # The live stream keeps its registration boundary; the batch never repeats.
        later=[];deliver_registered(self.db,CONFIG,lambda method,data:later.append(data),limit_per_user=10)
        self.assertEqual(later,[])
        before=len(self.sent)
        self.text(MONITOR_ON,uid=3);self.text(MONITOR_OFF,uid=3)
        self.assertFalse([d for d in self.calls('sendMessage')[-2:] if d['text'].startswith('🕘')])
        self.assertEqual(len(self.sent),before+2)
        callbacks=[b['callback_data'] for name,data in self.sent if name in ('sendMessage','editMessageText')
                   for b in buttons(data.get('reply_markup')) if 'callback_data' in b]
        self.assertLessEqual(max(len(item.encode()) for item in callbacks),64)

    def test_first_leads_for_monitoring_enabled_on_the_site(self):
        from leadgen.product import backfill_pending
        with self.db:
            self.db.execute('DELETE FROM user_leads WHERE user_id=?',(self.u,))
            self.db.execute('UPDATE user_preferences SET monitoring_active=true,show_without_budget=true WHERE user_id IN (?,?)',(self.u,self.other))
            self.db.execute("UPDATE leads SET first_seen=(now()-interval '3 hours')::text")
        sent=[];send=lambda method,data:sent.append(data)
        self.assertEqual(backfill_pending(self.db,CONFIG,send),2)
        self.assertEqual([item['chat_id'] for item in sent],['1','1'])
        self.assertTrue(all(item['text'].startswith('🕘 Из недавних') for item in sent))
        # The second user already had a delivered lead: only the marker is set, nothing is resent.
        self.assertTrue(self.db.execute("SELECT 1 FROM settings WHERE key=?",(f'first_run:{self.other}',)).fetchone())
        self.assertEqual(backfill_pending(self.db,CONFIG,send),0)
        self.assertEqual(deliver_registered(self.db,CONFIG,send,limit_per_user=10),0)
        self.assertEqual(len(sent),2)

    def test_start_shows_wizard_only_until_configured(self):
        self.text('/start')
        self.assertIn('keyboard',self.calls('sendMessage')[-1]['reply_markup'])
        with self.db:self.db.execute("UPDATE user_preferences SET topics='[]'::jsonb WHERE user_id=?",(self.u,))
        self.text('/start')
        self.assertEqual(buttons(self.calls('sendMessage')[-1]['reply_markup'])[0]['callback_data'],'ob:g:0')
        self.text(MONITOR_OFF)
        self.assertFalse(self.prefs(1)['monitoring_active'])
        self.assertEqual(buttons(self.calls('sendMessage')[-1]['reply_markup'])[0]['callback_data'],'ob:g:0')
        with self.db:self.db.execute('DELETE FROM user_leads WHERE user_id=?',(self.u,))
        with self.db:self.db.execute("UPDATE user_preferences SET topics='[\"Сайты\"]'::jsonb WHERE user_id=?",(self.u,))
        self.text('/start')
        self.assertEqual(buttons(self.calls('sendMessage')[-1]['reply_markup'])[0]['callback_data'],'ob:g:0')
        self.text(SETUP)
        self.assertIn('Шаг 1 из 4',self.calls('sendMessage')[-1]['text'])

    def test_card_feedback_and_miss_reasons_edit_in_place(self):
        lid=self.ids[0]
        self.callback(f'feedback:fit:{lid}',message_id=77)
        self.assertEqual(self.calls('sendMessage'),[])
        toast=self.calls('answerCallbackQuery')[-1];self.assertIn('подходит',toast['text'])
        edit=self.calls('editMessageReplyMarkup')[-1]
        self.assertEqual(edit['message_id'],77);self.assertIn('✓ 👍 Подходит',[b['text'] for b in buttons(edit['reply_markup'])])
        self.assertEqual(self.db.execute('SELECT label FROM lead_feedback WHERE lead_id=?',(lid,)).fetchone()['label'],'fit')
        self.callback(f'miss:{lid}',message_id=77)
        reasons=[b['callback_data'] for b in buttons(self.calls('editMessageReplyMarkup')[-1]['reply_markup'])]
        self.assertIn(f'feedback:ad:{lid}',reasons);self.assertNotIn(f'feedback:fit:{lid}',reasons);self.assertIn(f'card:{lid}',reasons)
        self.callback(f'feedback:ad:{lid}',message_id=77)
        self.assertIn('реклама',self.calls('answerCallbackQuery')[-1]['text'])
        self.assertIn('✓ 👎 Реклама',[b['text'] for b in buttons(self.calls('editMessageReplyMarkup')[-1]['reply_markup'])])
        self.callback(f'card:{lid}',message_id=77)
        self.assertIn('✓ 👎 Реклама',[b['text'] for b in buttons(self.calls('editMessageReplyMarkup')[-1]['reply_markup'])])
        self.assertEqual(self.calls('sendMessage'),[])
        self.callback(f'feedback:fit:{self.ids[1]}')
        self.assertIn('не найден',self.calls('answerCallbackQuery')[-1]['text'])
        self.assertIsNone(self.db.execute('SELECT 1 FROM lead_feedback WHERE lead_id=?',(self.ids[1],)).fetchone())
        self.assertEqual(len(self.calls('answerCallbackQuery')),self.clicks)
        # Buttons of cards sent before the redesign keep working.
        self.callback(f'pipeline:menu:{lid}')
        self.assertIn(f'status:working:{lid}',[b['callback_data'] for b in buttons(self.calls('sendMessage')[-1]['reply_markup'])])
        self.callback(f'status:working:{lid}')
        self.assertEqual(self.db.execute('SELECT pipeline_status FROM user_leads WHERE lead_id=?',(lid,)).fetchone()['pipeline_status'],'working')
        self.callback(f'remind:{lid}')
        self.assertTrue(self.db.execute('SELECT 1 FROM lead_reminders WHERE lead_id=?',(lid,)).fetchone())
        self.assertEqual(len(self.calls('answerCallbackQuery')),self.clicks)

    def test_legacy_service_and_filter_toggles_edit_in_place(self):
        self.callback('topic:0',message_id=91)
        self.assertNotIn(list(TOPICS)[0],self.prefs(1)['topics'])
        self.assertEqual(self.calls('editMessageReplyMarkup')[-1]['message_id'],91)
        self.callback('toggle:no_budget',message_id=92)
        self.assertTrue(self.prefs(1)['show_without_budget'])
        self.assertIn('✅ Без бюджета',[b['text'] for b in buttons(self.calls('editMessageReplyMarkup')[-1]['reply_markup'])])
        self.assertEqual(self.calls('sendMessage'),[])

    def test_six_button_menu_toggle_latest_cabinet_help_and_old_labels(self):
        self.text('/start')
        keyboard=self.calls('sendMessage')[-1]['reply_markup']['keyboard']
        self.assertEqual(sum(len(row) for row in keyboard),6)
        self.assertEqual(keyboard[0][0]['text'],MONITOR_OFF)
        self.text(MONITOR_OFF)
        self.assertTrue(self.prefs(1)['monitoring_active'])
        self.assertEqual(self.calls('sendMessage')[-1]['reply_markup']['keyboard'][0][0]['text'],MONITOR_ON)
        self.assertFalse([d for d in self.calls('sendMessage') if d['text'].startswith('🕘')])
        self.text(MONITOR_ON)
        self.assertFalse(self.prefs(1)['monitoring_active'])
        self.assertEqual(self.calls('sendMessage')[-1]['reply_markup']['keyboard'][0][0]['text'],MONITOR_OFF)
        self.text('▶️ Начать');self.assertTrue(self.prefs(1)['monitoring_active'])
        self.text('⏸ Пауза');self.assertFalse(self.prefs(1)['monitoring_active'])
        self.text(LATEST)
        card=self.calls('sendMessage')[-1]
        self.assertIn('https://t.me/test/0',card['text'])
        self.assertEqual(buttons(card['reply_markup'])[0],{'text':'Открыть','url':f'https://signalid.test/app/leads/{self.ids[0]}'})
        self.text(CABINET)
        self.assertEqual(buttons(self.calls('sendMessage')[-1]['reply_markup'])[0]['url'],'https://signalid.test/app')
        with patch.dict(os.environ,{'MINIAPP_ENABLED':'1'}):self.text(CABINET)
        self.assertEqual(buttons(self.calls('sendMessage')[-1]['reply_markup'])[0]['web_app'],{'url':'https://signalid.test/tg'})
        with patch.dict(os.environ,{'WEB_PUBLIC_URL':''}):self.text(CABINET)
        self.assertIn('keyboard',self.calls('sendMessage')[-1]['reply_markup'])
        self.text(HELP)
        help_buttons=buttons(self.calls('sendMessage')[-1]['reply_markup'])
        self.assertEqual(help_buttons[0]['callback_data'],'stats')
        self.assertIn({'text':'Поддержка','url':'https://t.me/signalid_help'},help_buttons)
        self.callback('stats')
        self.assertIn('Качество отбора',self.calls('sendMessage')[-1]['text'])
        self.text('🔌 Аккаунт')
        moved=self.calls('sendMessage')[-1]
        self.assertIn('https://signalid.test/app/telegram',moved['text'])
        self.assertIn(LATEST,[b['text'] for row in moved['reply_markup']['keyboard'] for b in row])
        self.text('📈 Статистика')
        self.assertIn('Воронка',self.calls('sendMessage')[-1]['text'])

    def test_latest_without_leads_and_suspended_access_buttons(self):
        with self.db:self.db.execute('DELETE FROM user_leads WHERE user_id=?',(self.u,))
        self.text(LATEST)
        self.assertIn('Пока нет присланных заказов',self.calls('sendMessage')[-1]['text'])
        with self.db:self.db.execute("UPDATE subscriptions SET ends_at=now()-interval '1 minute' WHERE user_id=?",(self.u,))
        self.text(LATEST)
        blocked=self.calls('sendMessage')[-1]
        self.assertIn('Доступ приостановлен',blocked['text'])
        self.assertEqual(buttons(blocked['reply_markup']),[{'text':'Выбрать тариф','url':'https://signalid.test/app/billing'},
                                                          {'text':'Поддержка','url':'https://t.me/signalid_help'}])
        self.callback(f'feedback:fit:{self.ids[0]}')
        self.assertIn('Доступ приостановлен',self.calls('sendMessage')[-1]['text'])
        self.assertEqual(len(self.calls('answerCallbackQuery')),1)
        self.text('💳 Подписка')
        self.assertEqual(buttons(self.calls('sendMessage')[-1]['reply_markup'])[0]['text'],'Выбрать тариф')

    def test_trial_reminders_fire_once_per_window(self):
        with self.db:
            self.db.execute("UPDATE subscriptions SET ends_at=now()+interval '36 hours' WHERE user_id=?",(self.u,))
            self.db.execute("UPDATE subscriptions SET ends_at=now()+interval '5 days' WHERE user_id=?",(self.other,))
        sent=[];send=lambda method,data:sent.append(data)
        self.assertEqual(notify_subscriptions(self.db,send),1)
        self.assertEqual(sent[0]['chat_id'],'1');self.assertIn('Пробный период закончится',sent[0]['text'])
        self.assertEqual(buttons(sent[0]['reply_markup']),[{'text':'Выбрать тариф','url':'https://signalid.test/app/billing'},
                                                          {'text':'Поддержка','url':'https://t.me/signalid_help'}])
        self.assertEqual(notify_subscriptions(self.db,send),0)
        with self.db:self.db.execute("UPDATE subscriptions SET ends_at=now()+interval '10 hours' WHERE user_id=?",(self.u,))
        with patch.dict(os.environ,{'MINIAPP_ENABLED':'1'}):self.assertEqual(notify_subscriptions(self.db,send),1)
        self.assertIn('последний день',sent[-1]['text'])
        self.assertEqual(buttons(sent[-1]['reply_markup'])[0]['web_app'],{'url':'https://signalid.test/tg/billing'})
        self.assertEqual(notify_subscriptions(self.db,send),0)
        with self.db:self.db.execute("UPDATE subscriptions SET status='active',plan_code='pro',ends_at=now()+interval '30 hours' WHERE user_id=?",(self.u,))
        self.assertEqual(notify_subscriptions(self.db,send),1)
        self.assertIn('«Профи»',sent[-1]['text'])
        def broken(method,data):raise RuntimeError('network')
        with self.db:self.db.execute("UPDATE subscriptions SET ends_at=now()+interval '40 hours' WHERE user_id=?",(self.other,))
        self.assertEqual(notify_subscriptions(self.db,broken),0)
        self.assertEqual(notify_subscriptions(self.db,send),0)

    def test_reminder_and_assignment_messages_open_the_lead(self):
        from leadgen.reminders import schedule,deliver_due
        from leadgen.team_notifications import deliver_assignments
        lid=self.ids[0]
        schedule(self.db,self.u,lid,datetime.now(timezone.utc)+timedelta(hours=1))
        with self.db:
            self.db.execute("UPDATE lead_reminders SET due_at=now()-interval '1 second' WHERE user_id=?",(self.u,))
            self.db.execute('''INSERT INTO lead_assignments(workspace_id,owner_user_id,lead_id,assignee_user_id,assigned_by)
                VALUES(?,?,?,?,?)''',(self.ws,self.u,lid,self.u,self.u))
        sent=[]
        with patch.dict(os.environ,{'MINIAPP_ENABLED':'1'}):
            self.assertEqual(deliver_due(self.db,lambda method,data:sent.append(data)),1)
        self.assertEqual(deliver_assignments(self.db,lambda method,data:sent.append(data)),1)
        self.assertEqual(buttons(sent[0]['reply_markup']),[{'text':'Открыть','web_app':{'url':f'https://signalid.test/tg/leads/{lid}'}}])
        self.assertEqual(buttons(sent[1]['reply_markup']),[{'text':'Открыть','url':f'https://signalid.test/app/leads/{lid}'}])

    def test_web_login_registration_records_consent(self):
        from leadgen.web_auth import begin_login
        token,_,code=begin_login(self.db,'Safari · macOS')
        self.text('/start web_'+token,uid=5)
        choices=[b['callback_data'] for b in buttons(self.calls('sendMessage')[-1]['reply_markup'])]
        self.assertIn(f'webok:{token}:{code}',choices)
        self.callback(f'webok:{token}:{code}',uid=5)
        user=self.db.execute('SELECT * FROM app_users WHERE telegram_user_id=5').fetchone()
        self.assertIsNotNone(user['terms_accepted_at'])
        self.assertIn('Вход подтверждён',self.calls('sendMessage')[-1]['text'])

    def test_reply_page_prefills_selected_variant(self):
        lid=self.ids[0]
        with self.db:
            self.db.execute('UPDATE leads SET url=?,payload=? WHERE id=?',('https://t.me/business_chat/101',json.dumps(dict(
                source='Telegram: test',url='https://t.me/business_chat/101',title='Нужен сайт 0',text='Нужен сайт',
                published=datetime.now(timezone.utc).isoformat())),lid))
            self.db.execute('''INSERT INTO telegram_connections(user_id,telegram_user_id,session_cipher,status,display_name)
                VALUES(?,9001,'encrypted','active','Мой аккаунт')''',(self.u,))
        detail=self.client.get(f'/app/leads/{lid}').text
        self.assertIn(f'/app/leads/{lid}/reply?draft=tpl%3A',detail)
        self.assertNotIn('Ответственный',detail)
        page=self.client.get(f'/app/leads/{lid}/reply?draft=tpl:Короткий').text
        self.assertIn('Быстрый шаблон «Короткий»',page)
        self.assertIn('Здравствуйте!',page.split('<textarea',1)[1])
        self.assertEqual(self.client.get(f'/app/leads/{lid}/reply?draft=tpl:Нет').status_code,200)
        with patch.dict(os.environ,{'GROQ_API_KEY':'test-key','GROQ_MODEL':'openai/gpt-oss-120b'}):
            self.client.post(f'/app/leads/{lid}/ai-offers',data={'csrf':'test-csrf','project_id':''})
            self.assertIn(f'/app/leads/{lid}/reply?draft=ai%3A',self.client.get(f'/app/leads/{lid}').text)
        page=self.client.get(f'/app/leads/{lid}/reply?draft=ai:Короткий').text
        self.assertIn('Готов обсудить вашу задачу',page)
        self.assertEqual(self.client.get(f'/app/leads/{lid}/reply?draft=tpl:Короткий&project=x').status_code,400)
        self.assertEqual(self.client.get(f'/app/leads/{lid}/reply?draft=tpl:Короткий&project=999999').status_code,404)

    def test_leads_page_quick_links_and_collapsed_controls(self):
        page=self.client.get('/app/leads').text
        for link in ('/app/leads?status=saved','/app/leads?status=working','/app/leads?feedback=fit'):self.assertIn(link,page)
        self.assertIn('<summary>Фильтры</summary>',page)
        self.assertLess(page.index('Нужен сайт 0'),page.index('Скачать CSV'))
        self.assertIn('<summary>Фильтры · 1</summary>',self.client.get('/app/leads?status=saved').text)
        nav=self.client.get('/app/team').text
        self.assertIn('class="nav-more" open',nav)
        self.assertNotIn('class="nav-more" open',self.client.get('/app/leads').text)
        self.assertNotIn('Выбрать все',self.client.get('/app/settings').text)


if __name__=='__main__':unittest.main()
