"""Input catalog, selection validation, image paths and atomic JSONL writes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as app


def catalog_row(sample, ids, seed=None, marked=None):
    seed = ids[0] if seed is None else seed
    marked = [seed] if marked is None else marked
    return {'sample_id': sample, 'case_type': 'INDIVIDUAL', 'seed_target_image_id': seed,
            'candidates': [{'image_id': i, 'image_url': f'/images/test/{i}.jpg', 'is_seed': i in marked} for i in ids]}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rows, name='input.jsonl'):
        path = self.root / name
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
        return path

    def test_missing_input_gives_an_empty_index(self):
        index = app.InputIndex(self.root / 'absent.jsonl')
        self.assertEqual((index.entries, index.by_id), ([], {}))

    def test_index_reads_one_task_by_offset(self):
        index = app.InputIndex(self.write([catalog_row('a', ['s', 'x']), catalog_row('b', ['t', 'y', 'z'])]))
        self.assertEqual([e['sample_id'] for e in index.entries], ['a', 'b'])
        self.assertEqual([c['image_id'] for c in index.read(1)['candidates']], ['t', 'y', 'z'])
        with self.assertRaises(ValueError):
            index.read(2)

    def test_invalid_catalogs_are_rejected(self):
        for bad in ([catalog_row('a', ['s']), catalog_row('a', ['s'])],           # duplicate sample_id
                    [catalog_row('a', ['s', 'x'], seed='x')],                     # seed is not first
                    [catalog_row('a', ['s', 'x'], marked=['s', 'x'])],            # two seed markers
                    [catalog_row('a', ['s', 'x'], marked=[])],                    # no seed marker
                    [catalog_row('a', ['s', 's'])], [catalog_row('a', [], seed='s')]):  # duplicate / empty candidates
            with self.assertRaises(ValueError, msg=bad):
                app.InputIndex(self.write(bad))

    def test_changed_input_is_detected(self):
        path = self.write([catalog_row('a', ['s', 'x'])])
        index = app.InputIndex(path)
        self.write([catalog_row('a', ['s', 'x']), catalog_row('b', ['s'])])
        with self.assertRaises(ValueError):
            index.read(0)

    def test_selection_validation(self):
        source = catalog_row('a', ['s', 'x', 'y'])
        self.assertEqual(app.validate_positive(source, {'sample_id': 'a', 'positive_image_ids': ['y', 'x']}),
                         {'sample_id': 'a', 'positive_image_ids': ['x', 'y']})            # canonical candidate order
        self.assertEqual(app.validate_positive(source, {'sample_id': 'a', 'positive_image_ids': []})['positive_image_ids'], [])   # seed not required
        for bad in ({'sample_id': 'b', 'positive_image_ids': []}, {'sample_id': 'a', 'positive_image_ids': ['zzz']},
                    {'sample_id': 'a', 'positive_image_ids': ['x', 'x']}, {'sample_id': 'a', 'positive_image_ids': 'x'},
                    {'sample_id': 'a', 'positive_image_ids': [1]}, {'sample_id': 'a'}):
            with self.assertRaises(ValueError, msg=bad):
                app.validate_positive(source, bad)

    def test_image_path_handling(self):
        for url in ('/PIPA/images/train/abc.jpg', '/images/train/abc.jpg', 'train/abc.jpg', '/x?d=/PIPA/images/train/abc.jpg'):
            self.assertEqual(app.image_relative_path(url), Path('train/abc.jpg'), url)
        for url in ('../secret.jpg', '/images/../../etc/passwd', '', '/', None, '/images/a/%2e%2e/b.jpg'):
            with self.assertRaises(ValueError, msg=url):
                app.image_relative_path(url)
        self.assertEqual(app.image_url('/PIPA/images/a b/c.jpg'), '/verify/api/image?path=a%20b/c.jpg')

    def test_publish_is_atomic_and_leaves_no_temp_file(self):
        path = self.root / 'out' / 'rows.jsonl'
        app.publish(path, [{'sample_id': 'é', 'positive_image_ids': ['a']}])
        app.publish(path, iter([{'sample_id': 'b', 'positive_image_ids': []}]))
        self.assertEqual(app.load(path), [{'sample_id': 'b', 'positive_image_ids': []}])
        self.assertEqual([p.name for p in path.parent.iterdir()], ['rows.jsonl'])


if __name__ == '__main__':
    unittest.main()
