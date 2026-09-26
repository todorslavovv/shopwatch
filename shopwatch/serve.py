"""Local-only URL check: paste a shop URL in a form, get the disclosure verdict.

Bound to 127.0.0.1 on purpose. This process fetches whatever URL it is handed, so
exposing it would hand strangers a fetch-proxy running on this machine's IP. Before it
ever faces a tunnel it needs, at minimum: a rate limit, a concurrency cap, and the
false-positive disclaimer made unmissable.

    python -m shopwatch.serve            # http://127.0.0.1:8778
"""
from __future__ import annotations

import argparse
import html
import http.server
import json
import re
import secrets
import socketserver
import threading
import time
import traceback
from collections import deque
from urllib.parse import parse_qs, urlsplit

from shopwatch import daylog, domaincheck
from shopwatch.investigate import investigate
from shopwatch.store import Store

# Abuse limits. Behind a tunnel every request arrives from 127.0.0.1, so the real
# client is read from the tunnel's header; anyone able to reach the port directly could
# forge it, which is why the port is never exposed itself.
PER_IP_CHECKS = 8          # per window
PER_IP_WINDOW = 300        # seconds
GLOBAL_DAILY = 400         # total checks per rolling day
MAX_CONCURRENT = 2         # live fetches at once
PER_TARGET_FETCHES = 5     # real fetches of one shop, across ALL clients
PER_TARGET_WINDOW = 600    # seconds
CACHE_TTL = 600            # serve a repeat of the same shop from memory

_hits: dict[str, deque] = {}
_targets: dict[str, deque] = {}
_cache: dict[str, tuple[float, str]] = {}
_day: deque = deque()
_lock = threading.Lock()
_slots = threading.Semaphore(MAX_CONCURRENT)
API_PATH = "/api/domain-check"
MAX_API_BODY = 4096
STORE: Store | None = None       # set in main(); tests may set their own


def rate_limit(client: str) -> str | None:
    """Return a refusal message if this client has had enough, else None."""
    now = time.time()
    with _lock:
        while _day and now - _day[0] > 86400:
            _day.popleft()
        if len(_day) >= GLOBAL_DAILY:
            return ("Дневният лимит на проверките е достигнат. "
                    "The daily check limit has been reached.")
        q = _hits.setdefault(client, deque())
        while q and now - q[0] > PER_IP_WINDOW:
            q.popleft()
        if len(q) >= PER_IP_CHECKS:
            wait = int(PER_IP_WINDOW - (now - q[0]))
            return (f"Твърде много проверки. Опитайте пак след {wait} секунди. "
                    f"Too many checks; retry in {wait}s.")
        q.append(now)
        _day.append(now)
    return None


def target_key(url: str) -> str:
    """Normalise to the host, so /a and /b?x=1 are one target."""
    host = urlsplit(url if urlsplit(url).scheme else "https://" + url).hostname or url
    return host.lower().removeprefix("www.")


def target_limit(host: str) -> str | None:
    """Bound real fetches per shop across everyone.

    A per-client limit does not stop twenty clients pointing at one victim. This is the
    limit that keeps the service from being turned into an amplifier at somebody.
    """
    now = time.time()
    with _lock:
        q = _targets.setdefault(host, deque())
        while q and now - q[0] > PER_TARGET_WINDOW:
            q.popleft()
        if len(q) >= PER_TARGET_FETCHES:
            wait = int(PER_TARGET_WINDOW - (now - q[0]))
            return (f"Този сайт беше проверен наскоро твърде често. Опитайте след "
                    f"{wait} секунди. · This site was checked too often recently; "
                    f"retry in {wait}s.")
        q.append(now)
    return None


def cache_get(host: str) -> str | None:
    with _lock:
        hit = _cache.get(host)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        if hit:
            del _cache[host]
    return None


def cache_put(host: str, rendered: str) -> None:
    with _lock:
        _cache[host] = (time.time(), rendered)


VERDICT_BG = {"ok": "в съответствие", "suspect": "съмнителен",
              "noncompliant": "без изискваните данни", "unreachable": "недостъпен",
              "blocked": "достъпът е отказан от сайта",
              "undetermined": "страницата се зарежда със скрипт",
              "out_of_scope": "извън българска юрисдикция"}
