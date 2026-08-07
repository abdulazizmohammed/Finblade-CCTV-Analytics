"""What is tracked that an EC2 deployment does not need, and what must never be.

Deployment here is `git pull`, so everything tracked lands on the server. That
makes "what should we stop tracking" and "what should not go to EC2" the same
question.

Three severities:
  BLOCK   credential- or host-shaped content. Must not be in the repo at all.
  DROP    dev-machine only: it cannot run on EC2, or has no reason to.
  KEEP    the runtime, the tests, the docs, and the scripts a human is told to
          run on the server.

Read-only.
"""
import os
import re
import subprocess

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(REPO)

tracked = subprocess.run(["git", "ls-files"], capture_output=True,
                         text=True).stdout.split()

# --------------------------------------------------------------- BLOCK ----
# Real secrets, not the words. A test fixture using "hunter2" or a masked
# rtsp://***:*** is fine and must not be reported, or the check gets ignored.
SECRET = [
    (re.compile(r"rtsp://(?!\*{3})[^:/\s]+:(?!\*{3})[^@/\s]+@"), "rtsp url with credentials"),
    (re.compile(r"192\.168\.200\.\d+"), "internal camera IP"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws access key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]
ALLOW_SECRET_IN = ("tests/", "scripts/deploy_exclusions.py", "docs/")

# ---------------------------------------------------------------- DROP ----
# Grouped so the reason travels with the file.
DROP = {
    "local Postgres test rig — EC2 has sudo and uses apt": [
        "scripts/pg_local_install.sh", "scripts/check_pg_install.sh",
        "scripts/check_postgres_env.sh", "scripts/pg_conn.py",
        "scripts/install_pg_driver.sh", "scripts/conf_summary.sh",
    ],
    "RTSP clip replay — dev only; EC2 has real cameras": [
        "scripts/rtsp_demo.sh", "scripts/get_ffmpeg.sh",
        "scripts/rtsp_env_check.sh", "config/mediamtx.demo.yml",
    ],
    "git/push helpers — dev workstation only": [
        "scripts/push_with_gcm.sh", "scripts/push_auth_state.sh",
        "scripts/check_push_auth.sh", "scripts/git_state.sh",
        "scripts/rebase_onto_origin.sh", "scripts/check_divergence.sh",
        "scripts/prepush_check.sh", "scripts/verify_remote.sh",
    ],
    "GPU/CUDA probes — the test box is CPU": [
        "scripts/cuda_test.py", "scripts/probe_gpu.sh",
        "scripts/probe_nvidia.sh", "scripts/install_cuda_torch.sh",
        "scripts/bench_device.py", "scripts/bench_raw.py",
    ],
    "one-off investigations, already answered": [
        "scripts/audit_option1.py", "scripts/audit_option1b.py",
        "scripts/join_cost.py", "scripts/relational_demo.py",
        "scripts/schema_report.py", "scripts/compare_averages.py",
        "scripts/show_density_update.py", "scripts/time_backfill.py",
        "scripts/probe_confidence.py", "scripts/probe_clip.py",
        "scripts/doc_audit.sh", "scripts/repo_audit.py",
        "scripts/classify_scripts.py", "scripts/deploy_exclusions.py",
        "scripts/whats_running.sh", "scripts/list_cameras.py",
        "scripts/check_views.py", "scripts/live_postgres_check.py",
    ],
    "superseded per-slice verifiers (verify_p2..p9)": [
        f"scripts/verify_p{n}.sh" for n in range(2, 10)
    ],
    "scratch helpers": [
        "scripts/_dedupe.sh", "scripts/_loop.sh", "scripts/_uicam.sh",
        "scripts/kill_py.sh", "scripts/run_live.sh",
        "scripts/run_live_short.sh", "scripts/probe_env.sh",
        "scripts/probe_net.py", "scripts/check_app.py",
        "scripts/check_dash.py", "scripts/check_redbox.py",
        "scripts/check_annotate.py", "scripts/check_history_ui.py",
        "scripts/dump_alerts.py", "scripts/check_cam2.sh",
    ],
}

print("== BLOCK — must not be in the repo")
blocked = 0
for f in tracked:
    if not os.path.exists(f) or f.startswith(ALLOW_SECRET_IN):
        continue
    try:
        with open(f, errors="ignore") as fh:
            body = fh.read()
    except OSError:
        continue
    for pat, why in SECRET:
        m = pat.search(body)
        if m:
            print(f"  {f}: {why} -> {m.group(0)[:48]}")
            blocked += 1
            break
if not blocked:
    print("  none")

print("\n== DROP — tracked, but an EC2 box has no use for it")
total = 0
count = 0
for reason, files in DROP.items():
    present = [f for f in files if f in tracked]
    if not present:
        continue
    size = sum(os.path.getsize(f) for f in present if os.path.exists(f))
    total += size
    count += len(present)
    print(f"\n  {reason}  ({len(present)} files, {size / 1024:.0f} KB)")
    for f in present:
        print(f"      {f}")
print(f"\n  total: {count} files, {total / 1024:.0f} KB")

print("\n== evidence/ — tracked artifacts")
ev = [f for f in tracked if f.startswith("evidence/")]
for f in ev:
    size = os.path.getsize(f) if os.path.exists(f) else 0
    print(f"  {size / 1024:>8.0f} KB  {f}")
print("  (CLAUDE.md keeps contact_sheet.jpg + metrics.json as proof; the rest\n"
      "   is regenerated and already ignored)")

loose = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                       capture_output=True, text=True).stdout.split()
print(f"\n== UNTRACKED and NOT ignored: {len(loose)} files")
print("  `git add -A` would commit these. Needs a .gitignore rule:")
prefixes = sorted({os.path.dirname(f) or "(root)" for f in loose})
for p in prefixes[:12]:
    n = len([f for f in loose if (os.path.dirname(f) or "(root)") == p])
    print(f"    {p}/  ({n} files)")
