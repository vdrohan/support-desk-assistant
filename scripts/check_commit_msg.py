"""Check a commit subject against the project's style (CLAUDE.md "Commits", decisions #32).

Used as the pre-commit ``commit-msg`` hook (the argument is the message file), by CI on a pull
request title (``--subject``), and by the tests. Standard library only.
"""

import argparse
import re
import sys
from pathlib import Path

TYPES = ("feat", "fix", "docs", "test", "refactor", "perf", "chore", "ci", "eval", "prompt")
MAX_SUBJECT_CHARS = 72
SUBJECT = re.compile(rf"^({'|'.join(TYPES)})(\([a-z0-9._-]+\))?: \S")
# Git writes these itself; a person does not type them.
GENERATED = re.compile(r"^(Merge |Revert |fixup! |squash! )")


def subject_problems(subject: str) -> list[str]:
    """What is wrong with a subject line; an empty list means it is fine."""
    if GENERATED.match(subject):
        return []
    problems = []
    if not SUBJECT.match(subject):
        problems.append("must start with 'type: summary', where type is one of " + ", ".join(TYPES))
    if len(subject) > MAX_SUBJECT_CHARS:
        problems.append(f"is {len(subject)} characters; keep it to {MAX_SUBJECT_CHARS} or fewer")
    if subject.endswith("."):
        problems.append("must not end with a period")
    return problems


def first_subject(message: str) -> str:
    """The first line that is not a comment (git adds '#' lines while a message is edited)."""
    for line in message.splitlines():
        if not line.startswith("#"):
            return line.rstrip()
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("message_file", nargs="?", type=Path, help="commit message file (hook)")
    source.add_argument("--subject", help="a subject line to check, such as a pull request title")
    args = parser.parse_args(argv)

    if args.subject is not None:
        subject = args.subject
    else:
        subject = first_subject(args.message_file.read_text(encoding="utf-8"))
    problems = subject_problems(subject)
    if not problems:
        return 0
    print(f"Commit subject not accepted: {subject!r}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print("Example: 'feat: add ticket override endpoint'. Details go in the body.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
