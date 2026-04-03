#!/usr/bin/env python3
import imaplib, email, email.utils, os, re, yaml, logging, signal, time, argparse, json, tempfile, shutil
from urllib.parse import urlparse
from email.header import decode_header, make_header

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.expanduser("~/.imap-daemon.log"))
    ]
)
logger = logging.getLogger(__name__)

IMAP_HOST = os.getenv("IMAP_HOST", "imap.mailbox.org")
IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASS = os.getenv("IMAP_PASS")

RULES_FILE = os.path.expanduser("~/.imap-rules.yaml")
STATS_FILE = os.path.expanduser("~/.imap-daemon-stats.json")

POLL_INTERVAL = 60      # seconds between filing passes
TRAIN_INTERVAL = 3600   # seconds between training cycles
DAILY_SUMMARY_HOUR = 19
OFFERS_FOLDER = "Offers"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

TRAIN_MAP = {
    "Train/Newsletters": [("move", "Newsletters")],
    "Train/Updates":     [("move", "Updates")],
    "Train/Offers":      [("mark_read", None), ("move", "Offers")],
    "Train/Receipts":    [("mark_read", None), ("move", "Receipts")],
    "Train/Travel":      [("forward", "plans@tripit.com"), ("move", "Travel/Flight Tickets"), ("mark_read", None)],
    "Train/AutoArchive": [("mark_read", None), ("move", "Archive")],
    "Train/AutoDelete":  [("move", "Autodelete")],
}

shutdown_requested = False

def signal_handler(signum, frame):
    global shutdown_requested
    try:
        sig_name = signal.Signals(signum).name
    except (ValueError, AttributeError):
        sig_name = f"signal {signum}"
    logger.info(f"Received {sig_name}, initiating graceful shutdown...")
    shutdown_requested = True


# ─── Shared utilities ─────────────────────────────────────────────────────────

def h(msg, name):
    raw = msg.get(name, "")
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw

def ensure_mailbox(imap, name):
    try:
        imap.create(name)
        logger.debug(f"Created mailbox: {name}")
    except imaplib.IMAP4.error as e:
        logger.debug(f"Mailbox {name} creation info: {e}")

def send_forward(raw_bytes, to_addr):
    import smtplib
    from email.message import EmailMessage
    smtp_host = os.getenv("SMTP_HOST", "smtp.mailbox.org")
    smtp_user = os.getenv("SMTP_USER", IMAP_USER)
    smtp_pass = os.getenv("SMTP_PASS", IMAP_PASS)
    try:
        msg = EmailMessage()
        msg["From"] = smtp_user
        msg["To"] = to_addr
        msg["Subject"] = "Fwd: travel docs"
        msg.set_content("Forwarded itinerary/booking.")
        msg.add_attachment(raw_bytes, maintype="message", subtype="rfc822", filename="message.eml")
        with smtplib.SMTP_SSL(smtp_host, 465) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        logger.info(f"Forwarded message to {to_addr}")
    except Exception as e:
        logger.error(f"Failed to forward message to {to_addr}: {e}")

def _connect():
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(IMAP_USER, IMAP_PASS)
    logger.info(f"Connected to {IMAP_HOST} as {IMAP_USER}")
    return imap


# ─── Rules I/O ────────────────────────────────────────────────────────────────

def load_rules():
    try:
        if os.path.exists(RULES_FILE):
            with open(RULES_FILE) as f:
                rules = yaml.safe_load(f) or {"rules": []}
            logger.info(f"Loaded {len(rules.get('rules', []))} rules from {RULES_FILE}")
            return rules
        else:
            logger.info("Rules file not found; starting with empty rules")
            return {"rules": []}
    except Exception as e:
        logger.error(f"Error loading rules: {e}")
        return {"rules": []}

def save_rules(data):
    try:
        d = tempfile.mkdtemp()
        tmp = os.path.join(d, "imap-rules.yaml.tmp")
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        shutil.move(tmp, RULES_FILE)
        logger.info(f"Saved {len(data.get('rules', []))} rules to {RULES_FILE}")
    except Exception as e:
        logger.error(f"Error saving rules: {e}")


