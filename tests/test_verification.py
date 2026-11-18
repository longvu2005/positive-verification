"""3-annotator positive verification: voting, statistics, persistence, identity. No external packages."""
import csv
import json
import random
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as app
import verification as ver

SUBJECTS = [{'subject_id': 1, 'identity_ids': ['alice']}]


def row(sample, case, split, ids, extra_split=True):
    r = {'sample_id': sample, 'case_type': case, 'subjects': SUBJECTS, 'query_image_id': 'q', 'query_image_url': '/PIPA/images/x/q.jpg',
         'seed_target_image_id': ids[0], 'final_desc': 'Identify Subject 1 as a woman', 'final_change': 'then retrieve target images where Subject 1 sits',
         'candidates': [{'image_id': i, 'image_url': f'/PIPA/images/x/{i}.jpg', 'is_seed': n == 0, 'subject_boxes': []} for n, i in enumerate(ids)]}
    if extra_split:
        r['split'] = split
    return r


ROWS = [row('v1', 'INDIVIDUAL', 'validation', ['s', 'a', 'b', 'c']), row('v2', 'DUAL', 'val', ['s', 'a', 'b']),
        row('t1', 'GROUP', 'test', ['s', 'a']), row('tr1', 'INDIVIDUAL', 'train', ['s', 'a'])]
VERIFY_IDS = ['v1', 'v2', 't1']


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')


def item(sample, cands, sets, split='validation', case='INDIVIDUAL', seed=None):
    return {'sample_id': sample, 'split': split, 'case_type': case, 'seed': seed or cands[0], 'candidates': cands,
            'status': ['ok' if s is not None else 'missing' for s in sets], 'sets': [None if s is None else set(s) for s in sets]}


def read_csv(path):
    with open(path, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def overall(report):
    return report['groups'][0]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input = self.root / 'positive_set_input.jsonl'
        self.dir = self.root / 'verification'
        write_jsonl(self.input, ROWS)
        self.dir.mkdir(parents=True)
        (self.dir / 'users.json').write_text(json.dumps({'annotators': {'ann2': 2, 'ann3': 3}, 'admins': ['boss']}))
        self.v = self.make()

    def make(self):
        return app.Verification(self.dir, app.InputIndex(self.input))

    def a1(self, records):
        write_jsonl(self.dir / 'positive_sets_annotator_1.jsonl', [{'sample_id': s, 'positive_image_ids': p} for s, p in records.items()])

    def annotate(self, n, records):
        for sample, ids in records.items():
            self.v.stores[n].save({'sample_id': sample, 'positive_image_ids': ids, 'complete': True, 'confirm_empty': not ids})

    def complete_all(self):
        self.a1({'v1': ['s', 'a'], 'v2': ['s'], 't1': ['s']})
        self.annotate(2, {'v1': ['s', 'b'], 'v2': ['s'], 't1': ['a']})
        self.annotate(3, {'v1': ['a', 'b'], 'v2': ['s', 'a'], 't1': ['a']})


class AnnotatorOneImport(Base):
    def test_import_is_read_only_validated_and_excludes_train(self):
        self.a1({'v1': ['s', 'a'], 'v2': ['s', 'zzz'], 't1': ['a'], 'tr1': ['s'], 'ghost': ['s']})
        path = self.dir / 'positive_sets_annotator_1.jsonl'
        before = path.read_bytes()
        rep = self.v.report()
        self.assertEqual(path.read_bytes(), before)                        # never modified
        by = {r['sample_id']: r for r in rep['per_query']}
        self.assertEqual((by['v1']['a1_status'], by['v1']['a1_positives']), ('ok', 2))
        self.assertEqual(by['v2']['a1_status'], 'invalid')                 # candidate outside pool
        self.assertEqual((by['t1']['a1_status'], by['t1']['a1_positives']), ('ok', 1))   # seed not silently added
        self.assertEqual(rep['diagnostics']['excluded_non_verification_records']['A1'], 1)   # Train excluded
        reasons = {(e['sample_id'], e['annotator']) for e in rep['diagnostics']['errors']}
        self.assertIn(('ghost', 'A1'), reasons)
        self.assertIn(('v2', 'A1'), reasons)
        self.assertEqual(overall(rep)['coverage']['annotators']['A1']['invalid_records'], 1)
        self.assertFalse(rep['complete'])

    def test_missing_a1_records_are_never_created(self):
        rep = self.v.report()
        self.assertFalse((self.dir / 'positive_sets_annotator_1.jsonl').exists())
        self.assertEqual(overall(rep)['coverage']['annotators']['A1']['pending_queries'], 3)
        self.assertEqual(rep['status'], 'partial')

    def test_malformed_lines_and_duplicates_are_reported(self):
        path = self.dir / 'positive_sets_annotator_1.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"sample_id":"v1","positive_image_ids":["s"]}\nnot json\n{"sample_id":"v1","positive_image_ids":["s"]}\n{"sample_id":"t1","positive_image_ids":"s"}\n')
        rep = self.v.report()
        self.assertEqual(overall(rep)['coverage']['annotators']['A1']['completed_queries'], 0)   # duplicate removes v1
        self.assertGreaterEqual(rep['diagnostics']['error_count'], 3)


