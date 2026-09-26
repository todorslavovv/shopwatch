"""Capability benchmark: Shopwatch against hand-verified ground truth for 20 real shops.

    python -m benchmark.run --observe [domain ...] > benchmark/observed.json   (network)
    python -m benchmark.run --report benchmark/observed.json                   (offline)
    python -m benchmark.run --publish benchmark/observed.json > web/sample_report.json

--observe runs the product exactly as the website does: the disclosure triage
(investigate, 5 pages) and the domain -> company check. --report compares that with
benchmark/shops.json and prints per-field coverage, shortfall reproduction and the
capability-gap log. Every count comes from those two files; nothing is typed in.

A field the site publishes that Shopwatch did not find is a capability gap. Its technical
reason lives in benchmark/gaps.json; a gap without one is reported as "unexplained".
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from shopwatch import profile as pr
from shopwatch.registry import name_matches

HERE = Path(__file__).parent
FIELDS = ("eik", "legal_name", "legal_form", "address", "phone", "email", "vat_number")
TRIAGE_KEY = {"eik": "eik", "legal_name": "company_name", "legal_form": "company_name",
              "address": "address", "phone": "phone", "email": "email",
              "vat_number": "vat_number"}
PROFILE_KEY = {"eik": "eik", "legal_name": "legal_name", "legal_form": "legal_form",
               "address": "registered_address", "phone": "phone", "email": "email",
               "vat_number": "vat_number"}
COVERAGE = ("found_by_deterministic_tooling", "not_found_by_deterministic_tooling",
            "unavailable_to_deterministic_tooling", "found_by_ai_fallback",
            "not_found_anywhere", "conflicting", "named_without_eik")
# Which triage finding reproduces which ground-truth shortfall.
TRIAGE_CODES = {"NO_EIK": {"NO_EIK"}, "EIK_INVALID": {"EIK_INVALID"},
                "NO_COMPANY": {"NO_COMPANY"}, "NO_LEGAL_FORM": {"NO_COMPANY"},
                "NO_ADDRESS": {"NO_ADDRESS"}, "PLACEHOLDER_ADDRESS": {"NO_ADDRESS"},
                "ADDRESS_INCOMPLETE": {"NO_ADDRESS"}, "NO_PHONE": {"NO_PHONE", "NO_CONTACT"},
                "NO_EMAIL": {"NO_EMAIL", "NO_CONTACT"}, "NO_CONTACT": {"NO_CONTACT"},
                "LEGAL_FORM_INCONSISTENT": {"COMPANY_INCONSISTENT"},
                "TRADER_NAME_INCONSISTENT": {"COMPANY_INCONSISTENT"}}
FORM_RX = re.compile(r"(?<![^\W\d_])(ЕООД|ЕАД|ООД|АД|ЕТ|КДА|КД|СД|Ltd)\.?$", re.IGNORECASE)


def load(path=HERE / "shops.json") -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- observe

def observe(domain: str) -> dict:
    """Run Shopwatch on one shop, keeping only what the comparison needs."""
    from shopwatch import domaincheck as dc
    from shopwatch.investigate import investigate
    t0 = time.time()
    tri = investigate(domain, max_pages=5, timeout=30)
    t1 = time.time()
    try:
        r = dc.check(domain)
        err = None
    except Exception as e:                        # recorded, never hidden
        r, err = {}, f"{type(e).__name__}: {e}"
    # The same check without the NAP list: NAP itself supplies the domain -> ЕИК link, so
    # this is what the independent sources (site, legal pages, archive, VIES) resolve.
    from shopwatch import sources
    try:
        r0 = dc.check(domain, nap_source=lambda *a: sources.state(sources.UNCONFIGURED, None,
                                                                    entries=[]))
        pf0 = (r0.get("profile") or {}).get("fields") or {}
        no_nap = {"eik": (r0.get("domain_resolution") or {}).get("company_identified") and
                  pf0.get("eik", {}).get("value") or None,
                  "name": pf0.get("legal_name", {}).get("value") or (r0.get("company") or {}).get("name"),
                  "basis": (r0.get("domain_resolution") or {}).get("basis"),
                  "company_identified": (r0.get("domain_resolution") or {}).get("company_identified"),
                  "confidence": (r0.get("domain_resolution") or {}).get("confidence_level"),
                  "candidate": ((r0.get("domain_resolution") or {}).get("candidate") or {}).get("eik"),
                  "error": None}
    except Exception as e:
        no_nap = {"eik": None, "company_identified": None, "error": f"{type(e).__name__}: {e}"}
    prof = r.get("profile") or {}
    fields = {}
    for name, f in (prof.get("fields") or {}).items():
        fields[name] = {"value": f.get("value"), "status": f.get("status"),
                        "coverage": f.get("coverage"),
                        "disclosed_by_site": f.get("disclosed_by_site"),
                        "site_values": [v["value"] for v in f.get("values") or []
                                        if any(s["type"].startswith("company_")
                                               for s in v["sources"])]}
    return {
        "domain": domain, "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "triage": {"merchant": tri["merchant"], "sources": tri["sources"],
                   "codes": [x["code"] for x in tri["assessment"]["findings"]],
                   "verdict": tri["assessment"].get("verdict"),
                   "reachable": tri["reachable"], "refused": tri["refused"],
                   "pages": [p["url"] for p in tri["pages_visited"]],
                   "seconds": round(t1 - t0, 1)},
        "domaincheck": {"error": err,
                        "company_identified": (r.get("domain_resolution") or {})
                        .get("company_identified"),
                        "eik": (fields.get("eik") or {}).get("value"),
                        "confidence": (r.get("domain_resolution") or {}).get("confidence_level"),
                        "conflicts": [c.get("type") for c in r.get("conflicts") or []],
                        "nap": (r.get("discovery") or {}).get("nap_register"),
                        "fetch_status": (r.get("target_site") or {}).get("fetch_status"),
                        "pages": [p["url"] for p in (r.get("target_site") or {})
                                  .get("pages_read") or []],
                        "fields": fields,
                        "ai_fallback": r.get("ai_fallback"),
                        "without_nap": no_nap,
                        "seconds": round(time.time() - t1, 1)},
    }


# ----------------------------------------------------------------------------- compare

def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _email_hash(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.strip().lower().encode()).hexdigest()[:16]


def agrees(field: str, found, truth: dict) -> bool:
    """Does a value Shopwatch found match what the ground truth says the site publishes?"""
    if not found:
        return False
    want = truth.get("value")
    wants = want if isinstance(want, list) else [want] if want else []
    if field == "eik":
        return any(_digits(found)[-9:] == _digits(w)[-9:] for w in wants)
    if field == "vat_number":
        return any(_digits(found) == _digits(w) for w in wants)
    if field == "legal_name":
        return any(name_matches(found, w) for w in wants)
    if field == "legal_form":
        return any(form_of(found) == form_of(w) for w in wants)
    if field == "address":
        return any(pr.addresses_agree(found, w) for w in wants)
    if field == "phone":
        return any(pr.phone_key(found) and pr.phone_key(found) == pr.phone_key(w) for w in wants)
    if field == "email":
        h = found[7:] if found.startswith("sha256:") else _email_hash(found)
        return (any(pr.email_key(found) == pr.email_key(w) for w in wants)
                or h in (truth.get("value_sha256") or [])
                or any(h == _email_hash(w) for w in wants))
    return False


def same_company(a: str, b: str) -> bool:
    """Strict: every significant word of the shorter name is in the other. name_matches is
    looser on purpose (one shared word) and would call "Диверсо Хеър" "ЕЛЪН ХЕЪР БУТИК"."""
    w = lambda s: {x for x in re.findall(r"\w+", (s or "").casefold())
                   if len(x) > 1 and not FORM_RX.fullmatch(x)}
    x, y = w(a), w(b)
    return bool(x and y) and (x <= y or y <= x)


def form_of(s) -> str | None:
    m = FORM_RX.search(str(s or "").strip())
    return m.group(1).upper() if m else None


def compare(shop: dict, obs: dict) -> dict:
    """One shop: coverage per field and whether each shortfall is reproduced."""
    tri, dcx = obs["triage"], obs["domaincheck"]
    unavailable = not tri["reachable"] and not dcx["pages"]
    rows = {}
    for field in FIELDS:
        truth = shop["published"].get(field) or {"value": None, "published": False}
        pf = (dcx["fields"] or {}).get(PROFILE_KEY[field]) or {}
        found = [(v, "triage") for v in [tri["merchant"].get(TRIAGE_KEY[field])] if v] + \
                [(v, "domaincheck") for v in pf.get("site_values") or []]
        if field == "legal_form":                    # the form, not the whole name
            found = [(form_of(v), by) for v, by in found if form_of(v)]
        hit = [(v, by) for v, by in found if agrees(field, v, truth)]
        if pf.get("status") == "ai_assisted":
            cov = "found_by_ai_fallback"
        elif truth.get("published"):
            cov = ("found_by_deterministic_tooling" if hit else
                   "unavailable_to_deterministic_tooling" if unavailable else
                   "conflicting" if found else "not_found_by_deterministic_tooling")
        else:
            other = [v for v, _ in found if not agrees(field, v, shop["published"]
                                                       .get("trade_name") or {})]
            cov = "conflicting" if other else "not_found_anywhere"
        rows[field] = {"coverage": cov, "by": sorted({by for _, by in hit}),
                       "found": [v for v, _ in found][:3]}
    # domain -> operator: the one link only an official source (NAP) gives
    declared = {d["eik"] for d in shop["official"]["nap_declarations"]}
    def operator_row(eik):
        return {"coverage": "found_by_deterministic_tooling" if eik in declared else
                "conflicting" if eik else "not_found_by_deterministic_tooling",
                "by": ["domaincheck"] if eik in declared else [], "found": [eik]}
    rows["domain_operator"] = operator_row(dcx["eik"])
    # Without the NAP list, which itself supplies the domain -> ЕИК link.
    wn = dcx.get("without_nap")
    if wn is not None:
        row = operator_row(wn.get("eik") or None)
        official = {n for n in [shop["official"]["vies"].get("name")] +
                    [d.get("declared_name") for d in shop["official"]["nap_declarations"]] if n}
        if not wn.get("eik") and wn.get("company_identified") and wn.get("name"):
            # Identified by name alone: the operator's name, or somebody else's.
            same = any(same_company(wn["name"], n) for n in official)
            row["coverage"] = "named_without_eik" if same else "conflicting"
            row["by"] = ["domaincheck"] if same else []
        row.update(confidence=wn.get("confidence"), name=wn.get("name"))
        # Why a miss is expected: the shop's own sources carry no valid ЕИК of the
        # operator (that is the NO_EIK / EIK_INVALID / EIK_OTHER_COMPANY finding).
        pub = shop["published"]["eik"]
        codes = {x["code"] for x in shop["shortfalls"]}
        if (row["coverage"] in ("not_found_by_deterministic_tooling", "named_without_eik") and (
                not pub["published"] or codes & {"EIK_INVALID"})) or \
                (row["coverage"] == "conflicting" and wn.get("eik") and "EIK_OTHER_COMPANY" in codes):
            row["expected_reason"] = (
                "the site publishes another company's ЕИК (EIK_OTHER_COMPANY), which the "
                "resolver takes without NAP" if "EIK_OTHER_COMPANY" in codes else
                "the site publishes no valid ЕИК of its operator (the NO_EIK / EIK_INVALID "
                "finding itself): without NAP no source links the domain to an ЕИК")
        rows["domain_operator_without_nap"] = row

    repro = {}
    for s in shop["shortfalls"]:
        code = s["code"]
        if code in TRIAGE_CODES:
            ok = bool(TRIAGE_CODES[code] & set(tri["codes"]))
        elif code == "VAT_NOT_DISCLOSED":
            vs = (dcx["fields"] or {}).get("vat_status") or {}
            vn = (dcx["fields"] or {}).get("vat_number") or {}
            ok = vs.get("value") == "valid" and not vn.get("disclosed_by_site")
        elif code == "EIK_OTHER_COMPANY":
            ok = "multiple_identifiers" in dcx["conflicts"] or ((dcx["fields"] or {})
                                                                .get("eik") or {}).get("status") == "conflicting"
        else:                                        # e.g. CONTACT_INCONSISTENT
            ok = False
        repro[code] = ok
    # A shortfall the triage reports that the ground truth says is not one.
    false_alarms = sorted(c for c in set(tri["codes"]) & {"NO_EIK", "EIK_INVALID", "NO_COMPANY",
                                                         "NO_ADDRESS", "NO_PHONE", "NO_EMAIL",
                                                         "NO_CONTACT", "COMPANY_INCONSISTENT"}
                          if not any(c in TRIAGE_CODES.get(s["code"], ()) for s in
                                     shop["shortfalls"]))
    return {"domain": shop["domain"], "fields": rows, "shortfalls": repro,
            "false_alarms": false_alarms}


def report(truth: dict, observed: list[dict], gaps: dict) -> dict:
    by_dom = {o["domain"]: o for o in observed}
    per_shop = [compare(s, by_dom[s["domain"]]) for s in truth["shops"] if s["domain"] in by_dom]
    fields = FIELDS + ("domain_operator",) + (
        ("domain_operator_without_nap",) if all("domain_operator_without_nap" in r["fields"]
                                                 for r in per_shop) else ())
    coverage = {f: {c: sum(r["fields"][f]["coverage"] == c for r in per_shop) for c in COVERAGE}
                for f in fields}
    totals = {c: sum(coverage[f][c] for f in fields) for c in COVERAGE}
    sf_total = sum(len(r["shortfalls"]) for r in per_shop)
    sf_ok = sum(sum(r["shortfalls"].values()) for r in per_shop)
    kind = {(s["domain"], x["code"]): x.get("kind", "shortfall") for s in truth["shops"]
            for x in s["shortfalls"]}
    by_kind = {k: {"total": sum(1 for r in per_shop for c in r["shortfalls"]
                                if kind[(r["domain"], c)] == k),
                   "reproduced": sum(ok for r in per_shop for c, ok in r["shortfalls"].items()
                                     if kind[(r["domain"], c)] == k)}
               for k in ("shortfall", "information")}
    operator = {}
    for f in ("domain_operator", "domain_operator_without_nap"):
        if f in fields:
            cov = coverage[f]
            operator[f] = {"correct": cov["found_by_deterministic_tooling"],
                           "named_without_eik": sum(r["fields"][f]["coverage"] == "named_without_eik"
                                                    for r in per_shop),
                           "different_company": cov["conflicting"],
                           "not_identified": cov["not_found_by_deterministic_tooling"]}
    if "domain_operator_without_nap" in fields:
        operator["without_nap_confidence"] = {
            lvl: sum(r["fields"]["domain_operator_without_nap"].get("confidence") == lvl
                     for r in per_shop) for lvl in ("high", "medium", "low", "insufficient")}
    log = []
    for r in per_shop:
        for f, row in r["fields"].items():
            if row["coverage"] in ("not_found_by_deterministic_tooling", "conflicting",
                                   "unavailable_to_deterministic_tooling", "named_without_eik"):
                g = gaps.get(f"{r['domain']}:{f}") or {}
                if not g and row.get("expected_reason"):
                    g = {"reason": "expected: " + row["expected_reason"], "improved": False,
                         "change": "none needed"}
                log.append({"domain": r["domain"], "field": f, "coverage": row["coverage"],
                            "found": row["found"], "reason": g.get("reason", "unexplained"),
                            "improved": g.get("improved"), "change": g.get("change")})
        for code, ok in r["shortfalls"].items():
            if not ok:
                g = gaps.get(f"{r['domain']}:{code}") or {}
                log.append({"domain": r["domain"], "shortfall": code, "coverage": "not_reproduced",
                            "reason": g.get("reason", "unexplained"),
                            "improved": g.get("improved"), "change": g.get("change")})
        for code in r["false_alarms"]:
            g = gaps.get(f"{r['domain']}:false:{code}") or {}
            log.append({"domain": r["domain"], "false_alarm": code, "coverage": "false_alarm",
                        "reason": g.get("reason", "unexplained"),
                        "improved": g.get("improved"), "change": g.get("change")})
    return {"shops": len(per_shop), "coverage": coverage, "totals": totals,
            "shortfalls": {"total": sf_total, "reproduced": sf_ok, "by_kind": by_kind},
            "domain_operator": operator,
            "false_alarms": sum(len(r["false_alarms"]) for r in per_shop),
            "ai_fallback_used": sum(r["fields"][f]["coverage"] == "found_by_ai_fallback"
                                    for r in per_shop for f in fields),
            "gaps": log, "unexplained": sum(g["reason"] == "unexplained" for g in log),
            "per_shop": per_shop}


def publish(truth: dict, observed: list[dict]) -> dict:
    """The public worklist: the verified shortfalls with their evidence, and whether the
    latest benchmark run shows Shopwatch detecting each one automatically."""
    by_dom = {o["domain"]: o for o in observed}
    shops = []
    for s in truth["shops"]:
        cmp = compare(s, by_dom[s["domain"]]) if s["domain"] in by_dom else None
        pub = s["published"]
        vies = s["official"]["vies"]
        shops.append({
            "domain": s["domain"], "checked_at": s["checked_at"],
            "declared_eik": sorted({d["eik"] for d in s["official"]["nap_declarations"]}),
            "vies_name": vies["name"] if vies["status"] == "confirmed" else None,
            # What the shop itself publishes, business contacts included, as published.
            "fields": {k: {"published": pub[k]["published"], "value": pub[k]["value"]}
                       for k in ("eik", "legal_name", "legal_form", "address", "phone",
                                 "email", "vat_number")},
            "shortfalls": [{"code": x["code"], "kind": x.get("kind", "shortfall"),
                            "fields": x["fields"], "en": x["summary_en"],
                            "bg": x["summary_bg"],
                            "auto": cmp["shortfalls"].get(x["code"]) if cmp else None,
                            "evidence": [{"url": e["url"], "source_type": e["source_type"],
                                          "retrieved_at": e["retrieved_at"],
                                          "excerpt": e.get("excerpt", "")[:260]}
                                         for e in x["evidence"]]}
                           for x in s["shortfalls"]]})
    total = sum(len(x["shortfalls"]) for x in shops)
    info = sum(f["kind"] == "information" for x in shops for f in x["shortfalls"])
    auto = sum(bool(f["auto"]) for x in shops for f in x["shortfalls"])
    return {"generated": truth["generated"], "method": truth["method"],
            "wording": truth["wording"], "benchmark_run": max(
                (o["observed_at"] for o in observed), default=None),
            "summary": {"shops": len(shops), "findings": total, "shortfalls": total - info,
                        "information": info, "detected_automatically": auto},
            "shops": shops}


def main(argv: list[str]) -> None:
    if argv[:1] == ["--observe"]:
        doms = argv[1:] or [s["domain"] for s in load()["shops"]]
        with ThreadPoolExecutor(4) as ex:
            out = list(ex.map(observe, doms))
        print(json.dumps(out, ensure_ascii=False, indent=1))
    elif argv[:1] == ["--report"]:
        observed = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        gp = HERE / "gaps.json"
        gaps = json.loads(gp.read_text(encoding="utf-8")) if gp.exists() else {}
        print(json.dumps(report(load(), observed, gaps), ensure_ascii=False, indent=1))
    elif argv[:1] == ["--publish"]:
        observed = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        print(json.dumps(publish(load(), observed), ensure_ascii=False, indent=1))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
