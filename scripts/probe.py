"""Save the pages of enabled sources for offline parser checks: python scripts/probe.py

Writes leadgen/research/<fixture>.html (ignored by Git), the files `python -m leadgen.app scan --fixtures`
reads. Read-only: it only downloads public pages. Not run automatically."""
import concurrent.futures
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leadgen.adapters import fetch  # noqa: E402  (system trust store or SSL_CERT_FILE, same as the worker)

RESEARCH = ROOT / 'leadgen' / 'research'
MAX_WORKERS = 6


def fixture_name(source):
    """File stem scan --fixtures expects for a source (FL.ru keeps its historical name)."""
    return 'fl_page' if source['id'] == 'fl' else source['id']


def save(source):
    try:
        body = fetch(source['url'])
    except Exception as exc:
        return source['id'], 'error', type(exc).__name__
    path = RESEARCH / (fixture_name(source) + '.html')
    path.write_bytes(body)
    return source['id'], 'saved', f'{len(body)} bytes -> {path.relative_to(ROOT)}'


def main():
    config = json.loads((ROOT / 'leadgen' / 'sources.json').read_text())
    sources = [s for s in config['sources'] if s['enabled'] and s.get('url')]
    RESEARCH.mkdir(exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(save, sources))
    for result in results:
        print(*result)
    return 1 if any(status == 'error' for _, status, _ in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
