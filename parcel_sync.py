#!/usr/bin/env python3
"""parcel-sync: add tracking numbers from shipping emails in Gmail to Parcel.

Reads Gmail with the read-only Gmail API scope, pulls tracking numbers out of
shipping notices, and adds each one to Parcel through Parcel's API.

Secrets never live in this file:
  PARCEL_API_KEY      env var (Parcel web app > API key), or
  PARCEL_API_KEY_FILE path to a file holding it (e.g. a Docker secret)
  credentials.json    OAuth client file from your Google Cloud project
                      (or set PARCEL_SYNC_CREDENTIALS to its path)
  token.json          written by `parcel_sync.py auth`, refreshed automatically

Usage:
  parcel_sync.py auth                 one-time Google consent, writes token.json
  parcel_sync.py run --dry-run        show what would be added, change nothing
  parcel_sync.py run                  add new deliveries to Parcel
  parcel_sync.py parse FILE           test the parser on a saved email body
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from email.utils import parseaddr
from pathlib import Path

# ---------------------------------------------------------------- config ----

CONFIG_DIR = Path(os.environ.get("PARCEL_SYNC_DIR", Path.home() / ".config" / "parcel-sync"))
CREDENTIALS_FILE = Path(os.environ.get("PARCEL_SYNC_CREDENTIALS", CONFIG_DIR / "credentials.json"))
TOKEN_FILE = CONFIG_DIR / "token.json"
STATE_FILE = CONFIG_DIR / "state.json"

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Gmail search for candidate mail. Amazon is excluded on purpose: its mails
# rarely carry a tracking number, and Parcel has its own Amazon integration.
GMAIL_QUERY = os.environ.get(
    "PARCEL_SYNC_QUERY",
    'newer_than:3d -from:amazon.com -category:promotions '
    '{subject:shipped subject:"on its way" subject:"on the way" subject:tracking '
    'subject:"en route" subject:shipment "tracking number"}',
)

PARCEL_ADD_URL = "https://api.parcel.app/external/add-delivery/"
PARCEL_LIST_URL = "https://api.parcel.app/external/deliveries/"

# Parcel allows 20 add requests per day, failures included. Stay under it.
MAX_ADDS_PER_24H = int(os.environ.get("PARCEL_SYNC_MAX_ADDS", "15"))
SEND_PUSH = os.environ.get("PARCEL_SYNC_PUSH", "1") == "1"
STATE_KEEP_DAYS = 60

log = logging.getLogger("parcel-sync")

# ---------------------------------------------------------------- parsing ---

# carrier_code values from Parcel's supported_carriers.json
CARRIER_KEYWORDS = [
    ("usps", r"\bUSPS\b|Postal Service|tools\.usps\.com"),
    ("ups", r"\bUPS\b|ups\.com"),
    ("fedex", r"\bFed ?Ex\b|fedex\.com"),
    ("dhl", r"\bDHL\b|dhl\.com"),
    ("ont", r"\bOnTrac\b"),
    ("cp", r"Canada Post"),
    ("uniuni", r"\bUniUni\b"),
    ("veho", r"\bVeho\b"),
]

# Tracking numbers inside carrier tracking links
URL_PATTERNS = [
    ("usps", r"usps\.com/[^\s\"'<>]{0,2048}?(?:tLabels|qtc_tLabels1|tRef=fullpage&tLc=\d+&text28777=&tLabels)=([A-Z0-9]{10,34})"),
    ("ups", r"ups\.com/[^\s\"'<>]{0,2048}?(?:tracknum|trackNums|InquiryNumber1)=([A-Z0-9]{10,34})"),
    ("fedex", r"fedex\.com/[^\s\"'<>]{0,2048}?(?:trknbr|tracknumbers|tracknumber_list)=(\d{12,22})"),
    ("dhl", r"dhl\.com/[^\s\"'<>]{0,2048}?(?:tracking-id|AWB|trackingNumber)=([A-Z0-9]{9,34})"),
]

# Tier 2: tracking pages from aggregators and merchants (Narvar, AfterShip,
# Route, Shopify order status and the like) that carry the number as a URL
# parameter. Carrier comes from the number's format or the mail's wording.
GENERIC_URL_PARAM_RE = re.compile(
    r"https?://[^\s\"'<>]{0,2048}?[?&](?:tracking_?numbers?|trackingNumbers?|tracking_?no|"
    r"tracking_?id|tracking|trackNum|tracknum|tn|tracking_code)=([A-Z0-9]{10,34})\b",
    re.IGNORECASE,
)

# Tier 3 (off by default): follow redirect-wrapped "click here to track" links,
# only for these hosts, reading only Location headers. Never renders a page.
RESOLVE_LINKS = os.environ.get("PARCEL_SYNC_RESOLVE_LINKS", "0") == "1"
RESOLVE_HOSTS = [h.strip().lower() for h in os.environ.get(
    "PARCEL_SYNC_RESOLVE_HOSTS",
    # click trackers and tracking-page providers commonly used in ship mails
    "narvar.com,aftership.com,route.com,parcelpanel.com,17track.net,"
    "sendgrid.net,list-manage.com,klclick.com,klaviyomail.com,mandrillapp.com,"
    "shopify.com,ups.com,fedex.com,usps.com,dhl.com,ontrac.com",
).split(",") if h.strip()]
MAX_LINKS_TO_RESOLVE = 5
LINK_RE = re.compile(r"https?://[^\s\"'<>)\]|]{1,2048}", re.IGNORECASE)
TRACK_WORDS_RE = re.compile(r"track|shipment|delivery|package", re.IGNORECASE)

# Formats distinctive enough to trust without a label
UPS_RE = re.compile(r"\b1Z[0-9A-Z]{16}\b")
USPS_RE = re.compile(r"\b9[2-5]\d{18,20}\b")  # 20-22 digit IMpb, starts 92-95

# "Tracking Number: X", "Tracking #X", "Tracking No. X"
# Mail bodies are untrusted: every repeat is bounded so a long run of
# whitespace can't cause catastrophic backtracking.
LABELED_RE = re.compile(
    r"tracking[ \t]{0,3}(?:number|no\.?|#|id)?[\s:#|]{0,10}([A-Z0-9]{10,34})\b",
    re.IGNORECASE,
)
# Apple-style: "Tracking Number" on one line, number on the next
LABELED_NEXTLINE_RE = re.compile(
    r"tracking[ \t]{0,3}(?:number|no\.?|#)[ \t]{0,10}\r?\n\s{0,10}([A-Z0-9]{10,34})\b",
    re.IGNORECASE,
)

# Parse at most this much of each mail. Real shipping notices are far smaller.
MAX_BODY_CHARS = 300_000
# A shipping notice rarely carries more than a few numbers. Capping this keeps
# one hostile mail from using up the daily add limit.
MAX_NUMBERS_PER_MESSAGE = int(os.environ.get("PARCEL_SYNC_MAX_PER_MESSAGE", "3"))


_BLOCK_OPEN_RE = re.compile(r"<(script|style)\b", re.IGNORECASE)


def _strip_script_style(raw: str) -> str:
    """Drop <script>/<style> blocks in one linear pass (an unclosed block
    drops the rest of the document)."""
    low, out, i = raw.lower(), [], 0
    while True:
        m = _BLOCK_OPEN_RE.search(raw, i)
        if not m:
            out.append(raw[i:])
            break
        out.append(raw[i:m.start()])
        end = low.find("</" + m.group(1).lower(), m.end())
        if end == -1:
            break
        close = low.find(">", end)
        if close == -1:
            break
        i = close + 1
    return " ".join(out)


def html_to_text(raw: str) -> str:
    """Rough HTML to text that keeps hrefs, so tracking links survive."""
    raw = _strip_script_style(raw[:MAX_BODY_CHARS])
    raw = re.sub(r'(?i)<a\s[^<>]{0,2000}?href=["\']([^"\'<>]{1,2048})["\'][^<>]{0,2000}>',
                 r" \1 ", raw)
    raw = re.sub(r"(?i)<br[ \t/]{0,5}>|</(p|div|tr|li|h\d)>", "\n", raw)
    raw = re.sub(r"<[^<>]*>", " ", raw)
    return html.unescape(raw)


def carrier_from_format(num: str) -> str | None:
    if UPS_RE.fullmatch(num):
        return "ups"
    if USPS_RE.fullmatch(num):
        return "usps"
    return None


def carrier_from_context(text: str, pos: int) -> str | None:
    """Carrier named closest before the number, else anywhere in the mail."""
    window = text[max(0, pos - 400):pos]
    best, best_at = None, -1
    for code, pat in CARRIER_KEYWORDS:
        for m in re.finditer(pat, window, re.IGNORECASE):
            if m.start() > best_at:
                best, best_at = code, m.start()
    if best:
        return best
    hits = [code for code, pat in CARRIER_KEYWORDS if re.search(pat, text, re.IGNORECASE)]
    return hits[0] if len(hits) == 1 else None


def plausible(num: str) -> bool:
    if not re.search(r"\d", num):
        return False
    if len(set(num)) < 4:  # 0000000000 and the like
        return False
    return True


def extract_tracking(text: str) -> list[tuple[str, str]]:
    """Return [(tracking_number, carrier_code)] found in an email body."""
    text = text[:MAX_BODY_CHARS]
    found: dict[str, str] = {}

    def add(num: str, carrier: str | None):
        num = num.upper()
        if carrier and plausible(num) and num not in found:
            found[num] = carrier

    for carrier, pat in URL_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            add(m.group(1), carrier)

    for m in GENERIC_URL_PARAM_RE.finditer(text):
        num = m.group(1)
        add(num, carrier_from_format(num.upper()) or carrier_from_context(text, m.start()))

    for m in UPS_RE.finditer(text):
        add(m.group(0), "ups")
    for m in USPS_RE.finditer(text):
        add(m.group(0), "usps")

    for rx in (LABELED_NEXTLINE_RE, LABELED_RE):
        for m in rx.finditer(text):
            num = m.group(1).upper()
            if num in found:
                continue
            carrier = carrier_from_format(num) or carrier_from_context(text, m.start())
            # FedEx/DHL numbers are plain digits; only trust them when labeled
            # and a carrier is named, which is the case on this path.
            add(num, carrier)

    return list(found.items())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _host_allowed(url: str) -> bool:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in RESOLVE_HOSTS)


def resolve_link(url: str, max_hops: int = 5) -> str:
    """Follow redirects on allowlisted hosts only; return the last URL seen.
    Uses HEAD and reads only Location headers, never page content."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(max_hops):
        if not _host_allowed(url):
            break
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "parcel-sync"})
        try:
            opener.open(req, timeout=10)
            break  # 2xx: no further redirect
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urllib.request.urljoin(url, loc)
                continue
            break
        except (urllib.error.URLError, TimeoutError):
            break
    return url


