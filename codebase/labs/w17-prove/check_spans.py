#!/usr/bin/env python3
"""Prove the deployed agent's OTel spans are queryable — the auditor's path.

The GenAI Observability console renders from the `aws/spans` log group; on a
locked-down account its list views can spin while the data is perfectly
queryable. This is the dependable check, and the one the runbooks teach.

    python3 codebase/labs/w17-prove/check_spans.py [--hours 24]
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    a = ap.parse_args()

    logs = boto3.client("logs", region_name="us-east-1")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=a.hours)
    q = logs.start_query(
        logGroupName="aws/spans",
        startTime=int(start.timestamp()), endTime=int(end.timestamp()),
        queryString=("fields @timestamp, name, attributes.session.id "
                     "| sort @timestamp desc | limit 10"))
    qid = q["queryId"]
    print(f"StartQuery ok (queryId={qid}), window={a.hours}h")
    for _ in range(30):
        r = logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled"):
            break
        time.sleep(1.5)
    rows = r.get("results", [])
    print(f"status={r['status']} rows={len(rows)}")
    for row in rows:
        d = {c["field"]: c["value"] for c in row}
        print(f"  {d.get('@timestamp')} | {d.get('name')} | session={d.get('attributes.session.id')}")
    if rows:
        print(f"SPANS FOUND: {len(rows)} span(s) in aws/spans")
        return 0
    print("no spans in the window (deploy+invoke a runtime, or widen --hours)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