# ─── Stats / daily summary ────────────────────────────────────────────────────

def load_stats():
    if not os.path.exists(STATS_FILE):
        return {"last_summary_date": None, "last_offers_cleanup_date": None, "stats": {"move": {}, "delete": 0, "forward": 0, "mark_read": 0}}
    try:
        with open(STATS_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading stats: {e}")
        return {"last_summary_date": None, "last_offers_cleanup_date": None, "stats": {"move": {}, "delete": 0, "forward": 0, "mark_read": 0}}

def save_stats(stats):
    try:
        tmp = STATS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(stats, f, indent=2)
        os.replace(tmp, STATS_FILE)
    except Exception as e:
        logger.error(f"Error saving stats: {e}")

def update_stats(action_name, action_arg=None):
    stats = load_stats()
    if action_name == "move":
        dest = action_arg
        if dest:
            stats["stats"]["move"].setdefault(dest, 0)
            stats["stats"]["move"][dest] += 1
    elif action_name in ("delete", "forward", "mark_read"):
        stats["stats"][action_name] += 1
    save_stats(stats)

def generate_summary_email():
    from datetime import datetime
    stats = load_stats()
    today = datetime.now().strftime("%Y-%m-%d")
    total = (stats["stats"]["delete"] + stats["stats"]["forward"] + stats["stats"]["mark_read"]
             + sum(stats["stats"]["move"].values()))
    lines = [f"Daily Email Filing Summary - {today}", "", f"Total emails processed: {total}", ""]
    if stats["stats"]["move"]:
        lines.append("Emails moved by destination:")
        for folder, count in sorted(stats["stats"]["move"].items()):
            lines.append(f"  {folder}: {count}")
        lines.append("")
    if stats["stats"]["delete"] > 0:
        lines.append(f"Emails deleted: {stats['stats']['delete']}")
    if stats["stats"]["forward"] > 0:
        lines.append(f"Emails forwarded: {stats['stats']['forward']}")
    if stats["stats"]["mark_read"] > 0:
        lines.append(f"Emails marked as read: {stats['stats']['mark_read']}")
    return "\n".join(lines), today

def send_daily_summary(imap):
    from email.mime.text import MIMEText
    try:
        try:
            imap.select('"INBOX"', readonly=False)
            typ, data = imap.uid("SEARCH", None, "SUBJECT", "Daily Email Filing Summary")
            if typ == "OK" and data and data[0]:
                old_uids = data[0].split()
                for uid in old_uids:
                    imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
                imap.expunge()
                logger.info(f"Deleted {len(old_uids)} previous summary email(s)")
        except Exception as e:
            logger.warning(f"Error deleting old summaries: {e}")

        body, today = generate_summary_email()
        msg = MIMEText(body)
        msg["Subject"] = f"Daily Email Filing Summary - {today}"
        msg["From"] = IMAP_USER
        msg["To"] = IMAP_USER
        msg["Date"] = email.utils.formatdate(localtime=True)
        imap.append('"INBOX"', "DailySummary", None, msg.as_bytes())
        logger.info(f"Daily summary email sent for {today}")

        stats = load_stats()
        stats["last_summary_date"] = today
        stats["stats"] = {"move": {}, "delete": 0, "forward": 0, "mark_read": 0}
        save_stats(stats)
    except Exception as e:
        logger.error(f"Error sending daily summary: {e}")

def check_and_send_daily_summary(imap):
    from datetime import datetime
    now = datetime.now()
    stats = load_stats()
    today = now.strftime("%Y-%m-%d")
    if now.hour >= DAILY_SUMMARY_HOUR and stats["last_summary_date"] != today:
        total = (stats["stats"]["delete"] + stats["stats"]["forward"] + stats["stats"]["mark_read"]
                 + sum(stats["stats"]["move"].values()))
        if total > 0:
            logger.info("Time to send daily summary")
            send_daily_summary(imap)


# ─── Filing ───────────────────────────────────────────────────────────────────

def list_unsub_domains(msg):
    lu = msg.get("List-Unsubscribe", "")
    if not lu:
        return []
    parts = re.findall(r"<([^>]+)>", lu) or [p.strip() for p in lu.split(",")]
    out = []
    for p in parts:
        p = p.strip("<> ").lower()
        if p.startswith("http"):
            host = urlparse(p).hostname
            if host:
                out.append(host)
        elif p.startswith("mailto:") and "@" in p:
            out.append(p.split("@")[-1])
    return out

def match_rule(msg, rule):
    m = rule["match"]
    hdr = m["header"].lower()
    needle = m["contains"].lower()
    if hdr == "list-id":
        return needle in h(msg, "List-Id").lower()
    if hdr == "list-unsubscribe":
        return any(needle in d for d in list_unsub_domains(msg))
    if hdr == "from":
        return needle in h(msg, "From").lower()
    if hdr == "subject":
        return needle in h(msg, "Subject").lower()
    if hdr == "any":
        combined = (h(msg, "From") + " " + h(msg, "Subject") + " " + h(msg, "List-Id")).lower()
        return needle in combined
    return False

def do_action(imap, uid, raw, action, track_stats=True):
    if isinstance(action, str):
        name, arg = action, None
    elif isinstance(action, dict):
        name, arg = next(iter(action.items()))
    elif isinstance(action, (list, tuple)):
        if not action:
            return
        name = action[0]
        arg = action[1] if len(action) > 1 else None
    else:
        logger.warning(f"Unknown action shape for UID {uid}: {action}")
        return

    uid_str = uid.decode() if isinstance(uid, bytes) else uid
    try:
        if name == "mark_read":
            imap.uid("STORE", uid, "+FLAGS", r"(\Seen)")
            logger.info(f"  [UID {uid_str}] Marked as read")
            if track_stats:
                update_stats("mark_read")
        elif name == "move":
            dest = arg
            ensure_mailbox(imap, dest)
            try:
                if raw is not None:
                    imap.append(dest, None, None, raw)
                    logger.info(f"  [UID {uid_str}] Appended to {dest} (kept unread)")
                else:
                    imap.uid("COPY", uid, dest)
                    logger.info(f"  [UID {uid_str}] Copied to {dest} (no raw available)")
            except Exception as e:
                logger.debug(f"APPEND to {dest} failed: {e}; falling back to COPY")
                try:
                    imap.uid("COPY", uid, dest)
                    logger.info(f"  [UID {uid_str}] Copied to {dest}")
                except Exception as e2:
                    logger.error(f"  [UID {uid_str}] Failed to copy to {dest}: {e2}")
            imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            if track_stats:
                update_stats("move", dest)
        elif name == "forward":
            if raw is None:
                typ, d = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = d[0][1] if d and d[0] else b""
            send_forward(raw, arg)
            if track_stats:
                update_stats("forward")
        elif name == "delete":
            imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            logger.info(f"  [UID {uid_str}] Marked for deletion")
            if track_stats:
                update_stats("delete")
        else:
            logger.warning(f"  [UID {uid_str}] Unknown action: {name}")
    except Exception as e:
        logger.error(f"  [UID {uid_str}] Error performing action '{name}': {e}")


# ─── Training ─────────────────────────────────────────────────────────────────

def extract_listid(msg):
    lid = h(msg, "List-Id")
    if not lid:
        return None
    m = re.search(r"<([^>]+)>", lid)
    return m.group(1).strip().lower() if m else lid.strip().lower()

def extract_listunsub(msg):
    lu = msg.get("List-Unsubscribe", "")
    if not lu:
        return None
    parts = re.findall(r"<([^>]+)>", lu) or [p.strip() for p in lu.split(",")]
    for p in parts:
        p = p.strip("<> ").lower()
        if p.startswith("http"):
            host = urlparse(p).hostname
            if host:
                return host
        if p.startswith("mailto:") and "@" in p:
            return p.split("@")[-1]
    return None

def from_domain(msg, rules=None):
    frm = h(msg, "From").lower()
    email_match = re.search(r'([a-z0-9\.\-_]+@[a-z0-9\.\-]+\.[a-z]{2,})', frm)
    if not email_match:
        return None
    email_addr = email_match.group(1)
    domain_match = re.search(r'@([a-z0-9\.\-]+\.[a-z]{2,})', email_addr)
    if not domain_match:
        return None
    domain = domain_match.group(1)
    blocked_domains = {'duck.com', 'protonmail.com', 'gmail.com', 'yahoo.com', 'outlook.com', 'linehan.me.uk'}
    if domain in blocked_domains:
        logger.debug(f"From domain {domain} is blocked, checking for Duck-Original-From")
        duck_original = h(msg, "Duck-Original-From").lower()
        if duck_original:
            orig_match = re.search(r'([a-z0-9\.\-_]+@[a-z0-9\.\-]+\.[a-z]{2,})', duck_original)
            if orig_match:
                email_addr = orig_match.group(1)
                orig_domain_match = re.search(r'@([a-z0-9\.\-]+\.[a-z]{2,})', email_addr)
                if orig_domain_match:
                    domain = orig_domain_match.group(1)
                    logger.debug(f"Found Duck-Original-From email: {email_addr}, domain: {domain}")
                    if domain in blocked_domains:
                        logger.debug(f"Duck-Original-From domain {domain} is also blocked")
                        return None
            else:
                logger.debug(f"No usable original email found, skipping blocked provider domain: {domain}")
                return None
        else:
            logger.debug(f"No usable original domain found, skipping blocked provider domain: {domain}")
            return None
    if rules:
        for rule in rules.get("rules", []):
            match = rule.get("match", {})
            if match.get("From") == domain:
                logger.debug(f"Domain {domain} already has a rule, skipping training")
                return None
    logger.debug(f"No existing rule for domain {domain}, using full email: {email_addr}")
    return email_addr

def subject_hint(msg):
    s = h(msg, "Subject").lower()
    for kw in ("itinerary", "booking", "reservation", "boarding", "ticket", "trip", "flight"):
        if kw in s:
            return kw
    return None

def _norm_actions(actions):
    out = []
    for a in actions:
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, dict):
            out.append(a)
        elif isinstance(a, (list, tuple)):
            name = a[0] if a else None
            if not name:
                continue
            arg = a[1] if len(a) > 1 else None
            out.append(name if arg is None else {name: arg})
    return out

