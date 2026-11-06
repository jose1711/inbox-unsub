"""
inbox-unsub – prehľad hromadných e-mailov a odhlásení.

Pravidelne sa pripojí do schránky cez IMAP (len číta hlavičky), nájde
hromadné / marketingové e-maily, zapíše ich do SQLite a cez webovú
stránku ti nechá rozhodnúť: ponechať, spam, alebo odhlásiť.
Keď odosielateľ píše aj po odhlásení, zvýrazní ho v záložke „Znova píšu“.
"""

import csv
import hashlib
import imaplib
import io
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, message_from_string
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, flash, g, redirect,
                   render_template, request, session, url_for)

# --------------------------------------------------------------------------
# Konfigurácia (všetko cez premenné prostredia, pozri .env.example)
# --------------------------------------------------------------------------


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


CFG = {
    "IMAP_HOST": os.environ.get("IMAP_HOST", "imap.gmail.com"),
    "IMAP_PORT": _env_int("IMAP_PORT", 993),
    "IMAP_USER": os.environ.get("IMAP_USER", ""),
    "IMAP_PASSWORD": os.environ.get("IMAP_PASSWORD", ""),
    "IMAP_FOLDERS": [f.strip() for f in os.environ.get("IMAP_FOLDERS", "INBOX").split(",") if f.strip()],
    "JUNK_FOLDER": os.environ.get("JUNK_FOLDER", "").strip(),
    # Automatický presun do JUNK_FOLDER: "spam" = odosielatelia označení ako spam,
    # "again" = odosielatelia, ktorí píšu po odhlásení. Prázdne = vypnuté.
    "AUTO_JUNK": {w.strip().lower() for w in os.environ.get("AUTO_JUNK", "").split(",") if w.strip()},
    "SCAN_INTERVAL_MIN": max(5, _env_int("SCAN_INTERVAL_MIN", 60)),
    "INITIAL_DAYS": _env_int("INITIAL_DAYS", 30),
    "GRACE_DAYS": _env_int("GRACE_DAYS", 3),
    "MIN_SCORE": _env_int("MIN_SCORE", 3),
    # Slová hľadané v tele e-mailu (na strane servera, obsah sa nesťahuje); prázdne = vypnuté
    "BODY_KEYWORDS": [w.strip() for w in os.environ.get(
        "BODY_KEYWORDS", "unsubscribe,abmelden,opt-out").split(",") if w.strip()],
    "DB_PATH": os.environ.get("DB_PATH", "data/inbox.db"),
    "WEB_USER": os.environ.get("WEB_USER", "admin"),
    "WEB_PASSWORD": os.environ.get("WEB_PASSWORD", ""),
    "PORT": _env_int("PORT", 8080),
}

try:
    DISPLAY_TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))
except Exception:
    DISPLAY_TZ = None  # systémový čas

STATUSES = {
    "new": "Na rozhodnutie",
    "keep": "Ponechané",
    "spam": "Spam",
    "unsubscribed": "Odhlásené",
}

TS_FMT = "%Y-%m-%d %H:%M:%S"  # všetky časy v DB sú v UTC v tomto tvare
IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# Hlavičky, ktoré pridávajú platformy na hromadné rozosielanie (ESP)
ESP_HEADERS = ("FEEDBACK-ID X-CSA-COMPLAINTS X-CAMPAIGN X-CAMPAIGN-ID X-CAMPAIGNID X-RPCAMPAIGN "
               "X-MAILGUN-TAG X-MAILGUN-SID X-SG-EID X-SES-OUTGOING X-MC-USER X-MAILCHIMP-ID "
               "X-LISTMEMBER X-MSYS-API X-SFMC-STACK X-MARKETO-ID X-MAILER-REFERENCE X-MESSAGE-REFERENCE")
HEADER_FIELDS = ("FROM SUBJECT DATE MESSAGE-ID RETURN-PATH ERRORS-TO " + ESP_HEADERS + " LIST-UNSUBSCRIBE "
                 "LIST-UNSUBSCRIBE-POST LIST-ID PRECEDENCE")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("inbox-unsub")
imaplib._MAXLINE = 10_000_000  # niektoré hlavičky sú dlhé

