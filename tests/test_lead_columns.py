"""Typed lead columns (first_seen/sent_at timestamptz, payload jsonb, source), trigram search, stored enrichment."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import test_web_postgres as fixtures
from leadgen.app import ingest
from leadgen.core import Lead, enrich, lead_data, load_lead, message, stored_lead
from leadgen.database import db_open

CONFIG={'min_budget':5000,'max_age_hours':72}
MIGRATIONS=Path('supabase/migrations')
TYPED='20261014140000_typed_lead_columns.sql'
INDEXES='20261014150000_lead_search_indexes.sql'


def fresh_lead(url='https://example.org/1',title='Нужен Telegram бот',text='Срочно нужен бот для заявок, бюджет 30 000 ₽'):
    return Lead(source='Telegram: тест',url=url,title=title,text=text,
                published=datetime.now(timezone.utc).isoformat(),budget_text='30 000 ₽')


class StoredEnrichment(unittest.TestCase):
    def test_lead_data_accepts_jsonb_dict_and_sqlite_text(self):
        data=fresh_lead().data()
        for value in (data,json.dumps(data,ensure_ascii=False),json.dumps(data).encode()):
            self.assertEqual(lead_data(value),data)
        self.assertIsNot(lead_data(data),data)

    def test_stored_lead_reuses_stored_scores(self):
        lead=enrich(fresh_lead());payload=lead.data()|{'score':7,'summary':'сохранённое','temperature':'холодный'}
        with patch('leadgen.core.enrich') as again:
            restored=stored_lead(payload)
        again.assert_not_called()
        self.assertEqual((restored.score,restored.summary,restored.temperature),(7,'сохранённое','холодный'))

    def test_stored_lead_enriches_rows_stored_without_scores(self):
        payload=fresh_lead().data()
        self.assertEqual(stored_lead(json.dumps(payload)),enrich(Lead(**payload)))

    def test_message_reuses_stored_scores_and_enriches_bare_leads(self):
        stored=enrich(fresh_lead())
        with patch('leadgen.core.enrich') as again:message(stored,'запрос')
        again.assert_not_called()
        self.assertIn('/100',message(fresh_lead(),'запрос'))

    def test_sqlite_ingest_stores_enriched_text_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            db=db_open(Path(folder)/'leads.sqlite')
            lead=fresh_lead();bare=Lead(**lead.data())
            with db:ingest(db,lead,CONFIG)
            row=db.execute('SELECT payload,first_seen FROM leads').fetchone()
            self.assertIsInstance(row['payload'],str);self.assertIsInstance(row['first_seen'],str)
            stored=load_lead(row['payload'])
            self.assertEqual(stored,enrich(bare))
            self.assertTrue(stored.summary and stored.temperature)
            db.close()


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
class TypedMigration(unittest.TestCase):
    """The migration on a scratch database holding the formats the code has written."""
    ROWS=[
        ('https://a/1','2026-10-14T09:00:00.123456+00:00','2026-10-14T09:05:00.5+00:00',
         {'source':'Telegram: чат','title':'Нужен сайт','text':'Строка 1\nСтрока "2" \\ 50%','published':'2026-10-14T12:00:00+03:00'}),
        ('https://a/2','2026-10-14T09:00:00+00:00','',{'source':'FL.ru','title':'Бот','text':'эмодзи 🔥','published':None}),
        ('https://a/3','2026-10-14T09:00:00Z',None,{'source':'Kwork','title':'t','text':'x'}),
        ('https://a/4','2026-10-14 09:00:00',' ',{'source':None,'title':'t','text':'x'}),
    ]

    @classmethod
    def setUpClass(cls):
        import psycopg
        url=urlsplit(fixtures.DSN);cls.dsn=urlunsplit(url._replace(path='/leadfinder_migration_test'))
        with psycopg.connect(fixtures.DSN,autocommit=True) as admin:
            admin.execute('DROP DATABASE IF EXISTS leadfinder_migration_test')
            admin.execute('CREATE DATABASE leadfinder_migration_test')

    @classmethod
    def tearDownClass(cls):
        import psycopg
        with psycopg.connect(fixtures.DSN,autocommit=True) as admin:
            admin.execute('DROP DATABASE IF EXISTS leadfinder_migration_test')

    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.connection=psycopg.connect(self.dsn,row_factory=dict_row,autocommit=True,options='-c search_path=leadgen,public')
        connection=self.connection
        connection.execute('DROP SCHEMA IF EXISTS leadgen CASCADE')
        for role in ('anon','authenticated'):
            if not connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
                connection.execute('CREATE ROLE '+role)
        connection.execute((MIGRATIONS/'20260929134042_create_leadgen_schema.sql').read_text(),prepare=False)
        for url,first_seen,sent_at,payload in self.ROWS:
            connection.execute('INSERT INTO leadgen.leads(url,fingerprint,payload,status,reason,first_seen,sent_at) '
                               "VALUES(%s,'fp',%s,'ready','r',%s,%s)",(url,json.dumps(payload,ensure_ascii=False),first_seen,sent_at))

    def tearDown(self):self.connection.close()

    def migrate(self):
        with self.connection.transaction():
            self.connection.execute((MIGRATIONS/TYPED).read_text(),prepare=False)

    def test_converts_every_written_format_and_is_idempotent(self):
        self.migrate();self.migrate()
        types={row['column_name']:row['data_type'] for row in self.connection.execute(
            "SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='leadgen' AND table_name='leads'")}
        self.assertEqual((types['first_seen'],types['sent_at'],types['payload'],types['source']),
                         ('timestamp with time zone','timestamp with time zone','jsonb','text'))
        rows={row['url']:row for row in self.connection.execute('SELECT url,first_seen,sent_at,payload,source FROM leadgen.leads')}
        moment=datetime(2026,10,14,9,tzinfo=timezone.utc)
        self.assertEqual(rows['https://a/1']['first_seen'],moment+timedelta(microseconds=123456))
        self.assertEqual(rows['https://a/1']['sent_at'],moment+timedelta(minutes=5,microseconds=500000))
        for url in ('https://a/2','https://a/3','https://a/4'):self.assertEqual(rows[url]['first_seen'],moment)  # no offset: UTC
        self.assertEqual([rows[u]['sent_at'] for u in ('https://a/2','https://a/3','https://a/4')],[None,None,None])
        for url,_,_,payload in self.ROWS:
            self.assertEqual(rows[url]['payload'],payload);self.assertEqual(rows[url]['source'],payload['source'])
        # source follows later payload updates (generated column).
        self.connection.execute("""UPDATE leadgen.leads SET payload=%s WHERE url='https://a/2'""",(json.dumps({'source':'Новый'}),))
        self.assertEqual(self.connection.execute("SELECT source FROM leadgen.leads WHERE url='https://a/2'").fetchone()['source'],'Новый')
        # Indexes, built the way web_migrate builds no-transaction files.
        from leadgen.web_migrate import concurrent_index_name,run_concurrent_statement,split_statements
        statements=split_statements((MIGRATIONS/INDEXES).read_text())
        self.assertEqual([concurrent_index_name(s) for s in statements],['leads_source','leads_search_trgm'])
        for statement in statements:run_concurrent_statement(self.connection,statement)

    def test_unparsable_value_rolls_the_migration_back(self):
        import psycopg
        self.connection.execute("UPDATE leadgen.leads SET first_seen='вчера' WHERE url='https://a/1'")
        with self.assertRaises(psycopg.errors.InvalidDatetimeFormat):self.migrate()
        self.assertEqual(self.connection.execute("""SELECT data_type FROM information_schema.columns
            WHERE table_schema='leadgen' AND table_name='leads' AND column_name='payload'""").fetchone()['data_type'],'text')


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
class TypedColumnsPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):fixtures.WebPostgres.setUp(self)

    def add_lead(self,lead,first_seen=None,stored=True,owner=None):
        payload=(enrich(lead) if stored else lead).data()
        with self.db:
            lid=self.db.execute('''INSERT INTO leads(url,fingerprint,payload,status,reason,first_seen)
                VALUES(?,?,?,'ready','запрос',?) RETURNING id''',(lead.url,lead.url,json.dumps(payload,ensure_ascii=False),
                first_seen or datetime.now(timezone.utc).isoformat())).fetchone()['id']
            self.db.execute("INSERT INTO user_leads(user_id,lead_id,delivery_status) VALUES(?,?,'sent')",(owner or self.u,lid))
        return lid

    def test_psycopg_returns_typed_values(self):
        row=self.db.execute('SELECT payload,first_seen,source FROM leads WHERE id=?',(self.ids[0],)).fetchone()
        self.assertIsInstance(row['payload'],dict);self.assertIsInstance(row['first_seen'],datetime)
        self.assertEqual(row['source'],'Telegram: test');self.assertEqual(load_lead(row['payload']).source,'Telegram: test')

    def test_search_matches_title_and_text_and_uses_trigram_index(self):
        self.add_lead(fresh_lead('https://ex/a','Лендинг для кофейни','Нужен сайт, бюджет 50% предоплата'))
        self.add_lead(fresh_lead('https://ex/b','Telegram-бот для записи','Нужен бот'))
        def titles(query):return self.client.get('/app/leads',query_string={'q':query}).text
        self.assertIn('Лендинг для кофейни',titles('кофейн'))
        self.assertNotIn('Telegram-бот для записи',titles('кофейн'))
        self.assertIn('Лендинг для кофейни',titles('50% пред'))       # text, % is literal
        self.assertNotIn('Лендинг для кофейни',titles('5_%'))
        self.assertIn('Telegram-бот для записи',titles('TELEGRAM-'))     # case-insensitive
        from leadgen.web import LEAD_SEARCH_TEXT,like_pattern
        with self.db:
            self.db.execute('SET LOCAL enable_seqscan=off')
            plan='\n'.join(row['QUERY PLAN'] for row in self.db.execute(
                "EXPLAIN SELECT id FROM leads l WHERE "+LEAD_SEARCH_TEXT+" ILIKE ? ESCAPE '\\'",(like_pattern('кофейн'),)).fetchall())
        self.assertIn('leads_search_trgm',plan)

    def test_source_list_and_filter_use_source_column(self):
        lead=fresh_lead('https://ex/fl');lead.source='FL.ru';self.add_lead(lead)
        page=self.client.get('/app/leads').text
        self.assertIn('FL.ru',page);self.assertIn('Telegram: test',page)
        filtered=self.client.get('/app/leads',query_string={'source':'FL.ru'}).text
        self.assertIn(lead.title,filtered);self.assertNotIn('Нужен сайт 0',filtered)

    def test_date_filters_compare_timestamps(self):
        old=self.add_lead(fresh_lead('https://ex/old','Старый заказ'),first_seen='2026-01-10T23:30:00+00:00')
        page=self.client.get('/app/leads',query_string={'from':'2026-01-10','to':'2026-01-10'}).text
        self.assertIn('Старый заказ',page);self.assertNotIn('Нужен сайт 0',page)
        self.assertNotIn('Старый заказ',self.client.get('/app/leads',query_string={'from':'2026-01-11'}).text)
        self.assertTrue(old)

    def test_lead_pages_use_stored_scores(self):
        lead=enrich(fresh_lead('https://ex/stored'));lead.score_reason='Сохранённое объяснение оценки';lead.score=42
        lid=self.add_lead(lead,stored=False)
        def only_legacy(item,minimum=5000):
            if item.url=='https://ex/stored':raise AssertionError('stored lead enriched on read')
            return enrich(item,minimum)
        with patch('leadgen.core.enrich',side_effect=only_legacy):
            self.assertIn('Сохранённое объяснение оценки',self.client.get(f'/app/leads/{lid}').text)
            self.assertEqual(self.client.get('/app/leads').status_code,200)
            csv=self.client.get('/app/leads/export').text
        self.assertIn('https://ex/stored',csv);self.assertIn(';42;',csv)

    @patch.dict(os.environ,{'INTEGRATIONS_ENABLED':'1'})
    def test_api_serializes_stored_lead_fields(self):
        lead=enrich(fresh_lead('https://ex/api'));lead.score=42;lead.temperature='тёплый'
        lid=self.add_lead(lead,stored=False)
        self.client.post('/app/integrations/tokens',data={'csrf':'test-csrf','name':'CRM','access':'read'})
        with self.client.session_transaction() as state:token=state['new_api_token']
        data=self.client.get('/api/v1/leads',headers={'Authorization':'Bearer '+token}).get_json()['data']
        item=next(row for row in data if row['id']==lid)
        self.assertEqual((item['score'],item['temperature'],item['published'],item['source']),
                         (42,'тёплый',lead.published,'Telegram: тест'))
        legacy=next(row for row in data if row['id']==self.ids[0])     # stored without scores: enriched on read
        self.assertEqual(legacy['score'],enrich(load_lead(self.db.execute('SELECT payload FROM leads WHERE id=?',
                                                                          (self.ids[0],)).fetchone()['payload'])).score)



if __name__=='__main__':
    unittest.main()
