"""Shared AI lead scoring with a mocked LLM: prefilter, strict parsing, per-user filters and delivery fallback."""
import io
import json
import os
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen import ai_scoring
from leadgen.ai_scoring import (ScoringError, build_request, card_lines, groq_score, personal_score, prefilter,
                                score_pending, user_allows)
from leadgen.core import Lead, message

NOW=datetime.now(timezone.utc).isoformat()
REQUEST='Нужен сайт для салона красоты, бюджет 50 000 руб, срочно. Пишите @salon_owner'


def lead(text,title=None):
    return Lead(source='Telegram: test',url='https://t.me/test/1',title=title or text[:60],text=text,published=NOW,budget_text=text)


class FakeResponse(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*args):self.close();return False


def opener_for(items,usage=None,capture=None):
    def opener(request,timeout):
        if capture is not None:capture.append((json.loads(request.data),timeout))
        body={'choices':[{'message':{'content':json.dumps({'items':items})}}],
              'usage':usage or {'prompt_tokens':900,'completion_tokens':150}}
        return FakeResponse(json.dumps(body).encode())
    return opener


def item(lead_id,**extra):
    return {'id':lead_id,'score':8,'category':'service_request','budget_amount':50000,'budget_currency':'RUB',
            'urgency':'high','summary':'Сделать сайт для салона красоты'}|extra


