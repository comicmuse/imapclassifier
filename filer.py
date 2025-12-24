#!/usr/bin/env python3
# ~/bin/filer.py
import imaplib, email, email.utils, os, re, ssl, yaml, logging, signal, time, argparse, sys, json
from urllib.parse import urlparse
from email.parser import BytesParser
from email.policy import default
from datetime import datetime

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
STATS_FILE = os.path.expanduser("~/.imap-filer-stats.json")

# Daemon configuration
POLL_INTERVAL_SECONDS = 60
DAILY_SUMMARY_HOUR = 19  # 19:00 local time

# Global flag for graceful shutdown
shutdown_requested = False

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global shutdown_requested
    try:
        sig_name = signal.Signals(signum).name
    except (ValueError, AttributeError):
        sig_name = f"signal {signum}"
    logger.info(f"Received {sig_name}, initiating graceful shutdown...")
    shutdown_requested = True

def load_stats():
    """Load statistics from disk"""
    if not os.path.exists(STATS_FILE):
        return {
            "last_summary_date": None,
            "stats": {
                "move": {},
                "delete": 0,
                "forward": 0,
                "mark_read": 0
            }
        }
    try:
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading stats file: {e}")
        return {
            "last_summary_date": None,
            "stats": {
                "move": {},
                "delete": 0,
                "forward": 0,
                "mark_read": 0
            }
        }

def save_stats(stats):
    """Save statistics to disk atomically"""
    try:
        # Write to temporary file first
        temp_file = STATS_FILE + ".tmp"
        with open(temp_file, 'w') as f:
            json.dump(stats, f, indent=2)
        # Atomic rename
        os.replace(temp_file, STATS_FILE)
    except Exception as e:
        logger.error(f"Error saving stats file: {e}")

def update_stats(action_name, action_arg=None):
    """Update statistics for an action"""
    stats = load_stats()
    
    if action_name == "move":
        dest = action_arg
        if dest:
            if dest not in stats["stats"]["move"]:
                stats["stats"]["move"][dest] = 0
            stats["stats"]["move"][dest] += 1
    elif action_name in ["delete", "forward", "mark_read"]:
        stats["stats"][action_name] += 1
    
    save_stats(stats)

def generate_summary_email():
    """Generate the daily summary email content"""
    stats = load_stats()
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Calculate total emails processed
    total = stats["stats"]["delete"] + stats["stats"]["forward"] + stats["stats"]["mark_read"]
    for count in stats["stats"]["move"].values():
        total += count
    
    # Build email body
    body_lines = [
        f"Daily Email Filing Summary - {today}",
        "",
        f"Total emails processed: {total}",
        ""
    ]
    
    if stats["stats"]["move"]:
        body_lines.append("Emails moved by destination:")
        for folder, count in sorted(stats["stats"]["move"].items()):
            body_lines.append(f"  {folder}: {count}")
        body_lines.append("")
    
    if stats["stats"]["delete"] > 0:
        body_lines.append(f"Emails deleted: {stats['stats']['delete']}")
    
    if stats["stats"]["forward"] > 0:
        body_lines.append(f"Emails forwarded: {stats['stats']['forward']}")
    
    if stats["stats"]["mark_read"] > 0:
        body_lines.append(f"Emails marked as read: {stats['stats']['mark_read']}")
    
    return "\n".join(body_lines), today

def send_daily_summary(imap):
    """Send daily summary email via IMAP APPEND"""
    try:
        # Delete any previous Daily Email Filing Summary messages
        try:
            imap.select('"INBOX"', readonly=False)
            typ, data = imap.uid("SEARCH", None, 'SUBJECT', 'Daily Email Filing Summary')
            if typ == "OK" and data and data[0]:
                old_summary_uids = data[0].split()
                for uid in old_summary_uids:
                    imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
                    logger.debug(f"Marked old summary UID {uid.decode() if isinstance(uid, bytes) else uid} for deletion")
                if old_summary_uids:
                    imap.expunge()
                    logger.info(f"Deleted {len(old_summary_uids)} previous Daily Summary email(s)")
        except Exception as e:
            logger.warning(f"Error deleting old summaries: {e}")
        
        body, today = generate_summary_email()
        
        # Create email message
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = f"Daily Email Filing Summary - {today}"
        msg["From"] = IMAP_USER
        msg["To"] = IMAP_USER
        msg["Date"] = email.utils.formatdate(localtime=True)
        
        # Convert to bytes
        msg_bytes = msg.as_bytes()
        
        # Append to INBOX without \Seen flag (keeps it unread), but mark with a custom flag
        # so that filing rules or the main loop can exclude these summary messages.
        imap.append('"INBOX"', 'DailySummary', None, msg_bytes)
        logger.info(f"Daily summary email sent for {today}")
        
        # Reset stats
        stats = load_stats()
        stats["last_summary_date"] = today
        stats["stats"] = {
            "move": {},
            "delete": 0,
            "forward": 0,
            "mark_read": 0
        }
        save_stats(stats)
        
    except Exception as e:
        logger.error(f"Error sending daily summary: {e}")

def check_and_send_daily_summary(imap):
    """Check if it's time to send daily summary (19:00)"""
    now = datetime.now()
    stats = load_stats()
    
    # Check if we should send summary
    today = now.strftime("%Y-%m-%d")
    should_send = False
    
    # Check if it's past 19:00 and we haven't sent today
    if now.hour >= DAILY_SUMMARY_HOUR:
        if stats["last_summary_date"] != today:
            # Only send if there's actual data to report
            total = stats["stats"]["delete"] + stats["stats"]["forward"] + stats["stats"]["mark_read"]
            for count in stats["stats"]["move"].values():
                total += count
            
            if total > 0:
                should_send = True
    
    if should_send:
        logger.info("Time to send daily summary")
        send_daily_summary(imap)


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
            update_stats("mark_read")
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
            update_stats("move", dest)
        elif name == "forward":
            if raw is None:
                typ, d = imap.uid("FETCH", uid, "(BODY.PEEK[])")  # full message without setting \Seen
                raw = d[0][1] if d and d[0] else b""
            send_forward(raw, arg)
            update_stats("forward")
        elif name == "delete":
            imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            logger.info(f"  [UID {uid.decode() if isinstance(uid, bytes) else uid}] Marked for deletion")
            update_stats("delete")
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
        
        # Check if it's time to send daily summary (daemon mode)
        check_and_send_daily_summary(imap)
        
        imap.logout()
        logger.info("Filer completed successfully")
        
    except AssertionError as e:
        logger.error(f"Configuration error: {e}")
    except Exception as e:
        logger.exception(f"Filer error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="IMAP Filer - Apply rules to INBOX messages (daemon mode only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run as daemon, polling every 60 seconds:
  python3 filer.py
  
Environment variables required:
  IMAP_HOST, IMAP_USER, IMAP_PASS
        """
    )
    args = parser.parse_args()
    
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Always run in daemon mode
    logger.info(f"Starting filer in daemon mode (polling every {POLL_INTERVAL_SECONDS} seconds)...")
    logger.info("Press Ctrl+C or send SIGTERM to stop")
    
    while not shutdown_requested:
        try:
            main()
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received, shutting down...")
            break
        except Exception as e:
            logger.exception(f"Error in daemon loop: {e}")
        
        if not shutdown_requested:
            logger.info(f"Sleeping for {POLL_INTERVAL_SECONDS} seconds until next poll...")
            # Sleep in small increments to allow quick shutdown
            for _ in range(POLL_INTERVAL_SECONDS):
                if shutdown_requested:
                    break
                time.sleep(1)
    
    logger.info("Daemon shutdown complete")