# --------------------------------------------------------------------------
# Databáza
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS senders (
    id              INTEGER PRIMARY KEY,
    address         TEXT UNIQUE NOT NULL,
    name            TEXT,
    domain          TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    score           INTEGER DEFAULT 0,
    first_seen      TEXT,
    last_seen       TEXT,
    unsub_http      TEXT,
    unsub_mailto    TEXT,
    one_click       INTEGER DEFAULT 0,
    decided_at      TEXT,
    unsubscribed_at TEXT,
    unsub_method    TEXT,
    note            TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    sender_id  INTEGER NOT NULL REFERENCES senders(id),
    msg_key    TEXT UNIQUE NOT NULL,
    folder     TEXT,
    subject    TEXT,
    sent_at    TEXT,
    seen_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id, sent_at);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    sender_id INTEGER REFERENCES senders(id),
    at        TEXT,
    action    TEXT,
    detail    TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def ts(dt):
    return dt.astimezone(timezone.utc).strftime(TS_FMT)


def db_connect():
    path = CFG["DB_PATH"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = db_connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def log_event(conn, sender_id, action, detail=""):
    conn.execute("INSERT INTO events(sender_id, at, action, detail) VALUES(?,?,?,?)",
                 (sender_id, ts(now_utc()), action, detail))


# --------------------------------------------------------------------------
# Rozbor hlavičiek
# --------------------------------------------------------------------------

BULK_LOCALPART = re.compile(
    r"(newsletter|news|noreply|no-reply|donotreply|marketing|promo|mailer|offers?|deals?|campaign)",
    re.I)


def decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:
        return str(value).strip()


def _is_bounce_address(value):
    """Return-Path / Errors-To typu bounce.firma.com alebo dlhá VERP adresa."""
    _, a = parseaddr(str(value or ""))
    a = a.lower()
    if "@" not in a:
        return False
    local, domain = a.split("@", 1)
    return ("bounce" in a or domain.startswith(("bnc.", "bounces.", "mail-bounce"))
            or (len(local) >= 24 and sum(c.isdigit() for c in local) >= 3))


def analyze(raw_headers: bytes, extra=None):
    """Z hlavičiek jedného e-mailu vráti slovník s údajmi a skóre „hromadnosti“.
    `extra` je zoznam (body, dôvod) zistených mimo hlavičiek (napr. hľadaním na serveri)."""
    # Niektorí odosielatelia dávajú do hlavičiek surové UTF-8 namiesto =?utf-8?...?=
    try:
        m = message_from_string(raw_headers.decode("utf-8"))
    except UnicodeDecodeError:
        m = message_from_bytes(raw_headers)
    name, addr = parseaddr(decode(m.get("From")))
    addr = addr.lower().strip()
    if not addr or "@" not in addr:
        return None

    subject = decode(m.get("Subject"))[:300]

    lu = " ".join(str(m.get("List-Unsubscribe", "")).split())
    links = [l.strip() for l in re.findall(r"<([^>]+)>", lu)]
    http = next((l for l in links if l.lower().startswith(("https://", "http://"))), "")
    mailto = next((l for l in links if l.lower().startswith("mailto:")), "")
    one_click = int("one-click" in str(m.get("List-Unsubscribe-Post", "")).lower()
                    and http.lower().startswith("https://"))

    score, reasons = 0, []
    if links:
        score += 3
        reasons.append("List-Unsubscribe")
    if m.get("List-Id"):
        score += 1
        reasons.append("List-Id")
    if str(m.get("Precedence", "")).strip().lower() in ("bulk", "list", "junk"):
        score += 2
        reasons.append("Precedence")
    if BULK_LOCALPART.search(addr.split("@")[0]):
        score += 1
        reasons.append("adresa odosielateľa")
    esp = [h for h in ESP_HEADERS.split() if m.get(h)]
    if esp:
        score += 2
        reasons.append("hlavička rozosielacej platformy (" + esp[0].title() + ")")
    if _is_bounce_address(m.get("Return-Path")) or _is_bounce_address(m.get("Errors-To")):
        score += 1
        reasons.append("bounce adresa")
    for pts, why in extra or ():
        score += pts
        reasons.append(why)

    sent = None
    try:
        sent = parsedate_to_datetime(m.get("Date"))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    mid = str(m.get("Message-ID", "")).strip()
    key = mid or hashlib.sha1(f"{addr}|{m.get('Date')}|{subject}".encode()).hexdigest()

    return {
        "address": addr,
        "name": name.strip()[:200],
        "domain": addr.split("@", 1)[1],
        "subject": subject,
        "sent": sent,
        "key": key,
        "unsub_http": http,
        "unsub_mailto": mailto,
        "one_click": one_click,
        "score": score,
        "reasons": reasons,
    }


def store(conn, info, folder):
    """Uloží správu. Vráti True, ak bola nová. Neznámych odosielateľov
    s nízkym skóre ignoruje, známych zapisuje vždy (aj keď po odhlásení
    zmiznú hlavičky hromadnej pošty)."""
    sender = conn.execute("SELECT * FROM senders WHERE address=?", (info["address"],)).fetchone()
    if sender is None and info["score"] < CFG["MIN_SCORE"]:
        return False
    if conn.execute("SELECT 1 FROM messages WHERE msg_key=?", (info["key"],)).fetchone():
        return False

    seen = ts(now_utc())
    sent = ts(info["sent"]) if info["sent"] else seen

    if sender is None:
        cur = conn.execute(
            "INSERT INTO senders(address, name, domain, score, first_seen, last_seen, "
            "unsub_http, unsub_mailto, one_click) VALUES(?,?,?,?,?,?,?,?,?)",
            (info["address"], info["name"], info["domain"], info["score"], sent, sent,
             info["unsub_http"], info["unsub_mailto"], info["one_click"]))
        sender_id = cur.lastrowid
        log_event(conn, sender_id, "nájdený", "znaky: " + ", ".join(info["reasons"]))
    else:
        sender_id = sender["id"]
        conn.execute(
            "UPDATE senders SET "
            " first_seen = MIN(first_seen, ?), last_seen = MAX(last_seen, ?),"
            " name = COALESCE(NULLIF(name, ''), ?),"
            " score = MAX(score, ?),"
            " unsub_http = COALESCE(NULLIF(?, ''), unsub_http),"
            " unsub_mailto = COALESCE(NULLIF(?, ''), unsub_mailto),"
            " one_click = MAX(one_click, ?) "
            "WHERE id = ?",
            (sent, sent, info["name"], info["score"], info["unsub_http"],
             info["unsub_mailto"], info["one_click"], sender_id))
        if sender["status"] == "unsubscribed" and sender["unsubscribed_at"]:
            limit = datetime.strptime(sender["unsubscribed_at"], TS_FMT) + timedelta(days=CFG["GRACE_DAYS"])
            if sent > limit.strftime(TS_FMT):
                log_event(conn, sender_id, "píše po odhlásení", info["subject"])

    conn.execute("INSERT INTO messages(sender_id, msg_key, folder, subject, sent_at, seen_at) "
                 "VALUES(?,?,?,?,?,?)", (sender_id, info["key"], folder, info["subject"], sent, seen))
    return True


# --------------------------------------------------------------------------
# IMAP
# --------------------------------------------------------------------------


def imap_quote(folder):
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_connect():
    if not CFG["IMAP_USER"] or not CFG["IMAP_PASSWORD"]:
        raise RuntimeError("Chýba IMAP_USER alebo IMAP_PASSWORD.")
    imap = imaplib.IMAP4_SSL(CFG["IMAP_HOST"], CFG["IMAP_PORT"])
    imap.login(CFG["IMAP_USER"], CFG["IMAP_PASSWORD"])
    return imap


def _search_uids(imap, *criteria):
    try:
        typ, data = imap.uid("SEARCH", None, *criteria)
        if typ == "OK" and data and data[0]:
            return set(data[0].split())
    except Exception as e:
        log.debug("Vyhľadávanie %s zlyhalo: %s", criteria, e)
    return set()


def server_side_signals(imap, since_str):
    """Signály, ktoré zistí server bez sťahovania obsahu e-mailov:
    slová ako „unsubscribe“ v tele a (pri Gmaile) kategória Promo akcie."""
    extras = {}

    def add(uids, pts, why):
        for u in uids:
            extras.setdefault(u, []).append((pts, why))

    if CFG["BODY_KEYWORDS"]:
        found = set()
        for word in CFG["BODY_KEYWORDS"]:
            found |= _search_uids(imap, "SINCE", since_str, "BODY", imap_quote(word))
        add(found, 2, "odkaz na odhlásenie v texte")
    if "X-GM-EXT-1" in imap.capabilities:
        add(_search_uids(imap, "SINCE", since_str, "X-GM-RAW", imap_quote("category:promotions")),
            3, "Gmail: Promo akcie")
    return extras


_scan_lock = threading.Lock()


def scan():
    """Prejde nové e-maily v nastavených priečinkoch (iba číta hlavičky)."""
    if not _scan_lock.acquire(blocking=False):
        return "Skenovanie už beží."
    conn = db_connect()
    started = now_utc()
    try:
        set_meta(conn, "scan_running", ts(started))
        conn.commit()
        last_ok = get_meta(conn, "last_scan_ok")
        if last_ok:
            since = datetime.strptime(last_ok, TS_FMT) - timedelta(days=2)
        else:
            since = started.replace(tzinfo=None) - timedelta(days=CFG["INITIAL_DAYS"])
        since_str = f"{since.day:02d}-{IMAP_MONTHS[since.month - 1]}-{since.year}"

        imap = imap_connect()
        added = checked = moved = 0
        try:
            for folder in CFG["IMAP_FOLDERS"]:
                to_move = {}  # uid -> (sender_id, dôvod)
                typ, _ = imap.select(imap_quote(folder), readonly=True)
                if typ != "OK":
                    log.warning("Priečinok %s sa nedá otvoriť", folder)
                    continue
                typ, data = imap.uid("SEARCH", None, "SINCE", since_str)
                uids = data[0].split() if typ == "OK" and data and data[0] else []
                extras = server_side_signals(imap, since_str) if uids else {}
                for i in range(0, len(uids), 200):
                    chunk = b",".join(uids[i:i + 200])
                    typ, resp = imap.uid("FETCH", chunk, f"(BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
                    if typ != "OK":
                        continue
                    for part in resp:
                        if isinstance(part, tuple) and len(part) > 1:
                            checked += 1
                            mu = re.search(rb"UID (\d+)", part[0])
                            info = analyze(part[1], extras.get(mu.group(1)) if mu else None)
                            if info and store(conn, info, folder):
                                added += 1
                            if info and mu:
                                why = auto_junk_reason(conn, info, folder)
                                if why:
                                    to_move[mu.group(1)] = why
                    conn.commit()
                if to_move:
                    try:
                        moved += move_uids(imap, folder, list(to_move))
                        per_sender = {}
                        for sid, why in to_move.values():
                            per_sender.setdefault((sid, why), 0)
                            per_sender[(sid, why)] += 1
                        for (sid, why), n in per_sender.items():
                            log_event(conn, sid, "automaticky presunuté do spamu", f"{n} e-mailov ({why})")
                    except Exception as e:
                        log.warning("Automatický presun do spamu zlyhal: %s", e)
                    conn.commit()
        finally:
            try:
                imap.logout()
            except Exception:
                pass

        msg = f"Skontrolovaných {checked} e-mailov, nových záznamov {added}."
        if moved:
            msg += f" Presunutých do spamu {moved}."
        set_meta(conn, "last_scan_ok", ts(started))
        set_meta(conn, "last_scan_msg", msg)
        set_meta(conn, "last_scan_error", "")
        log.info(msg)
        return msg
    except Exception as e:
        log.exception("Skenovanie zlyhalo")
        set_meta(conn, "last_scan_error", f"{ts(now_utc())}: {e}")
        return f"Chyba: {e}"
    finally:
        set_meta(conn, "scan_running", "")
        conn.commit()
        conn.close()
        _scan_lock.release()


def auto_junk_reason(conn, info, folder):
    """Ak sa má e-mail automaticky presunúť do spamu, vráti (sender_id, dôvod)."""
    if not CFG["AUTO_JUNK"] or not CFG["JUNK_FOLDER"] or folder == CFG["JUNK_FOLDER"]:
        return None
    s = conn.execute("SELECT id, status, unsubscribed_at FROM senders WHERE address=?",
                     (info["address"],)).fetchone()
    if s is None:
        return None
    if "spam" in CFG["AUTO_JUNK"] and s["status"] == "spam":
        return s["id"], "označený ako spam"
    if ("again" in CFG["AUTO_JUNK"] and s["status"] == "unsubscribed" and s["unsubscribed_at"]
            and info["sent"]):
        limit = datetime.strptime(s["unsubscribed_at"], TS_FMT) + timedelta(days=CFG["GRACE_DAYS"])
        if ts(info["sent"]) > limit.strftime(TS_FMT):
            return s["id"], "píše po odhlásení"
    return None


def move_uids(imap, folder, uids):
    """Presunie dané UID z priečinka do JUNK_FOLDER. Vráti počet presunutých."""
    if "MOVE" not in imap.capabilities:
        raise RuntimeError("Server nepodporuje príkaz IMAP MOVE.")
    typ, _ = imap.select(imap_quote(folder))  # zápis vyžaduje otvorenie bez readonly
    if typ != "OK":
        return 0
    uids = [u if isinstance(u, bytes) else str(u).encode() for u in uids]
    typ, _ = imap.uid("MOVE", b",".join(uids), imap_quote(CFG["JUNK_FOLDER"]))
    return len(uids) if typ == "OK" else 0


def move_to_junk(address):
    """Presunie všetky e-maily od odosielateľa do JUNK_FOLDER (vratná akcia)."""
    if not CFG["JUNK_FOLDER"]:
        raise RuntimeError("JUNK_FOLDER nie je nastavený.")
    imap = imap_connect()
    moved = 0
    try:
        for folder in CFG["IMAP_FOLDERS"]:
            if folder == CFG["JUNK_FOLDER"]:
                continue
            typ, _ = imap.select(imap_quote(folder), readonly=True)
            if typ != "OK":
                continue
            typ, data = imap.uid("SEARCH", None, "FROM", imap_quote(address))
            uids = data[0].split() if typ == "OK" and data and data[0] else []
            if uids:
                moved += move_uids(imap, folder, uids)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return moved


def one_click_unsubscribe(url):
    """Odhlásenie jedným klikom podľa RFC 8058 (POST na odkaz z hlavičky)."""
    req = urllib.request.Request(
        url, data=b"List-Unsubscribe=One-Click", method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "inbox-unsub/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status


def scheduler_loop():
    time.sleep(5)
    while True:
        scan()
        time.sleep(CFG["SCAN_INTERVAL_MIN"] * 60)


def start_scheduler():
    t = threading.Thread(target=scheduler_loop, daemon=True, name="scanner")
    t.start()


# --------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------

app = Flask(__name__)


def _secret_key():
    init_db()
    conn = db_connect()
    key = get_meta(conn, "secret_key")
    if not key:
        key = secrets.token_hex(32)
        set_meta(conn, "secret_key", key)
        conn.commit()
    conn.close()
    return key


app.secret_key = _secret_key()


def db():
    if "db" not in g:
        g.db = db_connect()
    return g.db


@app.teardown_appcontext
def _close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.before_request
def _auth_and_csrf():
    if CFG["WEB_PASSWORD"]:
        a = request.authorization
        if not a or not (secrets.compare_digest(a.username or "", CFG["WEB_USER"])
                         and secrets.compare_digest(a.password or "", CFG["WEB_PASSWORD"])):
            return Response("Prihlásenie je povinné.", 401,
                            {"WWW-Authenticate": 'Basic realm="inbox-unsub"'})
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("csrf", ""), session["csrf"]):
            abort(400, "Neplatný formulár, obnov stránku.")


@app.context_processor
def _ctx():
    conn = db()
    return {
        "csrf": session.get("csrf", ""),
        "STATUSES": STATUSES,
        "last_scan_ok": get_meta(conn, "last_scan_ok"),
        "last_scan_msg": get_meta(conn, "last_scan_msg"),
        "last_scan_error": get_meta(conn, "last_scan_error"),
        "scan_running": bool(get_meta(conn, "scan_running")),
        "junk_enabled": bool(CFG["JUNK_FOLDER"]),
        "auto_junk": CFG["AUTO_JUNK"] if CFG["JUNK_FOLDER"] else set(),
        "grace_days": CFG["GRACE_DAYS"],
    }


@app.template_filter("local")
def _local(value):
    """UTC reťazec z DB → čitateľný miestny čas."""
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, TS_FMT).replace(tzinfo=timezone.utc).astimezone(DISPLAY_TZ)
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


SENDER_COLS = """
    s.*,
    (SELECT COUNT(*) FROM messages m WHERE m.sender_id = s.id) AS msg_count,
    (SELECT subject FROM messages m WHERE m.sender_id = s.id ORDER BY sent_at DESC LIMIT 1) AS last_subject,
    (SELECT COUNT(*) FROM messages m WHERE m.sender_id = s.id AND s.status = 'unsubscribed'
        AND s.unsubscribed_at IS NOT NULL
        AND m.sent_at > datetime(s.unsubscribed_at, :grace)) AS again_count
"""

VIEWS = {
    "decide": ("Na rozhodnutie", "s.status = 'new'"),
    "again": ("Znova píšu", "again_count > 0"),
    "unsubscribed": ("Odhlásené", "s.status = 'unsubscribed'"),
    "spam": ("Spam", "s.status = 'spam'"),
    "keep": ("Ponechané", "s.status = 'keep'"),
    "all": ("Všetko", "1 = 1"),
}


def grace_param():
    return {"grace": f"+{CFG['GRACE_DAYS']} days"}


@app.route("/")
def index():
    view = request.args.get("view", "decide")
    if view not in VIEWS:
        view = "decide"
    q = request.args.get("q", "").strip().lower()
    params = grace_param()
    where = VIEWS[view][1]
    if q:
        where += " AND (s.address LIKE :q OR s.name LIKE :q OR s.domain LIKE :q)"
        params["q"] = f"%{q}%"
    rows = db().execute(
        f"SELECT * FROM (SELECT {SENDER_COLS} FROM senders s) s WHERE {where} "
        "ORDER BY again_count DESC, s.last_seen DESC", params).fetchall()

    counts = {}
    for key, (_, cond) in VIEWS.items():
        counts[key] = db().execute(
            f"SELECT COUNT(*) FROM (SELECT {SENDER_COLS} FROM senders s) s WHERE {cond}",
            grace_param()).fetchone()[0]
    return render_template("index.html", rows=rows, view=view, views=VIEWS, counts=counts, q=q)


def get_sender(sender_id):
    row = db().execute(f"SELECT {SENDER_COLS} FROM senders s WHERE s.id = :id",
                       {**grace_param(), "id": sender_id}).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/sender/<int:sender_id>")
def sender_detail(sender_id):
    s = get_sender(sender_id)
    messages = db().execute(
        "SELECT * FROM messages WHERE sender_id=? ORDER BY sent_at DESC LIMIT 200", (sender_id,)).fetchall()
    events = db().execute(
        "SELECT * FROM events WHERE sender_id=? ORDER BY at DESC, id DESC", (sender_id,)).fetchall()
    return render_template("sender.html", s=s, messages=messages, events=events)


def back(default_view=None):
    nxt = request.form.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("index", view=default_view or "decide"))