class Pure(unittest.TestCase):
    def test_prefilter_settles_junk_without_llm(self):
        self.assertIsNone(prefilter(lead(REQUEST)))
        self.assertEqual(prefilter(lead('Вакансия: frontend разработчик в штат, зарплата 200 000'))['category'],'job')
        self.assertEqual(prefilter(lead('Сделаю сайт под ключ быстро и недорого, пишите в лс'))['category'],'ad')
        self.assertEqual(prefilter(lead('Продам велосипед, почти новый, недорого'))['category'],'other')
        self.assertEqual(prefilter(lead('Нужен сайт'))['category'],'other')

    def test_request_is_strict_json_batch_with_examples(self):
        body=build_request([(7,lead(REQUEST)),(8,lead('Нужен бот '+'x'*5000))],'openai/gpt-oss-20b',
                           [('Нужен лендинг','service_request')])
        self.assertEqual(body['model'],'openai/gpt-oss-20b')
        self.assertTrue(body['response_format']['json_schema']['strict'])
        data=json.loads(body['messages'][1]['content'])
        self.assertEqual([x['id'] for x in data['items']],[7,8])
        self.assertLessEqual(len(data['items'][1]['text']),ai_scoring.MAX_LEAD_CHARS)
        self.assertEqual(data['examples'],[{'text':'Нужен лендинг','category':'service_request'}])
        self.assertIn('недоверенные данные',body['messages'][0]['content'])

    def test_response_is_validated_and_clamped(self):
        calls=[]
        items=[item(1,score=14,budget_currency='rur',summary='  Сайт\n для   салона '+'я'*300),
               item(2,category='poetry'),item(99),item(3,budget_amount=None,budget_currency='USD',urgency='low',score=2)]
        results,usage=groq_score([(1,lead(REQUEST)),(2,lead(REQUEST)),(3,lead(REQUEST))],api_key='k',model='m',
                                 opener=opener_for(items,capture=calls),timeout=7)
        self.assertEqual(set(results),{1,3})
        self.assertEqual(results[1]['score'],10);self.assertEqual(results[1]['budget_currency'],'RUB')
        self.assertLessEqual(len(results[1]['summary']),ai_scoring.SUMMARY_CHARS)
        self.assertNotIn('\n',results[1]['summary'])
        self.assertIsNone(results[3]['budget_amount']);self.assertIsNone(results[3]['budget_currency'])
        self.assertEqual(calls[0][1],7);self.assertEqual(usage['prompt_tokens'],900)

    def test_transport_and_format_errors_raise_scoring_error(self):
        def http_error(request,timeout):raise urllib.error.HTTPError('u',429,'busy',{},None)
        def timeout(request,timeout):raise TimeoutError
        def garbage(request,timeout):return FakeResponse(b'{"choices":[]}')
        for opener in (http_error,timeout,garbage):
            with self.assertRaises(ScoringError):groq_score([(1,lead(REQUEST))],api_key='k',model='m',opener=opener)

    def test_per_user_filter_and_personal_score(self):
        ai={'status':'scored','score':6,'category':'service_request','urgency':'medium','budget_amount':None,
            'budget_currency':None,'summary':'Сайт'}
        site=lead(REQUEST)
        self.assertEqual(personal_score(ai,site,['Сайты']),7)
        self.assertEqual(personal_score(ai,site,['SEO']),6)
        self.assertEqual(user_allows(None,site,{'ai_hide_noise':True,'ai_min_score':9},['Сайты']),(True,''))
        self.assertTrue(user_allows(ai,site,{'ai_hide_noise':True,'ai_min_score':7},['Сайты'])[0])
        self.assertFalse(user_allows(ai,site,{'ai_hide_noise':True,'ai_min_score':8},['Сайты'])[0])
        ad=ai|{'category':'ad'}
        self.assertEqual(user_allows(ad,site,{'ai_hide_noise':True,'ai_min_score':0},['Сайты']),(False,'AI: реклама'))
        self.assertTrue(user_allows(ad,site,{'ai_hide_noise':False,'ai_min_score':0},['Сайты'])[0])

    def test_card_shows_score_budget_urgency_and_summary(self):
        ai={'status':'scored','score':8,'category':'service_request','urgency':'high','budget_amount':50000,
            'budget_currency':'RUB','summary':'Сделать сайт для салона'}
        text=message(lead(REQUEST),'подходит',card_lines(ai,9))
        self.assertIn('🤖 AI: 9/10 · заказ услуги · срочность: высокая · бюджет 50 000 ₽',text)
        self.assertIn('Сделать сайт для салона',text)
        self.assertEqual(card_lines({'status':'failed'}),'')
        self.assertNotIn('🤖',message(lead(REQUEST),'подходит'))

    def test_bot_budget_step_has_ai_filters(self):
        from leadgen.product import next_ai_min,wizard_budget
        _,markup=wizard_budget({'min_budget':0,'show_without_budget':False})
        labels={b['callback_data']:b['text'] for row in markup['inline_keyboard'] for b in row}
        self.assertEqual(labels['ob:ai'],'✅ Скрывать рекламу и вакансии (AI)')
        self.assertEqual(labels['ob:aimin'],'AI-оценка: любая')
        self.assertEqual([next_ai_min(v) for v in (0,4,6,8,3)],[4,6,8,0,0])

    def test_defaults(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual((ai_scoring.wait_seconds(),ai_scoring.max_age_hours(),ai_scoring.scoring_model()),
                             (25,48,'openai/gpt-oss-20b'))

    def test_disabled_without_key(self):
        with patch.dict(os.environ,{'GROQ_API_KEY':''}):
            self.assertEqual(score_pending(None),0)


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
class ScoringPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.WebPostgres.setUp(self)
        self.env=patch.dict(os.environ,{'GROQ_API_KEY':'test-key','AI_SCORING_WAIT_SECONDS':'25','WEB_PUBLIC_URL':''})
        self.env.start()
        with self.db:
            self.db.execute('DELETE FROM user_leads')
            self.db.execute('UPDATE user_preferences SET monitoring_active=true')
        self.request=self.add('https://t.me/test/request',REQUEST)
        self.ad=self.add('https://t.me/test/ad','Нужен сайт? Мы делаем сайты под ключ, наша команда разрабатывает лендинги')
        self.calls=[]

    def tearDown(self):self.env.stop()

    def add(self,url,text):
        now=datetime.now(timezone.utc).isoformat()
        payload=dict(source='Telegram: test',url=url,title=text[:60],text=text,published=now,budget_text=text)
        with self.db:
            return self.db.execute("INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen) VALUES(?,?,?,'ready','',?) RETURNING id",
                (url,url,json.dumps(payload,ensure_ascii=False),now)).fetchone()['id']

    def scorer(self,result):
        def run(batch,examples,model=None):
            self.calls.append(([lead_id for lead_id,_ in batch],examples,model))
            if isinstance(result,Exception):raise result
            return {lead_id:result|{} for lead_id,_ in batch},{'prompt_tokens':600,'completion_tokens':100}
        return run

    def score_row(self,lead_id):
        return self.db.execute('SELECT * FROM lead_ai_scores WHERE lead_id=?',(lead_id,)).fetchone()

    def deliver(self):
        from leadgen.product import deliver_registered
        sent=[];deliver_registered(self.db,fixtures_config(),lambda method,data:sent.append(data),limit_per_user=10)
        return sent

    def test_scored_once_and_shared_card_shows_ai(self):
        # Fresh unscored leads wait for the score instead of going out bare.
        self.assertEqual(self.deliver(),[])
        verdict={'score':8,'category':'service_request','budget_amount':50000.0,'budget_currency':'RUB',
                 'urgency':'high','summary':'Сайт для салона красоты'}
        with self.db:self.db.execute("INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,'fit')",(self.u,self.ids[0]))
        settled=score_pending(self.db,self.scorer(verdict))
        self.assertEqual(len(self.calls),1)
        batch,examples,model=self.calls[0]
        self.assertEqual(settled,len(batch)+1)
        self.assertIn(self.request,batch);self.assertNotIn(self.ad,batch)
        self.assertEqual([category for _,category in examples],['service_request'])
        self.assertEqual(model,'openai/gpt-oss-20b')
        self.assertEqual(self.score_row(self.ad)['status'],'skipped')
        row=self.score_row(self.request)
        self.assertEqual((row['status'],row['score'],row['urgency'],row['input_tokens']),('scored',8,'high',600//len(batch)))
        # Computed once: a second pass makes no LLM call.
        self.assertEqual(score_pending(self.db,self.scorer(verdict)),0);self.assertEqual(len(self.calls),1)
        cards=[item for item in self.deliver() if 'салона' in item['text']]
        self.assertEqual(sorted(item['chat_id'] for item in cards),['1','2'])
        self.assertTrue(all('🤖 AI: 9/10' in item['text'] and 'бюджет 50 000 ₽' in item['text'] for item in cards))
        r=self.client.get(f'/app/leads/{self.request}')
        self.assertEqual(r.status_code,200);self.assertIn('AI 8/10',r.text);self.assertIn('Сайт для салона красоты',r.text)
        self.assertIn('AI 8/10',self.client.get('/app/leads').text)

    def test_ai_noise_hidden_by_default_and_min_score(self):
        with self.db:
            self.db.execute('UPDATE user_preferences SET ai_hide_noise=false WHERE user_id=?',(self.other,))
            self.db.execute('UPDATE user_preferences SET ai_min_score=0')
        score_pending(self.db,self.scorer({'score':3,'category':'ad','budget_amount':None,'budget_currency':None,
                                           'urgency':'low','summary':'Реклама'}))
        cards=[item for item in self.deliver() if 'салона' in item['text']]
        self.assertEqual([item['chat_id'] for item in cards],['2'])
        reason=self.db.execute('SELECT filter_reason FROM user_leads WHERE user_id=? AND lead_id=?',(self.u,self.request)).fetchone()
        self.assertEqual(reason['filter_reason'],'AI: реклама')
        # Settings page stores the per-user filters and re-evaluates filtered leads.
        r=self.client.post('/app/settings',data={'csrf':'test-csrf','min_budget':'0','topics':['Сайты'],'ai_min_score':'5','active':'1'})
        self.assertEqual(r.status_code,302)
        prefs=self.db.execute('SELECT ai_hide_noise,ai_min_score FROM user_preferences WHERE user_id=?',(self.u,)).fetchone()
        self.assertEqual((prefs['ai_hide_noise'],prefs['ai_min_score']),(False,5))
        reason=self.db.execute('SELECT filter_reason FROM user_leads WHERE user_id=? AND lead_id=?',(self.u,self.request)).fetchone()
        self.assertIsNone(reason)
        cards=[item for item in self.deliver() if 'салона' in item['text']]
        self.assertEqual(cards,[])
        reason=self.db.execute('SELECT filter_reason FROM user_leads WHERE user_id=? AND lead_id=?',(self.u,self.request)).fetchone()
        self.assertEqual(reason['filter_reason'],'AI-оценка 4/10 ниже порога 5')

    def test_llm_failure_still_delivers_unscored(self):
        score_pending(self.db,self.scorer(ScoringError('Groq HTTP 500')))
        row=self.score_row(self.request)
        self.assertEqual((row['status'],row['attempts'],row['error']),('failed',1,'Groq HTTP 500'))
        cards=[item for item in self.deliver() if 'салона' in item['text']]
        self.assertEqual(len(cards),2);self.assertTrue(all('🤖' not in item['text'] for item in cards))
        # Failed leads are retried later, not immediately.
        self.assertEqual(score_pending(self.db,self.scorer(ScoringError('x'))),0)

    def test_unscored_lead_goes_out_after_wait(self):
        with patch.dict(os.environ,{'AI_SCORING_WAIT_SECONDS':'0'}):
            cards=[item for item in self.deliver() if 'салона' in item['text']]
        self.assertEqual(len(cards),2)


def fixtures_config():
    return {'min_budget':0,'max_age_hours':72,'sources':[]}


if __name__=='__main__':unittest.main()
