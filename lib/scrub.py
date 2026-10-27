"""Redact secret-like strings from text files before sharing them (Jira, MR, chat).

The test runner calls this over the artifacts folder at the end of every run. URLs logged
by the addon already have their query/fragment stripped; this catches the rest (tokens
in mock bodies, step details, ...).

    python3 lib/scrub.py <folder>

Edits matching text files in place. Binary files (screenshots, videos) are left untouched.
"""

import re
import sys
from pathlib import Path

TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".log", ".md", ".yaml", ".yml"}

# (compiled pattern, replacement). Ordering matters: JWTs before generic tokens.
# Separators tolerate JSON quoting, e.g. "api_key":"value" -> key " : " value.
_SEP = r"[\"']?\s*[:=]\s*[\"']?"
REDACTIONS = [
    (re.compile(r"(?i)(authorization" + _SEP + r"bearer\s+)[A-Za-z0-9._~+/-]+=*"), r"\1[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "[REDACTED_JWT]"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{16,}"), "glpat-[REDACTED]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"), "gh_[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password)" + _SEP + r")[A-Za-z0-9._~+/-]{8,}=*"), r"\1[REDACTED]"),
]


def scrub_file(path):
    """Return the number of redactions applied to a single file."""
    try:
        original = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return 0
    updated = original
    count = 0
    for pattern, replacement in REDACTIONS:
        updated, n = pattern.subn(replacement, updated)
        count += n
    if count:
        path.write_text(updated, encoding="utf-8")
    return count


def scrub_dir(directory):
    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(f"folder not found: {directory}")
    redactions = 0
    files = 0
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            redactions += scrub_file(path)
            files += 1
    return files, redactions


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print("usage: scrub.py <folder>", file=sys.stderr)
        return 2
    files, redactions = scrub_dir(argv[0])
    print(f"scrubbed {files} text file(s) in {argv[0]}: {redactions} redaction(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
