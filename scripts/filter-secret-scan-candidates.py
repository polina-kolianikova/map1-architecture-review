#!/usr/bin/env python3
"""Narrow source-aware filtering for the shell scanner's candidate paths.

Never import scanned code or print matched values. All other scanner patterns
remain independent, including quoted assignments and known token signatures.
Parse/read failures retain the candidate. Test exceptions apply to individual
literal occurrences, never a whole line, file, test directory or hostname.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import sys


# Audited synthetic values used by transport rejection/redaction tests. An
# altered value or the same URL in any other file remains a finding.
FIXTURE_URLS = {
    "scripts/tests/test_backup_alert.py": {"https://<USER>:<PASSWORD>@example.test/"},
    "scripts/tests/test_stage3_host_network.py": {"http://<USER>:<PASSWORD>@localhost:9"},
}


def contains_match(node: ast.AST, line_number: int, line: str, match: re.Match) -> bool:
    # Python AST columns are UTF-8 byte offsets, not Unicode character offsets.
    start = (line_number, len(line[:match.start()].encode("utf-8")))
    end = (line_number, len(line[:match.end()].encode("utf-8")))
    return (node.lineno, node.col_offset) <= start and end <= (node.end_lineno, node.end_col_offset)


def has_unexplained_match(path: str, label: str, pattern: re.Pattern) -> bool:
    candidate = Path(path)
    if candidate.suffix != ".py" or candidate.is_symlink():
        return True
    try:
        source = candidate.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path)
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return True

    # A call made solely from runtime expressions contains no hardcoded value.
    # Do not exempt calls containing any literal, including nested arguments.
    lines = source.splitlines()
    runtime_calls = {
        node.lineno for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Call)
        and node.end_lineno == node.lineno
        and not lines[node.lineno - 1].encode("utf-8")[node.end_col_offset:].strip()
        and not any(isinstance(child, ast.Constant) for child in ast.walk(node.value))
    }
    allowed_values = FIXTURE_URLS.get(candidate.as_posix(), set())
    fixture_literals = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value in allowed_values
    ]
    found = False
    for number, line in enumerate(lines, 1):
        for match in pattern.finditer(line):
            found = True
            if label == "plain credential assignment" and number in runtime_calls:
                continue
            if label == "credential URL" and any(
                contains_match(node, number, line, match) for node in fixture_literals
            ):
                continue
            return True
    # A candidate not reproducible by this parser is not safe to suppress.
    return not found


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"plain credential assignment", "credential URL"}:
        print("Secret scanner candidate-filter arguments invalid", file=sys.stderr)
        return 2
    label, expression = sys.argv[1:]
    # These two POSIX ERE patterns use only the space character class.
    pattern = re.compile(expression.replace("[:space:]", r"\s"), re.IGNORECASE | re.ASCII)
    for path in sys.stdin.read().splitlines():
        if path and has_unexplained_match(path, label, pattern):
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