class AnnotatorStores(Base):
    def test_separate_persistence_and_resume(self):
        self.annotate(2, {'v1': ['a']})
        self.annotate(3, {'v1': ['b'], 'v2': ['s']})
        f2, f3 = (self.dir / f'positive_sets_annotator_{n}.jsonl' for n in (2, 3))
        self.assertEqual(app.load(f2), [{'sample_id': 'v1', 'positive_image_ids': ['a']}])
        self.assertEqual([r['positive_image_ids'] for r in app.load(f3)], [['b'], ['s']])
        again = self.make()                                                # restart: resume from disk
        self.assertEqual(again.stores[2].tasks()['completed'], 1)
        self.assertEqual(again.stores[3].tasks()['pending'], 1)
        self.assertEqual(again.stores[2].task(sample_id='v1')['task']['positive_image_ids'], ['a'])
        self.assertEqual(again.stores[3].task(sample_id='v1')['task']['positive_image_ids'], ['b'])
        self.annotate(2, {'v2': ['a']})                                    # A2 saving never alters A3's file
        self.assertEqual([r['positive_image_ids'] for r in app.load(f3)], [['b'], ['s']])

    def test_only_validation_and_test_tasks(self):
        tasks = self.v.stores[2].tasks()
        self.assertEqual([t['sample_id'] for t in tasks['tasks']], VERIFY_IDS)
        self.assertEqual([t['split'] for t in tasks['tasks']], ['validation', 'validation', 'test'])
        with self.assertRaises(ValueError):
            self.v.stores[2].task(sample_id='tr1')

    def test_independent_annotation_visibility(self):
        self.a1({'v1': ['s', 'a', 'b'], 'v2': ['s'], 't1': ['s']})
        self.annotate(3, {'v1': ['c']})
        payload = self.v.stores[2].task(sample_id='v1')
        task = payload['task']
        self.assertEqual(task['positive_image_ids'], [])                   # nothing pre-selected (not even the seed)
        self.assertTrue(all('is_seed' not in c for c in task['candidates']))     # no seed marker at all
        self.assertNotIn('is_seed', json.dumps(payload))
        self.assertEqual(sorted(c['image_id'] for c in task['candidates']), ['a', 'b', 'c', 's'])
        self.assertNotIn('c', payload['task']['positive_image_ids'])        # A3's selection is not exposed
        self.assertEqual(payload['task']['candidates'], self.make().stores[2].task(sample_id='v1')['task']['candidates'])   # deterministic

    def test_candidate_order_does_not_reveal_seed(self):
        big = [row(f'x{i}', 'INDIVIDUAL', 'test', [f's{i}'] + [f'c{i}_{j}' for j in range(29)]) for i in range(10)]
        firsts = [app.verify_task(r, None, 2)['candidates'][0]['image_id'] != r['seed_target_image_id'] for r in big]
        self.assertTrue(any(firsts))
        self.assertNotEqual([c['image_id'] for c in app.verify_task(big[0], None, 2)['candidates']],
                            [c['image_id'] for c in app.verify_task(big[0], None, 3)['candidates']])

    def test_seed_can_be_rejected(self):
        saved = self.v.stores[2].save({'sample_id': 'v1', 'positive_image_ids': ['a'], 'complete': True})
        self.assertEqual(saved['positive_image_ids'], ['a'])
        self.assertEqual(app.load(self.dir / 'positive_sets_annotator_2.jsonl')[0]['positive_image_ids'], ['a'])

    def test_empty_selection_is_a_completed_judgment_only_when_explicit(self):
        store = self.v.stores[2]
        with self.assertRaises(ValueError):
            store.save({'sample_id': 'v1', 'positive_image_ids': [], 'complete': True})               # not confirmed
        with self.assertRaises(ValueError):
            store.save({'sample_id': 'v1', 'positive_image_ids': ['a']})                               # completion not explicit
        self.assertEqual(store.tasks()['completed'], 0)
        store.save({'sample_id': 'v1', 'positive_image_ids': [], 'complete': True, 'confirm_empty': True})
        self.assertEqual(store.tasks()['completed'], 1)
        self.assertEqual(app.load(self.dir / 'positive_sets_annotator_2.jsonl'), [{'sample_id': 'v1', 'positive_image_ids': []}])
        self.assertTrue(store.task(sample_id='v1')['is_completed'])

    def test_sample_and_candidate_validation(self):
        store = self.v.stores[2]
        for bad in ({'sample_id': 'nope', 'positive_image_ids': []}, {'sample_id': 'tr1', 'positive_image_ids': ['s']},
                    {'sample_id': 'v1', 'positive_image_ids': ['zzz']}, {'sample_id': 'v1', 'positive_image_ids': ['a', 'a']},
                    {'sample_id': 'v1', 'positive_image_ids': 'a'}):
            with self.assertRaises(ValueError, msg=bad):
                store.save({**bad, 'complete': True, 'confirm_empty': True})
        self.assertFalse((self.dir / 'positive_sets_annotator_2.jsonl').exists())
        write_jsonl(self.dir / 'positive_sets_annotator_3.jsonl', [{'sample_id': 'tr1', 'positive_image_ids': ['s']}])
        with self.assertRaises(ValueError):
            self.make()                                                    # train record in an annotator file is rejected at startup

    def test_output_is_in_input_order_with_canonical_candidate_order(self):
        self.annotate(2, {'t1': ['a', 's'], 'v1': ['c', 'a']})
        self.assertEqual(app.load(self.dir / 'positive_sets_annotator_2.jsonl'),
                         [{'sample_id': 'v1', 'positive_image_ids': ['a', 'c']}, {'sample_id': 't1', 'positive_image_ids': ['s', 'a']}])


