"""Asynchronous domain-check jobs: submission, progress, completion, errors, dedup.

The resolver is faked where timing has to be controlled; one test runs the real
resolver (on fakes) both synchronously and as a job and compares the results.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from shopwatch import domaincheck as dc
from shopwatch import serve
from tests.test_domaincheck import EIK, FakeArchive, FakeRegistry, contact, getter, site, vies_table


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(serve, "STORE", None)
    serve._jobs.clear(); serve._active_by_domain.clear()
    serve._hits.clear(); serve._day.clear(); serve._targets.clear()
    yield
    serve._jobs.clear(); serve._active_by_domain.clear()


def submit(url="shop.bg", runner=None, client="1.1.1.1"):
    body = json.dumps({"url": url, "async": True}).encode()
    return serve.domain_check(client, body, runner=runner)


def wait_for(job_id, states=("completed", "failed", "cancelled"), timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        code, view = serve.job_status(job_id)
        if view.get("state") in states:
            return view
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached {states}: {view}")


class Gate:
    """A fake resolver that reports real-looking stages and waits where told."""

    def __init__(self, result=None, fail=False):
        self.go = {s: threading.Event() for s in dc.STAGES + ("end",)}
        self.calls = 0
        self.result = result or {"domain_resolution": {"company_identified": True}}
        self.fail = fail

    def release_all(self):
        for e in self.go.values():
            e.set()

    def __call__(self, url, store=None, progress=None):
        self.calls += 1
        for st in dc.STAGES:
            progress(st, {"fetching": "trying_site", "discovering": "related_hosts",
                          "legal_sources": "legal_docs", "verifying": "verifying"}[st],
                     provisional={"eik": EIK, "name": "ТЕСТ ЕООД"} if st == "legal_sources"
                     else None)
            self.go[st].wait(5)
        self.go["end"].wait(5)
        if self.fail:
            raise RuntimeError("resolver blew up")
        return self.result


def test_submission_returns_a_job_immediately():
    g = Gate()
    t0 = time.time()
    code, view = submit(runner=g)
    assert code == 202 and time.time() - t0 < 0.5
    assert view["job_id"] and view["state"] in ("queued", "running")
    assert view["status_url"].endswith(view["job_id"])
    g.release_all()


def test_status_reports_real_stages_and_provisional_evidence():
    g = Gate()
    _, view = submit(runner=g)
    jid = view["job_id"]
    time.sleep(0.05)
    v = serve.job_status(jid)[1]
    assert v["stage"] == "fetching" and v["stages"][0] == {"id": "fetching", "status": "active"}
    assert all(s["status"] == "pending" for s in v["stages"][1:])
    g.go["fetching"].set(); g.go["discovering"].set()
    time.sleep(0.05)
    v = serve.job_status(jid)[1]
    assert v["stage"] == "legal_sources"
    assert [s["status"] for s in v["stages"]] == ["done", "done", "active", "pending", "pending"]
    assert v["provisional"]["eik"] == EIK and "result" not in v      # provisional, not final
    g.release_all()
    done = wait_for(jid)
    assert done["state"] == "completed" and done["result"] == g.result
    assert all(s["status"] == "done" for s in done["stages"])


def test_job_result_matches_the_synchronous_resolver():
    def real(url, store=None, progress=None):
        return dc.check(url, store=store, progress=progress,
                        fetch=site({"https://shop.bg/": contact()}), getter=getter(), search=[],
                        vat_checker=vies_table({f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"}),
                        registry=FakeRegistry(available=False), archive=FakeArchive(),
                        tls_fn=lambda h: {"status": "ok"})
    sync = real("shop.bg")
    _, view = submit(runner=real)
    job = wait_for(view["job_id"])
    strip = lambda r: {k: v for k, v in r.items() if k not in ("checked_at", "target_site")}
    assert strip(job["result"]) == strip(sync)
    # This site named its ЕИК itself, so discovery was genuinely not needed.
    assert [s["status"] for s in job["stages"]] == ["done", "skipped", "skipped", "done", "done"]


def test_real_resolver_reports_stages_in_order_for_a_blocked_site():
    seen = []
    archive = FakeArchive({"https://shop.bg/": contact()})
    dc.check("shop.bg", fetch=site({"https://shop.bg/": "x"}, status={"https://shop.bg/": 403}),
             getter=getter(), search=[], vat_checker=vies_table({}),
             registry=FakeRegistry(available=False), archive=archive,
             tls_fn=lambda h: {"status": "ok"},
             progress=lambda st, code, **kw: seen.append((st, code)))
    stages = [s for s, _ in seen]
    assert stages[0] == "fetching" and stages[-1] == "verifying"
    assert [s for s in dict.fromkeys(stages)] == list(dc.STAGES)     # never goes backwards
    assert ("fetching", "site_blocked") in seen and ("legal_sources", "archive") in seen


def test_failed_resolver_gives_a_terminal_error_state():
    g = Gate(fail=True)
    _, view = submit(runner=g)
    g.release_all()
    v = wait_for(view["job_id"])
    assert v["state"] == "failed" and v["error"]["code"] == "internal"
    assert any(s["status"] == "failed" for s in v["stages"])
    assert "shop.bg" not in serve._active_by_domain                    # a new lookup may start


def test_unknown_malformed_and_expired_jobs_are_not_found(monkeypatch):
    assert serve.job_status("nope")[0] == 404
    assert serve.job_status("A" * 30)[0] == 404
    assert serve.job_status("../../etc/passwd")[0] == 404
    g = Gate()
    _, view = submit(runner=g)
    g.release_all()
    wait_for(view["job_id"])
    monkeypatch.setattr(serve, "JOB_TTL", 0)
    time.sleep(0.01)
    assert serve.job_status(view["job_id"])[0] == 404


def test_duplicate_submissions_share_one_job():
    g = Gate()
    a = submit(runner=g)[1]
    b = submit(runner=g, client="2.2.2.2")[1]                # another visitor, same site
    c = submit(url="https://www.shop.bg/", runner=g)[1]    # same registrable domain
    assert a["job_id"] == b["job_id"] == c["job_id"] and b["deduplicated"]
    g.release_all()
    wait_for(a["job_id"])
    assert g.calls == 1


def test_cancel_stops_the_job_and_frees_the_domain():
    g = Gate()
    _, view = submit(runner=g)
    time.sleep(0.05)
    code, v = serve.cancel_job(view["job_id"])
    assert code == 200 and v["state"] == "cancelled"
    g.go["fetching"].set()                                  # resolver reaches its next stage...
    time.sleep(0.05)
    assert g.calls == 1 and serve.job_status(view["job_id"])[1]["state"] == "cancelled"
    g.release_all()
    new = submit(runner=Gate())[1]
    assert new["job_id"] != view["job_id"]


def test_job_running_too_long_is_reported_as_timeout(monkeypatch):
    monkeypatch.setattr(serve, "JOB_MAX_RUNTIME", 0.05)
    g = Gate()
    _, view = submit(runner=g)
    time.sleep(0.1)
    v = serve.job_status(view["job_id"])[1]
    assert v["state"] == "failed" and v["error"]["code"] == "timeout"
    g.release_all()


def test_active_jobs_are_bounded(monkeypatch):
    monkeypatch.setattr(serve, "MAX_ACTIVE_JOBS", 2)
    gates = [Gate() for _ in range(3)]
    codes = [submit(url=f"shop{i}.bg", runner=gates[i], client=f"9.9.9.{i}")[0]
             for i in range(3)]
    assert codes == [202, 202, 503]
    for g in gates:
        g.release_all()


def test_synchronous_api_is_unchanged():
    code, body = serve.domain_check("1.1.1.1", b'{"url": "shop.bg"}',
                                    runner=lambda url, store: {"domain_resolution": {}, "u": url})
    assert code == 200 and body["u"] == "shop.bg"


def test_http_routes_end_to_end(monkeypatch):
    import socketserver
    g = Gate()
    # The handler calls domain_check() with its default runner: point that at the fake,
    # so no real site is ever contacted from a test.
    monkeypatch.setattr(serve.domain_check, "__defaults__", (g,))
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), serve.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        req = urllib.request.Request(base + serve.API_PATH, method="POST",
                                     data=b'{"url": "shop.bg", "async": true}',
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 202
            view = json.loads(r.read())
        g.release_all()
        wait_for(view["job_id"])
        with urllib.request.urlopen(base + view["status_url"], timeout=5) as r:
            assert json.loads(r.read())["state"] == "completed"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + serve.API_PATH + "/unknown-job-id-123456", timeout=5)
        assert e.value.code == 404
    finally:
        srv.shutdown()
