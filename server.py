"""RCR Full-positive verification server (3 annotators, strict 2-of-3 majority). Python standard library only."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import verification

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
IMAGES = DATA / 'images'
POSITIVE_IN = DATA / 'positive_set_input.jsonl'
VERIFY_DIR = DATA / 'verification'
IDENTITY_HEADER = 'X-Remote-User'   # set by Nginx from $remote_user; never trusted from the browser
PAGES = {'/': 'index.html', '/verify': 'annotate.html', '/verify/': 'annotate.html',
         '/verify/admin': 'admin.html', '/verify/admin/': 'admin.html'}
API_PREFIX = '/verify/api/'


def load(path):
    if not path.is_file():
        return []
    with path.open(encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


class InputIndex:
    """Small in-memory index of the positive-set input, never retaining candidate lists.

    Byte offsets are stable while the input file remains unchanged. Replace the
    input file only after stopping the service, then restart to rebuild the index.
    """

    def __init__(self, path):
        self.path = path
        self.entries = []
        self.by_id = {}
        self.signature = None
        if not path.is_file():
            return
        self.signature = self._signature()
        with path.open('rb') as file:
            while True:
                offset = file.tell()
                line = file.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                row = json.loads(line)
                sample = row['sample_id']
                if sample in self.by_id:
                    raise ValueError(f'{path}: duplicate sample_id: {sample}')
                ids = [c['image_id'] for c in row['candidates']]
                seed = row['seed_target_image_id']
                marked = [c['image_id'] for c in row['candidates'] if c.get('is_seed')]
                if not ids or len(ids) != len(set(ids)) or ids[0] != seed or marked != [seed]:
                    raise ValueError(f'{sample}: invalid positive catalog')
                self.by_id[sample] = len(self.entries)
                self.entries.append({'sample_id': sample, 'case_type': row['case_type'], 'split': row.get('split'), 'offset': offset})
        self._check_unchanged()

    def _signature(self):
        stat = self.path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _check_unchanged(self):
        if self._signature() != self.signature:
            raise ValueError('input file changed; stop and restart server to rebuild index')

    def read(self, index):
        if not 0 <= index < len(self.entries):
            raise ValueError('task index out of range')
        self._check_unchanged()
        with self.path.open('rb') as file:
            file.seek(self.entries[index]['offset'])
            row = json.loads(file.readline())
        if row['sample_id'] != self.entries[index]['sample_id']:
            raise ValueError('input changed; stop and restart server to rebuild index')
        return row


def publish(path, rows):
    """Atomically replace a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def image_relative_path(url):
    if not isinstance(url, str):
        raise ValueError('image URL must be a string')
    p = urlparse(url)
    value = parse_qs(p.query).get('d', [p.path])[0]
    value = unquote(value).replace('\\', '/')
    for marker in ('/PIPA/images/', '/images/'):
        if marker in value:
            value = value.split(marker, 1)[1]
            break
    value = value.lstrip('/')
    relative = Path(value)
    if not value or relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f'unsafe image path: {url!r}')
    return relative


def image_url(value):
    return f'{API_PREFIX}image?path=' + quote(image_relative_path(value).as_posix(), safe='/')


def validate_positive(source, payload):
    """Validate one selection against the original candidate catalog. The seed is neither required nor forced."""
    sample = source['sample_id']
    if payload.get('sample_id') != sample:
        raise ValueError('sample_id mismatch')
    candidates = [c['image_id'] for c in source['candidates']]
    if not candidates or len(candidates) != len(set(candidates)) or candidates[0] != source['seed_target_image_id']:
        raise ValueError(f'{sample}: invalid candidate catalog')
    ids = payload.get('positive_image_ids')
    if not isinstance(ids, list) or any(not isinstance(x, str) or not x for x in ids):
        raise ValueError('positive_image_ids must be a list of strings')
    if len(ids) != len(set(ids)) or set(ids) - set(candidates):
        raise ValueError('duplicate or unknown positive image ID')
    return {'sample_id': sample, 'positive_image_ids': [x for x in candidates if x in ids]}


class Forbidden(Exception):
    pass


