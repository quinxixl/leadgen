"""/admin/ai: prompt versions, runtime prompt loading, output contract, AI service filter, sandbox, journal, rescore, pause."""
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen import ai_admin, ai_offers, ai_prompts, ai_scoring
from leadgen.core import TOPICS, Lead
from test_leadgen import CONFIG, lead as rule_lead

REQUEST='Нужен сайт для салона красоты, бюджет 50 000 руб, срочно. Пишите @salon_owner'
DRAFT_MARK='ЧЕРНОВИК-МАРКЕР'


class FakeResponse(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*args):self.close();return False


def groq_reply(content,usage=None):
    body={'choices':[{'message':{'content':content if isinstance(content,str) else json.dumps(content,ensure_ascii=False)}}],
          'usage':usage or {'prompt_tokens':100,'completion_tokens':20}}
    return FakeResponse(json.dumps(body,ensure_ascii=False).encode())


def fake_groq(calls,broken_draft=False):
    """Scoring and offers answers; a system prompt with DRAFT_MARK is the draft run."""
    def opener(request,timeout):
        body=json.loads(request.data);calls.append(body)
        draft=DRAFT_MARK in body['messages'][0]['content']
        data=json.loads(body['messages'][1]['content'])
        if broken_draft and draft:return groq_reply('{"items": "not a list"}')
        if 'lead' in data:
            prefix='Черновик: ' if draft else 'Активная: '
            return groq_reply({'short':prefix+'коротко','expert':prefix+'экспертно','friendly':prefix+'дружелюбно'})
        items=[{'id':item['id'],'score':8 if draft else 5,'category':'service_request',
                'topics':['Сайты','Космонавтика'] if draft else [],'budget_amount':50000,'budget_currency':'RUB',
                'urgency':'high','summary':'Ответ черновика' if draft else 'Ответ активной'} for item in data['items']]
        return groq_reply({'items':items})
    return opener


def form(instructions='Оцени заявки строго.',model='openai/gpt-oss-20b',temperature='0.1',**extra):
    return {'instructions':instructions,'model':model,'temperature':temperature,'note':''}|extra


