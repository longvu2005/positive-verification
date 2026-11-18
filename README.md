# Standalone RCR Annotation Server

Annotation workflows in **one Python standard-library server**:

- **Review:** `/review/` — edit case, Subject→identity assignments, `final_desc`, `final_change`; produces `data/reviewed.jsonl`.
- **Positive Selection:** `/positives/` — select valid candidate target images while retaining the seed; produces `data/positive_sets.jsonl`.
- **Positive Verification (optional):** `/verify/` and `/verify/admin/` — 3-annotator Full-positive verification of the Validation/Test splits with strict 2-of-3 majority voting and SIGIR-ready statistics. See [section 6](#6-positive-verification-3-annotators-majority-vote). Disabled unless `data/verification/users.json` exists; Review and legacy Positive Selection are unchanged.

The two annotation pages use the supplied RCR Review and Full Positive Selection layouts. The backend follows the RCR repository's validation and JSONL output contracts. It does **not** require the research repository, Label Studio, Torch, transformers, DINO, or FAFA. Python **3.10+** is sufficient (Ubuntu 22.04/24.04).

## Annotation interface

- Both pages have a task sidebar with progress, All/Pending/Completed tabs, search by task number or sample ID, and a case filter. The browser remembers the last task and filters separately for each workflow.
- Review has case buttons, Query/Target viewports with independent zoom/pan/reset, and clickable identity boxes. S1 is red and S2 is blue. Clicking an identity updates both images; selecting multiple identities for S1 automatically derives GROUP. Keys `1`/`2` switch the active Subject.
- Positives follows the supplied CHANGE-first layout: two columns of target cards with colored Subject boxes, an explicit selection rail, a locked seed, selection counts, and a Selected only toggle. Candidate coordinates are pixels and are scaled against the loaded image's natural size; review coordinates remain percentages.
- Save updates the current task. Save & Next Pending (also `Ctrl/⌘+Enter`) saves successfully before opening the next unfinished task, wrapping to the beginning. Completed tasks remain editable from the Completed tab.
- Unsaved changes are flagged and guarded when switching tasks or leaving the page. Save/load temporarily locks editing to avoid mixing task data. Failed saves keep the current edits.
- Home and the other workflow are available in each sidebar. The pages are self-contained and keep the existing `/review/api/...` and `/positives/api/...` routes, JSONL schema, validation, and low-RAM server unchanged. No new dependencies are required.

## 1. Input/output structure

```text
annotation-server/
  server.py
  verification.py              # majority voting + statistics + exports (no server code)
  static/{index.html,review.html,positives.html,verify_admin.html,app.js,app.css}
  data/
    images/                    # uploaded images; original split-relative structure
    review_input.jsonl         # upload this to enable Review
    reviewed.jsonl             # generated/updated automatically
    positive_set_input.jsonl   # upload this to enable Positive Selection
    positive_sets.jsonl        # generated/updated automatically
  deploy/{annotation-server.service,nginx.conf}
  tests/test_server.py
  requirements.txt
```

Upload **inputs and images** only. Outputs are created by the web app and can be retrieved after annotation. Empty/missing input files produce an empty task queue rather than a crash. Input JSONL must have the **same schema as the research pipeline's `prepare_review` / `prepare_positives` output**. A `positive_set_input.jsonl` cannot be replaced by `review_input.jsonl`: these are different tasks. Do **not** replace or reorder input tasks during a live annotation batch.

Images may have URLs such as `/PIPA/images/train/abc.jpg` or `/images/train/abc.jpg` or `train/abc.jpg`; upload the referenced image at `data/images/train/abc.jpg`.

**Output compatibility:**

```json
{"sample_id":"...","case_type":"INDIVIDUAL","subjects":[{"subject_id":1,"identity_ids":["..."]}],"final_desc":"Identify Subject 1 as ...","final_change":"then retrieve target images where Subject 1 ..."}
```

```json
{"sample_id":"...","positive_image_ids":["seed_target_id","other_positive_id"]}
```

## 2. Local smoke run

```bash
cd annotation-server
python3 --version
python3 server.py --host 127.0.0.1 --port 8090
```

Visit `http://127.0.0.1:8090/`, `/review/` and `/positives/`. With missing inputs, queues will be empty. Test with `python3 -m unittest discover -s tests -v`.

## 3. Deploy on AWS Lightsail (Ubuntu)

Provision an Ubuntu 22.04 or 24.04 instance. Point a DNS **A record** (e.g. `annotation.example.com`) to its static public IP. In the **Lightsail networking firewall**, allow TCP 22 from your IP and TCP 80/443 from your annotators/the internet. Do **not** open 8090 publicly.

On your Mac/PC after extracting the ZIP:

```bash
# upload package directory (without copying any large data yet)
scp -r annotation-server ubuntu@YOUR_STATIC_IP:/home/ubuntu/

# copy prepared input JSONL and images
scp review_input.jsonl positive_set_input.jsonl ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/
rsync -av --progress ./images/ ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/images/
```

On Lightsail, connected via SSH:

```bash
sudo apt update
sudo apt install -y python3 nginx apache2-utils certbot python3-certbot-nginx
sudo cp /home/ubuntu/annotation-server/deploy/annotation-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now annotation-server
sudo systemctl status annotation-server --no-pager
curl -I http://127.0.0.1:8090/review/
```

Edit `deploy/nginx.conf` to replace `annotation.example.com` with your domain, then:

```bash
sudo cp ~/annotation-server/deploy/nginx.conf /etc/nginx/sites-available/annotation-server
sudo htpasswd -c /etc/nginx/.annotation-users annotator1
# Add a second account without -c (otherwise the original accounts are overwritten):
sudo htpasswd /etc/nginx/.annotation-users annotator2
sudo ln -s /etc/nginx/sites-available/annotation-server /etc/nginx/sites-enabled/annotation-server
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d annotation.example.com
```

**Important:** Basic Auth credentials must only be entered over **HTTPS**. Set up DNS and certbot before giving the URL to annotators. If no domain is available, do not expose this UI over unsecured HTTP; use a trusted VPN or SSH tunnel instead.

Access:
- `https://annotation.example.com/review/`
- `https://annotation.example.com/positives/`

Check logs:

```bash
sudo journalctl -u annotation-server -f
sudo systemctl restart annotation-server
```

## 4. Collect and back up outputs

From your local machine:

```bash
scp ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/reviewed.jsonl ./
scp ubuntu@YOUR_STATIC_IP:/home/ubuntu/annotation-server/data/positive_sets.jsonl ./
```

Make periodic backups of both **output JSONL files** and **input JSONL snapshots**. Each save atomically replaces the output file, preventing partially written lines. Use a persistent Lightsail disk/snapshot and avoid concurrent servers modifying the same data directory.

## 5. Limitations and security

- This is a minimal annotation deployment, **not** a multi-tenant enterprise annotation platform.
- Login is handled by Nginx Basic Auth; the JSONL records are **not stamped with individual annotator usernames**.
- Concurrent editing of the **same sample** uses last-write-wins. Split tasks between annotators and avoid assigning the same sample to different people.
- Any authenticated annotator can navigate/edit the Review and legacy Positive Selection workflows; no per-user restrictions there. The only per-user restrictions are the verification roles in section 6.
- Uploaded images must reside under `data/images/`. File requests reject parent traversal and symlink escapes.
- The app loads task catalogs at startup; **restart** when uploading new input JSONL. Do **not** replace inputs while annotation is underway, because saved output may become stale.
- If you prefer only Review or only Positives, leave the other input file absent.

## 6. Positive verification (3 annotators, majority vote)

Verifies the Full-positive sets of the **Validation and Test** splits with 3 logically independent annotation results and a strict per-candidate 2-of-3 majority. Train queries are never shown or aggregated.

### 6.1 Files

```text
data/verification/
  users.json                         # YOU create: enables verification, maps Nginx usernames to roles
  split_manifest.jsonl               # only if positive_set_input.jsonl rows have no "split" field
  positive_sets_annotator_1.jsonl    # YOU copy here: the original data/positive_sets.jsonl (read-only for the server)
  positive_sets_annotator_2.jsonl    # written by Annotator 2's page
  positive_sets_annotator_3.jsonl    # written by Annotator 3's page
  positive_sets_final.jsonl          # written by the administrator's "Generate" action (majority vote)
  statistics/{summary.json,summary.csv,agreement.csv,voting_distribution.csv,per_query.csv,disagreements.csv}
```

All four result files use exactly the existing schema `{"sample_id": "...", "positive_image_ids": [...]}`; no annotator IDs, votes or timestamps. All metadata lives in `statistics/`. Output order is the input task order; IDs inside a record follow the canonical candidate order.

**Annotator 1 (existing annotations).** Copy the original output once:

```bash
mkdir -p data/verification
cp data/positive_sets.jsonl data/verification/positive_sets_annotator_1.jsonl
```

The server only reads this file (it is re-read on every statistics refresh, so you may copy it while the server runs). A record means the original task was completed: selected IDs are Positive, all other candidates of that query are Negative. Missing records are *not* created (that query stays pending and blocks finalization), the seed is never added, Train records are ignored, and unknown sample IDs or candidate IDs outside the pool are reported as invalid. The finalization code refuses to write to any annotator file.

**Split metadata.** Verification needs each query's split. Either add a `"split"` field (`train`, `validation`/`val`, `test`) to every row of `positive_set_input.jsonl`, or provide `data/verification/split_manifest.jsonl` with one `{"sample_id": "...", "split": "..."}` line per sample. The manifest is validated (unknown/duplicate samples, invalid splits, conflicts with row fields, uncovered samples are errors). Splits are never guessed from sample IDs or image paths.

### 6.2 Identity and setup

Identity is **not** taken from anything the browser sends. Nginx authenticates (Basic Auth) and passes `X-Remote-User $remote_user` (already added to `deploy/nginx.conf`; it overwrites any client-supplied header). The server maps that username to a role using `data/verification/users.json`:

```json
{"annotators": {"alice": 2, "bob": 3}, "admins": ["carol"]}
```

Each of Annotators 2/3 maps to exactly one username; admins must be different usernames. Create the Nginx accounts (`sudo htpasswd /etc/nginx/.annotation-users alice` …), copy the new `nginx.conf`, reload Nginx, and restart the service. With `users.json` present the server must listen on `127.0.0.1` (or you must pass `--trust-proxy-headers` behind a proxy that overwrites the header), otherwise it refuses to start. For local browser testing without Nginx send the header yourself (e.g. a header-modifying browser extension, or `curl -H 'X-Remote-User: alice' …`).

While verification is enabled, users mapped to Annotator 2/3 get `403` on the legacy `/positives/` page/API, because it returns the original (Annotator 1) selections. Administrators and all other users keep the legacy workflows unchanged.

### 6.3 Annotator workflow (Annotators 2 and 3)

Open `https://YOUR_DOMAIN/verify/` (it is the same Positive Selection page in verification mode). The server decides the annotator from the login:

- Candidates are shown in a deterministic shuffled order (per annotator and query) with no seed badge, nothing pre-selected and nothing locked, so the seed can be rejected and its position is not revealed. The page shows the reference image, `IDENTIFY`/`CHANGE` text, Subject boxes, and every candidate.
- **Complete** (`Ctrl/⌘+Enter` = *Complete & Next Pending*) saves the judgment and marks the task completed. A saved record means “completed”. Selecting **0** candidates is a valid judgment, but needs an explicit confirmation (the API requires `complete: true`, plus `confirm_empty: true` for an empty set). Tasks can be re-opened from the Completed tab and updated. Unsaved work is lost if you leave the task (a guard asks first); progress is saved task by task, so you can stop and resume any time, even after a restart.
- Annotators only ever load their own file; they never see the other annotators' selections or any voting result.

### 6.4 Administrator: majority voting and export

Open `https://YOUR_DOMAIN/verify/admin/`. It shows progress, agreement, κ, voting percentages, original-positive confirmation, added/removed positives, final counts and zero-positive diagnostics for Overall, Validation, Test and each query type. Until all 3 annotators have a valid record for every Validation/Test query the status is **Partial** (statistics use only queries with 3 valid records) and **Generate final result & statistics** is disabled; the API also rejects it. The action writes `positive_sets_final.jsonl` and `statistics/` atomically (`summary.json` is written last and records SHA-256 hashes of the annotator files, so the page can warn when an export is **stale**). Re-running it with unchanged inputs reproduces identical files (apart from `generated_at`).

Voting, for every query `q` and candidate `t`:

```python
votes = [int(t in P1[q]), int(t in P2[q]), int(t in P3[q])]
is_positive = sum(votes) >= 2
```

A query whose majority gives 0 positives, or whose final set lacks the seed, is kept as voted and listed under `diagnostics` (`final_zero_positive_sample_ids`, `seed_rejected_sample_ids`). The existing downstream RCR validation requires a seed positive, so such queries will need handling there; the majority decision is never altered.

### 6.5 Statistics definitions

Groups: Overall (val+test), Validation, Test, and each of INDIVIDUAL/GROUP/DUAL/RELATIONAL within each split. A **pair** is one (query, candidate image). Agreement, voting, original-verification and distribution statistics use only queries with all 3 valid records (**complete queries**); coverage uses all queries. `*_pct` values are 0–100, `*_rate` and specific-agreement values are 0–1; undefined values are `null` with a `note` (`summary.json`, `agreement.csv`).

| Measure | Definition |
|---|---|
| Completion % | 100 · completed queries / total queries, per annotator (pending = total − completed, invalid records count as pending) |
| 3-way agreement % | 100 · pairs with identical 3/3 votes / evaluated pairs |
| Pairwise agreement % | 100 · (n11 + n00) / n for A1–A2, A1–A3, A2–A3 (observed agreement p_o) |
| Cohen's κ | (p_o − p_e) / (1 − p_e), p_e = pa·pb + (1−pa)(1−pb); `null` if p_e = 1 |
| Mean κ | mean of the defined pairwise κ values (undefined excluded; count reported) |
| Fleiss' κ | (P̄ − P_e) / (1 − P_e); P_i = Σ_j n_ij(n_ij−1) / (3·2), P̄ = mean P_i, P_e = p₁² + p₀² from pooled proportions; **not** the mean of Cohen's κ |
| Positive / negative specific agreement | pooled over the 3 pairs: 2·(both say label) / (label votes of both); = Σ C(s_i,2) / Σ s_i for Positive (s_i = Positive votes) and the analogue for Negative; pairwise values in `agreement.csv` |
| Voting counts | 3/3 Positive, 2/3 Positive (majority Positive), 1/3 Positive (majority Negative), 0/3 Positive (unanimous Negative); rates use evaluated pairs |
| Final positives by support | share of final positive pairs with 3/3 vs exactly 2/3 votes |
| Confirmation / rejection rate | \|P1 ∩ P_final\| / \|P1\| and \|P1 \ P_final\| / \|P1\| (pair-level, A1 = original); new positives = \|P_final \ P1\| (only within the existing candidate pool, not exhaustive gallery relevance) |
| Net change / queries changed | added − removed; queries with P_final ≠ P1; seeds rejected = queries whose seed ∉ P_final |
| Positive distribution | for A1, A2, A3, Final: total links, mean, median, sample std (n−1), min, max, #queries with 0 / 1 / >1 positives, % multiple |
| Query-level | identical sets across all 3 annotators, ≥1 disagreement, final ≠ A1, gained / lost / both, positive-count distributions before (A1) and after (final) |

Files: `summary.json` (everything + definitions, dataset counts, inputs' hashes, diagnostics), `summary.csv` (one row per group, flattened), `agreement.csv` (long format: split, query_type, statistic, annotators, evaluated_pairs, value, note), `voting_distribution.csv` (3/3, 2/3, 1/3, 0/3 positive votes with counts and %), `per_query.csv` (positive counts for A1/A2/A3/Final, added/removed, agreement rate, changed, seed rejected), `disagreements.csv` (non-unanimous pairs with the 3 votes and the final label).
# positive-verification
