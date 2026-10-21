#!/usr/bin/env python3
"""Release 3 documentation lint: active documents must not offer an unauthorized recovery path.

    python tools/recovery/doclint.py [--root REPO] [--handoff DIR ...]

Checks
  1. In active Markdown (everything except docs/history/), a line naming a forbidden recovery method
     (code-only rollback, SQL zero-count proof, DB-only .bak restore, the 05c47ca rollback, keeping
     candidate writes, optional full restore, deleting Demo data) must also carry a negation
     (not / never / no / refused / forbidden / retired / BLOCKED / ...).
  2. Text that is always forbidden in active documents ("no recovery needed", the mutable rollback
     tag, the old hand-typed tar snapshot and restore) must not appear at all.
  3. Every active document that discusses rollback or U4 links the authoritative runbook.
  4. Every docs/history/ file starts with the HISTORICAL RECORD banner and its warning.
  5. A hand-off directory holds no *.patch file outside historical/, and historical/ has a WARNING.
Exit 0 clean, 1 findings, 64 usage.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RUNBOOK = "RELEASE3_RECOVERY_RUNBOOK.md"
NEGATION = re.compile(r"\b(not|never|no|none|refus\w*|forbidden|prohibit\w*|retired|unsupported|blocked|stop|"
                      r"without|cannot|isn't|superseded|nothing)\b", re.I)
NEEDS_NEGATION = {
    "code-only rollback": re.compile(r"code[- ]only|code rollback", re.I),
    "SQL zero-count proof": re.compile(r"zero[- ]count", re.I),
    "DB-only .bak restore": re.compile(r"\.bak\b.*\b(restor\w*|rollback|fallback|copy)|"
                                       r"\b(restor\w*|rollback|fallback)\b.*\.bak\b", re.I),
    "05c47ca rollback": re.compile(r"05c47ca.*rollback|rollback.*05c47ca", re.I),
    "keeping candidate writes": re.compile(r"keep(ing|s)? (the )?candidate('s)? (writes|data)", re.I),
    "optional full restore": re.compile(r"optional(ly)? (full[- ])?(volume )?restore|restore is optional", re.I),
    "deleting Demo data": re.compile(r"delet\w* (the )?demo", re.I),
}
ALWAYS_FORBIDDEN = {
    "no recovery needed": re.compile(r"no recovery (is )?(needed|required)|recovery (is )?(not|un)(needed|necessary)", re.I),
    "mutable rollback tag": re.compile(r"rollback-147e315"),
    "hand-typed restore": re.compile(r"find /d -mindepth 1 -delete"),
    "hand-typed snapshot": re.compile(r"tar -C /src -cpf /dst/tip-data-pre-upgrade"),
}
TOPIC = re.compile(r"\brollback\b|\bU4\b|roll back", re.I)
BANNER = "# HISTORICAL RECORD"


def active_docs(root):
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(("docs/history/", "node_modules/")) or "/node_modules/" in rel or "/.venv" in rel:
            continue
        yield p, rel


def lint_text(rel, text, require_link=True):
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for what, rx in ALWAYS_FORBIDDEN.items():
            if rx.search(line):
                out.append(f"{rel}:{n}: forbidden instruction ({what})")
        for what, rx in NEEDS_NEGATION.items():
            if rx.search(line) and not NEGATION.search(line):
                out.append(f"{rel}:{n}: names '{what}' without marking it unauthorized")
    if require_link and not rel.endswith(RUNBOOK) and TOPIC.search(text) and RUNBOOK not in text:
        out.append(f"{rel}: discusses rollback/U4 but does not link {RUNBOOK}")
    return out


def lint_repo(root):
    findings = []
    for p, rel in active_docs(root):
        findings += lint_text(rel, p.read_text(encoding="utf-8", errors="replace"))
    hist = root / "docs" / "history"
    for p in sorted(hist.glob("*.md")) if hist.is_dir() else []:
        t = p.read_text(encoding="utf-8")
        if not t.startswith(BANNER) or "NOT authorized" not in t[:1200] or RUNBOOK not in t[:1200]:
            findings.append(f"docs/history/{p.name}: missing the HISTORICAL RECORD banner/warning/runbook link")
    return findings


def lint_handoff(d):
    findings = []
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d).as_posix()
        hist = rel.startswith("historical/")
        if p.suffix == ".patch" and not hist:
            findings.append(f"handoff {rel}: patch file outside historical/")
        if p.suffix == ".md" and not hist:
            findings += lint_text(f"handoff/{rel}", p.read_text(encoding="utf-8", errors="replace"), require_link=False)
    if (d / "historical").is_dir():
        w = d / "historical" / "WARNING.md"
        if not w.is_file() or "NOT" not in w.read_text(encoding="utf-8"):
            findings.append("handoff historical/: WARNING.md missing")
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doclint")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--handoff", action="append", default=[])
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 64 if exc.code else 0
    findings = lint_repo(Path(a.root))
    for h in a.handoff:
        findings += lint_handoff(Path(h))
    for f in findings:
        print(f)
    print(f"doclint: {len(findings)} finding(s)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
