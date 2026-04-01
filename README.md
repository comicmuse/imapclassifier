# IMAP Daemon

Teach-by-moving: create simple deterministic rules by moving emails into `Train/*` folders from **any** client (Thunderbird on Linux/Android, etc.). A local trainer learns from those examples and updates a rules file. A filer applies those rules to `INBOX` (move/mark-read/forward). No webmail or server-side Sieve editing required.

---

## Contents

* [Overview](#overview)
* [How it works](#how-it-works)
* [What this is good for](#what-this-is-good-for)
* [Requirements](#requirements)
* [Folder layout](#folder-layout)
* [Files in this repo](#files-in-this-repo)
* [Install](#install)
* [Configuration](#configuration)

  * [Environment](#environment)
  * [Rules file](#rules-file)
  * [Optional: Subject hints](#optional-subject-hints)
* [Run with systemd (user services)](#run-with-systemd-user-services)
* [Daily workflow (how you “train”)](#daily-workflow-how-you-train)
* [TripIt forwarding notes](#tripit-forwarding-notes)
* [Daily Summary Feature](#daily-summary-feature)
* [Safety: Archive & AutoDelete](#safety-archive--autodelete)
* [Troubleshooting](#troubleshooting)
* [Extending / Customising](#extending--customising)
* [Security](#security)
* [FAQ](#faq)
* [Uninstall](#uninstall)

---

## Overview

This setup is for people who want deterministic email classification **without** juggling Sieve UI or complex add‑ons. You:

1. Move a message to a training folder like `Train/Newsletters` or `Train/Receipts` from any client.
2. A local **trainer** script scans `Train/*`, learns a simple rule from the message (prefer `List-Id`, then `List-Unsubscribe`, then `From` domain; plus optional subject hints for travel), updates a YAML rules file, and immediately performs the requested action for that training message.
3. A local **filer** script continuously applies your saved rules to **INBOX**: moving, marking read, and (optionally) forwarding to TripIt for travel.

Stateless by design: the `Train/*` folders are queues. Each run empties them, so there’s **no per-message state** to keep.

---

## How it works

```
Client (TB Linux/Android)         Trainer (hourly)                  Filer (continuous)
┌─────────────────────────┐      ┌────────────────────────┐        ┌──────────────────────────┐
│ Move mail → Train/*     │──▶──▶│ Read Train/*           │  ┌───▶ │ Scan INBOX               │
│ (e.g., Train/Receipts)  │      │ Extract rule key:      │  │     │ First matching rule wins │
└─────────────────────────┘      │  List-Id ▷ List-Unsub  │  │     │ Actions: move/mark/forward│
                                 │  ▷ From domain         │  │     └──────────────────────────┘
                                 │ (Travel: +Subject hint)│  │
                                 │ Update ~/.imap-rules…  │  │
                                 │ Apply actions to Train/*│  │
                                 └────────────────────────┘  │
                                                             │ moves out of INBOX ⇒ no re-run
                                                             └────────────────────────────────
```

---

## What this is good for

* Newsletters, offers, receipts: stable **List-Id / List-Unsubscribe / From** based rules.
* Travel: optionally forward to TripIt, then file to a travel folder and mark read.
* Archiving: one-tap training from mobile or desktop.

Not a spam filter (keep Thunderbird’s/JMAP/ISP spam filtering on). Not ML—just clean deterministic rules you generate by example. You can layer ML later if you like.

---

## Requirements

* **Python 3.10+** with `pyyaml` installed
* An IMAP/SMTP account (e.g., mailbox.org)
* Thunderbird (any platform) or any IMAP client
* Ubuntu (or any system with **systemd user services**)

---

## Folder layout

Create these in your mailbox (case-insensitive, but keep as shown):

```
Train/Newsletters   → move to Newsletters/
Train/Offers        → mark read + move to Offers/
Train/Receipts      → mark read + move to Receipts/
Train/Travel        → forward to plans@tripit.com + move to Travel/Flight Tickets + mark read
Train/Archive       → mark read + move to Archive/
Train/AutoDelete    → move to Autodelete/ (see Safety)
```

> Create destination folders (`Newsletters`, `Offers`, `Receipts`, `Travel/Flight Tickets`, `Archive`, `Autodelete`) too. The scripts will also try to create them if missing.

---

## Files in this repo

```
imap_daemon.py     # single daemon: trains rules and files INBOX

~/.config/systemd/user/
  imap-daemon.service
```

You can keep the rules/config in your home as well (defaults used by scripts):

* `~/.imap-rules.yaml`

---

## Configuration

### Environment

The scripts read IMAP/SMTP settings from environment variables. With systemd user services, put them in `~/.config/systemd/user/imap.env` and reference via `EnvironmentFile=`.

**`~/.config/systemd/user/imap.env`**

```
IMAP_HOST=imap.mailbox.org
IMAP_USER=you@example.com
IMAP_PASS=YOUR_APP_PASSWORD

SMTP_HOST=smtp.mailbox.org
SMTP_USER=you@example.com
SMTP_PASS=YOUR_APP_PASSWORD
```

Use an **app password** if your provider supports it.

### Rules file

Rules live in YAML (default path `~/.imap-rules.yaml`). The trainer updates it; you can also hand-edit.

Each rule has a `match` and a list of `actions` executed **in order**. First matching rule wins.

**Example:**

```yaml
rules:
  - match: { header: List-Id, contains: "news.example.org" }
    actions: [ { move: "Newsletters" } ]

  - match: { header: List-Unsubscribe, contains: "offers.example.com" }
    actions: [ mark_read, { move: "Offers" } ]

  - match: { header: From, contains: "amazon.co.uk" }
    actions: [ mark_read, { move: "Receipts" } ]

  - match: { header: From, contains: "ba.com" }
    actions:
      - { forward: "plans@tripit.com" }
      - { move: "Travel/Flight Tickets" }
      - mark_read

  - match: { header: Subject, contains: "invoice" }
    actions: [ mark_read, { move: "Receipts" } ]
```

Supported headers: `List-Id`, `List-Unsubscribe`, `From`, `Subject`, and `any` (a loose catch‑all across common headers).

> **Tip:** Subject-only rules can be noisy. Prefer an AND of `From` + `Subject` for precision (see *Extending* below).

### Optional: Subject hints

If you frequently add Subject-based shortcuts, you can keep a hints file the trainer reads to auto-add Subject rules when you drop mail into a given `Train/*` folder.

**`~/.imap-subject-hints.yaml`** (optional)

```yaml
Train/Receipts:
  - receipt
  - invoice
  - "order confirmation"

Train/Offers:
  - "promo code"
  - discount
  - sale

Train/Travel:
  - itinerary
  - booking
  - reservation
```

> By default, the trainer includes a few travel-related hints (itinerary/booking/reservation/boarding/ticket/flight). The hints file lets you broaden this without code changes.

---

## Run with systemd (user services)

User services live in `~/.config/systemd/user/`. Manage them with `systemctl --user …`.

A single service handles both training (every hour) and filing (every 60 seconds).

**`~/.config/systemd/user/imap-daemon.service`**

```
[Unit]
Description=IMAP Daemon (trains rules + files INBOX)
After=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/systemd/user/imap.env
ExecStart=/usr/bin/python3 %h/bin/imap_daemon.py
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

**Enable & start:**

```bash
systemctl --user daemon-reload
systemctl --user enable --now imap-daemon.service

# allow user services to run without a logged-in session
loginctl enable-linger "$USER"
```

**Inspect logs:**

```bash
journalctl --user -u imap-daemon -f
tail -f ~/.imap-daemon.log
```

---

## Daily workflow (how you “train”)

1. On desktop or phone, move a message to one of:

   * `Train/Newsletters`, `Train/Offers`, `Train/Receipts`, `Train/Travel`, `Train/Archive`, `Train/AutoDelete`.
2. The daemon's training cycle (every hour) empties those queues:

   * Extracts a stable key: `List-Id` → `List-Unsubscribe` → `From` domain (for Travel also checks Subject hints)
   * Updates `~/.imap-rules.yaml` (atomic write)
   * Applies the actions to those training messages immediately
3. The filer continuously applies your rules to new `INBOX` mail (first-match wins).

---

## TripIt forwarding notes

* The trainer forwards anything in `Train/Travel` **immediately** to `plans@tripit.com` (as a `message/rfc822` attachment) before filing it to `Travel/Flight Tickets` and marking read.
* The filer will do the same for **new** matching travel mail in `INBOX`.
* Make sure SMTP creds are valid in your environment file. If your provider needs specific SMTP ports, adjust `SMTP_HOST` or code.

---

## Daily Summary Feature

The filer tracks email filing actions and sends a daily summary report at 19:00 (local time).

**What's tracked:**
* Emails moved to each destination folder
* Emails deleted
* Emails forwarded
* Emails marked as read

**How it works:**
1. Every action (move, delete, forward, mark_read) is recorded to `~/.imap-daemon-stats.json`
2. The state file uses atomic writes (write to `.tmp`, then rename) for crash safety
3. At 19:00 each day, a summary email is placed in your INBOX with statistics collected since the last summary was sent (i.e., since stats were last reset). If the filer has been down for one or more days, the summary may cover more than 24 hours.
4. Any previous Daily Summary emails in INBOX are automatically deleted before sending the new one
5. The summary email appears as **unread** so you'll notice it
6. Statistics are reset after sending the summary

**Example summary email:**

```
Daily Email Filing Summary - 2025-12-24

Total emails processed: 105

Emails moved by destination:
  Finance: 12
  Newsletters: 45
  Offers: 23

Emails deleted: 8
Emails forwarded: 2
Emails marked as read: 15
```

The state file survives daemon crashes and restarts, ensuring accurate tracking across system events.

---

## Safety: Archive & AutoDelete

* `Train/Archive` marks read & moves to `Archive/`.
* `Train/AutoDelete` moves to `Autodelete/` (a **quarantine**, not a hard delete). Recommended: purge items older than N days with a weekly job once confident.

**Optional weekly purge (e.g., 14 days)** – add another user timer/service or a tiny cron if you prefer. A quick Python snippet could list and move mail older than 14 days from `Autodelete/` to `Trash`.

---

## Troubleshooting

* **Service won’t start**: `journalctl --user -u imap-daemon -e`.
* **Nothing happens**: Confirm `~/.imap-rules.yaml` exists and contains at least one rule; run the trainer once manually: `IMAP_USER=… IMAP_PASS=… python3 ~/bin/imap_daemon.py --mode train`.
* **Folder names**: Some servers show localized names; use the exact server-side path. Adjust destinations in rules if needed.
* **First-match wins**: If a general rule catches mail before a specific one, reorder rules (place specific ones earlier in the YAML).
* **Character encoding**: The scripts decode common encodings; if you see garbled subjects/headers, file an issue or add a decoding fallback.

---

## Extending / Customising

* **AND/OR conditions**: You can support composite matches (e.g., From **AND** Subject) by extending `filer.py` to accept:

  ```yaml
  - match:
      all:
        - { header: From, contains: "expedia.co.uk" }
        - { header: Subject, contains: "itinerary" }
    actions:
      - { forward: "plans@tripit.com" }
      - { move: "Travel/Flight Tickets" }
      - mark_read
  ```

  Then update `match_rule` to evaluate `all` / `any` clauses. (A sample implementation is easy to drop in.)

* **More Train folders**: Add any `Train/Foo` mapping you like by editing the `TRAIN_MAP` in `imap_daemon.py` and creating the destination folder.

* **Alternative filing**: You can swap `imap_daemon.py` for `imapfilter` (Lua) or `imapautofilter` if you prefer, using the same rules file as input.

* **Layer ML later**: If you decide to add ML/LLM later, keep this deterministic pipeline and call the model only for “unknown” messages.

---

## Security

* Use **app passwords** for IMAP/SMTP.
* The env file is read by **your user** service; file permissions should be `0600`.
* The scripts operate only on your mailbox over TLS (IMAP4\_SSL / SMTP SSL).

---

## FAQ

**Q: Do I need server-side Sieve?**
No. This is entirely client-side (your Linux box) over IMAP/SMTP.

**Q: Will this work if Thunderbird for Android can’t run add-ons?**
Yes. Training is just moving mail to `Train/*`. Filing happens on your Linux box and applies to the mailbox server-wide.

**Q: What if a provider rotates unsubscribe domains?**
The trainer prefers `List-Id` when present, then `List-Unsubscribe` host, then `From` domain. You can keep multiple rules per sender or fall back to domain-only matches.

**Q: Can I dry-run first?**
Yes—temporarily change actions in YAML to just add tags (or copy instead of move) in the code, then switch to move when happy.

---

## Uninstall

```bash
systemctl --user disable --now imap-daemon.service
rm -f ~/.config/systemd/user/imap-daemon.service
systemctl --user daemon-reload

# optional: remove script and config
rm -f ~/bin/imap_daemon.py
rm -f ~/.imap-rules.yaml ~/.imap-subject-hints.yaml ~/.imap-daemon-stats.json
```
