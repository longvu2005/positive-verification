# RCR Full-Positive Verification

A small web server (Python standard library only, no database) for the RCR benchmark's **3-annotator Full-positive verification** of the **Validation and Test** splits:

- Annotator 1 = the existing original annotations (you copy the file in; the server only reads it).
- Annotators 2 and 3 independently re-review every query's candidate images in the browser.
- The administrator generates the final positive sets by **strict per-candidate 2-of-3 majority voting** and exports research statistics (agreement, Cohen's/Fleiss' κ, voting, confirmation of the original positives, ...).

## 1. Layout

```text
server.py            # HTTP server, identity/roles, per-annotator storage, admin API
verification.py      # majority voting, statistics, CSV/JSON export (no server code)
static/{index.html,annotate.html,admin.html}
deploy/{annotation-server.service,nginx.conf}
tests/{test_server.py,test_verification.py}
data/                # your data; see data/README.md (not committed)
```

```text
data/
  images/                              # e.g. data/images/test/abc.jpg
  positive_set_input.jsonl             # verification input
  verification/
    users.json                         # required: maps Nginx usernames to roles
    split_manifest.jsonl               # only if the input rows have no "split"
    positive_sets_annotator_1.jsonl    # you copy the original annotations here
    positive_sets_annotator_2.jsonl    # written by Annotator 2's page
    positive_sets_annotator_3.jsonl    # written by Annotator 3's page
    positive_sets_final.jsonl          # written by the administrator action (majority vote)
    statistics/{summary.json,summary.csv,agreement.csv,voting_distribution.csv,per_query.csv,disagreements.csv}
```

All four result files use exactly the schema `{"sample_id": "...", "positive_image_ids": ["...", "..."]}`; no annotator IDs, votes or timestamps. All metadata lives in `statistics/`. Output order is the input task order; IDs inside a record follow the canonical candidate order.

## 2. Input data

