"""Personal Telegram login flows against a scripted fake Telegram server.

The real Telethon TelegramClient, QRLogin and sign_in logic run unchanged; only the
network layer (connect, RPC calls, DC switch) is replaced, so these tests check what
the gateway really sends and which auth key / DC each step uses.
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from telethon import TelegramClient, errors
from telethon.crypto import AuthKey
from telethon.tl import functions, types

from leadgen import telegram_accounts
from leadgen.telegram_accounts import TelegramAccountError, TelethonGateway

DC_IP = {2: '149.154.167.51', 4: '149.154.167.91'}


class FakeTelegram:
    """Server-side state: auth keys, QR tokens and login codes."""

    def __init__(self, user_dc=2, password=None):
        self.user = types.User(id=777, first_name='Ника', last_name='Тест', access_hash=1, is_self=True)
        self.user_dc = user_dc
        self.password = password
        self.keys = {}      # auth key bytes -> {'dc': int, 'state': 'new'|'password'|'authorized'}
        self.tokens = {}    # token bytes -> {'key': bytes, 'expires': datetime, 'accepted': bool}
        self.codes = {}     # phone_code_hash -> {'key': bytes, 'code': str}
        self.push_update_on_wait = False
        self.calls = []

    def scan(self, token):
        """The user's phone accepts the QR token (auth.acceptLoginToken)."""
        entry = self.tokens[token]
        assert entry['expires'] > datetime.now(timezone.utc), 'scanned an expired token'
        entry['accepted'] = True
        key = self.keys[entry['key']]
        if key['dc'] == self.user_dc:
            # acceptLoginToken creates the authorization at once; a 2FA account waits for the password.
            key['state'] = 'password' if self.password else 'authorized'

    def expire_tokens(self):
        for entry in self.tokens.values():
            entry['expires'] = datetime.now(timezone.utc) - timedelta(seconds=5)

    def key_state(self, session_string):
        from telethon.sessions import StringSession
        session = StringSession(session_string)
        return session.dc_id, self.keys[session.auth_key.key]['state']

    def _login(self, key, request):
        if self.password:
            key['state'] = 'password'
            raise errors.SessionPasswordNeededError(request=request)
        key['state'] = 'authorized'
        return types.auth.Authorization(user=self.user)

    def handle(self, client, request):
        name = type(request).__name__
        self.calls.append(name)
        key = self.keys.setdefault(client.session.auth_key.key, {'dc': client.session.dc_id, 'state': 'new'})
        assert key['dc'] == client.session.dc_id, 'auth key used on a foreign DC'
        if isinstance(request, (functions.users.GetUsersRequest, functions.updates.GetStateRequest,
                                functions.updates.GetDifferenceRequest)):
            if key['state'] == 'password':
                raise errors.SessionPasswordNeededError(request=request)
            if key['state'] != 'authorized':
                raise errors.AuthKeyUnregisteredError(request=request)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            if isinstance(request, functions.users.GetUsersRequest):
                return [self.user]
            if isinstance(request, functions.updates.GetStateRequest):
                return types.updates.State(pts=1, qts=0, date=now, seq=0, unread_count=0)
            return types.updates.DifferenceEmpty(date=now, seq=0)
        if isinstance(request, functions.auth.ExportLoginTokenRequest):
            if key['state'] == 'password':
                raise errors.SessionPasswordNeededError(request=request)
            for token, entry in self.tokens.items():
                if entry['key'] == client.session.auth_key.key and entry['accepted']:
                    if self.user_dc != client.session.dc_id:
                        return types.auth.LoginTokenMigrateTo(dc_id=self.user_dc, token=token)
                    return types.auth.LoginTokenSuccess(authorization=self._login(key, request))
            token = os.urandom(16)
            expires = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=30)
            self.tokens[token] = {'key': client.session.auth_key.key, 'expires': expires, 'accepted': False}
            return types.auth.LoginToken(expires=expires, token=token)
        if isinstance(request, functions.auth.ImportLoginTokenRequest):
            entry = self.tokens[request.token]
            assert entry['accepted'] and client.session.dc_id == self.user_dc
            return types.auth.LoginTokenSuccess(authorization=self._login(key, request))
        if isinstance(request, functions.auth.SendCodeRequest):
            phone_code_hash = 'hash-%d' % len(self.codes)
            self.codes[phone_code_hash] = {'key': client.session.auth_key.key, 'code': '12345'}
            return types.auth.SentCode(type=types.auth.SentCodeTypeApp(length=5),
                                       phone_code_hash=phone_code_hash,
                                       next_type=types.auth.CodeTypeSms(), timeout=60)
        if isinstance(request, functions.auth.SignInRequest):
            entry = self.codes.get(request.phone_code_hash)
            if not entry or entry['key'] != client.session.auth_key.key:
                raise errors.PhoneCodeExpiredError(request=request)
            if entry['code'] != request.phone_code:
                raise errors.PhoneCodeInvalidError(request=request)
            return self._login(key, request)
        if isinstance(request, functions.account.GetPasswordRequest):
            return types.account.Password(new_algo=types.PasswordKdfAlgoUnknown(),
                                          new_secure_algo=types.SecurePasswordKdfAlgoUnknown(),
                                          secure_random=b'', has_password=True, srp_id=1)
        if isinstance(request, functions.auth.CheckPasswordRequest):
            if key['state'] != 'password':
                raise errors.AuthKeyUnregisteredError(request=request)
            if request.password.A != self.password.encode():
                raise errors.PasswordHashInvalidError(request=request)
            key['state'] = 'authorized'
            return types.auth.Authorization(user=self.user)
        raise AssertionError('unexpected request ' + name)


