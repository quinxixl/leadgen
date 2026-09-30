import unittest
from datetime import timedelta
from leadgen.projects import project_eligibility
from leadgen.core import classify
from test_leadgen import lead,CONFIG,NOW

P={'enabled':True,'keywords':[],'stop_words':[],'topics':['Сайты','Боты','CRM','Автоматизации'],
   'source_names':[],'min_budget':5000,'min_score':0,'show_without_budget':True,'show_possible_needs':True}


class Projects(unittest.TestCase):
    def test_words_exclusions_and_sources(self):
        self.assertTrue(project_eligibility(lead(),CONFIG,P)[0])
        for change in ({'enabled':False},{'keywords':['wordpress']},{'stop_words':['бота']},{'source_names':['another']},{'min_score':100}):
            with self.subTest(change=change):self.assertFalse(project_eligibility(lead(),CONFIG,P|change)[0])
    def test_seller_cannot_bypass_project(self):
        self.assertFalse(project_eligibility(lead(text='Создадим сайт, настроим CRM'),CONFIG,P)[0])
    def test_old_possible_need_is_rejected(self):
        item=lead(title='Проблема',text='Менеджеры вручную переносят заявки, теряем клиентов',budget_text='',published=(NOW-timedelta(days=4)).isoformat())
        self.assertEqual(classify(item)[0],'rejected')
    def test_implicit_topic_preferences(self):
        item=lead(title='Проблема',text='Вручную переносим заявки, теряем клиентов',budget_text='')
        self.assertTrue(project_eligibility(item,CONFIG,P)[0])

    def test_possible_need_respects_unknown_budget_switch(self):
        item=lead(title='Проблема',text='Вручную переносим заявки, теряем клиентов',budget_text='')
        self.assertFalse(project_eligibility(item,CONFIG,P|{'show_without_budget':False})[0])
        self.assertFalse(project_eligibility(item,CONFIG,P|{'show_possible_needs':False})[0])

    def test_budget_threshold_does_not_change_shared_score(self):
        from leadgen.app import classify_for_config
        item=lead()
        classify_for_config(item,CONFIG|{'min_budget':5000});score=item.score
        classify_for_config(item,CONFIG|{'min_budget':1000000})
        self.assertEqual(item.score,score)