FIELD_BG = {"eik": "ЕИК", "company_name": "Фирма", "address": "Адрес",
            "email": "Ел. поща", "phone": "Телефон", "vat_number": "ИН по ЗДДС"}

PAGE = """<!doctype html><html lang="bg"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Проверка на търговец</title><style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;
 background:#faf9f7;color:#1c1a17}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:#6b645c;font-size:13px;margin-bottom:22px}}
form{{display:flex;gap:8px;margin-bottom:24px}}
input{{flex:1;padding:10px 12px;border:1px solid #cdc6bd;border-radius:6px;font-size:15px}}
button{{padding:10px 18px;border:0;border-radius:6px;background:#1c1a17;color:#fff;
 font-size:15px;cursor:pointer}}
table{{border-collapse:collapse;width:100%;margin:14px 0}}
td,th{{text-align:left;padding:7px 10px;border-bottom:1px solid #e6e1da;font-size:14px;
 vertical-align:top}}
th{{width:130px;color:#6b645c;font-weight:500}}
.v{{display:inline-block;padding:3px 10px;border-radius:99px;font-size:13px;font-weight:600}}
.ok{{background:#dcefdc;color:#1d5c1d}} .suspect{{background:#fdf0d0;color:#79571a}}
.noncompliant{{background:#f9dcd8;color:#8c241a}} .unreachable{{background:#e6e1da;color:#4a443d}}
.blocked{{background:#e2e8f3;color:#2c4a75}}
.undetermined{{background:#ece4f3;color:#553b75}}
.out_of_scope{{background:#e6e1da;color:#4a443d}}
.reg{{margin:10px 0;padding:10px 14px;font-size:13px;border-left:3px solid #cdc6bd;
 background:#f3f1ee}}
.reg.ok-reg{{border-left-color:#1d5c1d;background:#dcefdc}}
.reg.warn-reg{{border-left-color:#c99a2e;background:#fdf0d0}}
.ev{{color:#6b645c;font-size:12px;font-family:ui-monospace,monospace;word-break:break-word}}
.warn{{background:#fdf0d0;border-left:3px solid #c99a2e;padding:10px 14px;font-size:13px;
 margin:18px 0}}
.err{{color:#8c241a;font-family:ui-monospace,monospace;font-size:12px}}
.today{{margin-top:30px}}
.today h2{{font-size:13px;text-transform:uppercase;letter-spacing:.1em;color:#6b645c;
 margin:0 0 6px}}
.today td{{font-size:13px}} .today .t{{color:#6b645c;font-family:ui-monospace,monospace;
 font-size:12px}}
footer{{margin-top:32px;color:#6b645c;font-size:12px;border-top:1px solid #e6e1da;padding-top:14px}}
</style></head><body>
<h1>Проверка на търговец</h1>
<div class="sub">Проверява какви данни за търговеца публикува онлайн магазинът
 (ЕИК, фирма, адрес, контакт).</div>
<div class="warn"><strong>Инструментът е в ранен етап и дава фалшиви сигнали.</strong>
 Чете началната страница и до 3 подстраници. Големи и напълно законни магазини може
 да се отчетат като „без изискваните данни“, ако публикуват ЕИК на място, което
 проверката не достига. Резултатът не е заключение за магазина и не е правно
 становище. · <span style="color:#6b645c">Early-stage tool; false positives are
 expected. Not a finding about any trader.</span></div>
<form method="get" action=""><input name="url" placeholder="например technopolis.bg"
 value="{url}" autofocus><button>Провери</button></form>
{result}
{today}
<footer>Валиден контролен разряд на ЕИК <strong>не</strong> означава, че фирмата
 съществува — това изисква справка в Търговския регистър. Оценката е евристика за
 триаж, не е правно заключение.</footer>
</body></html>"""


def render_today() -> str:
    """Today's checks. Emptied automatically at the day boundary."""
    rows = daylog.today(limit=40)
    if not rows:
        return ""
    body = "".join(
        f'<tr><td class="mono">{html.escape(r["host"])}</td>'
        f'<td><span class="v {html.escape(r["verdict"] or "")}">'
        f'{VERDICT_BG.get(r["verdict"], r["verdict"] or "—")}</span></td>'
        f'<td>{"—" if r["score"] is None else r["score"]}</td>'
        f'<td class="t">{html.escape((r["checked"] or "")[11:16])}</td></tr>'
        for r in rows)
    return (f'<div class="today"><h2>Проверени днес · checked today ({len(rows)})</h2>'
            f'<table>{body}</table>'
            f'<div class="t" style="margin-top:6px">Списъкът се изтрива автоматично в '
            f'00:00. · Cleared automatically at midnight.</div></div>')