class SplitMetadata(Base):
    def rebuild(self, rows, manifest=None):
        write_jsonl(self.input, rows)
        if manifest is not None:
            write_jsonl(self.dir / 'split_manifest.jsonl', manifest)
        return self.make()

    def test_manifest_supplies_split_when_input_has_none(self):
        rows = [row(r['sample_id'], r['case_type'], None, [c['image_id'] for c in r['candidates']], extra_split=False) for r in ROWS]
        with self.assertRaises(ValueError):
            self.rebuild(rows)                                             # never guessed from sample IDs
        v = self.rebuild(rows, [{'sample_id': 'v1', 'split': 'validation'}, {'sample_id': 'v2', 'split': 'validation'},
                                {'sample_id': 't1', 'split': 'test'}, {'sample_id': 'tr1', 'split': 'train'}])
        self.assertEqual([e['sample_id'] for e in v.entries], VERIFY_IDS)

    def test_manifest_validation(self):
        rows = [row(r['sample_id'], r['case_type'], None, [c['image_id'] for c in r['candidates']], extra_split=False) for r in ROWS]
        with self.assertRaises(ValueError):
            self.rebuild(rows, [{'sample_id': 'ghost', 'split': 'test'}])
        with self.assertRaises(ValueError):
            self.rebuild(rows, [{'sample_id': 'v1', 'split': 'dev'}])
        with self.assertRaises(ValueError):
            self.rebuild(rows, [{'sample_id': 'v1', 'split': 'test'}] * 2)
        rows[0]['split'] = 'validation'
        with self.assertRaises(ValueError):
            self.rebuild(rows, [{'sample_id': 'v1', 'split': 'test'}])     # conflicts with the input row


