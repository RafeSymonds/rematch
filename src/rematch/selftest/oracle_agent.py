"""Fake `claude` that writes the known answer into candidate.c and runs ./check (SPEC §14).

Answers come from `REMATCH_ORACLE_ANSWERS` (a JSON file outside the sandbox: symbol -> source).
For the symbol in `REMATCH_ORACLE_TYPES_FOR`, it also prepends an unused provisional type, so
the provisional-types path is exercised without changing the code.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from rematch.selftest.fake_common import emit, init_event, log_invocation, result_event


def main() -> int:
    init_event("oracle")
    sandbox = Path.cwd()
    # The worker-visible TASK.md names the symbol; task.json lives outside the sandbox.
    first = (sandbox / "TASK.md").read_text().splitlines()[0]
    symbol = re.match(r"# Task: match (\S+) ", first).group(1)  # type: ignore[union-attr]
    answers = json.loads(Path(os.environ["REMATCH_ORACLE_ANSWERS"]).read_text())
    source = answers[symbol]
    if os.environ.get("REMATCH_ORACLE_TYPES_FOR") == symbol:
        addr = symbol.rsplit("_", 1)[-1]
        source = (
            "/* rematch:types */\n"
            f"typedef struct UnkStruct_{addr} {{\n    u8 unk_00[3];\n    u16 unk_04;\n"
            f"    u32 unk_08;\n}} UnkStruct_{addr};\n/* rematch:end-types */\n" + source
        )
    (sandbox / "candidate.c").write_text(source, encoding="utf-8")
    emit(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "running ./check"}]}}
    )
    out = subprocess.run(["./check"], cwd=sandbox, capture_output=True, text=True, timeout=600)
    log_invocation("oracle", {"symbol": symbol, "check": out.stdout.splitlines()[:1]})
    result_event(out.stdout.splitlines()[0] if out.stdout else "", 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
