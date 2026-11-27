"""Readability lints on a matched candidate (SPEC §9): the gate before integration.

`fail` findings keep a match out of integration. `flag` findings are allowed but listed for
human review. Comments and string literals are blanked before matching, so prose about a rule
never trips it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

LINT_VERSION = 1

_COMMENT_OR_STRING = re.compile(
    r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", re.DOTALL
)
_TYPE = r"(?:unsigned\s+|signed\s+|const\s+|volatile\s+|struct\s+|union\s+|enum\s+)*\w+\s*\**"
_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("fail", "inline asm", re.compile(r"\b(?:asm|__asm__?)\b")),
    ("fail", "#pragma", re.compile(r"^\s*#\s*pragma\b", re.MULTILINE)),
    ("fail", "do { } while (0) outside a macro", re.compile(r"\bwhile\s*\(\s*0\s*\)\s*;")),
    ("fail", "self-assignment", re.compile(r"(?<![\w.>])(\w+)\s*=\s*\1\s*;")),
    ("flag", "goto", re.compile(r"\bgoto\b")),
    ("flag", "volatile", re.compile(r"\bvolatile\b")),
    (
        "flag",
        "cast chain",
        re.compile(rf"\(\s*{_TYPE}\s*\)\s*\(\s*{_TYPE}\s*\)\s*\(\s*{_TYPE}\s*\)\s*\w"),
    ),
]


@dataclass
class LintResult:
    fail: list[str] = field(default_factory=list)
    flag: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fail

    def to_json(self) -> str:
        return json.dumps({"version": LINT_VERSION, "fail": self.fail, "flag": self.flag})


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


# Checked on the raw text: blanking would erase the header name.
_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*(?!"ctx\.h")\S', re.MULTILINE)


def lint(source: str) -> LintResult:
    code = _COMMENT_OR_STRING.sub(_blank, source)
    out = LintResult()
    for m in _INCLUDE.finditer(source):
        out.fail.append(
            f'#include other than "ctx.h" (line {source.count(chr(10), 0, m.start()) + 1})'
        )
    for severity, name, pattern in _RULES:
        for m in pattern.finditer(code):
            line = code.count("\n", 0, m.start()) + 1
            (out.fail if severity == "fail" else out.flag).append(f"{name} (line {line})")
    return out


def passes(lint_json: str) -> bool:
    """Whether a stored lint result lets the match be integrated."""
    try:
        data = json.loads(lint_json)
    except json.JSONDecodeError:
        return False
    return data.get("version") == LINT_VERSION and not data.get("fail")