def upsert_rule(data, header, contains, actions):
    actions = _norm_actions(actions)
    for r in data["rules"]:
        if r.get("match", {}).get("header") == header and r["match"].get("contains") == contains:
            logger.debug(f"Updated existing rule: {header}={contains} -> {actions}")
            r["actions"] = actions
            return
    logger.debug(f"Added new rule: {header}={contains} -> {actions}")
    data["rules"].append({"match": {"header": header, "contains": contains}, "actions": actions})


# ─── Offers cleanup ───────────────────────────────────────────────────────────

def _extract_text(msg):
    text_parts = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/plain":
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            text_parts.append(payload.decode(charset, errors="ignore"))
        elif ct == "text/html" and not text_parts:
            import html as html_mod
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            html = payload.decode(charset, errors="ignore")
            # Strip <style> and <script> blocks entirely before tag removal
            html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", html)
            # Decode HTML entities, strip invisible chars and long URLs
            text = html_mod.unescape(text)
            text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u00ad\u034f\u200b-\u200f\u2028\u2029\ufeff]", "", text)
            text = re.sub(r"https?://\S{60,}", "", text)
            text_parts.append(text)
    return " ".join(text_parts)

def _parse_offer_end_date(text):
    import urllib.request, json
    import datetime as dt
    today = dt.date.today()
    prompt = (
        f"Today is {today.isoformat()}. "
        "Extract the expiry or end date of the offer from the text below. "
        "Reply with ONLY the date in YYYY-MM-DD format. "
        "If no expiry date is present, reply with 'none'.\n"
        "Date formats you may encounter include: 'Ends 31 May', 'Ends 15th Jan', "
        "'Ends 15-01-2026', 'until 30/03/2026', 'until 11.59pm on 28 March 2026', "
        "'on or before 31 March 2026', 'Offer ends 30.03.26'.\n\n"
        f"{text[:3000]}"
    )
    payload = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response_text = json.loads(resp.read())["response"].strip()
        if response_text.lower().startswith("none"):
            return None
        m = re.search(r"\d{4}-\d{2}-\d{2}", response_text)
        if m:
            return dt.datetime.strptime(m.group(), "%Y-%m-%d").date()
    except Exception as e:
        logger.warning(f"Ollama date extraction failed: {e}")
    return None