def render(entry: dict) -> str:
    """Render one investigation. Every value here came from a hostile page: escape all."""
    a = entry["assessment"]
    v = a["verdict"]
    score = "—" if a["score"] is None else a["score"]
    rows = []
    for field, label in FIELD_BG.items():
        val = entry["merchant"].get(field)
        src = entry["sources"].get(field)
        cell = html.escape(str(val)) if val else '<span class="ev">няма</span>'
        if val and src:
            cell += f'<div class="ev">{html.escape(src)}</div>'
        rows.append(f"<tr><th>{label}</th><td>{cell}</td></tr>")
    findings = "".join(
        f"<li>{html.escape(f['code'])} — {html.escape(f['message'])}</li>"
        for f in a["findings"])
    errors = "".join(f'<div class="err">{html.escape(e)}</div>'
                     for e in entry["errors"])
    pages = "".join(
        f'<div class="ev">{html.escape(str(p.get("status") or "—"))} '
        f'{html.escape(p["url"])}</div>' for p in entry["pages_visited"])
    reg = entry.get("registry") or {}
    st = reg.get("status")
    if st == "confirmed":
        nm = reg.get("name") or ""
        mism = ("" if entry.get("registry_name_matches") is not False else
                ' <strong>Името на сайта се различава от регистрираното.</strong>')
        reg_html = (f'<div class="reg ok-reg">Потвърден в регистъра по ДДС: '
                    f'<strong>{html.escape(nm)}</strong>'
                    f'{("<br>" + html.escape(reg.get("address") or "")) if reg.get("address") else ""}'
                    f'{mism}</div>')
    elif st == "not_found":
        reg_html = ('<div class="reg warn-reg">Този номер не е намерен в регистъра по ДДС. '
                    'Това е обичайно за малки търговци под прага за регистрация и '
                    '<strong>не е доказателство за измама</strong>.</div>')
    elif st == "unavailable":
        reg_html = ('<div class="reg">Регистърът по ДДС е недостъпен — съществуването '
                    'не е проверено.</div>')
    else:
        reg_html = ""
    return f"""
<h2 style="font-size:17px;margin-bottom:2px">{html.escape(entry['domain'])}</h2>
<span class="v {v}">{VERDICT_BG.get(v, v)} · {score}/100</span>
<table>{''.join(rows)}</table>
{reg_html}
<div class="warn">Извлечените данни може да са непълни: проверяват се началната
 страница и до 4 страници с данни за контакт. Липсата на ЕИК тук не доказва, че
 търговецът не го е публикувал другаде.</div>
<ul style="font-size:13px;color:#6b645c">{findings}</ul>
<details><summary style="font-size:13px;color:#6b645c;cursor:pointer">Заредени страници</summary>
{pages}{errors}</details>"""


def domain_check(client: str, body: bytes, runner=domaincheck.check) -> tuple[int, dict]:
    """POST /api/domain-check: validate, apply every limit, run, return (status, json).

    Order matters: input is validated before anything is counted, the per-client and
    daily limits come before the cache (a cached answer is still an answer handed out),
    and the per-domain limit comes only before a real fetch.

    With "async": true in the body the lookup becomes a job (see submit_job); without it
    the response is the full result, exactly as before.
    """
    try:
        data = json.loads(body.decode("utf-8"))
        raw = data.get("url") if isinstance(data, dict) else None
        n = domaincheck.normalise(raw)
    except (ValueError, UnicodeDecodeError) as e:
        msg = str(e) if isinstance(e, domaincheck.InputError) else "body must be JSON: {\"url\": \"...\"}"
        return 400, {"error": "invalid_input", "message": msg}
    if data.get("async") is True:
        return submit_job(client, raw, n, runner)
    refused = rate_limit(client)
    if refused:
        return 429, {"error": "rate_limited", "message": refused}
    domain = n["registrable_domain"]
    if STORE:
        hit = STORE.cache_get("domain", domaincheck.cache_key(domain),
                              domaincheck.TTL["domain"])
        if hit:
            return 200, {**hit, "cached": True}
    if not _slots.acquire(blocking=False):
        return 503, {"error": "busy", "message": "В момента се изпълняват други проверки. "
                                                 "Busy, try again shortly."}
    try:
        capped = target_limit(domain)
        if capped:
            return 429, {"error": "target_rate_limited", "message": capped}
        result = runner(raw, store=STORE)
        entry = (result.get("disclosure") or {})
        if entry:
            daylog.record(domain, {"assessment": entry["assessment"]})
        return 200, result
    except Exception:
        traceback.print_exc()
        return 500, {"error": "internal", "message": "Проверката е неуспешна. Check failed."}
    finally:
        _slots.release()


