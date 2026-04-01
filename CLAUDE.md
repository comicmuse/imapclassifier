# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the script

```bash
# Run one training pass (processes Train/* folders, updates ~/.imap-rules.yaml)
IMAP_HOST=imap.mailbox.org IMAP_USER=you@example.com IMAP_PASS=secret python3 imap_daemon.py --mode train

# Run one filing pass (applies rules to INBOX once, then exits)
IMAP_HOST=imap.mailbox.org IMAP_USER=you@example.com IMAP_PASS=secret python3 imap_daemon.py --mode file

# Run as daemon (default — trains on startup, then polls INBOX every 60s)
IMAP_HOST=imap.mailbox.org IMAP_USER=you@example.com IMAP_PASS=secret python3 imap_daemon.py
```

The script also reads `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS` for forwarding (used by the Travel rule and the daily summary).

## Architecture

A single script `imap_daemon.py` handles both training and filing. It writes `~/.imap-rules.yaml` as the shared rules contract and logs to `~/.imap-daemon.log`.

**Training cycle** (`run_training_cycle`): iterates over `TRAIN_MAP` folders (e.g. `Train/Newsletters`), extracts the best stable rule key from each message in priority order — `List-Id` > `List-Unsubscribe` domain > `From` address — and upserts a rule into the YAML file. It then immediately applies the mapped actions to that training message and expunges. The `from_domain()` function handles DuckDuckGo email aliases by checking the `Duck-Original-From` header and skips a hardcoded list of broad/masking domains.

**Filing cycle** (`run_filing_cycle`): loads rules, searches all of INBOX, fetches each message with `BODY.PEEK[]` (avoids setting `\Seen`), and applies the first matching rule. Move uses IMAP `APPEND` to preserve the unread state in the destination folder, falling back to `COPY` on failure. It tracks action counts in `~/.imap-daemon-stats.json` (atomic write via `.tmp` + rename) and appends a daily summary email directly to INBOX at 19:00.

**Daemon loop** (`main` with `--mode daemon`): runs a training cycle on startup and then every `TRAIN_INTERVAL` seconds (3600), with a filing cycle every `POLL_INTERVAL` seconds (60). Each cycle opens a fresh IMAP connection and logs out on completion.

**Rule matching** (`match_rule`) supports `header` values: `List-Id`, `List-Unsubscribe`, `From`, `Subject`, `any`. First-match wins; rule order in the YAML matters.

## Key conventions

- `upsert_rule()` deduplicates rules by `(header, contains)` key — re-training the same sender updates rather than duplicates.
- The `TRAIN_MAP` dict is the single place to add new training folder → action mappings.
- Rules YAML is written atomically via `tempfile` + `shutil.move`.
- `do_action(..., track_stats=False)` is used during training so daily stats reflect only INBOX filing.
- Log file: `~/.imap-daemon.log`
