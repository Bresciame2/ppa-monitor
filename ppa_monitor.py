#!/usr/bin/env python3
"""
PPA Lebanon Tender Monitor
--------------------------
Walks the sequential tender-ID space on ppa.gov.lb, parses each new tender,
scores it against BME's areas of interest, and emails the matches.

Filter strategy (as specified):
  Stream A - PROCURING ENTITY : army / MoD / ISF / General Security / State Security
  Stream B - SECTOR + TITLE   : security & equipment categories, plus keyword
                                matching on the tender title (the title is always
                                populated; the Sector field frequently is NOT)

A tender hit by both streams is flagged HIGH priority.
Borderline cases are adjudicated by Claude.

Config lives in config.yaml. Secrets come from environment variables.
"""

import os
import re
import sys
import json
import time
import smtplib
import logging
from pathlib import Path
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import yaml
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# Paths & logging
# ----------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "state.json"
CONFIG_FILE = BASE / "config.yaml"
LOG_FILE = BASE / "monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ppa")

BASE_URL = "https://www.ppa.gov.lb/en/tenders/details/{}"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_id": None, "seen": [], "notified": [], "pending": []}


def save_state(state):
    state["seen"] = sorted(set(state["seen"]))[-5000:]
    state["notified"] = sorted(set(state["notified"]))[-5000:]
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_config():
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------
# Fetch & parse
# ----------------------------------------------------------------------------
def fetch(tender_id, session, retries=3):
    """Return HTML for a tender id, or None if it doesn't exist."""
    url = BASE_URL.format(tender_id)
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                # The site returns HTTP 200 with a "Data not found!" shell for
                # tender IDs that don't exist yet, rather than a 404.
                if "data-not-found" in r.text or "Data not found" in r.text:
                    return None
                # A valid tender page always carries an active breadcrumb item
                # (the tender title itself).
                if "breadcrumb-item active" not in r.text:
                    return None
                return r.text
            log.warning("id=%s HTTP %s (attempt %s)", tender_id, r.status_code, attempt + 1)
        except requests.RequestException as e:
            log.warning("id=%s request error: %s (attempt %s)", tender_id, e, attempt + 1)
        time.sleep(2 * (attempt + 1))
    return None


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def parse(html, tender_id):
    """Extract the fields we care about from a tender detail page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for sel in soup.find_all("select"):        # huge, useless dropdowns
        sel.decompose()

    rec = {
        "id": tender_id,
        "url": BASE_URL.format(tender_id),
        "title": "",
        "entity": "",
        "stage": "",
        "status": "",
        "announced": "",
        "opening": "",
    }

    # Title lives in the active breadcrumb item.
    crumb = soup.select_one("li.breadcrumb-item.active")
    if crumb:
        rec["title"] = _clean(crumb.get_text())

    # Header block: <span class="article-date-title">LABEL</span><br>
    #               <span class="article-date-text">VALUE</span>
    header_map = {
        "Procuring entity": "entity",
        "Stage": "stage",
        "Status": "status",
        "Announced in": "announced",
        "Opening offers in": "opening",
    }
    for span in soup.find_all("span", class_="article-date-title"):
        label = _clean(span.get_text())
        key = header_map.get(label)
        if not key:
            continue
        val = span.find_next("span", class_=["article-date-text", "article-badge"])
        if val:
            rec[key] = _clean(val.get_text())

    # Detail table: <td><strong>LABEL :</strong></td><td>VALUE</td>
    for strong in soup.find_all("strong"):
        label = _clean(strong.get_text())
        if not label.endswith(":"):
            continue
        td = strong.find_parent("td")
        if not td:
            continue
        sib = td.find_next_sibling("td")
        if not sib:
            continue
        key = label[:-1].strip().lower().replace(" ", "_")
        rec[key] = _clean(sib.get_text(" "))

    return rec


# ----------------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------------
def norm_ar(s):
    """Normalise Arabic so alef/ya/ta-marbuta variants and diacritics match."""
    s = re.sub(r"[\u064B-\u0652\u0640]", "", s or "")
    s = re.sub(r"[إأآا]", "ا", s)
    s = s.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", s).strip()


def match_entity(rec, cfg):
    ent = norm_ar(rec.get("entity", ""))
    for target in cfg["entities"]:
        if norm_ar(target) and norm_ar(target) in ent:
            return target
    return None


def match_sector_or_title(rec, cfg):
    """Stream B. Sector is often blank, so the title carries most of the load."""
    hits = []
    sector = (rec.get("sector") or "").lower()
    for s in cfg["sectors"]:
        if s.lower() and s.lower() in sector:
            hits.append(f"sector:{s}")

    haystack_raw = " ".join([rec.get("title", ""), rec.get("description", ""),
                             rec.get("purchase_brief", "")])
    haystack = norm_ar(haystack_raw.lower())
    for kw in cfg["keywords"]:
        if norm_ar(kw.lower()) in haystack:
            hits.append(f"keyword:{kw}")
    return hits


def is_excluded(rec, cfg):
    hay = norm_ar((rec.get("title", "") + " " + rec.get("description", "")).lower())
    for kw in cfg.get("exclude_keywords", []):
        if norm_ar(kw.lower()) in hay:
            return kw
    return None


# ----------------------------------------------------------------------------
# Claude adjudication for borderline cases
# ----------------------------------------------------------------------------
def ask_claude(rec, cfg):
    """Return (relevant: bool, reason: str). Fails open: on error, treat as relevant."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return True, "no API key set - passed through unjudged"

    prompt = f"""You are screening Lebanese public procurement tenders for a firearms, \
ammunition, and defense-equipment distributor. The company supplies: firearms and \
ammunition, less-lethal and riot-control equipment, body armor and ballistic protection, \
tactical gear and uniforms, optics and night vision, security and detection systems, \
and related spare parts and servicing. It also buys government surplus weapons, \
ammunition, and equipment at auction.

Tender:
  Title: {rec.get('title')}
  Procuring entity: {rec.get('entity')}
  Sector: {rec.get('sector') or '(blank)'}
  Purchase type: {rec.get('purchase_type')}
  Method: {rec.get('procuring_method')}
  Description: {rec.get('description') or '(blank)'}

Is this tender relevant to that company's business? Consider both selling TO the \
government and buying surplus FROM it. Note the title may be in Arabic.

Respond with JSON only, no other text:
{{"relevant": true or false, "reason": "one short sentence", "confidence": "high" or "low"}}"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": cfg.get("claude_model", "claude-sonnet-4-6"),
                  "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", [])
                       if b.get("type") == "text")
        text = re.sub(r"```(?:json)?|```", "", text).strip()
        data = json.loads(text)
        return bool(data.get("relevant")), data.get("reason", "")
    except Exception as e:
        log.warning("Claude call failed for id=%s: %s", rec["id"], e)
        return True, f"AI check failed ({e}) - included to be safe"


# ----------------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------------
def build_email(matches, cfg):
    relevant = [m for m in matches if m["priority"] in ("HIGH", "NORMAL")]
    others = [m for m in matches if m["priority"] not in ("HIGH", "NORMAL")]

    rows = []
    for m in sorted(relevant, key=lambda x: (x["priority"] != "HIGH", x["rec"]["id"])):
        r = m["rec"]
        colour = "#b91c1c" if m["priority"] == "HIGH" else "#1d4ed8"
        rows.append(f"""
        <tr>
          <td style="padding:14px;border-bottom:1px solid #e5e7eb;vertical-align:top">
            <div style="font-size:11px;font-weight:700;color:{colour};letter-spacing:.5px">
              {m['priority']} &nbsp;&middot;&nbsp; #{r['id']}
            </div>
            <div style="font-size:15px;font-weight:600;margin:6px 0;color:#111827">
              {r.get('title','(no title)')}
            </div>
            <div style="font-size:13px;color:#374151;line-height:1.7">
              <b>Entity:</b> {r.get('entity','-')}<br>
              <b>Method:</b> {r.get('procuring_method','-')} &nbsp;|&nbsp;
              <b>Type:</b> {r.get('purchase_type','-')}<br>
              <b>Announced:</b> {r.get('announced','-')}<br>
              <b>Offers open:</b> <span style="color:#b91c1c;font-weight:600">{r.get('opening','-')}</span><br>
              <b>Submission deadline:</b> {r.get('deadline_for_submission_of_offers','-')}<br>
              <b>Guarantee:</b> {r.get('offer_guarantee_value','-')} {r.get('currency','')}<br>
              <b>Matched:</b> <span style="color:#6b7280">{m['why']}</span>
            </div>
            <div style="margin-top:10px">
              <a href="{r['url']}" style="background:#111827;color:#fff;padding:8px 14px;
                 border-radius:6px;text-decoration:none;font-size:13px">Open tender</a>
            </div>
          </td>
        </tr>""")

    other_rows = []
    for m in sorted(others, key=lambda x: x["rec"]["id"]):
        r = m["rec"]
        other_rows.append(f"""
        <tr>
          <td style="padding:8px 14px;border-bottom:1px solid #f3f4f6;font-size:12px;
              color:#9ca3af;vertical-align:top;white-space:nowrap">#{r['id']}</td>
          <td style="padding:8px 14px;border-bottom:1px solid #f3f4f6;font-size:12px;color:#374151">
            <a href="{r['url']}" style="color:#111827;text-decoration:none;font-weight:600">
              {r.get('title','(no title)')}</a><br>
            <span style="color:#6b7280">{r.get('entity','-')} &middot;
              opens: {r.get('opening','-')}</span>
          </td>
        </tr>""")

    sections = []
    if rows:
        sections.append(f"""
        <div style="padding:10px 20px;background:#fef2f2;font-size:12px;font-weight:700;
            color:#b91c1c;letter-spacing:.5px">RELEVANT MATCHES ({len(relevant)})</div>
        <table style="width:100%;border-collapse:collapse">{''.join(rows)}</table>""")
    else:
        sections.append("""
        <div style="padding:14px 20px;font-size:13px;color:#6b7280">
          No tenders matched your filters today.</div>""")
    if other_rows:
        sections.append(f"""
        <div style="padding:10px 20px;background:#f9fafb;font-size:12px;font-weight:700;
            color:#6b7280;letter-spacing:.5px;border-top:1px solid #e5e7eb">
          ALL OTHER NEW TENDERS ({len(others)})</div>
        <table style="width:100%;border-collapse:collapse">{''.join(other_rows)}</table>""")

    html = f"""<html><body style="margin:0;padding:24px;background:#f9fafb;
      font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
      <div style="max-width:680px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px">
        <div style="padding:18px 20px;border-bottom:2px solid #111827">
          <div style="font-size:18px;font-weight:700;color:#111827">PPA Tender Alert</div>
          <div style="font-size:13px;color:#6b7280;margin-top:2px">
            {len(matches)} new tender{'s' if len(matches)!=1 else ''} &middot;
            {len(relevant)} relevant &middot;
            {datetime.now(timezone.utc).astimezone().strftime('%d %b %Y, %H:%M')}
          </div>
        </div>
        {''.join(sections)}
        <div style="padding:14px 20px;font-size:11px;color:#9ca3af;border-top:1px solid #e5e7eb">
          Automated monitor &middot; ppa.gov.lb &middot; HIGH = matched both entity and category filters
        </div>
      </div></body></html>"""
    return html


def send_email(matches, cfg):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    to = cfg["notify_email"]

    if not all([host, user, pwd]):
        log.error("SMTP env vars missing - cannot send. Set SMTP_HOST/USER/PASS.")
        return False

    n_rel = sum(1 for m in matches if m["priority"] in ("HIGH", "NORMAL"))
    n_high = sum(1 for m in matches if m["priority"] == "HIGH")
    subject = (f"[PPA] {len(matches)} new tender{'s' if len(matches)!=1 else ''}"
               f" - {n_rel} relevant")
    if n_high:
        subject += f" ({n_high} high priority)"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(build_email(matches, cfg), "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        log.info("Emailed %s match(es) to %s", len(matches), to)
        return True
    except Exception as e:
        log.error("Email send failed: %s", e)
        return False


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    cfg = load_config()
    state = load_state()
    session = requests.Session()

    start = state.get("last_id") or cfg["start_id"]
    gap_limit = cfg.get("gap_limit", 25)
    max_scan = cfg.get("max_scan_per_run", 300)

    log.info("Scan starting after id=%s (gap limit %s)", start, gap_limit)

    # Matches from a previous run whose email failed to send ride along with
    # this run's matches, so a transient SMTP failure never loses a tender.
    matches = state.get("pending", [])
    consecutive_misses, tid, scanned, highest = 0, start + 1, 0, start

    while consecutive_misses < gap_limit and scanned < max_scan:
        scanned += 1
        html = fetch(tid, session)

        if html is None:
            consecutive_misses += 1
            tid += 1
            continue

        consecutive_misses = 0
        highest = tid
        rec = parse(html, tid)
        state["seen"].append(tid)
        log.info("id=%s | %s | %s", tid, rec.get("entity", "?")[:32], rec.get("title", "")[:60])

        ent_hit = match_entity(rec, cfg)
        cat_hits = match_sector_or_title(rec, cfg)
        excl = is_excluded(rec, cfg)

        # Classify. NOTHING is dropped from the email any more: tenders that
        # don't match the filters ride along as OTHER and are listed compactly
        # below the highlighted matches. Filters decide priority, not inclusion.
        priority, why = "OTHER", ""
        if excl and not ent_hit:
            why = f"noise word: {excl}"
        elif ent_hit and cat_hits:
            priority, why = "HIGH", f"entity:{ent_hit} + " + ", ".join(cat_hits[:3])
        elif cat_hits:
            priority, why = "NORMAL", ", ".join(cat_hits[:3])
        elif ent_hit:
            # Right buyer, unclear (or noisy) item -> let Claude set priority.
            ok, reason = ask_claude(rec, cfg)
            if ok:
                priority, why = "NORMAL", f"entity:{ent_hit} (AI: {reason})"
            else:
                why = f"entity:{ent_hit}, AI: {reason}"

        if tid not in state["notified"]:
            matches.append({"rec": rec, "priority": priority, "why": why})
            if priority != "OTHER":
                log.info("   >>> MATCH [%s] %s", priority, why)

        tid += 1
        time.sleep(cfg.get("delay_seconds", 1.5))

    state["last_id"] = highest
    log.info("Scan done. scanned=%s highest=%s matches=%s", scanned, highest, len(matches))

    if matches:
        if send_email(matches, cfg):
            state["notified"].extend(m["rec"]["id"] for m in matches)
            state["pending"] = []
        else:
            state["pending"] = matches
            log.warning("Email failed - %s match(es) queued for retry next run", len(matches))
    else:
        log.info("No new matches - no email sent.")

    save_state(state)


if __name__ == "__main__":
    main()