def verify_task(row, saved, annotator):
    """Task for independent verification: no seed marker, no pre-selection, seed-blind candidate order."""
    def order(c):
        return hashlib.sha256(f"{annotator}|{row['sample_id']}|{c['image_id']}".encode()).digest()
    return {'sample_id': row['sample_id'], 'case_type': row['case_type'], 'subjects': row['subjects'],
            'query': {'image_id': row['query_image_id'], 'image_url': image_url(row['query_image_url']), 'boxes': []},
            'final_desc': row['final_desc'], 'final_change': row['final_change'],
            'candidates': [{'image_id': c['image_id'], 'image_url': image_url(c['image_url']), 'boxes': c.get('subject_boxes', [])}
                           for c in sorted(row['candidates'], key=order)],
            'positive_image_ids': saved['positive_image_ids'] if saved else []}


class VerifyStore:
    """One annotator's private Validation/Test results. A record in the file means the task was explicitly completed."""

    def __init__(self, input_index, entries, annotator, output):
        self.input, self.rows, self.annotator, self.output = input_index, entries, annotator, output
        self.indices = {e['sample_id']: i for i, e in enumerate(entries)}
        existing = load(output)
        if len({x['sample_id'] for x in existing}) != len(existing):
            raise ValueError(f'{output}: duplicate sample_id')
        if any(x['sample_id'] not in self.indices for x in existing):
            raise ValueError(f'{output}: sample_id is not a validation/test verification task')
        self.saved = {x['sample_id']: validate_positive(input_index.read(input_index.by_id[x['sample_id']]), x) for x in existing}
        self.lock = threading.Lock()

    def tasks(self):
        tasks = [{'index': i, 'sample_id': e['sample_id'], 'case_type': e['case_type'], 'split': e['split'],
                  'completed': e['sample_id'] in self.saved} for i, e in enumerate(self.rows)]
        done = len(self.saved)
        return {'total': len(tasks), 'completed': done, 'pending': len(tasks) - done,
                'case_types': list(verification.CASES), 'tasks': tasks}

    def task(self, index=None, sample_id=None):
        if sample_id is not None:
            if sample_id not in self.indices:
                raise ValueError('unknown sample_id')
            index = self.indices[sample_id]
        index = 0 if index is None else index
        if not 0 <= index < len(self.rows):
            raise ValueError('task index out of range')
        sample = self.rows[index]['sample_id']
        row = self.input.read(self.input.by_id[sample])
        saved = self.saved.get(sample)
        overview = self.tasks()
        return {**{k: overview[k] for k in ('total', 'completed', 'pending')}, 'index': index,
                'is_completed': saved is not None, 'task': verify_task(row, saved, self.annotator)}

    def save(self, payload):
        if payload.get('complete') is not True:
            raise ValueError('explicit task completion required: send complete=true')
        sample = payload.get('sample_id')
        if sample not in self.indices:
            raise ValueError('unknown sample_id')
        row = validate_positive(self.input.read(self.input.by_id[sample]), payload)
        if not row['positive_image_ids'] and payload.get('confirm_empty') is not True:
            raise ValueError('0 positives must be confirmed explicitly (confirm_empty=true)')
        with self.lock:
            updated = dict(self.saved)
            updated[sample] = row
            publish(self.output, (updated[e['sample_id']] for e in self.rows if e['sample_id'] in updated))
            self.saved = updated
        return row


