"""Thin YooKassa API v3 client on urllib: payments, refunds and the notification IP allowlist."""
import base64
import ipaddress
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote

API_URL = 'https://api.yookassa.ru/v3/'
TIMEOUT = 20
# Sources of YooKassa HTTP notifications (https://yookassa.ru/developers/using-api/webhooks).
NOTIFICATION_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    '185.71.76.0/27', '185.71.77.0/27', '77.75.153.0/25', '77.75.156.11/32', '77.75.156.35/32',
    '77.75.154.128/25', '2a02:5180::/32'))


class YooKassaError(RuntimeError):
    """A definite API error: the request was rejected (4xx) and was not executed."""
    def __init__(self, message, status=None, code=None, payload=None):
        super().__init__(message)
        self.status, self.code, self.payload = status, code, payload or {}


class YooKassaUnavailable(YooKassaError):
    """Network failure, timeout or HTTP 5xx: the outcome is unknown, re-check before retrying."""


def credentials():
    return os.environ.get('YOOKASSA_SHOP_ID', '').strip(), os.environ.get('YOOKASSA_SECRET_KEY', '').strip()


def enabled():
    """Online payments work only when both the shop id and the secret key are set."""
    shop_id, secret = credentials()
    return bool(shop_id and secret)


def trusted_ip(address):
    """The address belongs to YooKassa's published notification networks."""
    try:
        ip = ipaddress.ip_address((address or '').strip())
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == net.version and ip in net for net in NOTIFICATION_NETWORKS)


class Client:
    def __init__(self, shop_id=None, secret_key=None, opener=None, base_url=API_URL, timeout=TIMEOUT):
        env_shop, env_secret = credentials()
        self.shop_id, self.secret_key = shop_id or env_shop, secret_key or env_secret
        if not self.shop_id or not self.secret_key:
            raise YooKassaError('YooKassa не настроена: задайте YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY.')
        self.opener = opener or urllib.request.urlopen
        self.base_url, self.timeout = base_url, timeout

    def request(self, method, path, body=None, idempotence_key=None):
        token = base64.b64encode(f'{self.shop_id}:{self.secret_key}'.encode()).decode()
        headers = {'Authorization': 'Basic ' + token, 'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        if method == 'POST':
            if not idempotence_key or len(str(idempotence_key)) > 64:
                raise ValueError('Idempotence-Key обязателен и не длиннее 64 символов')
            headers['Idempotence-Key'] = str(idempotence_key)
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode() or '{}')
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode() or '{}')
            except Exception:
                payload = {}
            message = f"YooKassa HTTP {exc.code}: {payload.get('description') or payload.get('code') or 'ошибка'}"
            error = YooKassaUnavailable if exc.code >= 500 else YooKassaError
            raise error(message, exc.code, payload.get('code'), payload) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise YooKassaUnavailable('YooKassa недоступна: ' + type(exc).__name__) from None
        except ValueError:
            raise YooKassaUnavailable('YooKassa вернула неожиданный ответ') from None

    def post(self, path, body, idempotence_key):
        """POST once; an unknown outcome is retried once with the same key, which YooKassa deduplicates."""
        try:
            return self.request('POST', path, body, idempotence_key)
        except YooKassaUnavailable:
            return self.request('POST', path, body, idempotence_key)

    def create_payment(self, body, idempotence_key):
        return self.post('payments', body, idempotence_key)

    def get_payment(self, payment_id):
        return self.request('GET', 'payments/' + quote(str(payment_id), safe=''))

    def create_refund(self, body, idempotence_key):
        return self.post('refunds', body, idempotence_key)

    def get_refund(self, refund_id):
        return self.request('GET', 'refunds/' + quote(str(refund_id), safe=''))