def run_offers_cleanup_cycle(imap):
    from datetime import date
    today = date.today()
    typ, _ = imap.select(f'"{OFFERS_FOLDER}"', readonly=False)
    if typ != "OK":
        logger.warning(f"Could not select {OFFERS_FOLDER}")
        return

    typ, data = imap.uid("SEARCH", None, "UNSEEN")
    uids = data[0].split() if data and data[0] else []
    logger.info(f"Offers cleanup: {len(uids)} unread messages to scan")

    marked = 0
    for uid in uids:
        typ, d = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not d or not d[0]:
            continue
        msg = email.message_from_bytes(d[0][1])
        subject = h(msg, "Subject")
        text = _extract_text(msg)
        end_date = _parse_offer_end_date(text)
        uid_str = uid.decode() if isinstance(uid, bytes) else uid
        if end_date and end_date < today:
            imap.uid("STORE", uid, "+FLAGS", r"(\Seen)")
            logger.info(f"  [UID {uid_str}] Expired {end_date}: {subject[:60]}")
            marked += 1
        else:
            logger.debug(f"  [UID {uid_str}] date={end_date}: {subject[:60]}")

    logger.info(f"Offers cleanup complete: marked {marked} expired offer(s) as read")

def check_and_run_offers_cleanup(imap):
    from datetime import datetime
    stats = load_stats()
    today = datetime.now().strftime("%Y-%m-%d")
    if stats.get("last_offers_cleanup_date") != today:
        run_offers_cleanup_cycle(imap)
        stats = load_stats()
        stats["last_offers_cleanup_date"] = today
        save_stats(stats)


