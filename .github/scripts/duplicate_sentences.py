#!/usr/bin/env python3
"""Fail on a sentence written in two places.

Every tracked text file is split into sentences: comment markers and string
quotes stripped, wrapped lines joined. A sentence of eight or more words that
is mostly words, and appears twice anywhere, is a duplicate: an explanation
belongs in one place, which the other places point to.

.github/duplicate-sentences-debt.txt lists the duplicates the tree carries
today. A duplicate not on it fails; so does an entry that is no longer
duplicated, so the list only shrinks.

    python3 .github/scripts/duplicate_sentences.py           check
    python3 .github/scripts/duplicate_sentences.py --list    print every duplicate
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEBT = ROOT / ".github" / "duplicate-sentences-debt.txt"
MIN_WORDS = 8
SKIP_SUFFIXES = (".lock", ".png", ".jpg", ".svg", ".ico", ".gz")
SKIP_FILES = {DEBT.relative_to(ROOT).as_posix()}

_MARKER = re.compile(r"^\s*(?:#+|//+|--|\*|;+|>)?\s?")
_QUOTES = re.compile(r"^\s*[fbr]?[\"']{1,3}|[\"']{1,3}\s*\)?,?\s*$")
_SPLIT = re.compile(r"(?<=[.!?:;])\s+(?=[A-Z`\"'(<\[])|\s+--\s+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_MD_COMMENT = re.compile(r"<!--.*?-->", re.S)


def normalize(sentence: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", sentence.lower()).split())


def sentences(text: str):
    """(line, sentence) for each prose sentence in `text`."""
    paragraph: list[str] = []
    start = 0

    def flush():
        for sentence in _SPLIT.split(" ".join(paragraph)):
            sentence = sentence.strip()
            tokens = sentence.split()
            words = [t for t in tokens if _WORD.fullmatch(t.strip(".,;:()`\"'"))]
            if len(_WORD.findall(sentence)) >= MIN_WORDS and len(words) >= 0.7 * len(tokens):
                yield start, sentence

    for number, raw in enumerate(text.splitlines(), 1):
        line = _QUOTES.sub("", _MARKER.sub("", raw.rstrip()).strip())
        if not line:
            yield from flush()
            paragraph = []
            continue
        if not paragraph:
            start = number
        paragraph.append(line)
    yield from flush()


def duplicates() -> dict[str, list[str]]:
    """Normalized sentence -> the places it appears, for every duplicate."""
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files"], check=True,
                           capture_output=True, text=True).stdout.splitlines()
    places: dict[str, list[str]] = defaultdict(list)
    first: dict[str, str] = {}
    for rel in files:
        path = ROOT / rel
        if rel in SKIP_FILES or rel.endswith(SKIP_SUFFIXES) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if rel.endswith(".md"):
            text = _MD_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
        for line, sentence in sentences(text):
            key = normalize(sentence)
            places[key].append(f"{rel}:{line}")
            first.setdefault(key, sentence)
    return {first[k]: v for k, v in places.items() if len(v) > 1}


def debt() -> set[str]:
    return {normalize(line) for line in DEBT.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")}


def main(argv: list[str]) -> int:
    found = duplicates()
    if "--list" in argv:
        for sentence in sorted(found):
            print(sentence)
        return 0
    known = debt()
    new = {s: p for s, p in found.items() if normalize(s) not in known}
    stale = known - {normalize(s) for s in found}
    for sentence, places in sorted(new.items()):
        print(f"duplicate sentence: {sentence}\n  " + "\n  ".join(places))
    for entry in sorted(stale):
        print(f"no longer duplicated, delete it from {DEBT.name}: {entry}")
    if new or stale:
        print(f"\n{len(new)} new duplicate(s), {len(stale)} stale debt entry(ies). "
              "Keep the explanation in one place and point to it from the others.")
        return 1
    print(f"no new duplicate sentences ({len(known)} carried in {DEBT.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