class FakeClient(TelegramClient):
    server = None

    async def connect(self):
        self._loop = asyncio.get_running_loop()
        if not self.session.server_address:
            self.session.set_dc(2, DC_IP[2], 443)
        if self.session.auth_key is None:
            self.session.auth_key = AuthKey(os.urandom(256))

    async def disconnect(self):
        pass

    async def _switch_dc(self, new_dc):
        self.session.set_dc(new_dc, DC_IP[new_dc], 443)
        self.session.auth_key = AuthKey(os.urandom(256))

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        return self.server.handle(self, request)

    def add_event_handler(self, callback, event=None):
        super().add_event_handler(callback, event)
        if self.server.push_update_on_wait:
            # The phone scans while this request is connected: Telegram pushes UpdateLoginToken.
            for token, entry in list(self.server.tokens.items()):
                if entry['key'] == self.session.auth_key.key and not entry['accepted']:
                    self.server.scan(token)
            asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(callback(types.UpdateLoginToken())))


def fake_check(_password_info, password):
    return types.InputCheckPasswordSRP(srp_id=1, A=password.encode(), M1=b'')


class LoginFlows(unittest.TestCase):
    def setUp(self):
        patches = [patch.dict(os.environ, {'TELEGRAM_API_ID': '12345', 'TELEGRAM_API_HASH': 'a' * 32}),
                   patch('telethon.TelegramClient', FakeClient),
                   patch('telethon.client.auth.pwd_mod.compute_check', fake_check),
                   patch.object(telegram_accounts, 'QR_POLL_SECONDS', 0.2)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def server(self, **kwargs):
        FakeClient.server = FakeTelegram(**kwargs)
        return FakeClient.server

    @staticmethod
    def gateway():
        return TelethonGateway()

    def token(self, state):
        import base64
        return base64.b64decode(state['token'])

    def test_qr_scan_between_polls_with_two_factor_switches_to_password_at_once(self):
        server = self.server(password='cloud-pass')
        state = self.gateway().begin_qr()
        self.assertEqual(self.gateway().wait_qr(state)['pending'], True)
        # The phone scans while no poll is connected: no UpdateLoginToken reaches the server.
        server.scan(self.token(state))
        result = self.gateway().wait_qr(state)
        self.assertTrue(result.get('password_required'), result)
        self.assertEqual(server.key_state(result['state']['session']), (2, 'password'))
        done = self.gateway().verify_password(result['state'], 'cloud-pass')
        self.assertEqual((done['telegram_user_id'], done['display_name']), (777, 'Ника Тест'))
        self.assertEqual(server.key_state(done['session']), (2, 'authorized'))

    def test_qr_scan_during_poll_with_two_factor(self):
        server = self.server(password='cloud-pass')
        state = self.gateway().begin_qr()
        server.push_update_on_wait = True
        result = self.gateway().wait_qr(state)
        self.assertTrue(result.get('password_required'), result)
        self.assertEqual(server.key_state(result['state']['session']), (2, 'password'))

    def test_qr_scan_then_token_expiry_with_two_factor(self):
        server = self.server(password='cloud-pass')
        state = self.gateway().begin_qr()
        server.scan(self.token(state))
        state['expires'] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        result = self.gateway().wait_qr(state)
        self.assertTrue(result.get('password_required'), result)

    def test_qr_dc_migration_with_two_factor_keeps_the_migrated_session(self):
        server = self.server(user_dc=4, password='cloud-pass')
        state = self.gateway().begin_qr()
        server.scan(self.token(state))
        # The DC 2 key is not authorized yet: completion needs re-export + import on DC 4.
        state['expires'] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        result = self.gateway().wait_qr(state)
        self.assertTrue(result.get('password_required'), result)
        self.assertEqual(server.key_state(result['state']['session']), (4, 'password'))
        done = self.gateway().verify_password(result['state'], 'cloud-pass')
        self.assertEqual(server.key_state(done['session']), (4, 'authorized'))

    def test_qr_dc_migration_during_poll_with_two_factor(self):
        server = self.server(user_dc=4, password='cloud-pass')
        state = self.gateway().begin_qr()
        server.push_update_on_wait = True
        result = self.gateway().wait_qr(state)
        self.assertTrue(result.get('password_required'), result)
        self.assertEqual(server.key_state(result['state']['session']), (4, 'password'))

    def test_qr_scan_without_two_factor_between_polls(self):
        server = self.server()
        state = self.gateway().begin_qr()
        server.scan(self.token(state))
        state['expires'] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        result = self.gateway().wait_qr(state)
        self.assertEqual(result.get('telegram_user_id'), 777, result)
        self.assertEqual(server.key_state(result['session']), (2, 'authorized'))

    def test_qr_refresh_keeps_the_auth_key_so_an_old_scan_still_completes(self):
        server = self.server(password='cloud-pass')
        state = self.gateway().begin_qr()
        old_token = self.token(state)
        state['expires'] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        refreshed = self.gateway().wait_qr(state)
        self.assertTrue(refreshed.get('refreshed'))
        self.assertEqual(refreshed['state']['session'], state['session'])
        self.assertNotEqual(self.token(refreshed['state']), old_token)
        # A phone that scanned the previous image (still within its lifetime) authorizes the same key.
        server.scan(old_token)
        result = self.gateway().wait_qr(refreshed['state'])
        self.assertTrue(result.get('password_required'), result)

    def test_qr_expired_flow_is_rejected(self):
        self.server()
        state = self.gateway().begin_qr()
        state['expires'] = state['deadline'] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        with self.assertRaises(TelegramAccountError):
            self.gateway().wait_qr(state)

    def test_phone_code_and_two_factor_use_the_same_session(self):
        server = self.server(password='cloud-pass')
        state = self.gateway().begin('+79991234567')
        self.assertEqual(state['delivery'], 'в служебный чат «Telegram» на уже авторизованном устройстве')
        self.assertEqual(state['next_delivery'], 'по SMS')
        result = self.gateway().verify_code(state, '12345')
        self.assertTrue(result['password_required'])
        self.assertEqual(server.key_state(result['state']['session']), (2, 'password'))
        with self.assertRaises(TelegramAccountError):
            self.gateway().verify_password(result['state'], 'wrong')
        done = self.gateway().verify_password(result['state'], 'cloud-pass')
        self.assertEqual(server.key_state(done['session']), (2, 'authorized'))

    def test_wrong_phone_code(self):
        self.server()
        state = self.gateway().begin('+79991234567')
        with self.assertRaises(TelegramAccountError):
            self.gateway().verify_code(state, '00000')

    def test_password_rejected_by_telegram_is_a_user_error(self):
        server = self.server(password='cloud-pass')
        state = self.gateway().begin('+79991234567')
        result = self.gateway().verify_code(state, '12345')
        server.keys.clear()  # Telegram dropped the pending login (e.g. it expired)
        with self.assertRaises(TelegramAccountError):
            self.gateway().verify_password(result['state'], 'cloud-pass')


if __name__ == '__main__':
    unittest.main()