# ─── Cycle functions ──────────────────────────────────────────────────────────

def run_training_cycle(imap):
    logger.info("Starting training cycle...")
    rules = load_rules()
    total_trained = 0

    for train, actions in TRAIN_MAP.items():
        typ, _ = imap.select(f'"{train}"', readonly=False)
        if typ != "OK":
            logger.debug(f"Could not select {train} (folder may not exist)")
            continue

        typ, data = imap.uid("SEARCH", None, "ALL")
        uids = data[0].split() if data and data[0] else []
        logger.info(f"Found {len(uids)} messages in {train}")

        for uid in uids:
            try:
                raw = imap.uid("FETCH", uid, "(RFC822)")[1][0][1]
                msg = email.message_from_bytes(raw)
                subj = email.header.decode_header(msg.get("Subject", "Unknown"))[0][0]
                if isinstance(subj, bytes):
                    subj = subj.decode("utf-8", errors="ignore")
                frm = msg.get("From", "Unknown")
                uid_str = uid.decode() if isinstance(uid, bytes) else uid
                logger.debug(f"Processing UID {uid_str} from {train}: From={frm[:50]}, Subject={str(subj)[:50]}")

                header, key = None, None
                lid = extract_listid(msg)
                if lid:
                    header, key = "List-Id", lid
                else:
                    lu = extract_listunsub(msg)
                    if lu:
                        header, key = "List-Unsubscribe", lu
                    else:
                        dom = from_domain(msg, rules)
                        if dom:
                            header, key = "From", dom
                        else:
                            logger.info(f"  Skipping UID {uid_str}: no extractable domain (blocked provider)")
                            continue

                if train == "Train/Travel":
                    sh = subject_hint(msg)
                    logger.debug(f"  Travel message - storing domain rule {header}={key}")
                    upsert_rule(rules, header, key, actions)
                    if sh:
                        logger.debug(f"  Found travel subject hint: {sh}")
                        upsert_rule(rules, "Subject", sh, actions)
                else:
                    upsert_rule(rules, header, key, actions)

                logger.info(f"Training on {train}: {header}={key}")
                for a in actions:
                    do_action(imap, uid, raw, a, track_stats=False)
                total_trained += 1

            except Exception as e:
                uid_str = uid.decode() if isinstance(uid, bytes) else uid
                logger.error(f"Error processing UID {uid_str}: {e}")

        logger.debug(f"Expunging {train}")
        imap.expunge()

    save_rules(rules)
    logger.info(f"Training cycle complete: trained {total_trained} messages")


