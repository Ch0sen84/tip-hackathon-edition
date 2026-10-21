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
  6. Outside the runbook, a rebuild command (`docker compose ... build`, `up --build`,
     `build --no-cache`, tools/build_with_version.sh) may appear only in a Markdown section that
     links the runbook (a fresh install), and never next to upgrade wording ("upgrade", "update",
     "rebuild", "code/dependency changes", "git pull", ...) unless that text itself points to the
     runbook or is negated. On an existing deployment such a command runs the Release 3 migrations
     with no U4, no acceptance and no rollback.
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
REBUILD = re.compile(r"compose\b[^`\n|]*?\bbuild\b|build_with_version\.sh|\bdocker\s+(image\s+)?build\b", re.I)
# R3D-5: a negation excuses a rebuild command only when it sits on the command's own line, before it
# ("never run `docker compose up --build` to upgrade"), never because a negation word appears somewhere nearby
CMD_NEGATED = re.compile(r"\b(never|do not|don't|must not)\b", re.I)
# a sentence that forbids something ("never", "do not", an emphatic NOT) is a warning, not upgrade advice;
# a weak "no" ("no downtime") never excuses anything (R3D-5)
STRONG_NEGATION = re.compile(r"\b(never|do not|don't|must not|NOT)\b")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?:])\s+|\n\s*\n")
UPGRADE_WORDS = re.compile(r"\bupgrad\w*|\bupdat\w*|\brebuild\w*|\b(code|dependency|dependencies|source)\b[^.\n]{0,20}"
                           r"\bchang\w*|\bgit pull\b|\bnew (version|release|commit)\b|\bpick(ed)? up\b", re.I)
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^#{1,6}\s")


def _sections(lines):
    """[(first_line_index, last_line_index)] split at Markdown headings outside fenced code."""
    starts, fenced = [0], False
    for i, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and i and HEADING.match(line):
            starts.append(i)
    return [(a, b - 1) for a, b in zip(starts, starts[1:] + [len(lines)])]


def lint_rebuild(rel, text):
    """Check 6: rebuild commands only in fresh-install sections that point upgrades to the runbook."""
    if rel.endswith(RUNBOOK):
        return []
    out, lines = [], text.splitlines()
    for a, b in _sections(lines):
        linked = any(RUNBOOK in lines[i] for i in range(a, b + 1))
        for i in range(a, b + 1):
            if not REBUILD.search(lines[i]):
                continue
            m = REBUILD.search(lines[i])
            negated = bool(CMD_NEGATED.search(lines[i][:m.start()]))
            # R3D-5: upgrade wording anywhere in the section before the command counts (sentence by sentence,
            # outside code fences), not just two lines up; a sentence naming the runbook or forbidding is exempt
            prose, fenced = [], False
            for j in range(a, i):
                if FENCE.match(lines[j]):
                    fenced = not fenced
                elif not fenced:
                    prose.append(lines[j])
            sentences = SENTENCE_SPLIT.split("\n".join(prose))
            upgrade = [] if negated else [x for x in sentences if UPGRADE_WORDS.search(x) and RUNBOOK not in x
                                          and not STRONG_NEGATION.search(x)]
            if not linked:
                out.append(f"{rel}:{i + 1}: rebuild command in a section that does not point existing-deployment "
                           f"upgrades to {RUNBOOK}")
            elif upgrade:
                out.append(f"{rel}:{i + 1}: rebuild command offered as upgrade advice (only {RUNBOOK} may give "
                           "upgrade steps)")
    return out


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
    if require_link:
        out += lint_rebuild(rel, text)
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