# ---- asynchronous jobs -------------------------------------------------------------
# A hard lookup (olx.bg from a blocked IP) takes about a minute. Rather than hold the
# browser on one request for that long, POST {"url": ..., "async": true} returns a job id
# at once and the resolver runs in a worker thread; GET /api/domain-check/<id> reports
# the stage it is really in. In-process on purpose: one small host, no queue service.
# ponytail: jobs are lost on restart; the page then says "job not found" and offers a
# new lookup. Upgrade path is the store, if restarts mid-lookup ever matter.
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
JOB_TTL = 600              # finished jobs are kept this long for the page to collect
JOB_MAX_RUNTIME = 150      # a job still running after this is reported as timed out
JOB_SLOT_WAIT = 60         # a queued job gives up if no worker slot frees in this time
MAX_ACTIVE_JOBS = MAX_CONCURRENT + 2    # running + queued
MAX_JOBS = 200             # all jobs incl. finished; the oldest finished ones go first
STAGE_ORDER = domaincheck.STAGES + ("result",)

_jobs: dict[str, dict] = {}
_active_by_domain: dict[str, str] = {}
_jobs_lock = threading.Lock()


class JobCancelled(Exception):
    pass


def _purge_jobs(now: float) -> None:
    """Called under _jobs_lock. Expire finished jobs; cap the registry."""
    for jid, j in list(_jobs.items()):
        if j["state"] in ("completed", "failed", "cancelled") and now - j["updated"] > JOB_TTL:
            del _jobs[jid]
    while len(_jobs) > MAX_JOBS:
        old = min((j for j in _jobs.values() if j["state"] in ("completed", "failed",
                                                               "cancelled")),
                  key=lambda j: j["updated"], default=None)
        if not old:
            break
        del _jobs[old["id"]]


def _new_job(domain: str, url: str, **fields) -> dict:
    now = time.time()
    job = {"id": secrets.token_urlsafe(18), "domain": domain, "url": url, "state": "queued",
           "stage": None, "skipped": [], "message": {"code": "queued", "params": {}},
           "provisional": None, "site": None, "result": None, "error": None,
           "cancel": False, "created": now, "updated": now, **fields}
    _jobs[job["id"]] = job
    return job


def _progress_for(job: dict):
    """The resolver's progress callback for one job. Raises to abort a cancelled job."""
    def progress(stage: str, code: str, **info) -> None:
        with _jobs_lock:
            if job["cancel"]:
                raise JobCancelled()
            if stage in STAGE_ORDER and (job["stage"] is None or
                                         STAGE_ORDER.index(stage) >= STAGE_ORDER.index(job["stage"])):
                job["stage"] = stage
            job["message"] = {"code": code, "params": {k: v for k, v in info.items()
                                                       if k in ("hosts",)}}
            if info.get("provisional"):
                job["provisional"] = info["provisional"]
            if info.get("site"):
                job["site"] = info["site"]
            if info.get("skipped"):
                job["skipped"] = list(info["skipped"])
            job["updated"] = time.time()
    return progress


