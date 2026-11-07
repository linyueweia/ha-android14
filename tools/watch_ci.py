#!/usr/bin/env python3
"""轮询 GitHub Actions 运行状态，结束后打印结论与失败步骤的日志尾部。"""
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = "linyueweia/ha-android14"
WORKFLOW = os.environ.get("WF", "dtb-build.yml")


ENV_PATH = os.environ.get("HERMES_ENV", r"D:\Hermes\hermes_data\.env")


def token():
    with open(ENV_PATH, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no token")


TOK = token()


def api(path):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": "token " + TOK, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    # 找最近一次该 workflow 的运行
    runs = api("/repos/%s/actions/workflows/%s/runs?per_page=1" % (REPO, WORKFLOW))
    run = runs["workflow_runs"][0]
    rid = run["id"]
    print("run %s status=%s" % (rid, run["status"]), flush=True)

    deadline = time.time() + 100 * 60
    last = None
    while time.time() < deadline:
        d = api("/repos/%s/actions/runs/%s" % (REPO, rid))
        st, cc = d["status"], d.get("conclusion")
        if st != last:
            print("[%s] status=%s conclusion=%s" % (time.strftime("%H:%M:%S"), st, cc), flush=True)
            last = st
        if st == "completed":
            jobs = api("/repos/%s/actions/runs/%s/jobs" % (REPO, rid))["jobs"]
            print("\n===== 步骤结论 =====", flush=True)
            failed = []
            for j in jobs:
                print("job: %s -> %s" % (j["name"], j["conclusion"]), flush=True)
                for s in j["steps"]:
                    mark = "OK  " if s["conclusion"] == "success" else "FAIL"
                    print("  %s %-52s %s" % (mark, s["name"], s["conclusion"]), flush=True)
                    if s["conclusion"] not in ("success", "skipped", None):
                        failed.append(j["id"])
            if failed:
                jid = failed[0]
                logs = subprocess.run(
                    ["curl", "-sL", "-H", "Authorization: token " + TOK,
                     "https://api.github.com/repos/%s/actions/jobs/%s/logs" % (REPO, jid)],
                    capture_output=True,
                ).stdout.decode("utf-8", "replace")
                tail = [l for l in logs.splitlines() if l.strip()][-80:]
                print("\n===== 失败步骤日志尾部 =====", flush=True)
                print("\n".join(tail), flush=True)
            return
        time.sleep(30)
    print("TIMEOUT waiting", flush=True)


if __name__ == "__main__":
    main()
