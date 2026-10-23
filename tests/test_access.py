"""Who may use a server that names its accounts.

The development server names one account. Two ways it could serve more: an
address not on the list, and the Google settings missing, which on an
unrestricted server means "local run, everyone is the operator". Both must
leave the visitor signed out.

    python3 -m tests.test_access
"""

import os
import sys

from ai_peer_reviewer import auth, serve


class _Request:
    headers: dict = {}


def main() -> int:
    failures = []
    saved = {k: os.environ.get(k) for k in (auth.ONLY, auth.CLIENT_ID, auth.CLIENT_SECRET)}
    try:
        os.environ[auth.ONLY] = "Me@Example.org, other@example.org"
        if not auth.admitted("me@example.org"):
            failures.append("a listed address, in another case, is turned away")
        if auth.admitted("stranger@example.org"):
            failures.append("an unlisted address is admitted")
        if auth.admitted(None):
            failures.append("nobody is admitted")

        serve.HOSTED = True
        for key in (auth.CLIENT_ID, auth.CLIENT_SECRET):
            os.environ.pop(key, None)
        if serve.Handler._viewer(_Request()) is not None:
            failures.append("a restricted server without Google settings lets everyone in")

        os.environ.pop(auth.ONLY)
        if not auth.admitted("anyone@example.org"):
            failures.append("with no list, a signed-in address is turned away")
    finally:
        serve.HOSTED = False
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    for f in failures:
        print("  " + f)
    print(f"  {len(failures)} access failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
