"""3-annotator Full-positive verification: tolerant reading, strict 2-of-3 voting, statistics, exports.

Standard library only and independent of server.py. Candidate-level votes are binary (1 = Positive).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SPLITS = ('validation', 'test')
CASES = ('INDIVIDUAL', 'GROUP', 'DUAL', 'RELATIONAL')
NAMES = ('A1', 'A2', 'A3')
PAIRS = ((0, 1), (0, 2), (1, 2))
SPLIT_ALIASES = {'train': 'train', 'val': 'validation', 'validation': 'validation', 'test': 'test'}

DEFINITIONS = {
    'units': '*_pct values are percentages in [0, 100]; *_rate and kappa/agreement-specific values are fractions in [0, 1] (kappa in [-1, 1]). null = undefined (see the matching note).',
    'population': 'Agreement, voting, original-verification and distribution statistics use only queries for which all 3 annotators have a valid record ("complete queries"). Coverage uses every query. A pair is one (query, candidate image).',
    'majority_vote': 'Candidate t of query q is Final-positive iff at least 2 of the 3 annotators selected it. Missing records are never treated as Negative.',
    'agreement_3way_pct': '100 * pairs with 3/3 identical votes / evaluated pairs.',
    'pairwise_agreement_pct': '100 * (n11 + n00) / n for an annotator pair (observed agreement p_o).',
    'cohen_kappa': '(p_o - p_e) / (1 - p_e); p_e = pa*pb + (1-pa)*(1-pb) from each annotator\'s Positive proportion. null when p_e = 1.',
    'mean_cohen_kappa': 'Arithmetic mean of the defined pairwise Cohen kappas (undefined ones excluded).',
    'fleiss_kappa': '(P_bar - P_e) / (1 - P_e) with 3 raters x 2 categories: item agreement P_i = (sum_j n_ij (n_ij - 1)) / (3*2), P_bar = mean P_i, P_e = p1^2 + p0^2 from pooled category proportions. null when P_e = 1.',
    'positive_specific_agreement': 'Pooled over the 3 annotator pairs: 2 * (both Positive) / (Positive votes of both annotators). Equals sum_i C(s_i,2) / sum_i s_i with s_i = Positive votes of item i. Pairwise: 2*n11 / (2*n11 + n10 + n01).',
    'negative_specific_agreement': 'Same as positive-specific agreement with the Negative label.',
    'voting': 'unanimous_positive = 3/3 Positive; majority_positive = 2/3 Positive; majority_negative = 1/3 Positive; unanimous_negative = 0/3 Positive. Rates use evaluated pairs as denominator; final_positives_*_pct use final positive pairs.',
    'confirmation_rate': '|P1 ∩ Pfinal| / |P1| over pairs (P1 = Annotator 1 positives).',
    'rejection_rate': '|P1 \\ Pfinal| / |P1|.',
    'new_positives': '|Pfinal \\ P1|; limited to the existing candidate pool, not exhaustive relevance coverage of the gallery.',
    'positive_distribution': 'Positives per complete query. std is the sample standard deviation (n-1); queries_with_multiple_pct = 100 * queries with >1 positive / complete queries.',
    'query_level': 'Set comparisons per query: identical_all3 = A1 = A2 = A3; with_disagreement = not identical; final_differs_a1 = Final != A1; gained/lost = Final adds/removes at least one A1 positive.',
    'seed_rejected': 'Query whose seed target is not in the final positive set. Downstream RCR validation requires the seed as a positive; such queries are reported, never repaired.',
}


def normalize_split(value):
    key = value.strip().lower() if isinstance(value, str) else None
    if key not in SPLIT_ALIASES:
        raise ValueError(f'invalid split: {value!r} (expected train, validation/val or test)')
    return SPLIT_ALIASES[key]


def is_final_positive(votes):
    return sum(votes) >= 2


def pct(n, d):
    return None if not d else 100 * n / d


def rate(n, d):
    return None if not d else n / d


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def read_records(path):
    """Tolerantly read an annotator JSONL. Returns (records, errors, sha256); never raises on bad rows."""
    records, errors, bad = {}, [], set()
    raw = Path(path).read_bytes() if Path(path).is_file() else b''
    digest = hashlib.sha256(raw).hexdigest()
    try:
        lines = raw.decode('utf-8').split('\n')
    except UnicodeDecodeError:
        return {}, [(None, 'file is not valid UTF-8')], digest
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            errors.append((None, f'line {number}: invalid JSON'))
            continue
        sample = row.get('sample_id') if isinstance(row, dict) else None
        ids = row.get('positive_image_ids') if isinstance(row, dict) else None
        sample = sample if isinstance(sample, str) else None
        if sample is None or not isinstance(ids, list) or any(not isinstance(x, str) or not x for x in ids):
            errors.append((sample, f'line {number}: malformed record'))
            if sample:
                bad.add(sample)
                records.pop(sample, None)
            continue
        if sample in records or sample in bad:
            errors.append((sample, f'line {number}: duplicate or conflicting sample_id'))
            bad.add(sample)
            records.pop(sample, None)
            continue
        records[sample] = ids
    return records, errors, digest


def cohen(t):
    """t = [n00, n01, n10, n11] with index 2*a + b. Returns (p_o, p_e, kappa, note)."""
    n = sum(t)
    if not n:
        return None, None, None, 'no evaluated pairs'
    ra, rb = t[2] + t[3], t[1] + t[3]
    po = (t[0] + t[3]) / n
    pa, pb = ra / n, rb / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if (ra == 0 and rb == 0) or (ra == n and rb == n):
        return po, pe, None, 'p_e = 1 (both annotators used one identical label); kappa undefined'
    return po, pe, (po - pe) / (1 - pe), None


def fleiss(h):
    """h[s] = number of items with s Positive votes out of 3."""
    n = sum(h)
    if not n:
        return None, 'no evaluated pairs'
    positives = sum(s * h[s] for s in range(4))
    if positives in (0, 3 * n):
        return None, 'P_e = 1 (all ratings use one label); kappa undefined'
    p_bar = sum(h[s] * (s * (s - 1) + (3 - s) * (2 - s)) for s in range(4)) / (6 * n)
    p1 = positives / (3 * n)
    pe = p1 ** 2 + (1 - p1) ** 2
    return (p_bar - pe) / (1 - pe), None


def specific(both, discordant):
    return None if not (2 * both + discordant) else 2 * both / (2 * both + discordant)


def dist_stats(xs):
    n = len(xs)
    return {'queries': n, 'total_positive_links': sum(xs),
            'mean': statistics.fmean(xs) if n else None, 'median': statistics.median(xs) if n else None,
            'std': statistics.stdev(xs) if n > 1 else None, 'min': min(xs) if n else None, 'max': max(xs) if n else None,
            'queries_with_0': sum(x == 0 for x in xs), 'queries_with_1': sum(x == 1 for x in xs),
            'queries_with_more_than_1': sum(x > 1 for x in xs), 'queries_with_multiple_pct': pct(sum(x > 1 for x in xs), n)}


class Group:
    def __init__(self, split, qtype):
        self.split, self.qtype = split, qtype
        self.queries = self.pairs = self.complete_queries = self.complete_pairs = 0
        self.done, self.invalid = [0, 0, 0], [0, 0, 0]
        self.hist = [0, 0, 0, 0]
        self.table = {p: [0, 0, 0, 0] for p in PAIRS}
        self.pos_votes = [0, 0, 0]
        self.kept = self.removed = self.added = 0
        self.counts = [[], [], [], []]
        self.q = Counter()
        self.dist = [Counter(), Counter()]

    def add_catalog(self, n_candidates, status):
        self.queries += 1
        self.pairs += n_candidates
        for k, s in enumerate(status):
            self.done[k] += s == 'ok'
            self.invalid[k] += s == 'invalid'

    def add_complete(self, n_candidates, sets, seed, votes, final):
        self.complete_queries += 1
        self.complete_pairs += n_candidates
        for v in votes:
            self.hist[sum(v)] += 1
            for p in PAIRS:
                self.table[p][2 * v[p[0]] + v[p[1]]] += 1
            for k in range(3):
                self.pos_votes[k] += v[k]
        fin, p1 = set(final), sets[0]
        self.kept += len(p1 & fin)
        self.removed += len(p1 - fin)
        self.added += len(fin - p1)
        for k, s in enumerate(sets):
            self.counts[k].append(len(s))
        self.counts[3].append(len(fin))
        identical = sets[0] == sets[1] == sets[2]
        gained, lost = bool(fin - p1), bool(p1 - fin)
        self.q['identical_all3'] += identical
        self.q['with_disagreement'] += not identical
        self.q['final_differs_a1'] += fin != p1
        self.q['gained'] += gained
        self.q['lost'] += lost
        self.q['gained_and_lost'] += gained and lost
        self.q['seed_rejected'] += seed not in fin
        self.dist[0][len(p1)] += 1
        self.dist[1][len(fin)] += 1

    def result(self):
        n, qn, h = self.complete_pairs, self.complete_queries, self.hist
        pairwise, kappa = {}, {}
        for (i, j) in PAIRS:
            name, t = f'{NAMES[i]}_{NAMES[j]}', self.table[(i, j)]
            po, pe, k, note = cohen(t)
            pairwise[name] = {'evaluated_pairs': n, 'agreement_pct': None if po is None else 100 * po,
                              'positive_specific_agreement': specific(t[3], t[1] + t[2]),
                              'negative_specific_agreement': specific(t[0], t[1] + t[2])}
            kappa[name] = {'evaluated_pairs': n, 'p_o': po, 'p_e': pe, 'kappa': k, 'note': note}
        defined = [v['kappa'] for v in kappa.values() if v['kappa'] is not None]
        fk, fnote = fleiss(h)
        pos_den, neg_den = h[1] + 2 * h[2] + 3 * h[3], 3 * h[0] + 2 * h[1] + h[2]
        p1_total, final_pairs = self.pos_votes[0], h[2] + h[3]
        zero_final = sum(x == 0 for x in self.counts[3])
        return {
            'split': self.split, 'query_type': self.qtype,
            'coverage': {
                'queries': self.queries, 'candidate_pairs': self.pairs,
                'annotators': {NAMES[k]: {'completed_queries': self.done[k], 'pending_queries': self.queries - self.done[k],
                                          'completion_pct': pct(self.done[k], self.queries), 'invalid_records': self.invalid[k]}
                               for k in range(3)},
                'queries_all_three_valid': qn, 'pairs_all_three_valid': n,
                'incomplete_queries': self.queries - qn, 'invalid_records_total': sum(self.invalid)},
            'agreement': {
                'evaluated_pairs': n, 'agreement_3way_pct': pct(h[0] + h[3], n),
                'pairwise': pairwise, 'cohen_kappa': kappa,
                'mean_cohen_kappa': {'value': statistics.fmean(defined) if defined else None, 'defined_pairs': len(defined),
                                     'note': None if defined else 'no defined pairwise kappa'},
                'fleiss_kappa': {'value': fk, 'evaluated_pairs': n, 'note': fnote},
                'positive_specific_agreement': {'value': rate(h[2] + 3 * h[3], pos_den), 'evaluated_pairs': n,
                                                'note': None if pos_den else 'no Positive votes'},
                'negative_specific_agreement': {'value': rate(h[1] + 3 * h[0], neg_den), 'evaluated_pairs': n,
                                                'note': None if neg_den else 'no Negative votes'}},
            'voting': {
                'evaluated_pairs': n, 'unanimous_positive_3of3': h[3], 'unanimous_negative_3of3': h[0],
                'majority_positive_2of3': h[2], 'majority_negative_2of3': h[1],
                'unanimous_agreement_rate': rate(h[0] + h[3], n), 'non_unanimous_majority_rate': rate(h[1] + h[2], n),
                'final_positive_pairs': final_pairs,
                'final_positives_3of3_pct': pct(h[3], final_pairs), 'final_positives_2of3_pct': pct(h[2], final_pairs)},
            'original_verification': {
                'original_positive_links': p1_total, 'retained': self.kept, 'removed': self.removed,
                'new_positives_added': self.added,
                'confirmation_rate': rate(self.kept, p1_total), 'rejection_rate': rate(self.removed, p1_total),
                'net_positive_change': self.added - self.removed,
                'queries_changed': self.q['final_differs_a1'], 'queries_unchanged': qn - self.q['final_differs_a1'],
                'original_seeds_rejected': self.q['seed_rejected']},
            'positive_distribution': {name: dist_stats(self.counts[k]) for k, name in enumerate(NAMES + ('final',))},
            'query_level': {
                'complete_queries': qn, 'identical_all3': self.q['identical_all3'], 'with_disagreement': self.q['with_disagreement'],
                'final_differs_a1': self.q['final_differs_a1'], 'gained_positives': self.q['gained'],
                'lost_positives': self.q['lost'], 'gained_and_lost': self.q['gained_and_lost'],
                'count_distribution': {name: {str(c): v for c, v in sorted(d.items())}
                                       for name, d in (('A1', self.dist[0]), ('final', self.dist[1]))}},
            'diagnostics': {'final_zero_positive_queries': zero_final,
                            'downstream_seed_requirement_violations': self.q['seed_rejected']},
        }


def build_report(items, file_errors=(), excluded=None):
    """items: dicts {sample_id, split, case_type, seed, candidates[list], status[3], sets[3 sets or None], errors[]}.

    Only queries with 3 valid records enter agreement/voting; the report is 'partial' otherwise.
    """
    groups = {('overall', 'ALL'): Group('overall', 'ALL')}
    for split in SPLITS:
        for qtype in ('ALL',) + CASES:
            groups[(split, qtype)] = Group(split, qtype)
    per_query, disagreements, final_rows, errors = [], [], [], list(file_errors)
    zero, rejected, complete = [], [], True
    for it in items:
        cands, sets, status, seed = it['candidates'], it['sets'], it['status'], it['seed']
        errors.extend(it.get('errors', []))
        mine = [groups[('overall', 'ALL')], groups[(it['split'], 'ALL')]]
        if (it['split'], it['case_type']) in groups:
            mine.append(groups[(it['split'], it['case_type'])])
        for g in mine:
            g.add_catalog(len(cands), status)
        row = {'sample_id': it['sample_id'], 'split': it['split'], 'case_type': it['case_type'],
               'complete': all(s == 'ok' for s in status)}
        for k, name in enumerate(NAMES):
            row[f'{name.lower()}_status'] = status[k]
            row[f'{name.lower()}_positives'] = None if sets[k] is None else len(sets[k])
        if not row['complete']:
            complete = False
            row.update(final_positives=None, added_positives=None, removed_positives=None, agreement_rate=None,
                       final_changed=None, seed_rejected=None)
            per_query.append(row)
            continue
        votes, final = [], []
        for t in cands:
            v = tuple(int(t in s) for s in sets)
            votes.append(v)
            if is_final_positive(v):
                final.append(t)
            if 0 < sum(v) < 3:
                disagreements.append({'sample_id': it['sample_id'], 'image_id': t, 'split': it['split'],
                                      'query_type': it['case_type'], 'a1': v[0], 'a2': v[1], 'a3': v[2],
                                      'final': int(is_final_positive(v))})
        for g in mine:
            g.add_complete(len(cands), sets, seed, votes, final)
        fin = set(final)
        row.update(final_positives=len(fin), added_positives=len(fin - sets[0]), removed_positives=len(sets[0] - fin),
                   agreement_rate=sum(sum(v) in (0, 3) for v in votes) / len(votes) if votes else None,
                   final_changed=fin != sets[0], seed_rejected=seed not in fin)
        per_query.append(row)
        final_rows.append({'sample_id': it['sample_id'], 'positive_image_ids': final})
        if not fin:
            zero.append(it['sample_id'])
        if seed not in fin:
            rejected.append(it['sample_id'])
    complete = complete and bool(per_query)
    return {
        'status': 'complete' if complete else 'partial', 'complete': complete,
        'groups': [g.result() for g in groups.values()], 'per_query': per_query,
        'disagreements': disagreements, 'final': final_rows,
        'diagnostics': {'error_count': len(errors), 'errors': errors,
                        'excluded_non_verification_records': dict(zip(NAMES, excluded or (0, 0, 0))),
                        'final_zero_positive_sample_ids': zero, 'seed_rejected_sample_ids': rejected,
                        'seed_note': 'Queries with a rejected seed or 0 final positives are kept as voted and would fail downstream RCR validation that requires the seed as a positive.'}}


def describe_incomplete(report):
    c = report['groups'][0]['coverage']
    missing = ', '.join(f"{n}: {a['pending_queries']}" for n, a in c['annotators'].items())
    return (f"cannot finalize: only {c['queries_all_three_valid']}/{c['queries']} queries have three valid annotation "
            f"records (pending or invalid per annotator - {missing}); {report['diagnostics']['error_count']} invalid records")


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    with tmp.open('w', encoding='utf-8', newline='') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _csv(header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow(header)
    for row in rows:
        writer.writerow(['' if v is None else int(v) if isinstance(v, bool) else v for v in row])
    return buf.getvalue()


def flatten(d, prefix=''):
    out = {}
    for key, value in d.items():
        if key == 'count_distribution':
            continue
        if isinstance(value, dict):
            out.update(flatten(value, f'{prefix}{key}_'))
        elif not isinstance(value, list):
            out[f'{prefix}{key}'] = value
    return out


def agreement_rows(g):
    a, split, qtype = g['agreement'], g['split'], g['query_type']
    rows = [(split, qtype, 'agreement_3way_pct', 'A1,A2,A3', a['evaluated_pairs'], a['agreement_3way_pct'], None)]
    for name, v in a['pairwise'].items():
        annotators = name.replace('_', ',')
        rows.append((split, qtype, 'pairwise_agreement_pct', annotators, v['evaluated_pairs'], v['agreement_pct'], None))
        rows.append((split, qtype, 'positive_specific_agreement', annotators, v['evaluated_pairs'], v['positive_specific_agreement'], None))
        rows.append((split, qtype, 'negative_specific_agreement', annotators, v['evaluated_pairs'], v['negative_specific_agreement'], None))
        k = a['cohen_kappa'][name]
        rows.append((split, qtype, 'cohen_kappa', annotators, k['evaluated_pairs'], k['kappa'], k['note']))
    m = a['mean_cohen_kappa']
    rows.append((split, qtype, 'mean_cohen_kappa', 'A1,A2,A3', m['defined_pairs'], m['value'], m['note']))
    for key in ('fleiss_kappa', 'positive_specific_agreement', 'negative_specific_agreement'):
        rows.append((split, qtype, key + ('_pooled' if 'specific' in key else ''), 'A1,A2,A3', a[key]['evaluated_pairs'], a[key]['value'], a[key]['note']))
    return rows


def write_exports(report, final_path, stats_dir, inputs, protected=()):
    """Write the final JSONL and statistics. Refuses incomplete reports and never touches protected files."""
    if not report['complete']:
        raise ValueError(describe_incomplete(report))
    final_path, stats_dir = Path(final_path), Path(stats_dir)
    if final_path.resolve() in {Path(p).resolve() for p in protected}:
        raise ValueError('refusing to overwrite an annotator file')
    groups = report['groups']
    flat = [flatten({'split': g['split'], 'query_type': g['query_type'], **{k: v for k, v in g.items() if k not in ('split', 'query_type')}}) for g in groups]
    _atomic_write(stats_dir / 'summary.csv', _csv(list(flat[0]), [list(r.values()) for r in flat]))
    _atomic_write(stats_dir / 'agreement.csv', _csv(
        ['split', 'query_type', 'statistic', 'annotators', 'evaluated_pairs', 'value', 'note'],
        [row for g in groups for row in agreement_rows(g)]))
    dist_rows = []
    for g in groups:
        v, n = g['voting'], g['voting']['evaluated_pairs']
        for votes, key in ((3, 'unanimous_positive_3of3'), (2, 'majority_positive_2of3'), (1, 'majority_negative_2of3'), (0, 'unanimous_negative_3of3')):
            dist_rows.append((g['split'], g['query_type'], f'{votes}/3', votes, v[key], pct(v[key], n), n))
    _atomic_write(stats_dir / 'voting_distribution.csv', _csv(
        ['split', 'query_type', 'positive_votes', 'n_positive_votes', 'pairs', 'pct', 'evaluated_pairs'], dist_rows))
    pq_cols = ['sample_id', 'split', 'case_type', 'complete', 'a1_status', 'a2_status', 'a3_status', 'a1_positives', 'a2_positives',
               'a3_positives', 'final_positives', 'added_positives', 'removed_positives', 'agreement_rate', 'final_changed', 'seed_rejected']
    _atomic_write(stats_dir / 'per_query.csv', _csv(pq_cols, [[r[c] for c in pq_cols] for r in report['per_query']]))
    dis_cols = ['sample_id', 'image_id', 'split', 'query_type', 'a1', 'a2', 'a3', 'final']
    _atomic_write(stats_dir / 'disagreements.csv', _csv(dis_cols, [[r[c] for c in dis_cols] for r in report['disagreements']]))
    _atomic_write(final_path, ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in report['final']))
    overall = groups[0]['coverage']
    summary = {'status': 'final', 'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
               'inputs': inputs,
               'dataset': {'queries': overall['queries'], 'candidate_pairs': overall['candidate_pairs'],
                           **{g['split']: g['coverage']['queries'] for g in groups if g['query_type'] == 'ALL' and g['split'] in SPLITS}},
               'definitions': DEFINITIONS, 'diagnostics': report['diagnostics'], 'groups': groups}
    _atomic_write(stats_dir / 'summary.json', json.dumps(summary, indent=2, ensure_ascii=False) + '\n')  # written last: commit marker