class Verification:
    """Roles, independent Annotator 2/3 stores, and the administrator's majority-vote report/export.

    Annotator 1 is the manually supplied original positive_sets.jsonl: it is only ever read, never written.
    """

    def __init__(self, directory, positive_input):
        self.dir, self.input = Path(directory), positive_input
        self.paths = [self.dir / f'positive_sets_annotator_{k}.jsonl' for k in (1, 2, 3)]
        self.final, self.stats = self.dir / 'positive_sets_final.jsonl', self.dir / 'statistics'
        self.lock = threading.Lock()
        cfg = json.loads((self.dir / 'users.json').read_text(encoding='utf-8'))
        self.annotators, self.admins = cfg.get('annotators', {}), set(cfg.get('admins', []))
        if (not isinstance(self.annotators, dict) or any(v not in (2, 3) or isinstance(v, bool) for v in self.annotators.values())
                or len(set(self.annotators.values())) != len(self.annotators) or self.admins & set(self.annotators)):
            raise ValueError('users.json: "annotators" maps each username to a distinct number 2 or 3; "admins" lists other usernames')
        self.splits = self._split_map()
        self.entries = [dict(e, split=self.splits[e['sample_id']]) for e in positive_input.entries
                        if self.splits[e['sample_id']] in verification.SPLITS]
        self.stores = {k: VerifyStore(positive_input, self.entries, k, self.paths[k - 1]) for k in (2, 3)}

    def role(self, user):
        if user in self.annotators:
            return 'annotator', self.annotators[user]
        return ('admin', None) if user in self.admins else None

    def _split_map(self):
        entries = self.input.entries
        splits = {e['sample_id']: verification.normalize_split(e['split']) for e in entries if e.get('split') is not None}
        manifest, seen = self.dir / 'split_manifest.jsonl', {}
        if manifest.is_file():
            for row in load(manifest):
                sample, split = row.get('sample_id'), verification.normalize_split(row.get('split'))
                if sample not in self.input.by_id or sample in seen:
                    raise ValueError(f'{manifest}: unknown or duplicate sample_id {sample!r}')
                if splits.get(sample, split) != split:
                    raise ValueError(f'{manifest}: split for {sample!r} conflicts with the input row')
                seen[sample] = split
            splits.update(seen)
        missing = [e['sample_id'] for e in entries if e['sample_id'] not in splits]
        if missing:
            raise ValueError(f'split metadata missing for {len(missing)} samples (e.g. {missing[0]!r}): add a "split" field to '
                             f'positive_set_input.jsonl rows or provide {manifest} with {{"sample_id","split"}} rows')
        return splits

    def report(self):
        read = [verification.read_records(p) for p in self.paths]
        recs = [r[0] for r in read]
        errors = [{'annotator': verification.NAMES[k], 'sample_id': s, 'reason': why} for k, r in enumerate(read) for s, why in r[1]]
        ids, excluded = {e['sample_id'] for e in self.entries}, [0, 0, 0]
        for k, r in enumerate(recs):
            for s in r:
                if s not in self.splits:
                    errors.append({'annotator': verification.NAMES[k], 'sample_id': s, 'reason': 'sample_id not in verification input'})
                elif s not in ids:
                    excluded[k] += 1   # Train record: not part of Validation/Test aggregation

        def items():
            for e in self.entries:
                row = self.input.read(self.input.by_id[e['sample_id']])
                cands = [c['image_id'] for c in row['candidates']]
                status, sets, errs = [], [], []
                for k in range(3):
                    got = recs[k].get(e['sample_id'])
                    if got is None:
                        status.append('missing'); sets.append(None)
                    elif len(got) != len(set(got)) or set(got) - set(cands):
                        status.append('invalid'); sets.append(None)
                        errs.append({'annotator': verification.NAMES[k], 'sample_id': e['sample_id'],
                                     'reason': 'duplicate image_id or image_id outside the candidate pool'})
                    else:
                        status.append('ok'); sets.append(set(got))
                yield {'sample_id': e['sample_id'], 'split': e['split'], 'case_type': e['case_type'],
                       'seed': row['seed_target_image_id'], 'candidates': cands, 'status': status, 'sets': sets, 'errors': errs}

        report = verification.build_report(items(), errors, excluded)
        report['files'] = {verification.NAMES[k]: {'file': self.paths[k].name, 'exists': self.paths[k].is_file(),
                                                   'sha256': read[k][2], 'records': len(recs[k])} for k in range(3)}
        return report

    def last_export(self, files):
        path = self.stats / 'summary.json'
        if not path.is_file():
            return {'exists': False}
        try:
            meta = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            return {'exists': True, 'readable': False}
        old = meta.get('inputs', {}).get('annotator_files', {})
        return {'exists': True, 'generated_at': meta.get('generated_at'), 'status': meta.get('status'),
                'up_to_date': all(old.get(n, {}).get('sha256') == f['sha256'] for n, f in files.items())}

    def admin_view(self):
        report = self.report()
        diagnostics = dict(report['diagnostics'], errors=report['diagnostics']['errors'][:50])
        return {'status': report['status'], 'groups': report['groups'], 'diagnostics': diagnostics,
                'files': report['files'], 'final_file': self.final.name, 'last_export': self.last_export(report['files'])}

    def export(self):
        with self.lock:
            report = self.report()
            inputs = {'annotator_files': report['files'],
                      'positive_set_input': {'file': self.input.path.name, 'sha256': verification.sha256_file(self.input.path)}}
            verification.write_exports(report, self.final, self.stats, inputs, protected=self.paths)
        return {'status': 'final', 'final_file': self.final.name, 'statistics_dir': self.stats.name}