def resolve_tracking_links(text: str) -> list[tuple[str, str]]:
    """Tier 3: expand wrapped tracking links, then parse the final URLs."""
    links = []
    for m in LINK_RE.finditer(text):
        url = m.group(0)
        around = text[max(0, m.start() - 80):m.end() + 80]
        if _host_allowed(url) and TRACK_WORDS_RE.search(around) and url not in links:
            links.append(url)
    finals = [resolve_link(u) for u in links[:MAX_LINKS_TO_RESOLVE]]
    # Parse only the final URLs, with the mail's wording for carrier context.
    return extract_tracking("\n".join(finals) + "\n" + text) if finals else []


def needs_manual(msg: dict, desc: str) -> None:
    """Tier 4: record mail we could not resolve, with a link to open it."""
    line = (f"{time.strftime('%Y-%m-%d %H:%M')}  {desc}  "
            f"https://mail.google.com/mail/u/0/#all/{msg.get('threadId', '')}\n")
    path = CONFIG_DIR / "needs_manual.log"
    with path.open("a") as f:
        f.write(line)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- state -----

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"messages": {}, "tracking": {}, "adds": []}


def save_state(state: dict) -> None:
    cutoff = time.time() - STATE_KEEP_DAYS * 86400
    state["messages"] = {k: v for k, v in state["messages"].items() if v > cutoff}
    state["tracking"] = {k: v for k, v in state["tracking"].items() if v > cutoff}
    state["adds"] = [t for t in state["adds"] if t > time.time() - 86400]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- parcel ----