`positive_set_input.jsonl` has one task per line (same schema as the research pipeline's positive-set preparation output): `sample_id`, `case_type` (`INDIVIDUAL`/`GROUP`/`DUAL`/`RELATIONAL`), `subjects`, `final_desc`, `final_change`, `query_image_id`, `query_image_url`, `seed_target_image_id`, and `candidates` (`image_id`, `image_url`, `is_seed`, `subject_boxes`; the seed is the first candidate and the only one marked `is_seed`). The server validates this catalog at startup. Do not replace or reorder the input during a live annotation batch; stop the service first.

Image URLs such as `/PIPA/images/test/abc.jpg`, `/images/test/abc.jpg` or `test/abc.jpg` are served from `data/images/test/abc.jpg`.

**Split metadata.** Verification needs each query's split. Either add a `"split"` field (`train`, `validation`/`val`, `test`) to every input row, or provide `data/verification/split_manifest.jsonl` with one `{"sample_id": "...", "split": "..."}` line per sample (see `data/verification/split_manifest.jsonl.example`). The manifest is validated (unknown/duplicate samples, invalid splits, conflicts with row fields and uncovered samples are errors). Splits are never guessed from sample IDs or image paths. Train queries are never shown to annotators or aggregated.

**Annotator 1.** Copy the original output once:

```bash
cp /path/to/original/positive_sets.jsonl data/verification/positive_sets_annotator_1.jsonl
```

The server only reads this file (it is re-read on every statistics refresh, so you may copy it while the server runs). A record means the original task was completed: selected IDs are Positive and all other candidates of that query are Negative. Missing records are *not* created (that query stays pending and blocks finalization), the seed is never added, Train records are ignored, and unknown sample IDs or candidate IDs outside the pool are reported as invalid. The export code refuses to write to any annotator file.

## 3. Identity and setup

Identity is **not** taken from anything the browser sends. Nginx authenticates (Basic Auth) and passes `X-Remote-User $remote_user` (see `deploy/nginx.conf`; it overwrites any client-supplied header). The server maps that username to a role with `data/verification/users.json` (copy `data/verification/users.json.example`):

```json
{"annotators": {"alice": 2, "bob": 3}, "admins": ["carol"]}
```

Annotators 2 and 3 each map to exactly one username; admins must be different usernames. The server refuses to start without `users.json`, and refuses a non-loopback `--host` unless `--trust-proxy-headers` is passed (only do that behind a proxy that overwrites `X-Remote-User`).

## 4. Run locally

```bash
python3 --version          # 3.10+
python3 server.py --host 127.0.0.1 --port 8090
curl -H 'X-Remote-User: alice' http://127.0.0.1:8090/verify/api/tasks
```

Pages: `/` (links), `/verify/` (annotators), `/verify/admin/` (administrator). Browsers do not send `X-Remote-User` without Nginx; for local browser testing use a header-modifying extension or put Nginx in front. Run the tests with `python3 -m unittest discover -s tests -v`.

## 5. Deploy on AWS Lightsail (Ubuntu)

Provision Ubuntu 22.04/24.04 and point a DNS **A record** (e.g. `annotation.example.com`) to its static IP. In the Lightsail firewall allow TCP 22 from your IP and TCP 80/443; do **not** open 8090.

```bash
# from your machine
scp -r annotation-server ubuntu@YOUR_STATIC_IP:/home/ubuntu/
scp positive_set_input.jsonl ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/
scp -r data/verification ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/   # users.json + Annotator 1 file (+ manifest)
rsync -av --progress ./images/ ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/images/
```

On the server:

```bash
sudo apt update
sudo apt install -y python3 nginx apache2-utils certbot python3-certbot-nginx
sudo cp ~/annotation-server/deploy/annotation-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now annotation-server
sudo systemctl status annotation-server --no-pager
```

Edit `deploy/nginx.conf` (domain), then create one Nginx account per person (the usernames used in `users.json`):

```bash
sudo cp ~/annotation-server/deploy/nginx.conf /etc/nginx/sites-available/annotation-server
sudo htpasswd -c /etc/nginx/.annotation-users alice     # -c only for the first account
sudo htpasswd /etc/nginx/.annotation-users bob
sudo htpasswd /etc/nginx/.annotation-users carol
sudo ln -s /etc/nginx/sites-available/annotation-server /etc/nginx/sites-enabled/annotation-server
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d annotation.example.com
```

**Important:** Basic Auth credentials must only be entered over **HTTPS**; set up DNS and certbot before giving out the URL (or use a VPN/SSH tunnel). Logs: `sudo journalctl -u annotation-server -f`. Make periodic backups of `data/verification/` and the input JSONL; every save atomically replaces its file. Run a single server per data directory.

## 6. Annotator workflow (Annotators 2 and 3)

Open `https://YOUR_DOMAIN/verify/`. The server decides the annotator from the login:

- Candidates are shown in a deterministic shuffled order (per annotator and query) with no seed marker, nothing pre-selected and nothing locked, so the seed can be rejected and its position is not revealed. The page shows the reference image, the `IDENTIFY`/`CHANGE` text, Subject boxes, and every candidate.
- **Complete** (`Ctrl/⌘+Enter` = *Complete & Next Pending*) saves the judgment and marks the task completed; a saved record means "completed". Selecting **0** candidates is a valid judgment but needs an explicit confirmation (the API requires `complete: true`, plus `confirm_empty: true` for an empty set). Completed tasks can be re-opened and updated. Unsaved selections are lost if you leave a task (a guard asks first); progress is saved task by task, so you can stop and resume at any time, even after a server restart.
- Filters (Pending/Completed/All, case type, search) and progress work as in a normal task queue.
- Each annotator only ever loads their own file; nobody sees another annotator's selections or any voting result.

## 7. Administrator: majority voting and export

Open `https://YOUR_DOMAIN/verify/admin/`. It shows progress, agreement, κ, voting percentages, original-positive confirmation, added/removed positives, final counts and zero-positive diagnostics for Overall, Validation, Test and each query type. Until all 3 annotators have a valid record for every Validation/Test query the status is **Partial** (statistics use only queries with 3 valid records) and **Generate final result & statistics** is disabled; the API also rejects it.

The action writes `positive_sets_final.jsonl` and `statistics/` atomically (`summary.json` is written last and records SHA-256 hashes of the annotator files, so the page can warn when an export is **stale**). Re-running with unchanged inputs reproduces identical files apart from `generated_at`; the three annotator files are never modified.

Voting, for every query `q` and candidate `t`:

```python
votes = [int(t in P1[q]), int(t in P2[q]), int(t in P3[q])]
is_positive = sum(votes) >= 2
```

A query whose majority gives 0 positives, or whose final set lacks the seed, is kept as voted and listed under `diagnostics` (`final_zero_positive_sample_ids`, `seed_rejected_sample_ids`). Downstream RCR validation that requires a seed positive will need to handle such queries; the majority decision is never altered.

## 8. Statistics definitions

Groups: Overall (val+test), Validation, Test, and each of INDIVIDUAL/GROUP/DUAL/RELATIONAL within each split. A **pair** is one (query, candidate image). Agreement, voting, original-verification and distribution statistics use only queries with all 3 valid records (**complete queries**); coverage uses all queries. `*_pct` values are 0–100; `*_rate` and specific-agreement values are 0–1; undefined values are `null` with a `note` (`summary.json`, `agreement.csv`).

| Measure | Definition |
|---|---|
| Completion % | 100 · completed queries / total queries, per annotator (pending = total − completed; invalid records count as pending) |
| 3-way agreement % | 100 · pairs with identical 3/3 votes / evaluated pairs |
| Pairwise agreement % | 100 · (n11 + n00) / n for A1–A2, A1–A3, A2–A3 (observed agreement p_o) |
| Cohen's κ | (p_o − p_e) / (1 − p_e), p_e = pa·pb + (1−pa)(1−pb); `null` if p_e = 1 |
| Mean κ | mean of the defined pairwise κ values (undefined excluded; count reported) |
| Fleiss' κ | (P̄ − P_e) / (1 − P_e); P_i = Σ_j n_ij(n_ij−1) / (3·2), P̄ = mean P_i, P_e = p₁² + p₀² from pooled proportions; **not** the mean of Cohen's κ |
| Positive / negative specific agreement | pooled over the 3 pairs: 2·(both say label) / (label votes of both); = Σ C(s_i,2) / Σ s_i for Positive (s_i = Positive votes) and the analogue for Negative; pairwise values in `agreement.csv` |
| Voting counts | 3/3 Positive, 2/3 Positive (majority Positive), 1/3 Positive (majority Negative), 0/3 Positive (unanimous Negative); rates use evaluated pairs |
| Final positives by support | share of final positive pairs with 3/3 vs exactly 2/3 votes |
| Confirmation / rejection rate | \|P1 ∩ P_final\| / \|P1\| and \|P1 \ P_final\| / \|P1\| (pair level, A1 = original); new positives = \|P_final \ P1\| (only within the existing candidate pool, not exhaustive gallery relevance) |
| Net change / queries changed | added − removed; queries with P_final ≠ P1; seeds rejected = queries whose seed ∉ P_final |
| Positive distribution | for A1, A2, A3, Final: total links, mean, median, sample std (n−1), min, max, #queries with 0 / 1 / >1 positives, % multiple |
| Query level | identical sets across all 3 annotators, ≥1 disagreement, final ≠ A1, gained / lost / both, positive-count distributions before (A1) and after (final) |

Files: `summary.json` (everything + definitions, dataset counts, input hashes, diagnostics), `summary.csv` (one flattened row per group), `agreement.csv` (long format: split, query_type, statistic, annotators, evaluated_pairs, value, note), `voting_distribution.csv` (3/3, 2/3, 1/3, 0/3 positive votes with counts and %), `per_query.csv` (positive counts for A1/A2/A3/Final, added/removed, agreement rate, changed, seed rejected), `disagreements.csv` (non-unanimous pairs with the 3 votes and the final label).

## 9. Limitations and security

- Outputs are plain JSONL/CSV files; run one server per data directory and back them up.
- Annotator identity and roles rest entirely on Nginx authentication plus `users.json`; never expose port 8090 directly.
- No per-task locking: two browser tabs of the *same* annotator can overwrite each other's task; different annotators never share a file.
- The input catalog is indexed at startup; changing the input file while the service runs is detected and rejected until it is restarted.