def _run_job(job: dict, runner) -> None:
    got_slot = _slots.acquire(timeout=JOB_SLOT_WAIT)
    try:
        with _jobs_lock:
            if job["cancel"]:
                return
            if not got_slot:
                job.update(state="failed", error={"code": "busy", "message":
                           "Всички работни места са заети. All workers are busy."},
                           updated=time.time())
                return
            job.update(state="running", updated=time.time())
        result = runner(job["url"], store=STORE, progress=_progress_for(job))
        entry = result.get("disclosure") or {}
        if entry:
            daylog.record(job["domain"], {"assessment": entry["assessment"]})
        with _jobs_lock:
            if job["state"] == "running":              # not timed out or cancelled meanwhile
                job.update(state="completed", stage="result", result=result,
                           message={"code": "done", "params": {}}, updated=time.time())
    except JobCancelled:
        pass
    except Exception:
        traceback.print_exc()
        with _jobs_lock:
            if job["state"] == "running":
                job.update(state="failed", error={"code": "internal", "message":
                           "Проверката е неуспешна. The check failed."}, updated=time.time())
    finally:
        if got_slot:
            _slots.release()
        with _jobs_lock:
            if _active_by_domain.get(job["domain"]) == job["id"]:
                del _active_by_domain[job["domain"]]


def job_view(job: dict) -> dict:
    """Public shape of a job: state, stage-by-stage progress, partial and final result."""
    cur = STAGE_ORDER.index(job["stage"]) if job["stage"] in STAGE_ORDER else -1
    stages = []
    for i, st in enumerate(STAGE_ORDER):
        if job["state"] == "completed" or (i < cur and st not in job["skipped"]):
            status = "skipped" if st in job["skipped"] else "done"
        elif st in job["skipped"]:
            status = "skipped"
        elif i == cur:
            status = {"running": "active", "failed": "failed",
                      "cancelled": "cancelled"}.get(job["state"], "pending")
        else:
            status = "pending"
        stages.append({"id": st, "status": status})
    out = {"job_id": job["id"], "domain": job["domain"], "state": job["state"],
           "stage": job["stage"], "stages": stages, "message": job["message"],
           "site": job["site"], "provisional": job["provisional"],
           "elapsed": round(job["updated"] - job["created"], 1),
           "status_url": f"{API_PATH}/{job['id']}"}
    if job["state"] == "completed":
        out["result"] = job["result"]
    if job["error"]:
        out["error"] = job["error"]
    return out


def submit_job(client: str, raw: str, n: dict, runner=domaincheck.check) -> tuple[int, dict]:
    domain = n["registrable_domain"]
    with _jobs_lock:
        _purge_jobs(time.time())
        running = _active_by_domain.get(domain)
        if running and running in _jobs:
            # A double-click, or a second visitor asking about the same site: share it.
            return 200, {**job_view(_jobs[running]), "deduplicated": True}
    refused = rate_limit(client)
    if refused:
        return 429, {"error": "rate_limited", "message": refused}
    if STORE:
        hit = STORE.cache_get("domain", domaincheck.cache_key(domain), domaincheck.TTL["domain"])
        if hit:
            with _jobs_lock:
                job = _new_job(domain, raw, state="completed", stage="result",
                               result={**hit, "cached": True},
                               message={"code": "done", "params": {}})
            return 200, job_view(job)
    with _jobs_lock:
        active = sum(1 for j in _jobs.values() if j["state"] in ("queued", "running"))
        if active >= MAX_ACTIVE_JOBS:
            return 503, {"error": "busy", "message": "В момента се изпълняват други проверки. "
                                                     "Busy, try again shortly."}
    capped = target_limit(domain)
    if capped:
        return 429, {"error": "target_rate_limited", "message": capped}
    with _jobs_lock:
        job = _new_job(domain, raw)
        _active_by_domain[domain] = job["id"]
    threading.Thread(target=_run_job, args=(job, runner), daemon=True,
                     name=f"domaincheck-{domain}").start()
    return 202, job_view(job)


def job_status(job_id: str) -> tuple[int, dict]:
    if not JOB_ID.match(job_id or ""):
        return 404, {"error": "job_not_found", "message": "Unknown or expired lookup."}
    with _jobs_lock:
        now = time.time()
        _purge_jobs(now)
        job = _jobs.get(job_id)
        if not job:
            return 404, {"error": "job_not_found", "message": "Unknown or expired lookup."}
        if job["state"] in ("queued", "running") and now - job["created"] > JOB_MAX_RUNTIME:
            job.update(state="failed", error={"code": "timeout", "message":
                       "Проверката отне твърде дълго. The check took too long."},
                       cancel=True, updated=now)
            if _active_by_domain.get(job["domain"]) == job_id:
                del _active_by_domain[job["domain"]]
        return 200, job_view(job)


