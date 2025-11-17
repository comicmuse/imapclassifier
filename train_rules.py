#!/usr/bin/env python3
import imaplib, email, os, re, ssl, yaml, tempfile, shutil, logging
from email.header import decode_header, make_header
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.expanduser("~/.imap-trainer.log"))
    ]
)
logger = logging.getLogger(__name__)

IMAP_HOST = os.getenv("IMAP_HOST", "imap.mailbox.org")
IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASS = os.getenv("IMAP_PASS")

TRAIN_MAP = {
    "Train/Newsletters": [("move", "Newsletters")],
    "Train/Updates": [("move", "Updates")],
    "Train/Offers": [("mark_read", None), ("move", "Offers")],
    "Train/Receipts": [("mark_read", None), ("move", "Receipts")],
    "Train/Travel": [("forward", "plans@tripit.com"), ("move", "Travel/Flight Tickets"), ("mark_read", None)],
    "Train/AutoArchive": [("mark_read", None), ("move", "Archive")],
    "Train/AutoDelete": [("move", "Autodelete")],
}

RULES_FILE = os.path.expanduser("~/.imap-rules.yaml")

def h(msg, name):
    raw = msg.get(name, "")
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw

def extract_listid(msg):
    lid = h(msg, "List-Id")
    if not lid: return None
    m = re.search(r"<([^>]+)>", lid)
    return m.group(1).strip().lower() if m else lid.strip().lower()

def extract_listunsub(msg):
    lu = msg.get("List-Unsubscribe", "")
    if not lu: return None
    # find all things inside <> first; else split on commas
    parts = re.findall(r"<([^>]+)>", lu) or [p.strip() for p in lu.split(",")]
    for p in parts:
        p = p.strip("<> ").lower()
        if p.startswith("http"):
            host = urlparse(p).hostname
            if host: return host
        if p.startswith("mailto:") and "@" in p:
            return p.split("@")[-1]
    return None

def from_domain(msg):
    frm = h(msg, "From").lower()
    m = re.search(r'@([a-z0-9\.\-]+\.[a-z]{2,})', frm)
    return m.group(1) if m else frm

def subject_hint(msg):
    s = h(msg, "Subject").lower()
    for kw in ("itinerary","booking","reservation","boarding","ticket","trip","flight"):
        if kw in s:
            return kw
    return None

def load_rules():
    try:
        if os.path.exists(RULES_FILE):
            with open(RULES_FILE) as f:
                rules = yaml.safe_load(f) or {"rules":[]}
            logger.info(f"Loaded {len(rules.get('rules', []))} existing rules from {RULES_FILE}")
            return rules
        else:
            logger.info(f"Rules file not found; starting with empty rules")
            return {"rules":[]}
    except Exception as e:
        logger.error(f"Error loading rules: {e}")
        return {"rules":[]}

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

def _norm_actions(actions):
    out = []
    for a in actions:
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, dict):
            out.append(a)
        elif isinstance(a, (list, tuple)):
            # ['mark_read', None] -> 'mark_read'
            # ['move', 'Offers']  -> {'move': 'Offers'}
            name = a[0] if a else None
            if not name:
                continue
            arg = a[1] if len(a) > 1 else None
            if arg is None:
                out.append(name)
            else:
                out.append({name: arg})
    return out

def upsert_rule(data, header, contains, actions):
    actions = _norm_actions(actions)
    for r in data["rules"]:
        if r.get("match", {}).get("header") == header and r["match"].get("contains") == contains:
            logger.debug(f"Updated existing rule: {header}={contains} -> {actions}")
            r["actions"] = actions
            return
    logger.debug(f"Added new rule: {header}={contains} -> {actions}")
    data["rules"].append({
        "match": {"header": header, "contains": contains},
        "actions": actions
    })

def ensure_mailbox(imap, name):
    try:
        imap.create(name)
        logger.debug(f"Created mailbox: {name}")
    except imaplib.IMAP4.error as e:
        logger.debug(f"Mailbox {name} creation info: {e}")

def do_actions(imap, uid, actions):
    for a in actions:
        try:
            if a[0]=="mark_read":
                imap.uid("STORE", uid, "+FLAGS", r"(\Seen)")
                logger.info(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Marked as read")
            elif a[0]=="move":
                dest = a[1]
                ensure_mailbox(imap, dest)
                imap.uid("COPY", uid, dest)
                imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
                logger.info(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Moved to {dest}")
            elif a[0]=="forward":
                # No-op here; the filer handles forwarding for INBOX matches.
                # We'll forward now too (nice immediate feedback):
                raw = imap.uid("FETCH", uid, "(RFC822)")[1][0][1]
                send_forward(raw, a[1])
        except Exception as e:
            logger.error(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Error performing action '{a[0]}': {e}")

def send_forward(raw_bytes, to_addr):
    # Minimal SMTP forward as message/rfc822 attachment (works with TripIt)
    import smtplib
    from email.message import EmailMessage
    try:
        smtp_host = os.getenv("SMTP_HOST", "smtp.mailbox.org")
        smtp_user = os.getenv("SMTP_USER", IMAP_USER)
        smtp_pass = os.getenv("SMTP_PASS", IMAP_PASS)
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
        logger.error(f"Failed to forward to {to_addr}: {e}")

def main():
    try:
        logger.info("Starting trainer...")
        assert IMAP_USER and IMAP_PASS, "Set IMAP_USER/IMAP_PASS"
        
        logger.info(f"Connecting to {IMAP_HOST} as {IMAP_USER}")
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(IMAP_USER, IMAP_PASS)
        logger.info("Connected and authenticated")

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
                        subj = subj.decode('utf-8', errors='ignore')
                    frm = msg.get("From", "Unknown")
                    logger.debug(f"Processing UID {uid.decode() if isinstance(uid, bytes) else uid} from {train}: From={frm[:50]}, Subject={str(subj)[:50]}")

                    # choose best key
                    header, key = None, None
                    lid = extract_listid(msg)
                    if lid:
                        header, key = "List-Id", lid
                        logger.debug(f"  Extracted List-Id: {lid}")
                    else:
                        lu = extract_listunsub(msg)
                        if lu:
                            header, key = "List-Unsubscribe", lu
                            logger.debug(f"  Extracted List-Unsubscribe: {lu}")
                        else:
                            dom = from_domain(msg)
                            header, key = "From", dom
                            logger.debug(f"  Extracted From domain: {dom}")

                    if train == "Train/Travel":
                        sh = subject_hint(msg)
                        logger.debug(f"  Travel message - storing domain rule {header}={key}")
                        # Store the domain rule regardless; add a subject rule if we found a hint
                        upsert_rule(rules, header, key, actions)
                        if sh:
                            logger.debug(f"  Found travel subject hint: {sh}")
                            upsert_rule(rules, "Subject", sh, actions)
                    else:
                        upsert_rule(rules, header, key, actions)

                    # perform actions now & remove from Train/*
                    logger.info(f"Training on {train}: {header}={key}")
                    do_actions(imap, uid, actions)
                    total_trained += 1
                    
                except Exception as e:
                    logger.error(f"Error processing UID {uid.decode() if isinstance(uid, bytes) else uid}: {e}")

            logger.debug(f"Expunging {train}")
            imap.expunge()

        save_rules(rules)
        logger.info(f"Trainer completed: trained {total_trained} messages")
        imap.logout()
        logger.info("Disconnected from IMAP server")
        
    except AssertionError as e:
        logger.error(f"Configuration error: {e}")
    except Exception as e:
        logger.exception(f"Trainer error: {e}")

if __name__ == "__main__":
    main()