@app.post("/sender/<int:sender_id>/status")
def set_status(sender_id):
    s = get_sender(sender_id)
    status = request.form.get("status")
    if status not in ("new", "keep", "spam"):
        abort(400)
    conn = db()
    conn.execute("UPDATE senders SET status=?, decided_at=? WHERE id=?", (status, ts(now_utc()), sender_id))
    log_event(conn, sender_id, "stav", STATUSES[status])
    conn.commit()
    flash(f"{s['address']}: {STATUSES[status]}.", "ok")
    return back()


def mark_unsubscribed(conn, sender_id, method):
    conn.execute("UPDATE senders SET status='unsubscribed', unsubscribed_at=?, unsub_method=?, "
                 "decided_at=? WHERE id=?", (ts(now_utc()), method, ts(now_utc()), sender_id))
    log_event(conn, sender_id, "odhlásené", method)


@app.post("/sender/<int:sender_id>/unsubscribe")
def unsubscribe(sender_id):
    s = get_sender(sender_id)
    conn = db()
    if s["one_click"] and s["unsub_http"]:
        try:
            code = one_click_unsubscribe(s["unsub_http"])
            if 200 <= code < 300:
                mark_unsubscribed(conn, sender_id, f"jedným klikom (HTTP {code})")
                conn.commit()
                flash(f"{s['address']}: odhlásené jedným klikom.", "ok")
                return back()
            log_event(conn, sender_id, "odhlásenie zlyhalo", f"HTTP {code}")
        except Exception as e:
            log_event(conn, sender_id, "odhlásenie zlyhalo", str(e)[:300])
        conn.commit()
        flash("Automatické odhlásenie nevyšlo, skús to ručne cez odkaz nižšie.", "err")
    elif not (s["unsub_http"] or s["unsub_mailto"]):
        flash("Tento odosielateľ neposiela odkaz na odhlásenie. Môžeš mu napísať GDPR námietku "
              "a potom ho tu označiť ako odhláseného.", "err")
    return redirect(url_for("sender_detail", sender_id=sender_id))


