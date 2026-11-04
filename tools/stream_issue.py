#!/usr/bin/env python3
"""Keep one GitHub issue, "Dead streams", in step with the nightly stream check.

Run by .github/workflows/stream-check.yml after check_stations.py. Lists the
stations that have failed at least two checks in a row (the same rule the
website uses to paint them blue). Opens the issue when the first one dies,
comments only when the list changes (so GitHub emails you about changes, not
every night), and closes it when everything plays again.

Usage: python3 tools/stream_issue.py stream-health.json   (needs the gh CLI + GH_TOKEN)
"""
import hashlib, json, subprocess, sys

TITLE = "Dead streams"
MIN_CHECKS = 2


def gh(*args, input=None):
    return subprocess.run(["gh", *args], input=input, text=True, check=True,
                          capture_output=True).stdout


def main() -> int:
    report = json.load(open(sys.argv[1], encoding="utf-8"))
    dead, seen = [], set()
    for e in report["stations"]:
        if e["status"] == "dead" and e.get("checks_dead", 1) >= MIN_CHECKS and (e["name"], e["url"]) not in seen:
            seen.add((e["name"], e["url"]))
            dead.append(e)
    dead.sort(key=lambda e: e["name"].lower())
    names = sorted({e["name"] for e in dead})
    sig = hashlib.sha1("\n".join(names).encode()).hexdigest()[:12]

    issues = json.loads(gh("issue", "list", "--state", "open", "--limit", "100",
                           "--json", "number,title,body"))
    issue = next((i for i in issues if i["title"] == TITLE), None)
    old_names = set()
    if issue and "<!-- dead:" in issue["body"]:
        old_names = set(json.loads(issue["body"].split("<!-- dead:", 1)[1].split("-->", 1)[0]))

    if not dead:
        if issue:
            gh("issue", "close", str(issue["number"]), "--comment",
               "✅ Every station plays again — closing.")
        print("no dead streams")
        return 0

    rows = "\n".join(f"| {e['name']} | {e.get('country') or '—'} | {e['detail']} | "
                     f"{(e.get('dead_since') or '')[:10]} | `{e['url'] or '—'}` |" for e in dead)
    body = (f"The nightly stream check found **{len(names)} station(s)** that failed "
            f"{MIN_CHECKS}+ checks in a row. They show as blue dots on https://oreloj.github.io/.\n\n"
            f"| Station | Country | Problem | Dead since | Stream |\n|---|---|---|---|---|\n{rows}\n\n"
            f"Last check: {report['checked_at']}. To fix one: find its new stream address and update it "
            f"in the globe's control page (or ask Claude), or delete the station.\n\n"
            f"<!-- dead:{json.dumps(names)}--><!-- sig:{sig} -->")

    if not issue:
        gh("issue", "create", "--title", TITLE, "--body-file", "-", input=body)
        print(f"opened issue: {len(names)} dead")
        return 0
    gh("issue", "edit", str(issue["number"]), "--body-file", "-", input=body)
    new, back = sorted(set(names) - old_names), sorted(old_names - set(names))
    if new or back:
        msg = []
        if new:
            msg.append("🔵 Newly dead: " + ", ".join(new))
        if back:
            msg.append("✅ Playing again: " + ", ".join(back))
        gh("issue", "comment", str(issue["number"]), "--body", "\n\n".join(msg))
    print(f"updated issue: {len(names)} dead, {len(new)} new, {len(back)} back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
