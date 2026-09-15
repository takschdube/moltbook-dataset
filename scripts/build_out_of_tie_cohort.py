#!/usr/bin/env python3
"""Build an analysis table for out-of-tie replies.

The design this serves: does an account that earlier expressed a collective
affiliation later reply to accounts it has no prior reply tie with?

  baseline window   every reply observed here establishes a tie between the
                    replier and whoever they replied to
  roots window      posts created here are the occasions for a new reply
  response window   replies counted only if made within N hours of the root

A reply is out-of-tie when the replier and the root author share no tie from
the baseline. That claim only holds on threads whose comments were actually
retrieved, so the input must already be restricted to a completeness cohort;
on any other thread an absent tie may just be an absent fetch.

Ties are recorded undirected by default, with the directed case kept as a
separate column, because "has interacted before" and "has replied to before"
are different controls and the choice belongs to the analyst.

Usage:
    python scripts/build_out_of_tie_cohort.py reply_edges.csv out.csv \\
        --baseline 2026-02-05 2026-02-15 --roots 2026-02-15 2026-02-24 \\
        [--response-hours 48] [--identity identity_history.csv]
"""
import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def target_of(row, comment_author):
    """Who a comment is addressed to: its parent's author, else the poster."""
    parent = row["parent_comment_id"]
    return comment_author.get(parent) if parent else row["root_author_id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("edges_csv")
    ap.add_argument("out_csv")
    ap.add_argument("--baseline", nargs=2, metavar=("START", "END"), required=True)
    ap.add_argument("--roots", nargs=2, metavar=("START", "END"), required=True)
    ap.add_argument("--response-hours", type=int, default=48)
    ap.add_argument("--identity", help="identity_history.csv, to attach affiliation text")
    ap.add_argument("--identity-asof", default=None,
                    help="observe identity at or before this date (default: the roots "
                         "window start, so identity precedes the outcome). The archive "
                         "cannot observe identity before its first crawl, which may be "
                         "later than the baseline start.")
    ap.add_argument("--cohort", help="cohort.csv, so roots that drew no reply stay in the denominator")
    ap.add_argument("--classes", default="complete,never_had_comments")
    ap.add_argument("--roots-out", help="write a root-level summary here")
    args = ap.parse_args()

    b0, b1 = args.baseline
    r0, r1 = args.roots
    window = timedelta(hours=args.response_hours)

    # Identity has to precede the outcome, not the baseline: the baseline only
    # records which ties already existed. Defaulting to the roots start keeps
    # the ordering the design needs while staying inside what the archive saw.
    asof = args.identity_asof or r0
    identity = {}
    if args.identity:
        with open(args.identity, encoding="utf-8") as f:
            for row in sorted(csv.DictReader(f), key=lambda r: r["observed_at"]):
                if row["observed_at"][:10] <= asof:
                    identity[row["id"]] = row.get("description", "")
        print(f"identity text for {len(identity)} accounts as of {asof}", file=sys.stderr)
        if not identity:
            print("  no observations that early; the archive's first crawl is later",
                  file=sys.stderr)

    # Pass 1: index comment authors so a parent id resolves to a person.
    comment_author = {}
    with open(args.edges_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            comment_author[row["comment_id"]] = row["author_id"]
    print(f"indexed {len(comment_author)} comments", file=sys.stderr)

    # Pass 2: ties from the baseline window.
    undirected, directed = set(), set()
    with open(args.edges_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not (b0 <= row["created_at"][:10] < b1):
                continue
            a = row["author_id"]
            b = target_of(row, comment_author)
            if a and b and a != b:
                undirected.add(frozenset((a, b)))
                directed.add((a, b))
    print(f"baseline {b0}..{b1}: {len(undirected)} undirected ties", file=sys.stderr)

    # Pass 3: replies to roots created in the roots window, inside the response window.
    rows_out = []
    seen_roots = set()
    with open(args.edges_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            root_created = row["root_created_at"][:10]
            if not (r0 <= root_created < r1):
                continue
            seen_roots.add(row["root_post_id"])
            t_root, t_reply = parse_ts(row["root_created_at"]), parse_ts(row["created_at"])
            if not t_root or not t_reply or not (t_root <= t_reply <= t_root + window):
                continue
            replier, poster = row["author_id"], row["root_author_id"]
            if not replier or not poster or replier == poster:
                continue
            rows_out.append({
                "root_post_id": row["root_post_id"],
                "root_author_id": poster,
                "root_created_at": row["root_created_at"],
                "comment_id": row["comment_id"],
                "replier_id": replier,
                "replier_name": row["author_name"],
                "reply_created_at": row["created_at"],
                "hours_after_root": round((t_reply - t_root).total_seconds() / 3600, 3),
                "depth": row["depth"],
                "content_chars": row["content_chars"],
                "submolt": row["submolt"],
                "root_completeness": row["root_completeness"],
                "prior_tie_undirected": int(frozenset((replier, poster)) in undirected),
                "prior_tie_directed": int((replier, poster) in directed),
                "replier_identity_text": identity.get(replier, ""),
                "replier_identity_known": int(replier in identity),
            })

    # A root that drew no reply is an observation, not a missing row. The edge
    # file only carries comments, so without the cohort the denominator would
    # silently exclude every thread nobody answered.
    root_universe = {}
    if args.cohort:
        keep = {c.strip() for c in args.classes.split(",") if c.strip()}
        with open(args.cohort, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["class"] in keep and r0 <= row["created_at"][:10] < r1:
                    root_universe[row["post_id"]] = row
        print(f"roots in cohort window: {len(root_universe)} "
              f"({len(root_universe) - len(seen_roots)} drew no reply at all)", file=sys.stderr)

    if args.roots_out:
        by_root = defaultdict(list)
        for r in rows_out:
            by_root[r["root_post_id"]].append(r)
        ids = root_universe.keys() if root_universe else by_root.keys()
        summary = []
        for pid in ids:
            rs = by_root.get(pid, [])
            meta = root_universe.get(pid, {})
            summary.append({
                "root_post_id": pid,
                "root_author_id": rs[0]["root_author_id"] if rs else meta.get("author_id", ""),
                "root_created_at": rs[0]["root_created_at"] if rs else meta.get("created_at", ""),
                "completeness": rs[0]["root_completeness"] if rs else meta.get("class", ""),
                "replies_in_window": len(rs),
                "repliers": len({r["replier_id"] for r in rs}),
                "out_of_tie_replies": sum(1 for r in rs if not r["prior_tie_undirected"]),
                "out_of_tie_repliers": len({r["replier_id"] for r in rs
                                            if not r["prior_tie_undirected"]}),
            })
        with open(args.roots_out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            w.writeheader(); w.writerows(summary)
        drew_none = sum(1 for r in summary if r["replies_in_window"] == 0)
        print(f"root summary: {len(summary)} roots, {drew_none} with no reply in window "
              f"-> {args.roots_out}", file=sys.stderr)

    if not rows_out:
        print("no replies matched the windows", file=sys.stderr)
        return 1

    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)

    out_of_tie = sum(1 for r in rows_out if not r["prior_tie_undirected"])
    print(f"roots in window: {len(seen_roots)}", file=sys.stderr)
    print(f"replies within {args.response_hours}h: {len(rows_out)}", file=sys.stderr)
    print(f"  out of tie: {out_of_tie} ({100 * out_of_tie / len(rows_out):.1f}%)", file=sys.stderr)
    print(f"  with identity text: {sum(r['replier_identity_known'] for r in rows_out)}", file=sys.stderr)
    print(f"-> {args.out_csv}", file=sys.stderr)
    return 0


def _demo():
    ca = {"c1": "u-a"}
    # a reply nested under c1 is addressed to c1's author
    assert target_of({"parent_comment_id": "c1", "root_author_id": "u-p"}, ca) == "u-a"
    # a top-level comment is addressed to the poster
    assert target_of({"parent_comment_id": "", "root_author_id": "u-p"}, ca) == "u-p"
    t = parse_ts("2026-02-15T10:00:00+00:00")
    assert (parse_ts("2026-02-16T09:00:00Z") - t) < timedelta(hours=48)
    assert parse_ts("") is None and parse_ts("nonsense") is None
    print("ok")


if __name__ == "__main__":
    sys.exit(_demo() if "--demo" in sys.argv else main())