@app.post("/sender/<int:sender_id>/mark-unsubscribed")
def manual_unsubscribed(sender_id):
    s = get_sender(sender_id)
    method = request.form.get("method", "ručne").strip()[:100] or "ručne"
    conn = db()
    mark_unsubscribed(conn, sender_id, method)
    conn.commit()
    flash(f"{s['address']}: zapísané ako odhlásené ({method}).", "ok")
    return back("unsubscribed")


@app.post("/sender/<int:sender_id>/junk")
def junk(sender_id):
    s = get_sender(sender_id)
    conn = db()
    try:
        n = move_to_junk(s["address"])
        log_event(conn, sender_id, "presunuté do spamu", f"{n} e-mailov")
        flash(f"Presunutých {n} e-mailov do priečinka {CFG['JUNK_FOLDER']}.", "ok")
    except Exception as e:
        flash(f"Presun zlyhal: {e}", "err")
    conn.commit()
    return back()


@app.post("/sender/<int:sender_id>/note")
def note(sender_id):
    get_sender(sender_id)
    conn = db()
    conn.execute("UPDATE senders SET note=? WHERE id=?", (request.form.get("note", "")[:2000], sender_id))
    conn.commit()
    flash("Poznámka uložená.", "ok")
    return redirect(url_for("sender_detail", sender_id=sender_id))


