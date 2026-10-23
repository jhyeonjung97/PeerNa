"""A signed-in visitor sees their own reviews, running and finished.

Written after the hosted list showed nothing at all for weeks: the rows the
listing built had no owner, so the owner filter dropped every one. A local run
lists ownerless rows, which is the only mode anyone had looked at.

    python3 -m tests.test_jobs
"""

import json
import sys
import tempfile
from pathlib import Path

from ai_peer_reviewer import serve


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        serve.JOB_DIR = Path(tmp)
        for job_id, owner, status in (("a", "me@uni.edu", "running"),
                                      ("b", "me@uni.edu", "done"),
                                      ("c", "you@uni.edu", "done"),
                                      ("d", None, "done")):
            (Path(tmp) / f"{job_id}.json").write_text(json.dumps(
                {"job_id": job_id, "owner": owner, "status": status, "filename": f"{job_id}.pdf"}))
        rows = serve._recent_jobs()
        hosted = {r["job_id"] for r in serve._visible_to("me@uni.edu", rows, hosted=True)}
        local = {r["job_id"] for r in serve._visible_to("local", rows, hosted=False)}
        failures = []
        if hosted != {"a", "b"}:
            failures.append(f"hosted viewer sees {sorted(hosted)}, expected a and b")
        if local != {"d"}:
            failures.append(f"local viewer sees {sorted(local)}, expected the ownerless d")
        if serve._visible_to(None, rows, hosted=True):
            failures.append("a signed-out visitor sees reviews")
    for f in failures:
        print("  " + f)
    print(f"  {len(failures)} job-listing failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
