"""Point the bot's menu button at the Mini App: python scripts/set_miniapp_menu.py [--reset].

Uses TELEGRAM_BOT_TOKEN and WEB_PUBLIC_URL from the environment or .env. Not run automatically."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leadgen.app import env_load, telegram  # noqa: E402


def menu_button(public_url):
    url = public_url.strip().rstrip('/')
    if not url.startswith('https://'):
        raise SystemExit('WEB_PUBLIC_URL должен начинаться с https:// — Telegram открывает мини-приложения только по HTTPS.')
    return {'type': 'web_app', 'text': 'Сигналид', 'web_app': {'url': url + '/tg'}}


def main(argv):
    env_load()
    button = {'type': 'commands'} if '--reset' in argv else menu_button(os.environ.get('WEB_PUBLIC_URL', ''))
    telegram('setChatMenuButton', {'menu_button': button})
    print('Кнопка меню бота:', button.get('web_app', {}).get('url', 'стандартное меню команд'))


if __name__ == '__main__':
    main(sys.argv[1:])
