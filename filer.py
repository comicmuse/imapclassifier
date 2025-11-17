#!/usr/bin/env python3
# ~/bin/filer.py
import imaplib, email, os, re, ssl, yaml, logging
from urllib.parse import urlparse
from email.parser import BytesParser
from email.policy import default

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.expanduser("~/.imap-filer.log"))
    ]
)
logger = logging.getLogger(__name__)

IMAP_HOST = os.getenv("IMAP_HOST", "imap.mailbox.org")
IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASS = os.getenv("IMAP_PASS")

RULES_FILE = os.path.expanduser("~/.imap-rules.yaml")

def load_rules():
    try:
        with open(RULES_FILE) as f:
            rules = yaml.safe_load(f).get("rules", [])
        logger.info(f"Loaded {len(rules)} rules from {RULES_FILE}")
        for i, rule in enumerate(rules, 1):
            logger.debug(f"  Rule {i}: {rule.get('match', {})}")
        return rules
    except FileNotFoundError:
        logger.error(f"Rules file not found: {RULES_FILE}")
        return []
    except Exception as e:
        logger.error(f"Error loading rules: {e}")
        return []

def h(msg, name):
    from email.header import decode_header, make_header
    raw = msg.get(name, "")
    try: return str(make_header(decode_header(raw)))
    except Exception: return raw

def list_unsub_domains(msg):
    lu = msg.get("List-Unsubscribe", "")
    if not lu: return []
    parts = re.findall(r"<([^>]+)>", lu) or [p.strip() for p in lu.split(",")]
    out = []
    for p in parts:
        p = p.strip("<> ").lower()
        if p.startswith("http"):
            host = urlparse(p).hostname
            if host: out.append(host)
        elif p.startswith("mailto:") and "@" in p:
            out.append(p.split("@")[-1])
    return out

def match_rule(msg, rule):
    m = rule["match"]; hdr = m["header"].lower(); needle = m["contains"].lower()
    if hdr == "list-id":
        lid = h(msg,"List-Id").lower()
        result = needle in lid
        logger.debug(f"  Checking List-Id: '{needle}' in '{lid[:60]}...' → {result}")
        return result
    if hdr == "list-unsubscribe":
        domains = list_unsub_domains(msg)
        result = any(needle in d for d in domains)
        logger.debug(f"  Checking List-Unsubscribe: '{needle}' in {domains} → {result}")
        return result
    if hdr == "from":
        frm = h(msg,"From").lower()
        result = needle in frm
        logger.debug(f"  Checking From: '{needle}' in '{frm[:60]}...' → {result}")
        return result
    if hdr == "subject":
        subj = h(msg,"Subject").lower()
        result = needle in subj
        logger.debug(f"  Checking Subject: '{needle}' in '{subj[:60]}...' → {result}")
        return result
    if hdr == "any":
        # last-resort catch-all; not usually needed
        combined = (h(msg,"From")+" "+h(msg,"Subject")+" "+h(msg,"List-Id")).lower()
        result = needle in combined
        logger.debug(f"  Checking any: '{needle}' in combined headers → {result}")
        return result
    return False

def ensure_mailbox(imap, name):
    try:
        imap.create(name)
        logger.debug(f"Created mailbox: {name}")
    except imaplib.IMAP4.error as e:
        # Folder likely already exists; that's fine
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

