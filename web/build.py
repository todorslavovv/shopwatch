#!/usr/bin/env python3
"""Assemble the standalone Merchant Disclosure Triage page.

The page must be self-contained (no fetch, no external scripts), so the report
JSON and the application code are inlined at build time.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent


def build(report_path=pathlib.Path("/tmp/shopwatch_report.json"), out=HERE / "index.html") -> pathlib.Path:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    tpl = (HERE / "_template.html").read_text(encoding="utf-8")
    app = (HERE / "_app.js").read_text(encoding="utf-8")

    # "</script>" inside embedded JSON would close the host <script> element early.
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/null", blob)
    html = html.replace("</script>", app + "\n</script>", 1)
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    p = build()
    print(f"{p} ({p.stat().st_size:,} bytes)")
