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
    "SCAN_INTERVAL_MIN": max(5, _env_int("SCAN_INTERVAL_MIN", 60)),
    "INITIAL_DAYS": _env_int("INITIAL_DAYS", 30),
    "GRACE_DAYS": _env_int("GRACE_DAYS", 3),
    "MIN_SCORE": _env_int("MIN_SCORE", 3),
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
HEADER_FIELDS = ("FROM SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE "
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


def analyze(raw_headers: bytes):
    """Z hlavičiek jedného e-mailu vráti slovník s údajmi a skóre „hromadnosti“."""
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
        added = checked = 0
        try:
            for folder in CFG["IMAP_FOLDERS"]:
                typ, _ = imap.select(imap_quote(folder), readonly=True)
                if typ != "OK":
                    log.warning("Priečinok %s sa nedá otvoriť", folder)
                    continue
                typ, data = imap.uid("SEARCH", None, "SINCE", since_str)
                uids = data[0].split() if typ == "OK" and data and data[0] else []
                for i in range(0, len(uids), 200):
                    chunk = b",".join(uids[i:i + 200])
                    typ, resp = imap.uid("FETCH", chunk, f"(BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
                    if typ != "OK":
                        continue
                    for part in resp:
                        if isinstance(part, tuple) and len(part) > 1:
                            checked += 1
                            info = analyze(part[1])
                            if info and store(conn, info, folder):
                                added += 1
                    conn.commit()
        finally:
            try:
                imap.logout()
            except Exception:
                pass

        msg = f"Skontrolovaných {checked} e-mailov, nových záznamov {added}."
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


def move_to_junk(address):
    """Presunie všetky e-maily od odosielateľa do JUNK_FOLDER (vratná akcia)."""
    if not CFG["JUNK_FOLDER"]:
        raise RuntimeError("JUNK_FOLDER nie je nastavený.")
    imap = imap_connect()
    moved = 0
    try:
        caps = imap.capabilities
        for folder in CFG["IMAP_FOLDERS"]:
            if folder == CFG["JUNK_FOLDER"]:
                continue
            typ, _ = imap.select(imap_quote(folder))
            if typ != "OK":
                continue
            typ, data = imap.uid("SEARCH", None, "FROM", imap_quote(address))
            uids = data[0].split() if typ == "OK" and data and data[0] else []
            if not uids:
                continue
            if "MOVE" not in caps:
                raise RuntimeError("Server nepodporuje príkaz IMAP MOVE.")
            typ, _ = imap.uid("MOVE", b",".join(uids), imap_quote(CFG["JUNK_FOLDER"]))
            if typ == "OK":
                moved += len(uids)
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
    if "--list-folders" in sys.argv:
        list_folders()
        sys.exit(0)
    if "--scan" in sys.argv:
        init_db()
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