def do_action(imap, uid, raw, action):
    # allow several shapes
    if isinstance(action, str):
        name, arg = action, None
    elif isinstance(action, dict):
        name, arg = next(iter(action.items()))
    elif isinstance(action, (list, tuple)):
        # e.g. ['move', 'Offers'] or ['mark_read', None]
        if len(action) == 0:
            return
        name = action[0]
        arg = action[1] if len(action) > 1 else None
    else:
        logger.warning(f"Unknown action shape for UID {uid}: {action}")
        return  # unknown shape; skip safely

    try:
        if name == "mark_read":
            imap.uid("STORE", uid, "+FLAGS", r"(\Seen)")
            logger.info(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Marked as read")
        elif name == "move":
            dest = arg
            ensure_mailbox(imap, dest)
            # Use APPEND with the original raw message so the copy in the
            # destination mailbox remains unread. Some servers mark copied
            # messages as \Seen; APPEND lets us control flags on the new
            # message. If APPEND fails, fall back to COPY.
            try:
                if raw is not None:
                    # append(message) expects bytes; no flags => stays unseen
                    imap.append(dest, None, None, raw)
                    logger.info(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Appended to {dest} (kept unread)")
                else:
                    # if we don't have raw data, fall back to COPY
                    imap.uid("COPY", uid, dest)
                    logger.info(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Copied to {dest} (no raw available)")
            except Exception as e:
                logger.debug(f"APPEND to {dest} failed: {e}; falling back to COPY")
                try:
                    imap.uid("COPY", uid, dest)
                    logger.info(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Copied to {dest}")
                except Exception as e2:
                    logger.error(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Failed to copy to {dest}: {e2}")
            # mark original for deletion (move semantics)
            imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        elif name == "forward":
            if raw is None:
                typ, d = imap.uid("FETCH", uid, "(BODY.PEEK[])")  # full message without setting \Seen
                raw = d[0][1] if d and d[0] else b""
            send_forward(raw, arg)
        elif name == "delete":
            imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            logger.info(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Marked for deletion")
        else:
            logger.warning(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Unknown action: {name}")
    except Exception as e:
        logger.error(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Error performing action '{name}': {e}")

def main():
    try:
        logger.info("Starting filer...")
        assert IMAP_USER and IMAP_PASS, "Set IMAP_USER/IMAP_PASS"
        
        rules = load_rules()
        if not rules:
            logger.warning("No rules loaded; nothing to do")
            return
        
        logger.info(f"Connecting to {IMAP_HOST} as {IMAP_USER}")
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(IMAP_USER, IMAP_PASS)
        logger.info("Connected and authenticated")
        
        imap.select('"INBOX"', readonly=False)
        logger.info("Selected INBOX")

        # Process ALL (seen/unseen) so backfill works; moving out prevents duplicates.
        typ, data = imap.uid("SEARCH", None, "ALL")
        uids = data[0].split() if data and data[0] else []
        logger.info(f"Found {len(uids)} messages in INBOX")
        
        processed = 0
        for uid in uids:
            typ, d = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not d or not d[0]:
                logger.debug(f"Skipping UID {uid.decode() if isinstance(uid, bytes) else uid}: fetch failed")
                continue
            
            raw = d[0][1]
            msg = email.message_from_bytes(raw)
            subj = email.header.decode_header(msg.get("Subject", "Unknown"))[0][0]
            if isinstance(subj, bytes):
                subj = subj.decode('utf-8', errors='ignore')
            frm = msg.get("From", "Unknown")
            
            logger.debug(f"Processing UID {uid.decode() if isinstance(uid, bytes) else uid}: From={frm[:50]}, Subject={str(subj)[:50]}")

            rule_matched = False
            for rule_idx, rule in enumerate(rules, 1):
                logger.debug(f"  Trying rule {rule_idx}: {rule.get('match', {})}")
                if match_rule(msg, rule):
                    logger.info(f"UID {uid.decode() if isinstance(uid, bytes) else uid} matched rule {rule_idx}")
                    for a in rule["actions"]:
                        do_action(imap, uid, raw, a)
                    rule_matched = True
                    processed += 1
                    break  # first-match wins
            
            if not rule_matched:
                logger.debug(f"UID {uid.decode() if isinstance(uid, bytes) else uid} did not match any rule")

        logger.info(f"Processed {processed} messages, expunging...")
        imap.expunge()
        imap.logout()
        logger.info("Filer completed successfully")
        
    except AssertionError as e:
        logger.error(f"Configuration error: {e}")
    except Exception as e:
        logger.exception(f"Filer error: {e}")

if __name__ == "__main__":
    main()

