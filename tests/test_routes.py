"""Every path the page calls must exist on the server.

Written after /api/estimate was deleted by accident: it sat between two routes
being removed and went with them. Nothing failed at import, nothing failed at
startup, and the server answered with an HTML 404 that the page tried to parse
as JSON — so the only symptom was `SyntaxError: Unexpected token '<'` in the
browser, several steps from the cause.

    python3 -m tests.test_routes
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVE = ROOT / "ai_peer_reviewer" / "serve.py"
PAGE = ROOT / "ai_peer_reviewer" / "web" / "index.html"


def called() -> set[str]:
    """Paths the page asks for, as literals in fetch() and post()."""
    text = PAGE.read_text()
    found = set(re.findall(r'(?:fetch|post)\(\s*["\'](/[^"\'?]+)', text))
    # Built from a variable at the call site; the prefix is what the server routes on.
    found |= {
        m + "/" for m in re.findall(r'["\'](/api/[a-z]+)/\$\{', text)
    }
    return found


def served() -> tuple[set[str], set[str]]:
    text = SERVE.read_text()
    exact = set(re.findall(r'path == "(/[^"]*)"', text))
    prefix = set(re.findall(r'path\.startswith\("(/[^"]*)"\)', text))
    return exact, prefix


def main() -> int:
    exact, prefix = served()
    missing = [
        path for path in sorted(called())
        if path not in exact and not any(path.startswith(p) for p in prefix)
    ]
    for path in missing:
        print(f"  the page calls {path} and the server does not answer it")
    print(f"  {len(called())} paths called, {len(missing)} unanswered")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
