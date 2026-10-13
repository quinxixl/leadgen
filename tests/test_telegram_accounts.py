import unittest

from cryptography.fernet import Fernet

from leadgen.telegram_accounts import SessionCipher, TelegramAccountError


class SessionEncryption(unittest.TestCase):
    def test_roundtrip_and_wrong_key(self):
        cipher = SessionCipher(Fernet.generate_key().decode())
        token = cipher.encrypt({'session': 'secret', 'phone': '+79990000000'})
        self.assertNotIn('secret', token)
        self.assertNotIn('+7999', token)
        self.assertEqual(cipher.decrypt(token)['session'], 'secret')
        with self.assertRaises(TelegramAccountError):
            SessionCipher(Fernet.generate_key().decode()).decrypt(token)

    def test_invalid_key_is_rejected(self):
        with self.assertRaises(TelegramAccountError):
            SessionCipher('short')