@app.post("/scan")
def scan_now():
    threading.Thread(target=scan, daemon=True).start()
    flash("Skenovanie spustené, o chvíľu obnov stránku.", "ok")
    return back()


@app.route("/export.csv")
def export_csv():
    rows = db().execute(f"SELECT {SENDER_COLS} FROM senders s ORDER BY s.address", grace_param()).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["adresa", "meno", "stav", "prvý e-mail (UTC)", "posledný e-mail (UTC)", "počet e-mailov",
                "odhlásené (UTC)", "spôsob odhlásenia", "e-mailov po odhlásení", "poznámka"])
    for r in rows:
        w.writerow([r["address"], r["name"], STATUSES.get(r["status"], r["status"]), r["first_seen"],
                    r["last_seen"], r["msg_count"], r["unsubscribed_at"] or "", r["unsub_method"] or "",
                    r["again_count"], r["note"] or ""])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=odhlasenia.csv"})


@app.route("/healthz")
def healthz():
    return {"ok": True}


def explain(query):
    """Diagnostika: kde sú e-maily od odosielateľa a prečo (ne)boli zapísané."""
    init_db()
    conn = db_connect()
    print("== Stav skenera ==")
    print("  priečinky:", ", ".join(CFG["IMAP_FOLDERS"]), "| MIN_SCORE:", CFG["MIN_SCORE"])
    print("  posledné úspešné skenovanie (UTC):", get_meta(conn, "last_scan_ok") or "žiadne")
    print("  posledná chyba:", get_meta(conn, "last_scan_error") or "žiadna")
    last_ok = get_meta(conn, "last_scan_ok")
    if last_ok:
        print("  ďalšie skenovanie pozrie e-maily od:",
              (datetime.strptime(last_ok, TS_FMT) - timedelta(days=2)).strftime("%Y-%m-%d"))

    print(f"\n== V databáze (hľadám '{query}') ==")
    rows = conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.sender_id=s.id) AS n FROM senders s "
        "WHERE s.address LIKE ? OR s.name LIKE ?", (f"%{query}%", f"%{query}%")).fetchall()
    for r in rows:
        print(f"  {r['address']}: stav '{STATUSES.get(r['status'], r['status'])}', e-mailov {r['n']}, "
              f"posledný {r['last_seen']} (na stránke v záložke „{STATUSES.get(r['status'])}“ alebo „Všetko“)")
    if not rows:
        print("  nič")

    print("\n== V schránke ==")
    imap = imap_connect()
    try:
        folders = list(CFG["IMAP_FOLDERS"])
        typ, data = imap.list()
        for line in data or []:
            line = line.decode(errors="replace")
            if "\\All" in line:  # Gmail „Všetky správy“ – nájde aj archivované
                name = line.rsplit(' "/" ', 1)[-1].strip().strip('"')
                if name not in folders:
                    folders.append(name)
        for folder in folders:
            typ, _ = imap.select(imap_quote(folder), readonly=True)
            if typ != "OK":
                print(f"  [{folder}] nedá sa otvoriť")
                continue
            typ, data = imap.uid("SEARCH", None, "FROM", imap_quote(query))
            uids = data[0].split() if typ == "OK" and data and data[0] else []
            scanned = folder in CFG["IMAP_FOLDERS"]
            print(f"  [{folder}] {len(uids)} e-mailov" + ("" if scanned else "  (tento priečinok sa NESKENUJE)"))
            for uid in uids[-5:]:
                typ, resp = imap.uid("FETCH", uid, f"(BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
                part = next((p for p in resp if isinstance(p, tuple)), None)
                if not part:
                    continue
                info = analyze(part[1])
                if not info:
                    continue
                stored = conn.execute("SELECT 1 FROM messages WHERE msg_key=?", (info["key"],)).fetchone()
                verdict = ("ZAPÍSANÝ" if stored else
                           "nezapísaný – nízke skóre" if info["score"] < CFG["MIN_SCORE"] else
                           "nezapísaný – priečinok sa neskenuje (pozri IMAP_FOLDERS)" if not scanned else
                           "nezapísaný – je starší ako skenované obdobie, alebo sken ešte neprebehol "
                           "(pomôže --rescan)")
                print(f"    {info['sent'] and ts(info['sent'])}  {info['subject'][:60]}")
                print(f"      skóre {info['score']} (bez signálov zo servera): {', '.join(info['reasons']) or '-'}")
                print(f"      → {verdict}")
    finally:
        imap.logout()


def list_folders():
    imap = imap_connect()
    try:
        typ, data = imap.list()
        for line in data or []:
            print(line.decode(errors="replace"))
    finally:
        imap.logout()


if __name__ == "__main__":
    import sys
    if "--explain" in sys.argv:
        idx = sys.argv.index("--explain")
        if idx + 1 >= len(sys.argv):
            sys.exit("Použitie: python app.py --explain <adresa alebo doména>")
        explain(sys.argv[idx + 1])
        sys.exit(0)
    if "--list-folders" in sys.argv:
        list_folders()
        sys.exit(0)
    if "--scan" in sys.argv or "--rescan" in sys.argv:
        init_db()
        if "--rescan" in sys.argv:
            # znova prejde posledných INITIAL_DAYS dní; už uložené e-maily sa nezdvoja
            c = db_connect()
            c.execute("DELETE FROM meta WHERE key='last_scan_ok'")
            c.commit()
            c.close()
        print(scan())
        sys.exit(0)
    init_db()
    if CFG["IMAP_USER"] and CFG["IMAP_PASSWORD"]:
        start_scheduler()
    else:
        log.warning("IMAP_USER/IMAP_PASSWORD nie sú nastavené – skenovanie je vypnuté.")
    if not CFG["WEB_PASSWORD"]:
        log.warning("WEB_PASSWORD nie je nastavené – stránka je bez hesla!")
    from waitress import serve
    log.info("Webové rozhranie beží na porte %s", CFG["PORT"])
    serve(app, host="0.0.0.0", port=CFG["PORT"], threads=4)
