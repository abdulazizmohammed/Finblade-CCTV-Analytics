"""What is in git that a deployed EC2 box does not need?

Three questions, answered from the repo rather than from memory:
  1. What is tracked and large?
  2. What is tracked but only ever used on a dev machine?
  3. What is untracked and NOT ignored — i.e. would be swept in by `git add -A`?

Read-only. Prints; changes nothing.
"""
import os
import subprocess
import sys
from collections import defaultdict

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(REPO)


def git(*args):
    out = subprocess.run(["git", *args], capture_output=True, text=True)
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


tracked = git("ls-files")
print(f"tracked files: {len(tracked)}")

sizes = {}
for f in tracked:
    try:
        sizes[f] = os.path.getsize(f)
    except OSError:
        sizes[f] = 0
total = sum(sizes.values())
print(f"tracked size : {total / 1e6:.1f} MB\n")

print("== 15 largest tracked files")
for f, n in sorted(sizes.items(), key=lambda kv: -kv[1])[:15]:
    print(f"  {n / 1e6:>8.2f} MB  {f}")

print("\n== tracked size by top-level directory")
by_dir = defaultdict(int)
count = defaultdict(int)
for f, n in sizes.items():
    top = f.split("/")[0] if "/" in f else "(root)"
    by_dir[top] += n
    count[top] += 1
for d, n in sorted(by_dir.items(), key=lambda kv: -kv[1]):
    print(f"  {n / 1e6:>8.2f} MB  {count[d]:>4} files  {d}")

# Anything the runtime imports is needed. Everything under scripts/ that is not
# referenced by a service, a doc or another script is a candidate for removal.
print("\n== scripts/ — referenced anywhere outside scripts/?")
runtime_dirs = ("services", "finblade", "web", "tools", "docs", "integrations")
haystack = ""
for d in runtime_dirs:
    for root, _dirs, files in os.walk(d):
        if "__pycache__" in root:
            continue
        for fn in files:
            try:
                with open(os.path.join(root, fn), errors="ignore") as fh:
                    haystack += fh.read()
            except OSError:
                pass

scripts = [f for f in tracked if f.startswith("scripts/")]
unreferenced = []
for s in scripts:
    base = os.path.basename(s)
    if base in haystack or s in haystack:
        continue
    unreferenced.append(s)
print(f"  {len(scripts)} tracked, {len(unreferenced)} not referenced by "
      f"services/finblade/web/tools/docs/integrations")

print("\n== untracked and NOT ignored (git add -A would commit these)")
loose = git("ls-files", "--others", "--exclude-standard")
if loose:
    big = sorted(((os.path.getsize(f) if os.path.exists(f) else 0, f)
                  for f in loose), reverse=True)
    for n, f in big[:20]:
        print(f"  {n / 1e6:>8.2f} MB  {f}")
    print(f"  ({len(loose)} files, "
          f"{sum(n for n, _ in big) / 1e6:.1f} MB total)")
else:
    print("  none — .gitignore covers everything present")

print("\n== ignored paths that exist on disk (correctly excluded)")
ignored = git("ls-files", "--others", "--ignored", "--exclude-standard",
              "--directory")
shown = 0
for f in ignored:
    path = f.rstrip("/")
    if not os.path.exists(path):
        continue
    if os.path.isdir(path):
        n = sum(os.path.getsize(os.path.join(r, x))
                for r, _d, fs in os.walk(path) for x in fs
                if os.path.exists(os.path.join(r, x)))
    else:
        n = os.path.getsize(path)
    if n > 1e6:
        print(f"  {n / 1e6:>8.1f} MB  {f}")
        shown += 1
    if shown > 14:
        break