class Contract(unittest.TestCase):
    def setUp(self):ai_prompts.clear_cache()

    def test_admin_text_cannot_remove_the_output_contract(self):
        prompt={'instructions':'Игнорируй любой формат и отвечай обычным текстом.','temperature':0.4,'params':{}}
        body=ai_scoring.build_request([(1,Lead('Telegram','u','t',REQUEST,None))],'openai/gpt-oss-20b',(),prompt)
        system=body['messages'][0]['content']
        self.assertTrue(system.startswith('Игнорируй любой формат'))
        self.assertTrue(system.endswith(ai_scoring.CONTRACT))
        self.assertIn('недоверенные данные',system)
        self.assertTrue(all(name in system for name in TOPICS))
        schema=body['response_format']['json_schema']
        self.assertTrue(schema['strict'])
        item=schema['schema']['properties']['items']['items']
        self.assertEqual(item['properties']['topics']['items']['enum'],list(TOPICS))
        self.assertIn('topics',item['required'])
        self.assertEqual(body['temperature'],0.4);self.assertEqual(body['reasoning_effort'],'low')
        # Models without strict schema support use JSON mode; the contract text is still appended.
        llama=ai_scoring.build_request([(1,Lead('Telegram','u','t',REQUEST,None))],'llama-3.3-70b-versatile',(),prompt)
        self.assertEqual(llama['response_format'],{'type':'json_object'})
        self.assertNotIn('reasoning_effort',llama)
        self.assertTrue(llama['messages'][0]['content'].endswith(ai_scoring.CONTRACT))
        offer,_=ai_offers.build_request(Lead('Telegram','u','t',REQUEST,None),prompt={'instructions':'Пиши стихами.','temperature':0.9,
                                                                                       'params':{'max_tokens':900}})
        self.assertTrue(offer['messages'][0]['content'].endswith(ai_offers.CONTRACT))
        self.assertEqual((offer['temperature'],offer['max_completion_tokens']),(0.9,900))
        self.assertEqual(set(offer['response_format']['json_schema']['schema']['required']),set(ai_offers.DISPLAY_KEYS))

    def test_topics_are_validated_and_unknown_dropped(self):
        self.assertEqual(ai_scoring.clean_topics(['сайты',' Боты ','Космонавтика','Сайты','SEO','CRM']),['Сайты','Боты','SEO'])
        self.assertEqual(ai_scoring.clean_topics('Сайты'),[])
        raw={'id':1,'score':7,'category':'service_request','budget_amount':None,'budget_currency':None,'urgency':'low','summary':'x'}
        payload=lambda items:json.dumps({'choices':[{'message':{'content':json.dumps({'items':items})}}]})
        results,_=ai_scoring.parse_response(payload([raw|{'topics':['Сайты','Мебель']}]),[1])
        self.assertEqual(results[1]['topics'],['Сайты'])
        # Answers without topics (JSON mode, older models) still parse.
        results,_=ai_scoring.parse_response(payload([raw]),[1])
        self.assertEqual(results[1]['topics'],[])
        self.assertEqual(ai_scoring.ai_topics({'status':'skipped','topics':['Сайты']}),[])
        self.assertEqual(ai_scoring.ai_topics({'status':'scored','topics':['Сайты','Нет такой']}),['Сайты'])
        with self.assertRaises(ai_offers.AIOfferError):
            ai_offers.parse_offer_response(json.dumps({'choices':[{'message':{'content':json.dumps(
                {'short':'a','expert':'b','friendly':'c','extra':'d'})}}]}))

    def test_validation_allowlist_and_limits(self):
        data=ai_prompts.validate('scoring',form(param_batch='4',param_daily_limit='',param_fewshot='0',param_other='9'))
        self.assertEqual((data['model'],data['temperature'],data['params']),('openai/gpt-oss-20b',0.1,{'batch':4,'fewshot':0}))
        for bad in (form(model='gpt-4o'),form(instructions=''),form(instructions='x'*8001),form(temperature='1.5'),
                    form(temperature='abc'),form(param_batch='11'),form(param_batch='-1')):
            with self.subTest(bad=bad),self.assertRaises(ValueError):ai_prompts.validate('scoring',bad)
        with self.assertRaises(ValueError):ai_prompts.validate('poetry',form())
        self.assertEqual(ai_prompts.validate('offers',form(param_max_tokens='900'))['params'],{'max_tokens':900})
        self.assertEqual(set(ai_prompts.MODELS),{'openai/gpt-oss-20b','openai/gpt-oss-120b','llama-3.3-70b-versatile','llama-3.1-8b-instant'})

    def test_content_hash_covers_every_editable_field(self):
        base=ai_prompts.content_hash('scoring','A','openai/gpt-oss-20b',0,{'batch':3})
        self.assertEqual(base,ai_prompts.content_hash('scoring','A','openai/gpt-oss-20b',0.0,{'batch':3}))
        for changed in (('scoring','B','openai/gpt-oss-20b',0,{'batch':3}),('scoring','A','openai/gpt-oss-120b',0,{'batch':3}),
                        ('scoring','A','openai/gpt-oss-20b',0.1,{'batch':3}),('scoring','A','openai/gpt-oss-20b',0,{'batch':4}),
                        ('offers','A','openai/gpt-oss-20b',0,{})):
            self.assertNotEqual(base,ai_prompts.content_hash(*changed))

    def test_params_override_env_defaults_and_caps(self):
        with patch.dict(os.environ,{'AI_SCORING_BATCH':'5','AI_SCORING_DAILY_LIMIT':'100','AI_SCORING_FEWSHOT':'1'}):
            self.assertEqual((ai_scoring.batch_size(),ai_scoring.daily_limit(),ai_scoring.fewshot_count()),(5,100,3))
            prompt={'params':{'batch':2,'daily_limit':5000,'fewshot':1}}
            self.assertEqual((ai_scoring.batch_size(prompt),ai_scoring.daily_limit(prompt),ai_scoring.fewshot_count(prompt)),(2,100,1))
            self.assertEqual(ai_scoring.daily_limit({'params':{'daily_limit':10}}),10)
        with patch.dict(os.environ,{'AI_SCORING_FEWSHOT':'0'}):
            self.assertEqual(ai_scoring.fewshot_count({'params':{'fewshot':5}}),0)

    def test_factory_is_used_without_database(self):
        prompt=ai_prompts.active_prompt(None,'scoring')
        self.assertEqual((prompt['source'],prompt['id'],prompt['instructions']),('factory',None,ai_scoring.FACTORY_INSTRUCTIONS))
        self.assertEqual(ai_prompts.active_prompt(None,'offers')['instructions'],ai_offers.FACTORY_INSTRUCTIONS)
        self.assertFalse(ai_prompts.paused(None))


