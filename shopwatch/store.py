"""Small current-state store for the domain check: SQLite, a few MB at most.

Holds structured current facts and evidence METADATA (source, claim, time, content
hash), never raw pages or registry archives. The target machine has 2-3 GB free, and a
copy of the Commercial Register is neither needed nor lawful to keep for this purpose.

Every entity write passes privacy.filter_record() first; the API filters again on the
way out. Data-subject requests, restrictions and an audit trail live here too, so a
correction or objection is applied where the data is, not in a side spreadsheet.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from shopwatch import privacy

DB_PATH = Path(__file__).resolve().parent.parent / "domaincheck.db"
RETENTION_DAYS = 180       # storage limitation: forget domains nobody asked about since

SCHEMA = """
CREATE TABLE IF NOT EXISTS domains(
  id INTEGER PRIMARY KEY, domain TEXT UNIQUE, canonical_url TEXT,
  first_seen TEXT, last_checked TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY, eik TEXT UNIQUE, legal_name TEXT, legal_form TEXT,
  status TEXT, registered_address TEXT, capital TEXT, registration_date TEXT,
  vat_number TEXT, source TEXT, registry_last_updated TEXT, last_verified TEXT);
CREATE TABLE IF NOT EXISTS domain_entities(
  domain_id INTEGER, entity_id INTEGER, confidence REAL, resolution_status TEXT,
  first_seen TEXT, last_verified TEXT, PRIMARY KEY(domain_id, entity_id));
CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS entity_people(
  entity_id INTEGER, person_id INTEGER, role TEXT, source TEXT,
  source_timestamp TEXT, last_verified TEXT, PRIMARY KEY(entity_id, person_id, role));
CREATE TABLE IF NOT EXISTS evidence(
  id INTEGER PRIMARY KEY, domain_id INTEGER, entity_id INTEGER, source_type TEXT,
  source_url TEXT, claim TEXT, confidence REAL, retrieved_at TEXT, content_hash TEXT);
CREATE TABLE IF NOT EXISTS update_subscriptions(
  entity_id INTEGER, provider TEXT, subscription_id TEXT, status TEXT,
  last_update TEXT, PRIMARY KEY(entity_id, provider));
CREATE TABLE IF NOT EXISTS cache(
  kind TEXT, key TEXT, stored_at REAL, value TEXT, PRIMARY KEY(kind, key));