class TransientError(Exception):
    pass


def parcel_api_key() -> str | None:
    """PARCEL_API_KEY, else the contents of the file named by PARCEL_API_KEY_FILE."""
    key = os.environ.get("PARCEL_API_KEY", "").strip()
    path = os.environ.get("PARCEL_API_KEY_FILE")
    if not key and path:
        try:
            key = Path(path).read_text().strip()
        except OSError as e:
            sys.exit(f"Could not read PARCEL_API_KEY_FILE {path}: {e}")
    return key or None


def parcel_request(url: str, api_key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if body is not None else "GET",
        headers={"api-key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        payload = e.read().decode(errors="replace")
        if e.code == 429 or e.code >= 500:
            raise TransientError(f"HTTP {e.code}: {payload[:200]}")
        try:
            return json.loads(payload)
        except ValueError:
            return {"success": False, "error_message": f"HTTP {e.code}: {payload[:200]}"}
    except (urllib.error.URLError, TimeoutError) as e:
        raise TransientError(str(e))


def parcel_known_numbers(api_key: str) -> set[str]:
    """Tracking numbers already in Parcel (limit: 20 calls/hour)."""
    try:
        res = parcel_request(f"{PARCEL_LIST_URL}?filter_mode=recent", api_key)
    except TransientError as e:
        log.warning("could not list Parcel deliveries: %s", e)
        return set()
    if not res.get("success"):
        log.warning("could not list Parcel deliveries: %s", res.get("error_message"))
        return set()
    return {d.get("tracking_number", "").upper() for d in res.get("deliveries", [])}


# ---------------------------------------------------------------- gmail -----

def gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN_FILE.exists():
        sys.exit(f"No {TOKEN_FILE}. Run `parcel_sync.py auth` first.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
        os.chmod(TOKEN_FILE, 0o600)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def cmd_auth(args) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CREDENTIALS_FILE.exists():
        sys.exit(f"Put your OAuth client file at {CREDENTIALS_FILE} first.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
    creds = flow.run_local_server(port=args.port, bind_addr=args.bind,
                                  open_browser=not args.no_browser)
    TOKEN_FILE.write_text(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)
    print(f"Saved {TOKEN_FILE}")


def message_text(msg: dict) -> str:
    parts, plain, htmls = [msg["payload"]], [], []
    while parts:
        p = parts.pop()
        parts.extend(p.get("parts", []))
        data = p.get("body", {}).get("data")
        if not data:
            continue
        body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        mime = p.get("mimeType", "")
        if mime == "text/plain":
            plain.append(body)
        elif mime == "text/html":
            htmls.append(html_to_text(body))
    # Use both: some senders put the number only in the HTML part.
    return "\n".join(plain + htmls)


def header(msg: dict, name: str) -> str:
    for h in msg["payload"].get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def describe(msg: dict) -> str:
    sender_name, sender_addr = parseaddr(header(msg, "From"))
    who = sender_name or sender_addr.split("@")[-1]
    subject = re.sub(r"\s+", " ", header(msg, "Subject")).strip()
    return f"{who}: {subject}"[:60]


# ---------------------------------------------------------------- run -------

def cmd_run(args) -> int:
    api_key = parcel_api_key()
    if not api_key and not args.dry_run:
        sys.exit("PARCEL_API_KEY is not set (or set PARCEL_API_KEY_FILE).")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    svc = gmail_service()

    ids, page = [], None
    while True:
        res = svc.users().messages().list(userId="me", q=GMAIL_QUERY, pageToken=page).execute()
        ids += [m["id"] for m in res.get("messages", [])]
        page = res.get("nextPageToken")
        if not page:
            break
    new_ids = [i for i in ids if i not in state["messages"]]
    log.info("%d candidate mails, %d new", len(ids), len(new_ids))
    if not new_ids:
        return 0

    known = parcel_known_numbers(api_key) if api_key else set()
    exit_code = 0

    for mid in new_ids:
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        desc = describe(msg)
        text = message_text(msg)
        hits = extract_tracking(text)
        if not hits and RESOLVE_LINKS:
            hits = resolve_tracking_links(text)
        if len(hits) > MAX_NUMBERS_PER_MESSAGE:
            log.warning("MANUAL %d numbers in one mail (limit %d), not adding: %s",
                        len(hits), MAX_NUMBERS_PER_MESSAGE, desc)
            if not args.dry_run:
                needs_manual(msg, f"{len(hits)} numbers: {desc}")
                state["messages"][mid] = time.time()
            continue
        if not hits:
            log.info("MANUAL no tracking number found: %s", desc)
            if not args.dry_run:
                needs_manual(msg, desc)
                state["messages"][mid] = time.time()
            continue

        done = True
        for num, carrier in hits:
            if num in state["tracking"] or num in known:
                log.info("DUP  %s %s already added (%s)", carrier, num, desc)
                continue
            if args.dry_run:
                log.info("WOULD ADD %s %s  %s", carrier, num, desc)
                continue
            state["adds"] = [t for t in state["adds"] if t > time.time() - 86400]
            if len(state["adds"]) >= MAX_ADDS_PER_24H:
                log.warning("daily cap of %d reached, leaving %s for next run", MAX_ADDS_PER_24H, num)
                done = False
                continue
            try:
                state["adds"].append(time.time())
                res = parcel_request(PARCEL_ADD_URL, api_key, {
                    "tracking_number": num,
                    "carrier_code": carrier,
                    "description": desc,
                    "send_push_confirmation": SEND_PUSH,
                })
            except TransientError as e:
                log.warning("Parcel unreachable for %s, will retry: %s", num, e)
                done = False
                exit_code = 1
                continue
            if res.get("success"):
                log.info("ADD  %s %s  %s", carrier, num, desc)
            else:
                # Permanent rejection (bad format, unknown carrier, etc.).
                log.warning("FAIL %s %s  %s: %s", carrier, num, desc, res.get("error_message"))
            state["tracking"][num] = time.time()

        if done and not args.dry_run:
            state["messages"][mid] = time.time()

    if not args.dry_run:
        save_state(state)
    return exit_code


def cmd_parse(args) -> None:
    text = Path(args.file).read_text(errors="replace")
    if args.file.endswith((".html", ".htm")):
        text = html_to_text(text)
    for num, carrier in extract_tracking(text):
        print(carrier, num)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth", help="one-time Google consent")
    a.add_argument("--port", type=int, default=8765)
    a.add_argument("--bind", default=os.environ.get("PARCEL_SYNC_AUTH_BIND", "localhost"),
                   help="address the sign-in server listens on (0.0.0.0 inside Docker)")
    a.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    r = sub.add_parser("run", help="check Gmail and add to Parcel")
    r.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("parse", help="test the parser on a saved email body")
    p.add_argument("file")
    args = ap.parse_args()
    if args.cmd == "auth":
        cmd_auth(args)
    elif args.cmd == "run":
        sys.exit(cmd_run(args))
    else:
        cmd_parse(args)


if __name__ == "__main__":
    main()
