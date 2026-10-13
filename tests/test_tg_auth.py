import hashlib
import hmac
import json
import unittest
from urllib.parse import parse_qsl, urlencode

from leadgen.tg_auth import sign_init_data, validate_init_data

TOKEN = '123456:' + 'A' * 35
NOW = 1_800_000_000
USER = {'id': 4242, 'first_name': 'Анна', 'last_name': 'Петрова', 'username': 'anna', 'language_code': 'ru'}


def init_data(token=TOKEN, auth_date=NOW, **extra):
    fields = {'query_id': 'AAE1', 'user': json.dumps(USER, ensure_ascii=False, separators=(',', ':')),
              'auth_date': str(auth_date)} | extra
    return sign_init_data(fields, token)


class ValidateInitData(unittest.TestCase):
    def test_valid_returns_user_start_param_and_date(self):
        data = validate_init_data(init_data(start_param='lead_77'), TOKEN, now=NOW + 5)
        self.assertEqual(data['user'], USER)
        self.assertEqual(data['start_param'], 'lead_77')
        self.assertEqual(data['auth_date'], NOW)

    def test_signature_follows_telegram_spec(self):
        # Independent re-implementation of core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
        fields = {'auth_date': str(NOW), 'query_id': 'Q', 'user': json.dumps({'id': 1, 'first_name': 'A'})}
        check = '\n'.join(f'{k}={fields[k]}' for k in sorted(fields))
        secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
        raw = urlencode(fields | {'hash': hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})
        self.assertEqual(validate_init_data(raw, TOKEN, now=NOW)['user']['id'], 1)

    def test_missing_start_param_is_empty(self):
        self.assertEqual(validate_init_data(init_data(), TOKEN, now=NOW)['start_param'], '')

    def test_tampered_field_rejected(self):
        fields = dict(parse_qsl(init_data()))
        fields['user'] = fields['user'].replace('4242', '4243')
        with self.assertRaises(ValueError):
            validate_init_data(urlencode(fields), TOKEN, now=NOW)

    def test_added_field_rejected(self):
        with self.assertRaises(ValueError):
            validate_init_data(init_data() + '&start_param=billing', TOKEN, now=NOW)

    def test_tampered_hash_rejected(self):
        fields = dict(parse_qsl(init_data()))
        fields['hash'] = ('0' if fields['hash'][0] != '0' else '1') + fields['hash'][1:]
        with self.assertRaises(ValueError):
            validate_init_data(urlencode(fields), TOKEN, now=NOW)

    def test_non_ascii_hash_rejected_without_crash(self):
        fields = dict(parse_qsl(init_data()))
        fields['hash'] = 'ж' * 64
        with self.assertRaises(ValueError):
            validate_init_data(urlencode(fields), TOKEN, now=NOW)

    def test_missing_hash_rejected(self):
        fields = dict(parse_qsl(init_data()))
        del fields['hash']
        with self.assertRaises(ValueError):
            validate_init_data(urlencode(fields), TOKEN, now=NOW)

    def test_wrong_bot_token_rejected(self):
        with self.assertRaises(ValueError):
            validate_init_data(init_data(token='999:' + 'B' * 35), TOKEN, now=NOW)

    def test_expired_rejected_and_boundary_accepted(self):
        self.assertTrue(validate_init_data(init_data(), TOKEN, now=NOW + 86400))
        with self.assertRaises(ValueError):
            validate_init_data(init_data(), TOKEN, now=NOW + 86401)
        with self.assertRaises(ValueError):
            validate_init_data(init_data(), TOKEN, max_age=60, now=NOW + 61)

    def test_future_date_rejected_beyond_skew(self):
        self.assertTrue(validate_init_data(init_data(auth_date=NOW + 60), TOKEN, now=NOW))
        with self.assertRaises(ValueError):
            validate_init_data(init_data(auth_date=NOW + 61), TOKEN, now=NOW)

    def test_bad_auth_date_rejected(self):
        with self.assertRaises(ValueError):
            validate_init_data(init_data(auth_date='yesterday'), TOKEN, now=NOW)

    def test_missing_or_invalid_user_rejected(self):
        for user in (None, 'not json', json.dumps({'first_name': 'A'}), json.dumps({'id': '5'}),
                     json.dumps({'id': True}), json.dumps({'id': -5}), json.dumps([1])):
            fields = {'auth_date': str(NOW)}
            if user is not None:
                fields['user'] = user
            with self.assertRaises(ValueError, msg=user):
                validate_init_data(sign_init_data(fields, TOKEN), TOKEN, now=NOW)

    def test_duplicate_keys_rejected(self):
        raw = init_data()
        with self.assertRaises(ValueError):
            validate_init_data(raw + '&auth_date=' + str(NOW), TOKEN, now=NOW)

    def test_empty_and_garbage_input_rejected(self):
        for raw in ('', None, 'garbage', '&&&', 'a' * 9000):
            with self.assertRaises(ValueError):
                validate_init_data(raw, TOKEN, now=NOW)
        with self.assertRaises(ValueError):
            validate_init_data(init_data(), '', now=NOW)


if __name__ == '__main__':
    unittest.main()
