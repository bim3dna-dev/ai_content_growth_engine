# AI Content Growth Engine

A policy-compliant, local-first CLI for content research and deterministic
performance intelligence. Milestone 1 provides local ideation workflows.
Milestone 2 adds a narrow, owner-authorized, read-only YouTube pipeline.

The application cannot upload, edit, delete, like, comment, subscribe, publish,
generate traffic, or guarantee views, subscribers, monetization, or any other
outcome. Instagram and TikTok integrations are not part of Milestone 2.

## Install

Python 3.11 or newer is required.

```bash
python -m pip install -e ".[dev]"
growth-engine init --creator "Example Creator" --niche "sustainable design"
```

The command creates an untracked `.growth-engine` workspace in the current
directory. Every command supports `--workspace PATH`; workflow commands support
`--dry-run`, and `--json` selects machine-readable output.

## Milestone 1 workflow

```bash
growth-engine research "low-carbon renovations" --platform youtube
growth-engine ideas generate --count 5
growth-engine ideas rank
growth-engine brief create
growth-engine report daily
```

Research accepts creator-supplied observations:

```bash
growth-engine research "adaptive reuse" \
  --observation "Audience questions often focus on cost and planning risk" \
  --observation "Before-and-after explanations retain attention"
```

Milestone 1 performs no network requests.

## YouTube prerequisites

In a Google Cloud project:

1. enable **YouTube Data API v3** and **YouTube Analytics API**;
2. configure the OAuth consent screen;
3. create an OAuth client with application type **Desktop app**;
4. download its client-secrets JSON outside the repository.

The integration uses only Google's official maintained clients and these
minimum scopes:

```text
https://www.googleapis.com/auth/youtube.readonly
https://www.googleapis.com/auth/yt-analytics.readonly
```

It never requests a write scope. OAuth uses Google's installed-application
local-browser flow. The refreshable token is stored under the ignored
`.growth-engine/credentials/youtube/` directory. Neither tokens nor secret
values are printed or added to audit records.

Optional environment overrides are documented in `.env.example`:

```text
YOUTUBE_CLIENT_SECRETS_PATH
YOUTUBE_TOKEN_PATH
```

## YouTube workflow

Configure a safe local reference to the downloaded client file:

```bash
growth-engine youtube configure \
  --client-secrets /secure/path/client_secrets.json \
  --channel-alias art-forever
```

Authorize and verify the owned channel:

```bash
growth-engine youtube auth --channel art-forever
growth-engine youtube status --channel art-forever
```

Synchronize channel metadata, the uploads playlist, videos, and owner analytics:

```bash
growth-engine youtube sync channel --channel art-forever
growth-engine youtube sync videos --channel art-forever --max-items 100
growth-engine youtube sync analytics \
  --channel art-forever \
  --start-date 2026-01-01 \
  --end-date 2026-01-31
```

The full workflow executes those steps in order and reports `succeeded`,
`partially_succeeded`, `failed`, `skipped`, `blocked_by_policy`, or
`not_authorized`:

```bash
growth-engine youtube sync all \
  --channel art-forever \
  --start-date 2026-01-01 \
  --end-date 2026-01-31 \
  --max-items 100
```

Analyze only the matching, locally persisted date grain, run versioned
diagnosis rules, and generate JSON plus Markdown:

```bash
growth-engine analytics analyze \
  --channel art-forever \
  --start-date 2026-01-01 \
  --end-date 2026-01-31

growth-engine analytics diagnose \
  --channel art-forever \
  --start-date 2026-01-01 \
  --end-date 2026-01-31

growth-engine report youtube \
  --channel art-forever \
  --start-date 2026-01-01 \
  --end-date 2026-01-31
```

Analysis, diagnosis, and report generation make zero network requests.

## Data architecture and provenance

