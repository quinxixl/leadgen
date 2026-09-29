"""Read-only availability check; saved pages are ignored by Git."""
import concurrent.futures
import pathlib
import urllib.request
import ssl

URLS = {
    'habr': 'https://freelance.habr.com/rss/tasks',
    'fl': 'https://www.fl.ru/rss/all.xml',
    'fl_page': 'https://www.fl.ru/projects/',
    'tg_jobs': 'https://t.me/s/tgram_jobs',
    'tg_dev': 'https://t.me/s/job_developer',
    'tg_frilans': 'https://t.me/s/frilans',
    'tg_taverna': 'https://t.me/s/freelancetaverna',
    'freelance': 'https://freelance.ru/project/search/pro',
    'avito': 'https://developers.avito.ru/api-catalog',
}

def fetch(pair):
    key, url = pair
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'LeadFinder/0.1'})
        with urllib.request.urlopen(request, timeout=25, context=ssl.create_default_context(cafile='/etc/ssl/cert.pem')) as response:
            body = response.read()
            root = pathlib.Path(__file__).parent / 'research'
            root.mkdir(exist_ok=True)
            (root / (key + '.html')).write_bytes(body)
            return key, response.status, len(body), response.url
    except Exception as exc:
        return key, type(exc).__name__, str(exc)

if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(fetch, URLS.items()):
            print(result)
