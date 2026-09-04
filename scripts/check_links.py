#!/usr/bin/env python3
"""Relative-link checker for ``README.md`` and ``docs/**/*.md`` (spec §14 docs list).

Every Markdown link/image target that is not an absolute URL, a mail address or a pure
anchor must resolve to a file in the repository — a portfolio README whose links 404 is
worse than one with fewer links. Also flags targets that point outside the repo root.

    python scripts/check_links.py [--root .] [--quiet]

Exit code 0 when every relative target exists, 1 otherwise. Stdlib only: CI runs it
without installing the package.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "data:", "tel:")


def targets(text: str) -> list[str]:
    """Link targets outside fenced code blocks (fences carry example commands, not links)."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.extend(LINK_RE.findall(line))
    return out


def check(root: Path) -> list[str]:
    files = [root / "README.md", *sorted(root.glob("docs/**/*.md"))]
    problems: list[str] = []
    for md in files:
        if not md.is_file():
            problems.append(f"{md.relative_to(root)}: 파일 없음")
            continue
        for raw in targets(md.read_text(encoding="utf-8")):
            if raw.startswith(SKIP_PREFIXES):
                continue
            target = raw.split("#", 1)[0]
            if not target:
                continue
            resolved = (md.parent / target).resolve()
            try:
                rel = resolved.relative_to(root.resolve())
            except ValueError:
                problems.append(f"{md.relative_to(root)} → {raw} (저장소 밖)")
                continue
            if not resolved.exists():
                problems.append(f"{md.relative_to(root)} → {raw} (없음: {rel})")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root)

    problems = check(root)
    for problem in problems:
        print(f"[깨진 링크] {problem}", file=sys.stderr)
    if problems:
        return 1
    if not args.quiet:
        n = len(list(root.glob("docs/**/*.md"))) + 1
        print(f"상대 링크 정상 ({n}개 문서)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