```text
.growth-engine/
  config.json
  youtube/config.json
  credentials/youtube/                         # ignored OAuth tokens
  raw/research/
  raw/metrics/
  raw/youtube/<alias>/
    channel/<retrieval-id>.json
    playlist_items/<retrieval-id>.json
    videos/<retrieval-id>.json
    analytics/<retrieval-id>.json
  normalized/youtube/<alias>/
    channels.json
    videos.json
    analytics_rows.json
  derived/ideas/
  derived/briefs/
  derived/youtube/<alias>/
    metrics.json
    diagnoses.json
  reports/youtube/<alias>/<report-id>.json
  reports/youtube/<alias>/<report-id>.md
  jobs/
  logs/audit.jsonl
```

Raw API snapshots are immutable and carry retrieval time, method, and API
provenance. Normalized records are stable and deterministically upserted.
Derived records reference normalized source IDs. Repeating synchronization
creates a new immutable raw observation while avoiding duplicate normalized
records.

YouTube Data API public counters can be lifetime values. Owner Analytics API
metrics are date-range scoped and remain separate; the application never
divides lifetime counters by period metrics. Missing API fields remain `null`,
not fabricated zeroes. Some owner metrics may be unavailable, incompatible,
delayed, or withheld by YouTube.

## Metrics and diagnoses

Formula version `youtube-formulas-v1` defines:

- engagement rate = `(likes + comments + shares) / views * 100`;
- subscriber conversion = `(gained - lost) / views * 100`;
- like, comment, and share rates = the corresponding action divided by views;
- views per video = period video views divided by videos with view data;
- publishing frequency = videos published in the period divided by period days,
  multiplied by seven.

Zero or missing denominators produce `null` with a reason. Each metric records
its formula, version, normalized source IDs, exact date range, calculation time,
and nullability reason.

Diagnosis version `youtube-diagnosis-v1` compares each video with channel-local
medians when at least three values exist. Otherwise it records use of documented
fallbacks: 1,000 impressions, 2% click-through rate, 40% average viewed, 0.5%
subscriber conversion, and 3% engagement. Results separate evidence,
observations, and recommended experiments. No LLM participates in diagnosis.

## Quotas and reliability

Video inventory uses the channel uploads playlist, not `search.list`.
Playlist pages are bounded, `videos.list` uses batches of 50, requests have a
30-second transport timeout, and retryable HTTP 429/5xx failures use bounded
exponential backoff with jitter. HTTP 401, non-quota 403, 404, malformed
requests, authorization denial, and policy rejection are not retried. Local
per-API rate limits apply before requests.

If a combined Analytics report rejects an incompatible metric, the application
checks the requested metrics individually, persists the deterministic supported
subset, and reports unsupported names. Quota or authorization failures do not
trigger that fallback.

## Troubleshooting and credential removal

- `not_configured`: run `youtube configure`.
- `configured_not_authorized`: run `youtube auth`.
- `token_refresh_required` or `invalid_or_revoked_authorization`: remove the
  local token and authorize again.
- `channel_identity_mismatch`: confirm that the Google account owns the channel
  pinned during the first successful channel sync.
- quota/rate-limit responses: wait for quota availability; do not bypass limits.
- browser startup failure: run authorization in a desktop session where the
  localhost callback and browser are available.

To remove local authorization, delete the alias token under
`.growth-engine/credentials/youtube/` and revoke the application's access from
the Google Account security page. The CLI does not implement remote revocation.

## Safety

The policy boundary rejects platform writes, uploads, metadata changes,
comments, ratings, subscriptions, fake engagement, artificial traffic,
simulated watch time, metric manipulation, protection bypasses, and unofficial
platform automation before network execution.

## Development and Windows test runtime

The observed Windows Pytest failure was caused by a missing parent for the
explicit relative `--basetemp`, while the previously selected global temp
directory was inaccessible. The repository includes an empty `.runtime` anchor;
all generated runtime children are ignored.

```bash
python -m pytest -q --basetemp=.runtime/pytest-temp -o cache_dir=.runtime/pytest-cache
python -m ruff check .
python -m mypy src
git diff --check
```