class MajorityVoting(unittest.TestCase):
    def test_all_eight_combinations(self):
        cands = [f'c{a}{b}{c}' for a in (0, 1) for b in (0, 1) for c in (0, 1)]
        sets = [{x for x in cands if x[1 + k] == '1'} for k in range(3)]
        rep = ver.build_report([item('q', cands, sets, seed='c111')])
        self.assertEqual(rep['final'], [{'sample_id': 'q', 'positive_image_ids': ['c011', 'c101', 'c110', 'c111']}])
        votes = {(r['a1'], r['a2'], r['a3']): r['final'] for r in rep['disagreements']}
        self.assertEqual(votes, {(0, 0, 1): 0, (0, 1, 0): 0, (0, 1, 1): 1, (1, 0, 0): 0, (1, 0, 1): 1, (1, 1, 0): 1})   # unanimous rows are omitted
        v = overall(rep)['voting']
        self.assertEqual((v['unanimous_positive_3of3'], v['majority_positive_2of3'], v['majority_negative_2of3'], v['unanimous_negative_3of3']), (1, 3, 3, 1))

    def test_voting_is_per_candidate_and_zero_positive_is_flagged_not_repaired(self):
        rep = ver.build_report([item('q', ['s', 'a', 'b'], [{'s'}, {'a'}, {'b'}])])
        self.assertEqual(rep['final'][0]['positive_image_ids'], [])
        self.assertEqual(rep['diagnostics']['final_zero_positive_sample_ids'], ['q'])
        self.assertEqual(rep['diagnostics']['seed_rejected_sample_ids'], ['q'])
        self.assertEqual(overall(rep)['diagnostics']['final_zero_positive_queries'], 1)
        self.assertEqual(overall(rep)['original_verification']['original_seeds_rejected'], 1)