class ServiceFilter(unittest.TestCase):
    """AI-detected services replace keyword services when matching the user's or a project's services."""
    def test_ai_topics_replace_keyword_topics(self):
        from leadgen.product import eligible_for_user
        prefs={'show_without_budget':True,'show_possible_needs':True}
        bot=rule_lead()  # keyword service: «Боты»
        self.assertTrue(eligible_for_user(bot,CONFIG|{'topics':['Боты']},prefs)[0])
        allowed,reason=eligible_for_user(bot,CONFIG|{'topics':['Боты']},prefs,['CRM'])
        self.assertFalse(allowed);self.assertEqual(reason,'AI: услуга не выбрана — CRM')
        allowed,reason=eligible_for_user(bot,CONFIG|{'topics':['CRM']},prefs,['CRM','Автоматизации'])
        self.assertTrue(allowed);self.assertTrue(reason.startswith('AI: услуга CRM'))
        # Empty AI topics fall back to the keyword rules.
        self.assertEqual(eligible_for_user(bot,CONFIG|{'topics':['CRM']},prefs,[]),(False,'направление выключено'))
        # Other rules still apply with AI topics: a seller offer stays rejected.
        seller=rule_lead(text='Создадим бота под ключ, пишите')
        self.assertFalse(eligible_for_user(seller,CONFIG|{'topics':['Боты']},prefs,['Боты'])[0])

    def test_projects_use_ai_topics(self):
        from leadgen.projects import project_eligibility
        project={'enabled':True,'keywords':[],'stop_words':[],'topics':['CRM'],'source_names':[],'min_budget':5000,
                 'min_score':0,'show_without_budget':True,'show_possible_needs':True}
        self.assertFalse(project_eligibility(rule_lead(),CONFIG,project)[0])
        self.assertTrue(project_eligibility(rule_lead(),CONFIG,project,['CRM'])[0])
        self.assertFalse(project_eligibility(rule_lead(),CONFIG,project|{'topics':['Боты']},['CRM'])[0])


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
class AIAdminPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        ai_prompts.clear_cache();self.addCleanup(ai_prompts.clear_cache)
        with self.db:
            self.db.execute('DELETE FROM settings WHERE key LIKE ? OR key=?',('rl:%',ai_prompts.PAUSE_KEY))
            self.db.execute('DELETE FROM user_leads')
            self.db.execute('UPDATE user_preferences SET monitoring_active=true')
        self.env=patch.dict(os.environ,{'GROQ_API_KEY':'test-key','WEB_ADMIN_TELEGRAM_IDS':'1','AI_SCORING_WAIT_SECONDS':'25',
                                        'WEB_PUBLIC_URL':'','AI_SCORING_DAILY_LIMIT':'2000','AI_SCORING_FEWSHOT':'1'})
        self.env.start();self.addCleanup(self.env.stop)
        self.calls=[];self.app.config['GROQ_OPENER']=fake_groq(self.calls)
        self.addCleanup(self.app.config.pop,'GROQ_OPENER',None)

    # --- helpers ------------------------------------------------------------------------------------
    def add_lead(self,url,text=REQUEST,source='Telegram: test',first_seen=None):
        now=first_seen or datetime.now(timezone.utc).isoformat()
        payload=dict(source=source,url=url,title=text[:60],text=text,published=datetime.now(timezone.utc).isoformat(),budget_text=text)
        with self.db:
            return self.db.execute("INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen) VALUES(?,?,?,'ready','',?) RETURNING id",
                (url,url,json.dumps(payload,ensure_ascii=False),now)).fetchone()['id']

    def add_score(self,lead_id,status='scored',category='service_request',topics=(),score=7,error='',when=None):
        with self.db:
            self.db.execute('''INSERT INTO lead_ai_scores(lead_id,status,score,category,topics,urgency,summary,error,input_tokens,output_tokens,updated_at)
                VALUES(?,?,?,?,?::jsonb,?,?,?,90,10,coalesce(?::timestamptz,now()))''',
                (lead_id,status,score if status=='scored' else None,category if status!='failed' else None,
                 json.dumps(list(topics),ensure_ascii=False),'low' if status=='scored' else None,'Сводка '+str(lead_id),error,when))

    def draft(self,instructions='Оцени заявки. '+DRAFT_MARK,task='scoring',**extra):
        data=ai_prompts.validate(task,form(instructions=instructions,**extra))
        return ai_prompts.create_draft(self.db,task,data,self.u)

    def row(self,prompt_id):return ai_prompts.get(self.db,prompt_id)

    def pass_test(self,prompt_id):
        ai_prompts.mark_tested(self.db,prompt_id,ai_prompts.row_hash(self.row(prompt_id)))

    def deliver(self):
        from leadgen.product import deliver_registered
        sent=[];deliver_registered(self.db,{'min_budget':0,'max_age_hours':72,'sources':[]},
                                   lambda method,data:sent.append(data),limit_per_user=20)
        return sent

    def reason(self,user_id,lead_id):
        row=self.db.execute('SELECT delivery_status,filter_reason FROM user_leads WHERE user_id=? AND lead_id=?',(user_id,lead_id)).fetchone()
        return (row['delivery_status'],row['filter_reason']) if row else None

    def as_other(self):
        with self.client.session_transaction() as state:state.update(user_id=self.other)

    # --- versions -------------------------------------------------------------------------------------
    def test_activation_requires_a_fresh_sandbox_pass_and_keeps_one_active(self):
        first=self.draft()
        with self.assertRaisesRegex(ValueError,'песочнице'):ai_prompts.activate(self.db,first)
        self.pass_test(first)
        self.assertTrue(ai_prompts.test_current(self.row(first)))
        self.assertEqual(ai_prompts.activate(self.db,first)['status'],'active')
        with self.assertRaisesRegex(ValueError,'уже активна'):ai_prompts.activate(self.db,first)
        # Active versions are not editable.
        self.assertFalse(ai_prompts.update_draft(self.db,first,ai_prompts.validate('scoring',form())))
        second=self.draft('Вторая версия '+DRAFT_MARK)
        self.assertEqual(self.row(second)['version'],2)
        self.pass_test(second)
        # Any edit voids the test.
        ai_prompts.update_draft(self.db,second,ai_prompts.validate('scoring',form(instructions='Правка '+DRAFT_MARK)))
        self.assertIsNone(self.row(second)['test_passed_at'])
        with self.assertRaisesRegex(ValueError,'песочнице'):ai_prompts.activate(self.db,second)
        # A change that bypasses the editor is caught by the content hash.
        self.pass_test(second)
        with self.db:self.db.execute("UPDATE ai_prompts SET temperature=0.9 WHERE id=?",(second,))
        with self.assertRaisesRegex(ValueError,'изменился после проверки'):ai_prompts.activate(self.db,second)
        with self.db:self.db.execute("UPDATE ai_prompts SET temperature=0.1 WHERE id=?",(second,))
        ai_prompts.activate(self.db,second)
        statuses={row['id']:row['status'] for row in ai_prompts.versions(self.db,'scoring')}
        self.assertEqual(statuses,{first:'archived',second:'active'})
        # Rollback: a previously active version goes live again without a new test.
        ai_prompts.activate(self.db,first)
        self.assertEqual(ai_prompts.current_active(self.db,'scoring')['id'],first)
        self.assertEqual(self.db.execute("SELECT count(*) AS n FROM ai_prompts WHERE status='active'").fetchone()['n'],1)
        # The database refuses a second active version per task.
        with self.assertRaises(Exception):
            with self.db:self.db.execute("UPDATE ai_prompts SET status='active' WHERE id=?",(second,))
        # Offers versions are independent.
        offers=self.draft('Пиши отклики. '+DRAFT_MARK,task='offers');self.pass_test(offers);ai_prompts.activate(self.db,offers)
        self.assertEqual(ai_prompts.current_active(self.db,'scoring')['id'],first)
        self.assertTrue(ai_prompts.reset_to_factory(self.db,'scoring'))
        self.assertIsNone(ai_prompts.current_active(self.db,'scoring'))
        self.assertEqual(ai_prompts.active_prompt(self.db,'scoring')['source'],'factory')
        self.assertEqual(ai_prompts.active_prompt(self.db,'offers')['id'],offers)

    def test_runtime_uses_active_version_with_cache_and_factory_fallback(self):
        self.assertEqual(ai_prompts.active_prompt(self.db,'scoring')['source'],'factory')
        prompt_id=self.draft(model='openai/gpt-oss-120b',temperature='0.2',param_batch='2',param_fewshot='0')
        self.pass_test(prompt_id)
        with self.db:  # Activated by another process: this one keeps its cached answer for up to a minute.
            self.db.execute("UPDATE ai_prompts SET status='active',activated_at=now() WHERE id=?",(prompt_id,))
        self.assertEqual(ai_prompts.active_prompt(self.db,'scoring')['source'],'factory')
        ai_prompts.clear_cache()
        prompt=ai_prompts.active_prompt(self.db,'scoring')
        self.assertEqual((prompt['id'],prompt['model'],prompt['temperature'],prompt['params']),
                         (prompt_id,'openai/gpt-oss-120b',0.2,{'batch':2,'fewshot':0}))
        leads=[self.add_lead(f'https://t.me/test/rt{i}') for i in range(3)]
        seen=[]
        def scorer(batch,examples,model=None,prompt=None):
            seen.append((len(batch),examples,model,prompt['id']))
            return {lead_id:{'score':8,'category':'service_request','topics':['Сайты','Мебель'],'budget_amount':None,
                             'budget_currency':None,'urgency':'low','summary':'s'} for lead_id,_ in batch},{}
        ai_scoring.score_pending(self.db,scorer)
        self.assertEqual(seen,[(2,[],'openai/gpt-oss-120b',prompt_id)])
        row=self.db.execute('SELECT status,topics,prompt_id,model FROM lead_ai_scores WHERE lead_id=?',(leads[-1],)).fetchone()
        self.assertEqual((row['status'],row['topics'],row['prompt_id'],row['model']),('scored',['Сайты'],prompt_id,'openai/gpt-oss-120b'))
        # The real client sends the version's instructions followed by the code contract.
        captured=[]
        def opener(request,timeout):
            captured.append(json.loads(request.data))
            return fake_groq([])(request,timeout)
        ai_scoring.groq_score([(1,Lead('Telegram','u','t',REQUEST,None))],api_key='k',opener=opener,prompt=prompt)
        system=captured[0]['messages'][0]['content']
        self.assertTrue(system.startswith('Оцени заявки строго') or system.startswith('Оцени заявки.'))
        self.assertTrue(system.endswith(ai_scoring.CONTRACT));self.assertEqual(captured[0]['model'],'openai/gpt-oss-120b')
        # A version whose model left the allowlist is ignored in favour of the factory prompt.
        with self.db:self.db.execute("UPDATE ai_prompts SET model='retired-model' WHERE id=?",(prompt_id,))
        ai_prompts.clear_cache()
        self.assertEqual(ai_prompts.active_prompt(self.db,'scoring')['source'],'factory')

    def test_offers_use_active_version(self):
        prompt_id=self.draft('Пиши отклики коротко. '+DRAFT_MARK,task='offers',model='openai/gpt-oss-20b',temperature='0.5')
        self.pass_test(prompt_id);ai_prompts.activate(self.db,prompt_id)
        captured=[]
        def generator(lead,profile_services='',portfolio='',settings=None,api_key=None,model=None,opener=None,user_id=None,prompt=None):
            captured.append((model,prompt['id']))
            return fixtures.fake_ai_generator(lead)
        with self.db:self.db.execute("INSERT INTO user_leads(user_id,lead_id,delivery_status) VALUES(?,?,'sent')",(self.u,self.ids[0]))
        lead=Lead(**json.loads(self.db.execute('SELECT payload FROM leads WHERE id=?',(self.ids[0],)).fetchone()['payload']))
        ai_offers.generate_for_lead(self.db,self.ws,self.u,self.u,self.ids[0],lead,generator=generator)
        self.assertEqual(captured,[('openai/gpt-oss-20b',prompt_id)])
        self.assertEqual(self.db.execute('SELECT model FROM ai_offer_generations ORDER BY id DESC LIMIT 1').fetchone()['model'],'openai/gpt-oss-20b')

    # --- delivery -------------------------------------------------------------------------------------
    def test_delivery_filter_by_ai_category_and_services(self):
        with self.db:
            self.db.execute('''UPDATE user_preferences SET topics=?::jsonb WHERE user_id=?''',(json.dumps(['Боты'],ensure_ascii=False),self.u))
            self.db.execute('''UPDATE user_preferences SET topics=?::jsonb WHERE user_id=?''',(json.dumps(['Сайты'],ensure_ascii=False),self.other))
        # Keyword service of REQUEST: «Сайты».
        bots=self.add_lead('https://t.me/test/ai-bots');self.add_score(bots,topics=['Боты'])
        empty=self.add_lead('https://t.me/test/ai-empty');self.add_score(empty,topics=[])
        vacancy=self.add_lead('https://t.me/test/ai-job');self.add_score(vacancy,category='job',topics=['Сайты'])
        failed=self.add_lead('https://t.me/test/ai-failed');self.add_score(failed,status='failed',error='Groq HTTP 500')
        self.deliver()
        # AI topics intersect the user's services: delivered even though the keywords say «Сайты».
        status,reason=self.reason(self.u,bots)
        self.assertEqual(status,'sent');self.assertTrue(reason.startswith('AI: услуга Боты'),reason)
        self.assertEqual(self.reason(self.other,bots),('filtered','AI: услуга не выбрана — Боты'))
        # Scored without detected services: keyword rules decide.
        self.assertEqual(self.reason(self.u,empty),('filtered','направление выключено'))
        self.assertEqual(self.reason(self.other,empty)[0],'sent')
        # Vacancy verdict hides the lead.
        self.assertEqual(self.reason(self.other,vacancy),('filtered','AI: вакансия'))
        # No AI verdict: unchanged keyword behaviour.
        self.assertEqual(self.reason(self.other,failed)[0],'sent')
        self.assertEqual(self.reason(self.u,failed),('filtered','направление выключено'))

    def test_paused_scoring_delivers_without_waiting_and_makes_no_calls(self):
        fresh=self.add_lead('https://t.me/test/fresh')
        self.assertEqual([item for item in self.deliver() if 'салона' in item['text']],[])
        r=self.client.post('/admin/ai/pause',data={'csrf':'test-csrf','paused':'1','next':'/admin/ai/journal'})
        self.assertEqual((r.status_code,r.headers['Location']),(302,'/admin/ai/journal'))
        self.assertIsNotNone(ai_prompts.pause_info(self.db))
        self.assertFalse(ai_scoring.scoring_enabled(self.db))
        calls=[]
        self.assertEqual(ai_scoring.score_pending(self.db,lambda *a,**k:calls.append(a)),0);self.assertEqual(calls,[])
        cards=[item for item in self.deliver() if 'салона' in item['text']]
        self.assertEqual(len(cards),2)
        self.assertIn('на паузе',self.client.get('/admin/ai').text)
        self.client.post('/admin/ai/pause',data={'csrf':'test-csrf','paused':'0'})
        self.assertIsNone(ai_prompts.pause_info(self.db))
        self.assertTrue(ai_scoring.scoring_enabled(self.db))
        self.assertIsNone(self.db.execute('SELECT 1 FROM lead_ai_scores WHERE lead_id=?',(fresh,)).fetchone())

    # --- admin pages ------------------------------------------------------------------------------------
    def test_only_platform_admins(self):
        prompt_id=self.draft()
        with patch.dict(os.environ,{'WEB_ADMIN_TELEGRAM_IDS':'2'}):
            for path in ('/admin/ai','/admin/ai?task=offers','/admin/ai/journal'):
                self.assertEqual(self.client.get(path).status_code,403,path)
            for path,data in (('/admin/ai/prompts',{'task':'scoring'}|form()),(f'/admin/ai/prompts/{prompt_id}',form()),
                              (f'/admin/ai/prompts/{prompt_id}/activate',{}),(f'/admin/ai/prompts/{prompt_id}/discard',{}),
                              ('/admin/ai/reset',{'task':'scoring'}),('/admin/ai/pause',{'paused':'1'}),
                              ('/admin/ai/sandbox',{'task':'scoring','text':REQUEST}),('/admin/ai/rescore',{'mode':'last','last':'5'})):
                self.assertEqual(self.client.post(path,data={'csrf':'test-csrf'}|data).status_code,403,path)
        self.assertEqual(self.calls,[]);self.assertIsNone(ai_prompts.pause_info(self.db))
        # CSRF is required for admins too.
        self.assertEqual(self.client.post('/admin/ai/pause',data={'paused':'1'}).status_code,400)
        # Signed-out visitors are sent to the login page.
        with self.client.session_transaction() as state:state.clear()
        self.assertEqual(self.client.get('/admin/ai').status_code,302)

    def test_admin_pages_editor_and_versions(self):
        page=self.client.get('/admin').text
        self.assertIn('href="/admin/ai"',page)
        page=self.client.get('/admin/ai').text
        self.assertIn('Заводской промпт',page);self.assertIn('Системная часть',page)
        self.assertIn('недоверенные данные',page);self.assertIn('ai_admin.js',page)
        self.assertNotIn('<script>',page.split('</head>')[1])  # no inline scripts in the page body
        r=self.client.post('/admin/ai/prompts',data={'csrf':'test-csrf','task':'scoring'}|form(model='gpt-4o'))
        self.assertEqual(r.status_code,400);self.assertIn('Выберите модель из списка',r.text)
        self.assertIn('Оцени заявки строго.',r.text)  # typed text is kept
        r=self.client.post('/admin/ai/prompts',data={'csrf':'test-csrf','task':'scoring','param_batch':'3'}|form())
        self.assertEqual(r.status_code,302)
        row=ai_prompts.versions(self.db,'scoring')[0]
        self.assertEqual((row['version'],row['status'],row['params'],row['created_by']),(1,'draft',{'batch':3},self.u))
        page=self.client.get('/admin/ai?task=scoring').text
        self.assertIn('v1',page);self.assertIn('Не проверен',page);self.assertIn('Первый',page)
        # Activation through the page is refused until the sandbox passes.
        self.client.post(f"/admin/ai/prompts/{row['id']}/activate",data={'csrf':'test-csrf'})
        self.assertEqual(self.row(row['id'])['status'],'draft')
        r=self.client.post(f"/admin/ai/prompts/{row['id']}",data={'csrf':'test-csrf'}|form(instructions='Новая редакция'))
        self.assertEqual(r.status_code,302);self.assertEqual(self.row(row['id'])['instructions'],'Новая редакция')
        self.assertIn('Новая редакция',self.client.get(f"/admin/ai?task=scoring&edit={row['id']}").text)
        self.client.post(f"/admin/ai/prompts/{row['id']}/discard",data={'csrf':'test-csrf'})
        self.assertEqual(self.row(row['id'])['status'],'archived')
        self.assertEqual(self.client.get('/admin/ai?task=offers').status_code,200)
        self.assertEqual(self.client.get('/admin/ai?task=poetry').status_code,400)

    def test_sandbox_runs_draft_and_active_side_by_side_and_marks_the_draft(self):
        lead_id=self.add_lead('https://t.me/test/sandbox')
        with self.db:self.db.execute("INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,'fit')",(self.u,lead_id))
        prompt_id=self.draft()
        r=self.client.post('/admin/ai/sandbox',data={'csrf':'test-csrf','task':'scoring','draft':str(prompt_id),
                                                       'with_feedback':'1','last':'3'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(len(self.calls),2)
        systems=sorted(DRAFT_MARK in body['messages'][0]['content'] for body in self.calls)
        self.assertEqual(systems,[False,True])
        self.assertTrue(all(body['messages'][0]['content'].endswith(ai_scoring.CONTRACT) for body in self.calls))
        self.assertTrue(all([item['id'] for item in json.loads(body['messages'][1]['content'])['items']]==[lead_id] for body in self.calls))
        page=r.text
        self.assertIn('Ответ черновика',page);self.assertIn('Ответ активной',page)
        self.assertIn('Схема: ответ корректен',page);self.assertIn('Отброшены услуги не из списка: Космонавтика',page)
        self.assertIn('разметка: Подходит',page);self.assertIn('Сырой ответ модели',page)
        row=self.row(prompt_id)
        self.assertIsNotNone(row['test_passed_at']);self.assertTrue(ai_prompts.test_current(row))
        r=self.client.post(f'/admin/ai/prompts/{prompt_id}/activate',data={'csrf':'test-csrf'})
        self.assertEqual(r.status_code,302);self.assertEqual(self.row(prompt_id)['status'],'active')
        # Pasted text, with a draft that breaks the schema: the draft stays untested.
        broken=self.draft('Сломанный '+DRAFT_MARK)
        self.app.config['GROQ_OPENER']=fake_groq(self.calls,broken_draft=True)
        r=self.client.post('/admin/ai/sandbox',data={'csrf':'test-csrf','task':'scoring','draft':str(broken),'text':REQUEST})
        self.assertIn('Схема: ошибка',r.text);self.assertIn('не прошёл проверку',r.text)
        self.assertIsNone(self.row(broken)['test_passed_at'])

    def test_offers_sandbox_and_rate_limit(self):
        prompt_id=self.draft('Пиши отклики. '+DRAFT_MARK,task='offers')
        r=self.client.post('/admin/ai/sandbox',data={'csrf':'test-csrf','task':'offers','draft':str(prompt_id),'lead_id':str(self.ids[0])})
        self.assertEqual(r.status_code,200)
        self.assertIn('Черновик: коротко',r.text);self.assertIn('Активная: экспертно',r.text)
        self.assertTrue(ai_prompts.test_current(self.row(prompt_id)))
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM ai_offer_generations').fetchone()['n'],0)
        r=self.client.post('/admin/ai/sandbox',data={'csrf':'test-csrf','task':'offers','lead_id':'999999'})
        self.assertEqual(r.status_code,400);self.assertIn('Лид не найден',r.text)
        with patch.object(ai_admin,'SANDBOX_LIMIT',(1,3600)):
            with self.db:self.db.execute('DELETE FROM settings WHERE key LIKE ?',('rl:%',))
            self.client.post('/admin/ai/sandbox',data={'csrf':'test-csrf','task':'scoring','text':REQUEST})
            calls=len(self.calls)
            r=self.client.post('/admin/ai/sandbox',data={'csrf':'test-csrf','task':'scoring','text':REQUEST})
            self.assertEqual(r.status_code,429);self.assertIn('Слишком много попыток',r.text)
            self.assertEqual(len(self.calls),calls)
        with patch.dict(os.environ,{'GROQ_API_KEY':''}):
            r=self.client.post('/admin/ai/sandbox',data={'csrf':'test-csrf','task':'scoring','text':REQUEST})
            self.assertEqual(r.status_code,400);self.assertIn('GROQ_API_KEY',r.text)

    def test_journal_filters_and_quality(self):
        site=self.add_lead('https://t.me/test/j-site',source='Telegram: сайты');self.add_score(site,topics=['Сайты'])
        job=self.add_lead('https://t.me/test/j-job',text='Вакансия разработчика сайтов в штат',source='Биржа');self.add_score(job,category='job')
        old=self.add_lead('https://t.me/test/j-old');self.add_score(old,topics=['SEO'],when='2026-01-05T10:00:00+00:00')
        with self.db:
            self.db.execute("INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,'fit')",(self.u,site))
            self.db.execute("INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,'fit')",(self.u,job))
        def ids(query=''):
            page=self.client.get('/admin/ai/journal'+query);self.assertEqual(page.status_code,200)
            return {lead_id for lead_id in (site,job,old) if f'value="{lead_id}"' in page.text}
        self.assertEqual(ids(),{site,job,old})
        self.assertEqual(ids('?topic=Сайты'),{site})
        self.assertEqual(ids('?category=job'),{job})
        self.assertEqual(ids('?source=Биржа'),{job})
        self.assertEqual(ids('?feedback=1'),{site,job})
        self.assertEqual(ids('?date_from=2026-01-01&date_to=2026-01-31'),{old})
        self.assertEqual(ids('?topic=Неизвестная'),{site,job,old})  # unknown filter values are ignored
        summary=ai_admin.summary(self.db)
        self.assertEqual((summary['week']['total'],summary['week']['agree'],summary['week']['rate']),(2,1,50))
        page=self.client.get('/admin/ai/journal').text
        self.assertIn('50%',page);self.assertIn('Расход по дням',page)
        self.assertIn('Подходит',page);self.assertIn('вакансия',page)

    def test_rescore_resets_rows_and_scorer_picks_them_up(self):
        old_time=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
        old=self.add_lead('https://t.me/test/r-old',first_seen=old_time);self.add_score(old,topics=['SEO'])
        other=self.add_lead('https://t.me/test/r-other');self.add_score(other)
        r=self.client.post('/admin/ai/rescore',data={'csrf':'test-csrf','mode':'selected','lead_id':[str(old)]})
        self.assertEqual(r.status_code,302)
        row=self.db.execute('SELECT * FROM lead_ai_scores WHERE lead_id=?',(old,)).fetchone()
        self.assertEqual((row['status'],row['score'],row['category'],row['topics'],row['attempts']),('pending',None,None,[],0))
        self.assertEqual(self.db.execute('SELECT status FROM lead_ai_scores WHERE lead_id=?',(other,)).fetchone()['status'],'scored')
        # A pending (rescored) row is not an AI verdict: delivery keeps the keyword rules.
        self.assertEqual(ai_scoring.ai_topics(ai_scoring.fetch_scores(self.db,[old])[old]),[])
        seen=[]
        def scorer(batch,examples,model=None,prompt=None):
            seen.extend(lead_id for lead_id,_ in batch)
            return {lead_id:{'score':9,'category':'service_request','topics':['Сайты'],'budget_amount':None,
                             'budget_currency':None,'urgency':'low','summary':'новая'} for lead_id,_ in batch},{}
        ai_scoring.score_pending(self.db,scorer,limit=10)
        self.assertIn(old,seen)  # older than the age window, still picked up
        row=self.db.execute('SELECT status,score,topics FROM lead_ai_scores WHERE lead_id=?',(old,)).fetchone()
        self.assertEqual((row['status'],row['score'],row['topics']),('scored',9,['Сайты']))
        # Last N, capped; leads without a row get one.
        fresh=self.add_lead('https://t.me/test/r-fresh')
        self.client.post('/admin/ai/rescore',data={'csrf':'test-csrf','mode':'last','last':'2'})
        pending={row['lead_id'] for row in self.db.execute("SELECT lead_id FROM lead_ai_scores WHERE status='pending'").fetchall()}
        self.assertEqual(pending,{fresh,other})
        r=self.client.post('/admin/ai/rescore',data={'csrf':'test-csrf','mode':'last','last':'500'})
        self.assertIn('от 1 до 200',self.client.get('/admin/ai/journal').text)
        self.assertLessEqual(ai_admin.rescore(self.db,last=10**6),ai_admin.RESCORE_MAX)


if __name__=='__main__':unittest.main()
