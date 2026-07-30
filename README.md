# AI Content Growth Engine

A policy-compliant, local-first Content Intelligence CLI for researching topics,
generating and ranking original content ideas, creating briefs, and producing
daily reports for YouTube, Instagram, and TikTok.

Milestone 1 is deliberately read-only with respect to social platforms. It does
not publish content, interact with audiences, or generate artificial engagement.

## Install

Python 3.11 or newer is required.

```bash
python -m pip install -e ".[dev]"
growth-engine init --creator "Example Creator" --niche "sustainable design"
```

The command creates a `.growth-engine` workspace in the current directory.
Credentials are never written there; future API credentials must be supplied by
environment variables or a secret manager.

## Workflow

```bash
growth-engine research "low-carbon renovations" --platform youtube
growth-engine ideas generate --count 5
growth-engine ideas rank
growth-engine brief create
growth-engine report daily
```

Every command supports `--workspace PATH`. Workflow commands also support
`--dry-run`, which validates and previews the operation without writing files.
Use `--json` for machine-readable output.

Research accepts creator-supplied observations so the raw evidence remains
separate from recommendations:

```bash
growth-engine research "adaptive reuse" \
  --observation "Audience questions often focus on cost and planning risk" \
  --observation "Before-and-after explanations retain attention"
```

No network requests are performed in Milestone 1. The research command records
the topic and supplied observations; future connectors must use official
platform APIs, platform-specific rate limiting, and the existing interaction
audit log.

## Data layout

```text
.growth-engine/
  config.json
  raw/research/       # immutable source observations
  raw/metrics/        # imported platform evidence (none collected in Milestone 1)
  derived/ideas/      # generated and ranked recommendations
  derived/briefs/
  reports/
  jobs/               # idempotency receipts
  logs/audit.jsonl
```

Artifacts have explicit workflow states. Jobs use stable input hashes so a safe
retry returns the prior artifact rather than duplicating work.

## Safety

The package contains a deny-by-default policy boundary for platform actions.
Milestone 1 permits research and local analysis only. Publishing, engagement,
account automation, invalid traffic, protection bypasses, and metric
manipulation are rejected.

## Development

```bash
python -m pytest
python -m ruff check .
python -m mypy src
```