class Statistics(unittest.TestCase):
    def test_cohen_kappa_textbook_example(self):
        po, pe, kappa, note = ver.cohen([15, 10, 5, 20])        # [no/no, no/yes, yes/no, yes/yes] = 15, 10, 5, 20
        self.assertAlmostEqual(po, 0.7)
        self.assertAlmostEqual(pe, 0.5)
        self.assertAlmostEqual(kappa, 0.4)
        self.assertIsNone(note)

    def test_cohen_undefined_returns_null_with_note(self):
        for table in ([0, 0, 0, 5], [5, 0, 0, 0], [0, 0, 0, 0]):
            kappa, note = ver.cohen(table)[2:]
            self.assertIsNone(kappa)
            self.assertTrue(note)
        self.assertEqual(ver.cohen([0, 4, 0, 0])[2], 0.0)       # a constant vs a different constant: defined, kappa 0

    def test_fleiss_hand_example_and_degenerate(self):
        k, note = ver.fleiss([2, 1, 1, 2])                      # P_bar = (1+1+1/3+1/3+1+1)/6 = 7/9, P_e = .5
        self.assertAlmostEqual(k, 5 / 9)
        self.assertIsNone(ver.fleiss([4, 0, 0, 0])[0])
        self.assertIsNone(ver.fleiss([0, 0, 0, 0])[0])

    def test_fleiss_and_agreement_match_an_independent_reference(self):
        rng = random.Random(7)
        cands = [f'c{i}' for i in range(60)]
        ratings = [[int(rng.random() < p) for p in (.6, .5, .4)] for _ in cands]
        sets = [{c for c, r in zip(cands, ratings) if r[k]} for k in range(3)]
        g = overall(ver.build_report([item('q', cands, sets)]))['agreement']
        n_items, n_raters = len(cands), 3                       # reference: standard Fleiss from the n_ij matrix
        matrix = [[3 - sum(r), sum(r)] for r in ratings]
        p_i = [(sum(x * x for x in row_) - n_raters) / (n_raters * (n_raters - 1)) for row_ in matrix]
        p_j = [sum(row_[j] for row_ in matrix) / (n_items * n_raters) for j in range(2)]
        expected = (sum(p_i) / n_items - sum(p * p for p in p_j)) / (1 - sum(p * p for p in p_j))
        self.assertAlmostEqual(g['fleiss_kappa']['value'], expected)
        self.assertEqual(g['evaluated_pairs'], 60)
        self.assertAlmostEqual(g['agreement_3way_pct'], 100 * sum(len(set(r)) == 1 for r in ratings) / 60)
        for (i, j), name in zip(((0, 1), (0, 2), (1, 2)), ('A1_A2', 'A1_A3', 'A2_A3')):
            self.assertAlmostEqual(g['pairwise'][name]['agreement_pct'], 100 * sum(r[i] == r[j] for r in ratings) / 60)
            po = sum(r[i] == r[j] for r in ratings) / 60
            pa, pb = sum(r[i] for r in ratings) / 60, sum(r[j] for r in ratings) / 60
            pe = pa * pb + (1 - pa) * (1 - pb)
            self.assertAlmostEqual(g['cohen_kappa'][name]['kappa'], (po - pe) / (1 - pe))
        kappas = [g['cohen_kappa'][n]['kappa'] for n in ('A1_A2', 'A1_A3', 'A2_A3')]
        self.assertAlmostEqual(g['mean_cohen_kappa']['value'], sum(kappas) / 3)
        self.assertNotAlmostEqual(g['mean_cohen_kappa']['value'], g['fleiss_kappa']['value'], places=6)   # not the same statistic
        both = sum(sum(r) * (sum(r) - 1) // 2 for r in ratings)           # pairs of annotators that both said Positive
        self.assertAlmostEqual(g['positive_specific_agreement']['value'], both / sum(sum(r) for r in ratings))

    def test_confirmation_rejection_added_removed(self):
        # seed s: 3 votes (kept); a: A1+A2 (kept); b, e: A1 only (removed); c: A2+A3 (added); d: A3 only (stays negative)
        cands = ['s', 'a', 'b', 'c', 'd', 'e']
        rep = ver.build_report([item('q', cands, [{'s', 'a', 'b', 'e'}, {'s', 'a', 'c'}, {'s', 'c', 'd'}])])
        o, v, q = overall(rep)['original_verification'], overall(rep)['voting'], overall(rep)['query_level']
        self.assertEqual((o['original_positive_links'], o['retained'], o['removed'], o['new_positives_added']), (4, 2, 2, 1))
        self.assertAlmostEqual(o['confirmation_rate'], 2 / 4)
        self.assertAlmostEqual(o['rejection_rate'], 2 / 4)
        self.assertEqual((o['net_positive_change'], o['queries_changed'], o['queries_unchanged'], o['original_seeds_rejected']), (-1, 1, 0, 0))
        self.assertEqual((v['unanimous_positive_3of3'], v['majority_positive_2of3'], v['majority_negative_2of3'], v['unanimous_negative_3of3']), (1, 2, 3, 0))
        self.assertAlmostEqual(v['final_positives_3of3_pct'], 100 / 3)
        self.assertAlmostEqual(v['final_positives_2of3_pct'], 200 / 3)
        self.assertAlmostEqual(v['unanimous_agreement_rate'], 1 / 6)
        self.assertAlmostEqual(v['non_unanimous_majority_rate'], 5 / 6)
        self.assertEqual((q['gained_positives'], q['lost_positives'], q['gained_and_lost'], q['identical_all3'], q['with_disagreement']), (1, 1, 1, 0, 1))
        row_ = rep['per_query'][0]
        self.assertEqual((row_['added_positives'], row_['removed_positives'], row_['final_positives'], row_['final_changed']), (1, 2, 3, True))
        self.assertAlmostEqual(row_['agreement_rate'], 1 / 6)

    def test_query_level_gain_only_and_loss_only(self):
        rep = ver.build_report([item('gain', ['s', 'a'], [{'s'}, {'s', 'a'}, {'s', 'a'}]), item('loss', ['s', 'a'], [{'s', 'a'}, {'s'}, {'s'}]),
                                item('same', ['s', 'a'], [{'s'}] * 3)])
        q, o = overall(rep)['query_level'], overall(rep)['original_verification']
        self.assertEqual((q['gained_positives'], q['lost_positives'], q['gained_and_lost'], q['identical_all3'], q['final_differs_a1']), (1, 1, 0, 1, 2))
        self.assertEqual((o['queries_changed'], o['queries_unchanged'], o['new_positives_added'], o['removed']), (2, 1, 1, 1))
        self.assertEqual(q['count_distribution'], {'A1': {'1': 2, '2': 1}, 'final': {'1': 2, '2': 1}})

    def test_distribution_statistics(self):
        items = [item('q0', ['s', 'a', 'b'], [{'s'}] * 3), item('q1', ['s', 'a', 'b'], [{'s', 'a'}] * 3),
                 item('q2', ['s', 'a', 'b'], [{'s', 'a', 'b'}] * 3)]            # final positives per query: 1, 2, 3
        d = overall(ver.build_report(items))['positive_distribution']['final']
        self.assertEqual((d['total_positive_links'], d['min'], d['max'], d['median'], d['queries_with_1'], d['queries_with_more_than_1']), (6, 1, 3, 2, 1, 2))
        self.assertAlmostEqual(d['mean'], 2)
        self.assertAlmostEqual(d['std'], 1.0)                              # sample std of [1, 2, 3]
        self.assertAlmostEqual(d['queries_with_multiple_pct'], 100 * 2 / 3)

    def test_split_and_query_type_groups(self):
        items = [item('a', ['s', 'x'], [{'s'}] * 3, 'validation', 'INDIVIDUAL'), item('b', ['s', 'x', 'y'], [{'s'}] * 3, 'validation', 'DUAL'),
                 item('c', ['s'], [{'s'}] * 3, 'test', 'DUAL')]
        groups = {(g['split'], g['query_type']): g for g in ver.build_report(items)['groups']}
        self.assertEqual(groups[('overall', 'ALL')]['coverage']['candidate_pairs'], 6)
        self.assertEqual(groups[('validation', 'ALL')]['coverage']['queries'], 2)
        self.assertEqual(groups[('test', 'ALL')]['coverage']['queries'], 1)
        self.assertEqual(groups[('validation', 'DUAL')]['coverage']['candidate_pairs'], 3)
        self.assertEqual(groups[('test', 'DUAL')]['coverage']['candidate_pairs'], 1)
        self.assertEqual(groups[('test', 'GROUP')]['coverage']['queries'], 0)
        self.assertIsNone(groups[('test', 'GROUP')]['original_verification']['confirmation_rate'])   # empty group -> null, no crash

    def test_incomplete_report_is_partial_and_only_uses_complete_queries(self):
        rep = ver.build_report([item('a', ['s', 'x'], [{'s'}, {'s'}, {'s'}]), item('b', ['s', 'x'], [{'s'}, None, {'s'}])])
        c = overall(rep)['coverage']
        self.assertEqual((rep['status'], c['queries'], c['queries_all_three_valid'], c['pairs_all_three_valid'], c['incomplete_queries']), ('partial', 2, 1, 2, 1))
        self.assertEqual(c['annotators']['A2']['pending_queries'], 1)
        self.assertAlmostEqual(c['annotators']['A2']['completion_pct'], 50)
        with self.assertRaises(ValueError):
            ver.write_exports(rep, Path('/nonexistent/final.jsonl'), Path('/nonexistent/stats'), {})


class Finalization(Base):
    def test_missing_annotations_block_finalization(self):
        self.a1({'v1': ['s'], 'v2': ['s'], 't1': ['s']})
        self.annotate(2, {'v1': ['s'], 'v2': ['s']})                       # t1 missing for A2; A3 has nothing
        with self.assertRaises(ValueError) as err:
            self.v.export()
        self.assertIn('cannot finalize', str(err.exception))
        self.assertFalse((self.dir / 'positive_sets_final.jsonl').exists())
        self.assertFalse((self.dir / 'statistics').exists())
        self.assertEqual(self.v.admin_view()['status'], 'partial')

    def test_final_export_schema_order_and_untouched_inputs(self):
        self.complete_all()
        paths = [self.dir / f'positive_sets_annotator_{n}.jsonl' for n in (1, 2, 3)]
        before = [p.read_bytes() for p in paths]
        out = self.v.export()
        self.assertEqual(out['status'], 'final')
        self.assertEqual([p.read_bytes() for p in paths], before)          # annotator files unchanged
        final = app.load(self.dir / 'positive_sets_final.jsonl')
        self.assertEqual([r['sample_id'] for r in final], VERIFY_IDS)      # input order, train excluded
        self.assertTrue(all(set(r) == {'sample_id', 'positive_image_ids'} for r in final))
        # v1: s -> A1,A2 ; a -> A1,A3 ; b -> A2,A3 ; c -> none.  v2: s -> 3, a -> A3 only.  t1: s -> A1 only, a -> A2,A3
        self.assertEqual(final, [{'sample_id': 'v1', 'positive_image_ids': ['s', 'a', 'b']}, {'sample_id': 'v2', 'positive_image_ids': ['s']},
                                 {'sample_id': 't1', 'positive_image_ids': ['a']}])
        for name in ('summary.json', 'summary.csv', 'agreement.csv', 'voting_distribution.csv', 'per_query.csv', 'disagreements.csv'):
            self.assertTrue((self.dir / 'statistics' / name).is_file(), name)
        summary = json.loads((self.dir / 'statistics' / 'summary.json').read_text())
        self.assertEqual((summary['status'], summary['dataset']['queries'], summary['dataset']['validation'], summary['dataset']['test']), ('final', 3, 2, 1))
        self.assertEqual(summary['diagnostics']['seed_rejected_sample_ids'], ['t1'])      # reported, not repaired
        self.assertTrue(self.v.admin_view()['last_export']['up_to_date'])

    def test_exports_are_deterministic_and_staleness_is_detected(self):
        self.complete_all()
        self.v.export()
        names = ('positive_sets_final.jsonl', 'statistics/summary.csv', 'statistics/agreement.csv', 'statistics/voting_distribution.csv',
                 'statistics/per_query.csv', 'statistics/disagreements.csv')
        first = [(self.dir / n).read_bytes() for n in names]
        self.v.export()
        self.assertEqual([(self.dir / n).read_bytes() for n in names], first)
        self.annotate(2, {'v1': ['s']})                                    # annotation changes after the export
        self.assertFalse(self.v.admin_view()['last_export']['up_to_date'])

    def test_csv_contents(self):
        self.complete_all()
        self.v.export()
        stats = self.dir / 'statistics'
        per_query = read_csv(stats / 'per_query.csv')
        self.assertEqual([r['sample_id'] for r in per_query], VERIFY_IDS)
        self.assertEqual((per_query[0]['a1_positives'], per_query[0]['final_positives'], per_query[0]['added_positives'], per_query[0]['removed_positives']), ('2', '3', '1', '0'))
        dis = read_csv(stats / 'disagreements.csv')
        self.assertEqual({(r['sample_id'], r['image_id']): (r['a1'], r['a2'], r['a3'], r['final'], r['split'], r['query_type']) for r in dis}[('t1', 's')],
                         ('1', '0', '0', '0', 'test', 'GROUP'))
        self.assertTrue(all(0 < int(r['a1']) + int(r['a2']) + int(r['a3']) < 3 for r in dis))
        dist = [r for r in read_csv(stats / 'voting_distribution.csv') if r['split'] == 'overall' and r['query_type'] == 'ALL']
        self.assertEqual([r['positive_votes'] for r in dist], ['3/3', '2/3', '1/3', '0/3'])
        self.assertAlmostEqual(sum(float(r['pct']) for r in dist), 100)
        agree = read_csv(stats / 'agreement.csv')
        self.assertIn('fleiss_kappa', {r['statistic'] for r in agree})
        self.assertEqual(len([r for r in agree if r['statistic'] == 'cohen_kappa' and r['split'] == 'overall']), 3)
        summary_rows = read_csv(stats / 'summary.csv')
        self.assertEqual(len(summary_rows), 11)                           # overall + (ALL + 4 types) x 2 splits
        self.assertEqual({(r['split'], r['query_type']) for r in summary_rows if r['split'] != 'overall'},
                         {(s, q) for s in ('validation', 'test') for q in ('ALL',) + ver.CASES})

    def test_export_cannot_overwrite_annotator_one(self):
        self.complete_all()
        rep = self.v.report()
        with self.assertRaises(ValueError):
            ver.write_exports(rep, self.v.paths[0], self.dir / 'statistics', {}, protected=self.v.paths)


class HttpIdentity(Base):
    def start(self):
        srv = ThreadingHTTPServer(('127.0.0.1', 0), type('H', (app.Handler,), {'verify': self.v}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{srv.server_port}'
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))

    def request(self, method, path, user=None, payload=None, raw=False):
        headers = {'Content-Type': 'application/json'}
        if user:
            headers['X-Remote-User'] = user
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as response:
                body = response.read()
                return response.status, (body if raw else json.loads(body)), response.headers
        except urllib.error.HTTPError as err:
            body = err.read()
            try:
                return err.code, json.loads(body or b'{}'), err.headers
            except ValueError:
                return err.code, {}, err.headers

    def test_roles_visibility_and_isolation(self):
        self.start()
        self.a1({'v1': ['s', 'a', 'b'], 'v2': ['s'], 't1': ['s']})
        r = lambda *args, **kw: self.request(*args, **kw)[:2]
        self.assertEqual(r('GET', '/verify/api/tasks')[0], 403)                        # no identity
        self.assertEqual(r('GET', '/verify/api/tasks', 'stranger')[0], 403)            # not in users.json
        code, body = r('GET', '/verify/api/me', 'ann2')
        self.assertEqual((code, body['role'], body['annotator']), (200, 'annotator', 2))
        self.assertEqual(r('GET', '/verify/api/tasks', 'ann2')[1]['total'], 3)
        payload = r('GET', '/verify/api/task?id=v1', 'ann2')[1]
        self.assertEqual(payload['task']['positive_image_ids'], [])                    # A1's selection is not visible
        self.assertNotIn('is_seed', json.dumps(payload))
        # a client-supplied annotator number is ignored; the stored identity decides where results go
        code, _ = r('POST', '/verify/api/positive', 'ann2', {'sample_id': 'v1', 'positive_image_ids': ['a'], 'complete': True, 'annotator': 3})
        self.assertEqual(code, 200)
        self.assertEqual(r('GET', '/verify/api/task?id=v1', 'ann3')[1]['task']['positive_image_ids'], [])
        self.assertEqual(r('GET', '/verify/api/task?id=v1', 'ann2')[1]['task']['positive_image_ids'], ['a'])
        self.assertTrue((self.dir / 'positive_sets_annotator_2.jsonl').exists())
        self.assertFalse((self.dir / 'positive_sets_annotator_3.jsonl').exists())
        self.assertEqual(r('POST', '/verify/api/positive', 'ann2', {'sample_id': 'v1', 'positive_image_ids': ['a']})[0], 400)   # completion explicit
        self.assertEqual(r('POST', '/verify/api/positive', None, {'sample_id': 'v1', 'positive_image_ids': ['a'], 'complete': True})[0], 403)
        # annotators cannot reach the administrator API
        self.assertEqual(r('GET', '/verify/api/admin/stats', 'ann2')[0], 403)
        self.assertEqual(r('POST', '/verify/api/admin/export', 'ann2', {})[0], 403)
        # administrator: stats/export only
        self.assertEqual(r('GET', '/verify/api/tasks', 'boss')[0], 403)
        self.assertEqual(r('GET', '/verify/api/me', 'boss')[1]['role'], 'admin')
        code, stats = r('GET', '/verify/api/admin/stats', 'boss')
        self.assertEqual((code, stats['status']), (200, 'partial'))
        code, body = r('POST', '/verify/api/admin/export', 'boss', {})
        self.assertEqual(code, 400)
        self.assertIn('cannot finalize', body['error'])

    def test_pages_routes_and_images(self):
        self.start()
        for page in ('/', '/verify', '/verify/', '/verify/admin', '/verify/admin/'):
            code, body, headers = self.request('GET', page, raw=True)
            self.assertEqual((code, headers['Content-Type']), (200, 'text/html; charset=utf-8'), page)
            self.assertIn(b'<html', body)
        for gone in ('/review/', '/positives/', '/positives/api/tasks', '/verify/api/nothing', '/static/app.js'):
            self.assertEqual(self.request('GET', gone, 'ann2')[0], 404, gone)
        self.assertEqual(self.request('POST', '/positives/api/positive', 'ann2', {'a': 1})[0], 404)
        images = self.root / 'images'
        (images / 'test').mkdir(parents=True)
        (images / 'test' / 'a.jpg').write_bytes(b'\xff\xd8jpeg')
        (images / 'test' / 'a.txt').write_text('secret')
        (self.root / 'secret.jpg').write_bytes(b'x')
        with patch.object(app, 'IMAGES', images):
            code, body, headers = self.request('GET', '/verify/api/image?path=test/a.jpg', 'ann2', raw=True)
            self.assertEqual((code, headers['Content-Type'], body), (200, 'image/jpeg', b'\xff\xd8jpeg'))
            self.assertEqual(self.request('GET', '/verify/api/image?path=test/a.jpg')[0], 403)        # identity required
            for bad in ('../secret.jpg', 'test/a.txt', 'test/missing.jpg', '%2e%2e/secret.jpg'):
                self.assertEqual(self.request('GET', f'/verify/api/image?path={bad}', 'ann2')[0], 400, bad)


class UsersConfig(Base):
    def test_invalid_users_file_is_rejected(self):
        for cfg in ({'annotators': {'a': 2, 'b': 2}}, {'annotators': {'a': 1}}, {'annotators': {'a': 2}, 'admins': ['a']}, {'annotators': ['a']}):
            (self.dir / 'users.json').write_text(json.dumps(cfg))
            with self.assertRaises(ValueError, msg=cfg):
                self.make()


if __name__ == '__main__':
    unittest.main()
