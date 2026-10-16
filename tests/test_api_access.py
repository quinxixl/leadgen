"""Public API access rules against a disposable local PostgreSQL database."""
import os
import unittest
from unittest.mock import patch

import test_web_postgres as fixtures
from leadgen import billing


class PlanAccess(unittest.TestCase):
    def test_only_running_non_free_plans_include_integrations(self):
        for code in ('start','pro','team','trial','stub'):
            self.assertTrue(billing.plan_includes_integrations(code),code)
        self.assertFalse(billing.plan_includes_integrations('free'))
        self.assertFalse(billing.plan_includes_integrations('unknown'))


@unittest.skipUnless(fixtures.DSN,'WEB_TEST_DATABASE_URL is not configured')
@patch.dict(os.environ,{'INTEGRATIONS_ENABLED':'1'})
class ApiAccess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.WebPostgres.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.WebPostgres.tearDownClass.__func__(cls)

    def setUp(self):fixtures.WebPostgres.setUp(self)

    def token(self,access='read'):
        self.assertEqual(self.client.post('/app/integrations/tokens',data={
            'csrf':'test-csrf','name':'CRM','access':access}).status_code,302)
        with self.client.session_transaction() as state:
            return {'Authorization':'Bearer '+state['new_api_token']}

    def listed(self,headers):
        response=self.client.get('/api/v1/leads',headers=headers)
        self.assertEqual(response.status_code,200)
        return [row['id'] for row in response.get_json()['data']]

    def test_free_plan_has_no_api_access(self):
        headers=self.token('write')
        self.assertEqual(self.listed(headers),[self.ids[0]])
        with self.db:self.db.execute("UPDATE subscriptions SET status='active',plan_code='free',ends_at=NULL WHERE user_id=?",(self.u,))
        self.assertEqual(self.client.get('/api/v1/leads',headers=headers).status_code,403)
        self.assertEqual(self.client.patch(f'/api/v1/leads/{self.ids[0]}',headers=headers,
                                           json={'status':'won'}).status_code,403)
        # An ended paid period falls back to Free and closes the API as well.
        with self.db:self.db.execute("""UPDATE subscriptions SET status='active',plan_code='pro',
            ends_at=now()-interval '1 day' WHERE user_id=?""",(self.u,))
        self.assertEqual(self.client.get('/api/v1/leads',headers=headers).status_code,403)

    def test_plan_delay_hides_fresh_leads(self):
        headers=self.token('write')
        with patch.dict(billing.LIMITS['trial'],{'delay_minutes':60}):
            self.assertEqual(self.listed(headers),[])
            self.assertEqual(self.client.patch(f'/api/v1/leads/{self.ids[0]}',headers=headers,
                                               json={'status':'won'}).status_code,404)
            with self.db:self.db.execute("UPDATE leads SET first_seen=now()-interval '2 hours' WHERE id=?",(self.ids[0],))
            self.assertEqual(self.listed(headers),[self.ids[0]])
            self.assertEqual(self.client.patch(f'/api/v1/leads/{self.ids[0]}',headers=headers,
                                               json={'status':'won'}).status_code,200)

    def test_token_stops_working_when_creator_loses_access(self):
        with self.db:self.db.execute("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,'admin')",(self.ws,self.other))
        with self.client.session_transaction() as state:state.update(user_id=self.other,workspace_id=self.ws)
        headers=self.token()
        self.assertEqual(self.listed(headers),[self.ids[0]])
        with self.db:self.db.execute("UPDATE workspace_members SET role='viewer' WHERE workspace_id=? AND user_id=?",(self.ws,self.other))
        self.assertEqual(self.client.get('/api/v1/leads',headers=headers).status_code,401)
        with self.db:self.db.execute("UPDATE workspace_members SET role='admin' WHERE workspace_id=? AND user_id=?",(self.ws,self.other))
        self.assertEqual(self.listed(headers),[self.ids[0]])
        with self.db:self.db.execute('DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?',(self.ws,self.other))
        self.assertEqual(self.client.get('/api/v1/leads',headers=headers).status_code,401)

    def test_token_stops_working_when_owner_or_creator_is_blocked(self):
        headers=self.token()
        self.assertEqual(self.listed(headers),[self.ids[0]])
        with self.db:self.db.execute("UPDATE app_users SET status='blocked' WHERE id=?",(self.u,))
        self.assertEqual(self.client.get('/api/v1/leads',headers=headers).status_code,401)


if __name__=='__main__':
    unittest.main()