class Handler(BaseHTTPRequestHandler):
    verify = None   # set by main() / tests: the Verification instance
    protocol_version = 'HTTP/1.1'

    def principal(self, *roles):
        """(role, annotator) from the proxy-set identity header and the server-side users.json mapping."""
        user = self.headers.get(IDENTITY_HEADER, '').strip()
        role = self.verify.role(user) if user else None
        if role is None or role[0] not in roles:
            raise Forbidden('not authorized for this verification page')
        return role

    def verify_api(self, method, action, qs, payload):
        role, number = self.principal('annotator', 'admin')
        if action == 'me':
            return self.reply({'role': role, 'annotator': number})
        if action.startswith('admin/'):
            if role != 'admin':
                raise Forbidden('administrator only')
            if (method, action) == ('GET', 'admin/stats'):
                return self.reply(self.verify.admin_view())
            if (method, action) == ('POST', 'admin/export'):
                return self.reply(self.verify.export())
            return self.send_error(404)
        if role != 'annotator':
            raise Forbidden('administrators do not annotate; use /verify/admin/')
        store = self.verify.stores[number]
        if method == 'GET' and action == 'tasks':
            self.reply(store.tasks())
        elif method == 'GET' and action == 'task':
            sid = qs.get('id', [None])[0]
            self.reply(store.task(None if sid else int(qs.get('index', ['0'])[0]), sid))
        elif method == 'GET' and action == 'image':
            self.send_image(qs)
        elif method == 'POST' and action == 'positive':
            self.reply({'positive': store.save(payload), 'summary': store.tasks()})
        else:
            self.send_error(404)

    def send_image(self, qs):
        path = image_relative_path(qs.get('path', [''])[0])
        resolved = (IMAGES / path).resolve()
        if not resolved.is_relative_to(IMAGES.resolve()) or not resolved.is_file():
            raise ValueError('image not found or outside image root')
        mime = mimetypes.guess_type(resolved.name)[0] or 'application/octet-stream'
        if not mime.startswith('image/'):
            raise ValueError('not an image')
        self.write_data(resolved.read_bytes(), mime)

    def reply(self, payload, code=200):
        self.write_data(json.dumps(payload, ensure_ascii=False).encode(), 'application/json; charset=utf-8', code)

    def write_data(self, data, mime, code=200):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self, method):
        try:
            url = urlparse(self.path)
            payload = None
            if method == 'POST':   # the body is always read before any authorization failure, keeping keep-alive in sync
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > 1_000_000:
                    raise ValueError('invalid payload size')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('payload must be an object')
            if method == 'GET' and url.path in PAGES:   # pages are static; every API call enforces identity
                self.write_data((ROOT / 'static' / PAGES[url.path]).read_bytes(), 'text/html; charset=utf-8')
            elif url.path.startswith(API_PREFIX):
                self.verify_api(method, url.path[len(API_PREFIX):], parse_qs(url.query), payload)
            else:
                self.send_error(404)
        except Forbidden as exc:
            self.reply({'error': str(exc)}, 403)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.reply({'error': str(exc)}, 400)

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')

    def log_message(self, fmt, *args):
        pass


def main():
    p = argparse.ArgumentParser(description='RCR Full-positive verification server')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8090)
    p.add_argument('--trust-proxy-headers', action='store_true',
                   help=f'allow a non-loopback --host; only behind a proxy that overwrites {IDENTITY_HEADER}')
    args = p.parse_args()
    if not (VERIFY_DIR / 'users.json').is_file():
        raise SystemExit(f'Missing {VERIFY_DIR / "users.json"}: copy users.json.example there and set your usernames.')
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not args.trust_proxy_headers:
        raise SystemExit(f'Identity comes from the {IDENTITY_HEADER} header; bind to 127.0.0.1 behind Nginx or pass --trust-proxy-headers.')
    IMAGES.mkdir(parents=True, exist_ok=True)
    verify = Verification(VERIFY_DIR, InputIndex(POSITIVE_IN))
    cls = type('VerificationHandler', (Handler,), {'verify': verify})
    with ThreadingHTTPServer((args.host, args.port), cls) as server:
        print(f'Verification server: http://{args.host}:{args.port}/verify/ (admin: /verify/admin/)', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
