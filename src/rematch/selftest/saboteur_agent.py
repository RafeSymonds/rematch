"""Fake `claude` that tampers with read-only inputs and writes outside the sandbox (SPEC §14).

Each attempt and its result go to the invocation log. The harness must classify the attempt
`invalid`, and the vanilla worktree (`REMATCH_SABOTEUR_VANILLA`, a file in it) must be unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

from rematch.selftest.fake_common import init_event, log_invocation, result_event


def _chmod_write(path: Path, text: str) -> None:
    path.chmod(0o644)
    with path.open("a") as f:
        f.write(text)


def _try(action: str, fn: object) -> str:
    try:
        assert callable(fn)
        fn()
        return f"{action}: done"
    except OSError as exc:
        return f"{action}: denied ({exc.__class__.__name__})"


def main() -> int:
    init_event("saboteur")
    sandbox = Path.cwd()
    ctx = sandbox / "ctx.h"
    results = [
        _try("write ctx.h", lambda: ctx.open("a").write("\n#define CHEAT 1\n")),
        _try("chmod+write ctx.h", lambda: _chmod_write(ctx, "\nint cheat;\n")),
        _try("write target.s", lambda: (sandbox / "target.s").write_text("bx lr\n")),
    ]
    vanilla = os.environ.get("REMATCH_SABOTEUR_VANILLA")
    if vanilla:
        path = Path(vanilla)
        results.append(_try("write vanilla file", lambda: path.open("a").write("\n// sabotage\n")))
        results.append(
            _try("create file in vanilla", lambda: (path.parent / "sabotage.c").write_text("x"))
        )
    log_invocation("saboteur", {"results": results})
    result_event("done", 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