def cancel_job(job_id: str) -> tuple[int, dict]:
    with _jobs_lock:
        job = _jobs.get(job_id) if JOB_ID.match(job_id or "") else None
        if not job:
            return 404, {"error": "job_not_found", "message": "Unknown or expired lookup."}
        if job["state"] in ("queued", "running"):
            # Cooperative: the resolver stops at its next stage boundary. The job is
            # final for the page right now, and the domain is free for a new lookup.
            job.update(state="cancelled", cancel=True, updated=time.time(),
                       message={"code": "cancelled", "params": {}})
            if _active_by_domain.get(job["domain"]) == job_id:
                del _active_by_domain[job["domain"]]
        return 200, job_view(job)


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _client(self) -> str:
        return (self.headers.get("cf-connecting-ip")
                or self.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or self.address_string())

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith(API_PATH + "/") and path.endswith("/cancel"):
            return self._json(*cancel_job(path[len(API_PATH) + 1:-len("/cancel")]))
        if path != API_PATH:
            return self._json(404, {"error": "not_found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_API_BODY:
            return self._json(413 if length > MAX_API_BODY else 400,
                              {"error": "invalid_input",
                               "message": f"body must be 1..{MAX_API_BODY} bytes"})
        status, payload = domain_check(self._client(), self.rfile.read(length))
        self._json(status, payload)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith(API_PATH + "/"):
            return self._json(*job_status(path[len(API_PATH) + 1:]))
        if path.startswith("/api/"):
            return self._json(405, {"error": "method_not_allowed", "message": "use POST"})
        q = parse_qs(urlsplit(self.path).query)
        url = (q.get("url") or [""])[0].strip()
        result = ""
        if url:
            client = (self.headers.get("cf-connecting-ip")
                      or self.headers.get("x-forwarded-for", "").split(",")[0].strip()
                      or self.address_string())
            refused = rate_limit(client)
            if refused:
                result = f'<div class="warn">{html.escape(refused)}</div>'
            elif not _slots.acquire(blocking=False):
                result = ('<div class="warn">В момента се изпълняват други проверки. '
                          'Опитайте отново след минута. · Busy, try again shortly.</div>')
            else:
                try:
                    host = target_key(url)
                    cached = cache_get(host)
                    if cached:
                        result = ('<div class="warn">Запазен резултат от последните '
                                  '10 минути. · Cached result.</div>') + cached
                    else:
                        capped = target_limit(host)
                        if capped:
                            result = f'<div class="warn">{html.escape(capped)}</div>'
                        else:
                            entry = investigate(url, max_pages=5, timeout=30)
                            daylog.record(host, entry)
                            result = render(entry)
                            cache_put(host, result)
                except Exception:
                    traceback.print_exc()
                    result = ('<div class="err">Проверката е неуспешна. '
                              'Details in the server console.</div>')
                finally:
                    _slots.release()
        body = PAGE.format(url=html.escape(url), result=result,
                           today=render_today()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8778)
    ap.add_argument("--db", default=None, help="domain-check store (default: domaincheck.db)")
    args = ap.parse_args(argv)
    global STORE
    STORE = Store(args.db) if args.db else Store()
    domaincheck.start_worker(STORE)

    class Reuse(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Reuse(("127.0.0.1", args.port), Handler) as httpd:
        print(f"shopwatch check form: http://127.0.0.1:{args.port}/  (ctrl-c to stop)")
        print(f"domain check API:     POST http://127.0.0.1:{args.port}{API_PATH}"
              f"  ({{\"async\": true}} -> job; GET {API_PATH}/<job_id>)")
        print(f"limits: {PER_IP_CHECKS} checks/{PER_IP_WINDOW}s per client, "
              f"{PER_TARGET_FETCHES} fetches/{PER_TARGET_WINDOW}s per target, "
              f"{GLOBAL_DAILY}/day total, {MAX_CONCURRENT} concurrent, "
              f"{CACHE_TTL}s cache")
        print("still bound to localhost; publish with:  "
              f"cloudflared tunnel --url http://127.0.0.1:{args.port}")
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
