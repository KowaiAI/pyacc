#!/usr/bin/env python3
"""
Check that every factual claim in the documentation is still true.

Documentation drifts. A number that was right when it was written becomes a
false claim the moment the code changes, and nobody notices because prose has
no tests. This script re-measures the things the README and docs assert and
fails if any of them no longer hold.

    python tests/audit_claims.py

Exit code is the number of stale claims, so CI can gate on it.
"""

import os
import re
import sys
import json
import shutil
import hashlib
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
FAILED = []


def check(label, ok, detail=""):
    ok = bool(ok)
    print("  [%s] %s%s" % ("OK " if ok else "BAD", label,
                           ("  -- " + detail) if detail and not ok else ""))
    if not ok:
        FAILED.append(label)
    return ok


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def build(args, out):
    path = os.path.join(ROOT, out)
    r = subprocess.run([PY, os.path.join(ROOT, "acc.py")] + args + ["-o", path],
                       capture_output=True, text=True, cwd=ROOT)
    return r, path


def main():
    readme = read("README.md")
    tool = read("docs/TOOLCHAIN.md")
    ver = read("docs/VERIFICATION.md")
    temps = []

    # -- line counts quoted in the docs ------------------------------------
    counts = {}
    for f in ("acc.py", "accld.py", "accrun.py", "accpp.py"):
        with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
            counts[f] = len(fh.readlines())
        claim = "| `%s` | %s |" % (f, format(counts[f], ","))
        check("README line count for %s (%d)" % (f, counts[f]),
              claim in readme)
    total = counts["acc.py"] + counts["accld.py"] + counts["accrun.py"]
    check("README toolchain total (%d)" % total,
          "%s lines, zero dependencies" % format(total, ",") in readme)
    check("TOOLCHAIN accld count (%d)" % counts["accld.py"],
          "| `accld.py` | %d |" % counts["accld.py"] in tool)

    # -- the size comparison, both compilers on one source -----------------
    _, acc_exe = build(["examples/same.c"], "_audit_acc.exe")
    temps.append(acc_exe)
    a = os.path.getsize(acc_exe)
    check("README acc binary size (%d)" % a,
          "**%s bytes**" % format(a, ",") in readme)

    # gcc's output size depends on which gcc built it, so the README's figure
    # can only be re-measured with the version the README names. Any other
    # gcc (a CI runner's, say) would "fail" a claim that is actually true.
    gcc = shutil.which("gcc")
    m = re.search(r"\| gcc ([\d.]+) \(", readme)
    quoted = m.group(1) if m else None
    have = None
    if gcc:
        have = subprocess.run([gcc, "-dumpfullversion"], capture_output=True,
                              text=True).stdout.strip() or None
    if not gcc:
        print("  [--] gcc not on PATH; gcc size not re-measured")
    elif quoted and have != quoted:
        print("  [--] this is gcc %s, the README quotes gcc %s; a different "
              "compiler gives a different size, so not re-measured"
              % (have, quoted))
    else:
        gcc_exe = os.path.join(ROOT, "_audit_gcc.exe")
        r = subprocess.run([gcc, "-o", gcc_exe,
                            os.path.join(ROOT, "examples/same.c")],
                           capture_output=True, text=True)
        if r.returncode == 0:
            temps.append(gcc_exe)
            g = os.path.getsize(gcc_exe)
            check("README gcc binary size (%d)" % g,
                  "%s bytes" % format(g, ",") in readme)
        else:
            check("gcc builds examples/same.c", False, r.stderr.strip()[:200])

    # -- the DLL banner quoted in the README -------------------------------
    r, dll = build(["examples/mathlib.c", "--dll"], "_audit_mathlib.dll")
    temps.append(dll)
    # compare the measured figures, not the path acc happened to be given
    m = re.search(r"\((\d+) bytes, (\d+) instructions, (\d+) code bytes\)",
                  r.stdout)
    figures = m.group(0) if m else "<no banner>"
    check("README DLL figures match real output", m and figures in readme,
          figures)

    # -- the test suite ----------------------------------------------------
    r = subprocess.run([PY, os.path.join(HERE, "run_tests.py"), "--json"],
                       capture_output=True, text=True, cwd=ROOT)
    d = json.loads(r.stdout)
    n = d["total"]
    check("suite passes (%d/%d, %d skipped)"
          % (d["passed"], n, d.get("skipped", 0)), d["failed"] == 0)
    check("README badge count (%d)" % n,
          "tests-%d%%2F%d%%20passing" % (n, n) in readme)
    check("README quotes the suite total", "%d of %d passed" % (n, n) in readme)
    check("VERIFICATION quotes the suite total",
          "%d of %d passed" % (n, n) in ver)

    # -- properties the docs assert ----------------------------------------
    _, d1 = build(["examples/features.c"], "_audit_d1.exe")
    _, d2 = build(["examples/features.c"], "_audit_d2.exe")
    temps += [d1, d2]
    h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
    check("builds are reproducible", h(d1) == h(d2))

    check("acc imports nothing outside the stdlib or this project",
          not re.search(r"^\s*import\s+(?!os|re|sys|json|struct|shutil|hashlib|"
                        r"tempfile|subprocess|ctypes|accpp|accld|accrun)\w+",
                        read("acc.py"), re.M))

    # -- links and images --------------------------------------------------
    for m in set(re.findall(r"\]\(([^)]+\.md)\)", readme)):
        if not m.startswith("http"):
            check("README link -> %s" % m,
                  os.path.exists(os.path.join(ROOT, m)))
    for m in set(re.findall(r'src="([^"]+)"', readme)):
        if not m.startswith("http"):
            check("README image -> %s" % m,
                  os.path.exists(os.path.join(ROOT, m)))

    for p in temps:
        try:
            os.remove(p)
        except OSError:
            pass

    print("\n  %d stale claim(s)" % len(FAILED))
    return len(FAILED)


if __name__ == "__main__":
    sys.exit(main())
