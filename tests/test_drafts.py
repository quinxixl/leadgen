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
