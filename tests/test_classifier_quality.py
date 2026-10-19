"""Quality of the rule classifier on a labeled set of realistic freelance-chat messages.

tests/fixtures/lead_labels.jsonl: one message per line, "expected" is "order" (a client looking for a contractor)
or "noise" ("category": seller, job, ad, chatter, other_service). An order counts as detected when classify()
does not reject it (ready or review), or rejects it only for a budget below the threshold.
Run alone for the report: python -m unittest tests.test_classifier_quality -v
"""
import json
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

from leadgen.core import UTC, Lead, classify

LABELS = Path(__file__).parent / 'fixtures' / 'lead_labels.jsonl'
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
MINIMUM = 5000
# Measured after the rule changes (see the PR description for the numbers before them).
MIN_PRECISION = 0.95
MIN_RECALL = 0.95
MIN_TOPIC_ACCURACY = 0.95
MAX_NOISE_READY = 0


def load_cases():
    with LABELS.open(encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def to_lead(case):
    text = case['text']
    # Same shape as telegram_monitor.to_lead: title is the first meaningful line, budget is read from the text.
    title = case.get('title') or next((x.strip() for x in text.splitlines() if len(x.strip()) >= 8), text)[:220]
    return Lead(source=case['source'], url='https://example.org/' + case['id'], title=title, text=text,
                published=NOW.isoformat(), budget_text=case.get('budget_text', text),
                trusted_order=case.get('trusted_order', False))


def detected_order(state, reason):
    return state != 'rejected' or reason == 'бюджет ниже порога'


def evaluate(cases):
    rows = []
    for case in cases:
        state, reason, tags = classify(to_lead(case), MINIMUM, now=NOW)
        rows.append({'case': case, 'state': state, 'reason': reason, 'tags': tags,
                     'predicted': 'order' if detected_order(state, reason) else 'noise'})
    tp = sum(r['predicted'] == 'order' and r['case']['expected'] == 'order' for r in rows)
    fp = sum(r['predicted'] == 'order' and r['case']['expected'] == 'noise' for r in rows)
    fn = sum(r['predicted'] == 'noise' and r['case']['expected'] == 'order' for r in rows)
    found = [r for r in rows if r['case']['expected'] == 'order' and r['predicted'] == 'order']
    topic_ok = [r for r in found if set(r['case'].get('topics', ())) <= set(r['tags'])
                and not set(r['case'].get('not_topics', ())) & set(r['tags'])]
    return {
        'rows': rows,
        'precision': tp / (tp + fp) if tp + fp else 1.0,
        'recall': tp / (tp + fn) if tp + fn else 1.0,
        'topic_accuracy': len(topic_ok) / len(found) if found else 1.0,
        'noise_ready': [r for r in rows if r['case']['expected'] == 'noise' and r['state'] == 'ready'],
        'confusion': Counter((r['case']['expected'], r['case'].get('category', 'order'), r['predicted']) for r in rows),
        'topic_errors': [r for r in found if r not in topic_ok],
    }


def headline(result):
    ready = sum(r['state'] == 'ready' for r in result['rows'] if r['case']['expected'] == 'order')
    orders = sum(r['case']['expected'] == 'order' for r in result['rows'])
    return (f"precision={result['precision']:.3f} recall={result['recall']:.3f} "
            f"topic_accuracy={result['topic_accuracy']:.3f} noise_ready={len(result['noise_ready'])} "
            f"orders_ready={ready}/{orders}")


def report(result):
    held_out = evaluate([r['case'] for r in result['rows'] if r['case'].get('held_out')])
    lines = ['all: ' + headline(result), 'held-out: ' + headline(held_out)]
    for (expected, category, predicted), count in sorted(result['confusion'].items()):
        lines.append(f'  {expected:5} {category:13} -> {predicted:5}: {count}')
    for r in result['rows']:
        if r['predicted'] != r['case']['expected']:
            lines.append(f"  MISS {r['case']['id']} [{r['state']}: {r['reason']}] {r['case']['text'][:90]}")
    for r in result['topic_errors']:
        lines.append(f"  TOPIC {r['case']['id']} got {r['tags']} want {r['case'].get('topics')} "
                     f"not {r['case'].get('not_topics', [])}")
    return '\n'.join(lines)


class ClassifierQuality(unittest.TestCase):
    def test_labeled_set_is_balanced_and_unique(self):
        cases = load_cases()
        self.assertGreaterEqual(len(cases), 150)
        self.assertEqual(len({c['id'] for c in cases}), len(cases))
        kinds = Counter(c['expected'] for c in cases)
        self.assertGreaterEqual(kinds['noise'], 60)
        self.assertGreaterEqual(kinds['order'], 80)

    def test_order_detection_meets_thresholds(self):
        result = evaluate(load_cases())
        summary = report(result)
        print('\n' + summary)
        self.assertGreaterEqual(result['precision'], MIN_PRECISION, summary)
        self.assertGreaterEqual(result['recall'], MIN_RECALL, summary)
        self.assertGreaterEqual(result['topic_accuracy'], MIN_TOPIC_ACCURACY, summary)
        self.assertLessEqual(len(result['noise_ready']), MAX_NOISE_READY, summary)


if __name__ == '__main__':
    unittest.main()
