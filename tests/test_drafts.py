import unittest
from datetime import datetime, timezone

from leadgen.core import Lead
from leadgen.drafts import reply_drafts


class Drafts(unittest.TestCase):
    def test_three_styles_use_only_message_and_profile(self):
        lead=Lead(source='test',url='https://example.com',title='Нужен сайт для школы',
                  text='Нужен сайт для школы',published=datetime.now(timezone.utc).isoformat())
        drafts=reply_drafts(lead,portfolio='Делаю корпоративные сайты')
        self.assertEqual(list(drafts),['Короткий','Экспертный','Дружелюбный'])
        self.assertTrue(all('Подскажите' in text for text in drafts.values()))
        self.assertIn('Делаю корпоративные сайты',drafts['Экспертный'])
        self.assertNotIn('10 лет',str(drafts))
        self.assertNotIn('гарантир',str(drafts).lower())

    def test_long_untrusted_text_is_bounded(self):
        lead=Lead(source='test',url='https://example.com',title='x'*5000,text='y'*5000,
                  published=datetime.now(timezone.utc).isoformat())
        self.assertTrue(all(len(text)<=1500 for text in reply_drafts(lead,portfolio='z'*5000).values()))

    def test_project_profile_controls_facts_phrases_and_length(self):
        lead=Lead(source='test',url='https://example.com',title='Нужна CRM',text='Теряем заявки',
                  published=datetime.now(timezone.utc).isoformat())
        drafts=reply_drafts(lead,settings={'reply_sender':'Команда Альфа','reply_offer':'Настраиваем CRM',
            'reply_proof':['Внедрили CRM для сервиса'], 'reply_question':'Сколько заявок приходит в день?',
            'reply_signature':'Иван','forbidden_phrases':['лучше'],'reply_max_length':1000})
        self.assertTrue(all(len(text)<=1000 for text in drafts.values()))
        self.assertTrue(all('Команда Альфа' in text and 'Сколько заявок' in text for text in drafts.values()))
        self.assertIn('Внедрили CRM для сервиса',drafts['Экспертный'])
        self.assertNotIn('лучше',str(drafts).lower())