CREATE TABLE IF NOT EXISTS dsr_requests(
  id INTEGER PRIMARY KEY, kind TEXT, subject TEXT, eik TEXT, field TEXT,
  note TEXT, status TEXT, opened_at TEXT, closed_at TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS restrictions(
  eik TEXT, field TEXT, reason TEXT, since TEXT, PRIMARY KEY(eik, field));
CREATE TABLE IF NOT EXISTS registry_records(
  eik TEXT PRIMARY KEY, name TEXT, record TEXT, received_at TEXT);
CREATE TABLE IF NOT EXISTS ai_findings(
  domain TEXT, field TEXT, record TEXT, stored_at REAL, PRIMARY KEY(domain, field));
CREATE TABLE IF NOT EXISTS ai_usage(day TEXT PRIMARY KEY, count INTEGER);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, at TEXT, action TEXT, detail TEXT);
"""

_lock = threading.Lock()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    """Thread-safe by serialising writes; each call opens its own connection."""

    def __init__(self, path: Path | str = DB_PATH):
        self.path = str(path)
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _write(self, sql: str, args=()) -> int:
        with _lock, self._db() as db:
            return db.execute(sql, args).lastrowid

    # ---- cache ----------------------------------------------------------------
    def cache_get(self, kind: str, key: str, ttl: float):
        with self._db() as db:
            row = db.execute("SELECT stored_at, value FROM cache WHERE kind=? AND key=?",
                             (kind, key)).fetchone()
        if row and time.time() - row["stored_at"] < ttl:
            return json.loads(row["value"])
        return None

    def cache_put(self, kind: str, key: str, value) -> None:
        self._write("INSERT OR REPLACE INTO cache VALUES(?,?,?,?)",
                    (kind, key, time.time(), json.dumps(value, ensure_ascii=False)))

    # ---- current state ----------------------------------------------------------
    def upsert_domain(self, domain: str, canonical_url: str, status: str) -> int:
        t = now_iso()
        with _lock, self._db() as db:
            db.execute("INSERT INTO domains(domain, canonical_url, first_seen, last_checked,"
                       " status) VALUES(?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET"
                       " canonical_url=excluded.canonical_url,"
                       " last_checked=excluded.last_checked, status=excluded.status",
                       (domain, canonical_url, t, t, status))
            return db.execute("SELECT id FROM domains WHERE domain=?",
                              (domain,)).fetchone()["id"]

    def entity(self, eik: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM entities WHERE eik=?", (eik,)).fetchone()
        return dict(row) if row else None

    def upsert_entity(self, rec: dict, source: str) -> tuple[int, list[str]]:
        """Write the current facts for one EIK. Returns (id, fields that changed)."""
        clean = privacy.filter_record(rec, source)
        cols = {"legal_name": clean.get("name"), "legal_form": clean.get("legal_form"),
                "status": clean.get("status"),
                "registered_address": clean.get("registered_address"),
                "capital": clean.get("capital"),
                "registration_date": clean.get("registration_date"),
                "vat_number": clean.get("vat_number"), "source": source,
                "registry_last_updated": clean.get("source_timestamp"),
                "last_verified": now_iso()}
        old = self.entity(clean["eik"]) or {}
        changed = [k for k in ("legal_name", "status", "registered_address")
                   if old and cols[k] and old.get(k) and old[k] != cols[k]]
        with _lock, self._db() as db:
            db.execute(
                "INSERT INTO entities(eik," + ",".join(cols) + ") VALUES(?"
                + ",?" * len(cols) + ") ON CONFLICT(eik) DO UPDATE SET "
                + ",".join(f"{k}=COALESCE(excluded.{k},{k})" for k in cols),
                (clean["eik"], *cols.values()))
            eid = db.execute("SELECT id FROM entities WHERE eik=?",
                             (clean["eik"],)).fetchone()["id"]
            if changed:
                db.execute("INSERT INTO audit(at, action, detail) VALUES(?,?,?)",
                           (now_iso(), "entity_changed",
                            json.dumps({"eik": clean["eik"], "fields": changed,
                                        "source": source}, ensure_ascii=False)))
        return eid, changed

    def link(self, domain_id: int, entity_id: int, confidence: float, status: str) -> None:
        t = now_iso()
        self._write("INSERT INTO domain_entities VALUES(?,?,?,?,?,?) ON CONFLICT DO UPDATE"
                    " SET confidence=excluded.confidence,"
                    " resolution_status=excluded.resolution_status,"
                    " last_verified=excluded.last_verified",
                    (domain_id, entity_id, confidence, status, t, t))

    def add_evidence(self, domain_id, entity_id, source_type, source_url, claim,
                     confidence, retrieved_at, content_hash=None) -> None:
        self._write("INSERT INTO evidence(domain_id, entity_id, source_type, source_url,"
                    " claim, confidence, retrieved_at, content_hash)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (domain_id, entity_id, source_type, source_url, claim, confidence,
                     retrieved_at, content_hash))

    def set_people(self, entity_id: int, eik: str, persons: list[dict], source: str,
                   source_ts: str | None) -> list[dict]:
        """Store people for an entity if, and only if, the privacy layer allows it."""
        if not privacy.personal_data_enabled():
            return []
        blocked = self.restricted(eik)
        kept = []
        t = now_iso()
        with _lock, self._db() as db:
            db.execute("DELETE FROM entity_people WHERE entity_id=?", (entity_id,))
            for p in persons:
                p = privacy.filter_record(p, source, subject="person", restricted=blocked)
                if not p.get("name"):
                    continue
                db.execute("INSERT OR IGNORE INTO people(name) VALUES(?)", (p["name"],))
                pid = db.execute("SELECT id FROM people WHERE name=?",
                                 (p["name"],)).fetchone()["id"]
                db.execute("INSERT OR REPLACE INTO entity_people VALUES(?,?,?,?,?,?)",
                           (entity_id, pid, p.get("role"), source, source_ts, t))
                kept.append({**p, "source": source, "source_timestamp": source_ts,
                             "last_verified": t})
        return kept

    def subscribe(self, entity_id: int, provider: str, status: str,
                  subscription_id: str | None = None) -> None:
        self._write("INSERT INTO update_subscriptions VALUES(?,?,?,?,?) ON CONFLICT DO"
                    " UPDATE SET status=excluded.status, last_update=excluded.last_update",
                    (entity_id, provider, subscription_id, status, now_iso()))

    def stale_entities(self, older_than_s: float, limit: int = 50) -> list[dict]:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(time.time() - older_than_s))
        with self._db() as db:
            rows = db.execute("SELECT * FROM entities WHERE last_verified < ?"
                              " ORDER BY last_verified LIMIT ?", (cutoff, limit)).fetchall()
        return [dict(r) for r in rows]

    def purge(self, retention_days: int = RETENTION_DAYS) -> int:
        """Storage limitation: drop domains, evidence and orphans past retention."""
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(time.time() - retention_days * 86400))
        with _lock, self._db() as db:
            old = [r["id"] for r in db.execute(
                "SELECT id FROM domains WHERE last_checked < ?", (cutoff,))]
            for did in old:
                db.execute("DELETE FROM evidence WHERE domain_id=?", (did,))
                db.execute("DELETE FROM domain_entities WHERE domain_id=?", (did,))
                db.execute("DELETE FROM domains WHERE id=?", (did,))
            orphans = [r["id"] for r in db.execute(
                "SELECT id FROM entities WHERE id NOT IN"
                " (SELECT entity_id FROM domain_entities)")]
            for eid in orphans:
                for t in ("entity_people", "update_subscriptions", "evidence"):
                    db.execute(f"DELETE FROM {t} WHERE entity_id=?", (eid,))
                db.execute("DELETE FROM entities WHERE id=?", (eid,))
            db.execute("DELETE FROM people WHERE id NOT IN"
                       " (SELECT person_id FROM entity_people)")
            # Full check results carry the shop's published contact details: keep them no
            # longer than their 10-minute cache life (give or take one worker cycle).
            db.execute("DELETE FROM cache WHERE (kind='domain' AND stored_at < ?)"
                       " OR stored_at < ?", (time.time() - 600, time.time() - 7 * 86400))
            if old or orphans:
                db.execute("INSERT INTO audit(at, action, detail) VALUES(?,?,?)",
                           (now_iso(), "retention_purge",
                            json.dumps({"domains": len(old), "entities": len(orphans)})))
        return len(old)

    # ---- Commercial Register current-state cache ---------------------------------
    def put_registry_record(self, rec: dict) -> None:
        """One trader's current registered circumstances, allowlisted, no history.

        Persons are kept only while the privacy layer allows personal data at all.
        """
        clean = privacy.filter_record(rec, "registry")
        clean["website"] = rec.get("website")
        if privacy.personal_data_enabled():
            clean["persons"] = [privacy.filter_record(p, "registry", subject="person")
                                for p in rec.get("persons") or []]
        self._write("INSERT OR REPLACE INTO registry_records VALUES(?,?,?,?)",
                    (clean["eik"], clean.get("name"), json.dumps(clean, ensure_ascii=False),
                     now_iso()))
        self.upsert_entity(clean, source="registry")

    def registry_record(self, eik: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT record, received_at FROM registry_records WHERE eik=?",
                             (eik,)).fetchone()
        return {**json.loads(row["record"]), "received_at": row["received_at"]} if row \
            else None

    def registry_by_name(self, name: str, limit: int = 5) -> list[dict]:
        core = " ".join(w for w in name.upper().replace('"', " ").split()
                        if w not in ("ЕООД", "ООД", "ЕАД", "АД", "ЕТ", "-"))
        with self._db() as db:
            rows = db.execute("SELECT record FROM registry_records WHERE upper(name) LIKE ?"
                              " LIMIT ?", (f"%{core}%", limit)).fetchall()
        return [json.loads(r["record"]) for r in rows]

    # ---- AI fallback: compact cache of located fields, and a daily request count ---
    def ai_cache_get(self, domain: str, field: str, days: int) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT record, stored_at FROM ai_findings WHERE domain=? AND field=?",
                             (domain, field)).fetchone()
        return json.loads(row["record"]) if row and time.time() - row["stored_at"] < days * 86400 \
            else None

    def ai_cache_put(self, domain: str, field: str, record: dict) -> None:
        self._write("INSERT OR REPLACE INTO ai_findings VALUES(?,?,?,?)",
                    (domain, field, json.dumps(record, ensure_ascii=False), time.time()))

    def ai_usage(self, day: str) -> int:
        with self._db() as db:
            row = db.execute("SELECT count FROM ai_usage WHERE day=?", (day,)).fetchone()
        return row["count"] if row else 0

    def ai_usage_add(self, day: str) -> None:
        self._write("INSERT INTO ai_usage VALUES(?,1) ON CONFLICT(day) DO UPDATE SET count=count+1",
                    (day,))

    # ---- data-subject workflow ------------------------------------------------
    def restricted(self, eik: str) -> frozenset[str]:
        with self._db() as db:
            return frozenset(r["field"] for r in db.execute(
                "SELECT field FROM restrictions WHERE eik=?", (eik,)))

    def audit(self, action: str, detail: dict) -> None:
        self._write("INSERT INTO audit(at, action, detail) VALUES(?,?,?)",
                    (now_iso(), action, json.dumps(detail, ensure_ascii=False)))

    def open_request(self, kind: str, subject: str, eik: str | None, field: str | None,
                     note: str) -> int:
        rid = self._write("INSERT INTO dsr_requests(kind, subject, eik, field, note, status,"
                          " opened_at) VALUES(?,?,?,?,?,?,?)",
                          (kind, subject, eik, field, note, "open", now_iso()))
        self.audit("dsr_opened", {"id": rid, "kind": kind, "eik": eik, "field": field})
        return rid

    def resolve_request(self, rid: int, action: str, note: str) -> dict:
        """Apply the decision on one request. action: restrict|erase|correct|reject.

        'erase' removes what this system holds about the subject. It does not, and
        cannot, remove a fact from the Commercial Register: a legally required record
        is restricted here instead, and the outcome says so.
        """
        with self._db() as db:
            req = db.execute("SELECT * FROM dsr_requests WHERE id=?", (rid,)).fetchone()
        if not req:
            raise KeyError(f"no request {rid}")
        req = dict(req)
        with _lock, self._db() as db:
            if action in ("restrict", "erase") and req["eik"] and req["field"]:
                db.execute("INSERT OR REPLACE INTO restrictions VALUES(?,?,?,?)",
                           (req["eik"], req["field"], f"dsr #{rid}: {note}", now_iso()))
            if action == "erase" and req["subject"]:
                row = db.execute("SELECT id FROM people WHERE name=?",
                                 (req["subject"],)).fetchone()
                if row:
                    db.execute("DELETE FROM entity_people WHERE person_id=?", (row["id"],))
                    db.execute("DELETE FROM people WHERE id=?", (row["id"],))
            db.execute("UPDATE dsr_requests SET status='closed', closed_at=?, outcome=?"
                       " WHERE id=?", (now_iso(), f"{action}: {note}", rid))
            db.execute("INSERT INTO audit(at, action, detail) VALUES(?,?,?)",
                       (now_iso(), "dsr_" + action,
                        json.dumps({"id": rid, "note": note}, ensure_ascii=False)))
        return {**req, "status": "closed", "outcome": f"{action}: {note}"}

    def access_report(self, subject: str) -> dict:
        """Everything held about one named person: the data-access request answer."""
        with self._db() as db:
            rows = db.execute(
                "SELECT e.eik, e.legal_name, ep.role, ep.source, ep.source_timestamp,"
                " ep.last_verified FROM people p JOIN entity_people ep ON ep.person_id=p.id"
                " JOIN entities e ON e.id=ep.entity_id WHERE p.name=?", (subject,)).fetchall()
            reqs = db.execute("SELECT * FROM dsr_requests WHERE subject=?",
                              (subject,)).fetchall()
        return {"subject": subject, "held": [dict(r) for r in rows],
                "requests": [dict(r) for r in reqs], "generated_at": now_iso()}