def run_filing_cycle(imap, rules):
    logger.info("Starting filing cycle...")
    rule_list = rules.get("rules", [])
    if not rule_list:
        logger.warning("No rules loaded; nothing to do")
        return

    imap.select('"INBOX"', readonly=False)

    typ, data = imap.uid("SEARCH", None, "UNSEEN")
    uids = data[0].split() if data and data[0] else []
    logger.info(f"Found {len(uids)} unread messages in INBOX")

    processed = 0
    for uid in uids:
        uid_str = uid.decode() if isinstance(uid, bytes) else uid
        typ, d = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
        if typ != "OK" or not d or not d[0]:
            logger.debug(f"Skipping UID {uid_str}: fetch failed")
            continue

        msg = email.message_from_bytes(d[0][1])

        for rule_idx, rule in enumerate(rule_list, 1):
            if match_rule(msg, rule):
                logger.info(f"UID {uid_str} matched rule {rule_idx}")
                # Only fetch full body if an action needs it (move uses APPEND, forward sends it)
                needs_body = any(
                    (a if isinstance(a, str) else (next(iter(a)) if isinstance(a, dict) else a[0] if a else None))
                    in ("move", "forward")
                    for a in rule["actions"]
                )
                raw = None
                if needs_body:
                    typ2, d2 = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                    if typ2 == "OK" and d2 and d2[0]:
                        raw = d2[0][1]
                for a in rule["actions"]:
                    do_action(imap, uid, raw, a)
                processed += 1
                break

    logger.info(f"Filing cycle: processed {processed} messages, expunging...")
    imap.expunge()
    check_and_send_daily_summary(imap)
    check_and_run_offers_cleanup(imap)


# ─── Main / CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="IMAP Daemon — trains rules and files INBOX",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 imap_daemon.py                  # full daemon (default)
  python3 imap_daemon.py --mode train     # one training pass then exit
  python3 imap_daemon.py --mode file      # one filing pass then exit

Environment variables required:
  IMAP_HOST, IMAP_USER, IMAP_PASS
        """
    )
    parser.add_argument(
        "--mode", choices=["daemon", "file", "train"], default="daemon",
        help="daemon: full loop (default); file: one filing pass; train: one training pass"
    )
    args = parser.parse_args()

    assert IMAP_USER and IMAP_PASS, "Set IMAP_USER and IMAP_PASS"

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    if args.mode == "train":
        imap = _connect()
        run_training_cycle(imap)
        imap.logout()
        return

    if args.mode == "file":
        rules = load_rules()
        imap = _connect()
        run_filing_cycle(imap, rules)
        imap.logout()
        return

    # daemon mode
    logger.info(f"Starting daemon (poll every {POLL_INTERVAL}s, train every {TRAIN_INTERVAL}s)...")
    logger.info("Press Ctrl+C or send SIGTERM to stop")
    last_train_time = 0.0  # force training on first iteration

    while not shutdown_requested:
        now = time.monotonic()
        if now - last_train_time >= TRAIN_INTERVAL:
            try:
                imap = _connect()
                run_training_cycle(imap)
                imap.logout()
            except Exception as e:
                logger.exception(f"Training cycle error: {e}")
            last_train_time = time.monotonic()

        try:
            rules = load_rules()
            imap = _connect()
            run_filing_cycle(imap, rules)
            imap.logout()
        except Exception as e:
            logger.exception(f"Filing cycle error: {e}")

        logger.info(f"Sleeping {POLL_INTERVAL}s until next poll...")
        for _ in range(POLL_INTERVAL):
            if shutdown_requested:
                break
            time.sleep(1)

    logger.info("Daemon shutdown complete")


if __name__ == "__main__":
    main()
