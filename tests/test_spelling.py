"""American English in the tracked text.

EGMTrans is published by a U.S. agency, so its code, comments, docs and
messages use American spellings. This test fails on the common British ones,
so the rule is checked rather than remembered.
"""

import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXT_SUFFIXES = (".py", ".pyt", ".xml", ".md", ".sh", ".toml", ".yml", ".yaml", ".txt", ".dockerignore")
TEXT_NAMES = ("Dockerfile",)
SKIP_PREFIXES = ("samples/", "img/", "crs/", "datums/", "EGMTrans_Explorer")
SKIP_FILES = ("LICENSE", ".pyHistory", "tests/test_spelling.py")

BRITISH = [
    r"\blabell(ed|ing|er)\b",
    r"\bunlabelled\b",
    r"\b(milli|centi|kilo)?metres?\b",
    r"\bneighbour\w*",
    r"\banalys(e|ed|ing)\b",
    r"\bcentres?\b",
    r"\bcentred\b",
    r"\bcentre-",
    r"\b(optimis|standardis|recognis|normalis|initialis|minimis|summaris|organis|customis|prioritis)"
    r"(e|ed|es|ing|ation)\b",
    r"\bbehaviour\w*",
    r"\bcolour\w*",
    r"\bcatalogue\b",
    r"\bhonour\w*",
    r"\bfavour\w*",
    r"\bartefact\w*",
    r"\bwhilst\b",
    r"\bamongst\b",
    r"\bprogramme\b",
    r"\blicence\b",
    r"\bgrey\b",
    r"\bdefence\b",
    r"\bjudgement\b",
    r"\bmodelling\b",
    r"\btravelling\b",
]
PATTERN = re.compile("|".join(f"(?:{p})" for p in BRITISH), re.IGNORECASE)
# EPSG spells the unit "metre" inside WKT strings; that is data, not prose.
ALLOWED_LINE = re.compile(r"UNIT\[|LENGTHUNIT|UNIT\(")


def tracked_text_files():
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git is not available to list the tracked files")
    for path in out.splitlines():
        if path.startswith(SKIP_PREFIXES) or path in SKIP_FILES:
            continue
        if path.endswith(TEXT_SUFFIXES) or os.path.basename(path) in TEXT_NAMES:
            yield path


def test_no_british_spellings():
    hits = []
    for path in tracked_text_files():
        with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace") as f:
            for number, line in enumerate(f, 1):
                if ALLOWED_LINE.search(line):
                    continue
                for match in PATTERN.finditer(line):
                    hits.append(f"{path}:{number}: {match.group(0)}")
    assert not hits, "British spellings found (use American English):\n" + "\n".join(hits)
