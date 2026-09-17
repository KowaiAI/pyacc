#!/usr/bin/env python3
"""
acc test suite.

Every case compiles real C with acc, executes the resulting binary, and
compares stdout and the exit code against a hand-computed expected value.
Nothing is mocked and no result is asserted to be "whatever the compiler
produced" -- each expectation was worked out from the C semantics first.

    python tests/run_tests.py           run everything
    python tests/run_tests.py -v        show the output of each case
    python tests/run_tests.py --json    machine-readable results

Exit code is the number of failures, so CI can gate on it.
"""

import os
import sys
import json
import shutil
import struct
import hashlib
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ACC = os.path.join(ROOT, "acc.py")
ACCLD = os.path.join(ROOT, "accld.py")
ACCRUN = os.path.join(ROOT, "accrun.py")
PY = sys.executable

RESULTS = []


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def record(group, name, ok, detail="", got="", want=""):
    """ok is True, False, or None meaning "could not be attempted here"."""
    RESULTS.append({"group": group, "name": name,
                    "ok": None if ok is None else bool(ok),
                    "detail": detail, "got": got, "want": want})
    return ok


# ---------------------------------------------------------------------------
# programs whose output was derived by hand, then checked against the compiler
# ---------------------------------------------------------------------------

PROGRAMS = [
    {
        "name": "arithmetic and precedence",
        "src": """
int main(void) {
    printf("%d\\n", 2 + 3 * 4);
    printf("%d\\n", (2 + 3) * 4);
    printf("%d\\n", 100 / 7);
    printf("%d\\n", 100 % 7);
    printf("%d\\n", -5 + 3);
    printf("%d\\n", 2 * 3 + 4 * 5 - 6 / 2);
    return 0;
}
""",
        "out": "14\n20\n14\n2\n-2\n23\n",
        "exit": 0,
    },
    {
        "name": "signed division truncates toward zero",
        "src": """
int main(void) {
    printf("%d %d\\n", -7 / 2, -7 % 2);
    printf("%d %d\\n", 7 / -2, 7 % -2);
    printf("%d %d\\n", -7 / -2, -7 % -2);
    return 0;
}
""",
        # C99 requires truncation toward zero, so -7/2 is -3 and -7%2 is -1
        "out": "-3 -1\n-3 1\n3 -1\n",
        "exit": 0,
    },
    {
        "name": "control flow",
        "src": """
int main(void) {
    int total = 0;
    for (int i = 0; i < 10; i++) {
        if (i % 3 == 0) continue;
        if (i == 8) break;
        total += i;
    }
    printf("%d\\n", total);
    int n = 5;
    while (n > 0) { printf("%d", n); n--; }
    printf("\\n");
    return 0;
}
""",
        # i = 1,2,4,5,7 contribute; 3,6,9 skipped; loop breaks at 8
        "out": "19\n54321\n",
        "exit": 0,
    },
    {
        "name": "recursion and call depth",
        "src": """
int fib(int n) {
    if (n < 2) return n;
    return fib(n - 1) + fib(n - 2);
}
int ack(int m, int n) {
    if (m == 0) return n + 1;
    if (n == 0) return ack(m - 1, 1);
    return ack(m - 1, ack(m, n - 1));
}
int main(void) {
    printf("%d\\n", fib(20));
    printf("%d\\n", ack(2, 3));
    return 0;
}
""",
        "out": "6765\n9\n",
        "exit": 0,
    },
    {
        "name": "mutual recursion",
        "src": """
int is_odd(int n);
int is_even(int n) {
    if (n == 0) return 1;
    return is_odd(n - 1);
}
int is_odd(int n) {
    if (n == 0) return 0;
    return is_even(n - 1);
}
int main(void) {
    printf("%d %d %d %d\\n", is_even(10), is_odd(10), is_even(7), is_odd(7));
    return 0;
}
""",
        "out": "1 0 0 1\n",
        "exit": 0,
    },
    {
        "name": "short-circuit evaluation",
        "src": """
int calls = 0;
int bump(void) { calls++; return 1; }
int main(void) {
    if (0 && bump()) { }
    printf("%d\\n", calls);
    if (1 || bump()) { }
    printf("%d\\n", calls);
    if (1 && bump()) { }
    printf("%d\\n", calls);
    if (0 || bump()) { }
    printf("%d\\n", calls);
    return 0;
}
""",
        # the right operand must only run when the left cannot decide
        "out": "0\n0\n1\n2\n",
        "exit": 0,
    },
    {
        "name": "increment and decrement semantics",
        "src": """
int main(void) {
    int i = 5;
    int a = i++;
    printf("%d %d\\n", a, i);
    int b = ++i;
    printf("%d %d\\n", b, i);
    int c = i--;
    printf("%d %d\\n", c, i);
    int d = --i;
    printf("%d %d\\n", d, i);
    return 0;
}
""",
        # postfix yields the old value, prefix the new one. Each result is
        # bound to its own variable first: putting i++ and i in the same call
        # would be undefined behaviour, not a compiler test.
        "out": "5 6\n7 7\n7 6\n5 5\n",
        "exit": 0,
    },
    {
        "name": "argument evaluation order is right to left",
        "src": """
int seq = 0;
int mark(int n) { seq = seq * 10 + n; return n; }
int sink(int a, int b, int c) { return a + b + c; }
int main(void) {
    int total = sink(mark(1), mark(2), mark(3));
    printf("%d %d\\n", total, seq);
    return 0;
}
""",
        # C leaves argument evaluation order unspecified. acc evaluates right
        # to left, because popping arguments in that order lands them exactly
        # where the Win64 convention wants them. This test pins that choice so
        # a future change to the calling sequence cannot alter it silently.
        "out": "6 321\n",
        "exit": 0,
    },
    {
        "name": "pointers and address-of",
        "src": """
int main(void) {
    int x = 10;
    int *p = &x;
    *p = *p * 4;
    printf("%d\\n", x);
    int y = 3;
    int *q = &y;
    *q = *p + *q;
    printf("%d %d\\n", x, y);
    return 0;
}
""",
        "out": "40\n40 43\n",
        "exit": 0,
    },
    {
        "name": "pointer arithmetic scales by element size",
        "src": """
int main(void) {
    int *buf = malloc(8 * 8);
    for (int i = 0; i < 8; i++) {
        *(buf + i) = i * 10;
    }
    printf("%d %d %d\\n", *buf, *(buf + 3), *(buf + 7));
    int *end = buf + 8;
    printf("%d\\n", end - buf);
    char *s = "abcdef";
    printf("%c%c%c\\n", *s, *(s + 2), *(s + 5));
    free(buf);
    return 0;
}
""",
        # int* steps by 8, char* steps by 1, and ptr-ptr divides by the size.
        # s[0]=a s[2]=c s[5]=f
        "out": "0 30 70\n8\nacf\n",
        "exit": 0,
    },
    {
        "name": "globals and nested scope shadowing",
        "src": """
int counter = 100;
int main(void) {
    int counter = 1;
    {
        int counter = 2;
        printf("%d\\n", counter);
    }
    printf("%d\\n", counter);
    return 0;
}
""",
        "out": "2\n1\n",
        "exit": 0,
    },
    {
        "name": "C library calls",
        "src": """
int main(void) {
    printf("%d\\n", strlen("compiler"));
    printf("%d\\n", abs(-42));
    printf("%d\\n", atoi("1234"));
    char *buf = malloc(16);
    memset(buf, 65, 5);
    *(buf + 5) = 0;
    printf("%s\\n", buf);
    free(buf);
    putchar(79); putchar(75); putchar(10);
    return 0;
}
""",
        "out": "8\n42\n1234\nAAAAA\nOK\n",
        "exit": 0,
    },
    {
        "name": "more than four call arguments (stack passing)",
        "src": """
int main(void) {
    printf("%d %d %d %d %d %d %d\\n", 1, 2, 3, 4, 5, 6, 7);
    printf("%s=%d %s=%d\\n", "a", 10, "b", 20);
    return 0;
}
""",
        # args 5+ go above the 32-byte shadow space
        "out": "1 2 3 4 5 6 7\na=10 b=20\n",
        "exit": 0,
    },
    {
        "name": "comparison operators",
        "src": """
int main(void) {
    printf("%d%d%d%d%d%d\\n", 1 < 2, 2 < 1, 2 <= 2, 3 > 4, 4 >= 4, 5 == 5);
    printf("%d%d\\n", 5 != 5, !0);
    printf("%d %d\\n", -1 < 0, 0 - 1 < 0);
    return 0;
}
""",
        "out": "101011\n01\n1 1\n",
        "exit": 0,
    },
    {
        "name": "exit code propagation",
        "src": """
int main(void) {
    printf("done\\n");
    return 42;
}
""",
        "out": "done\n",
        "exit": 42,
    },
    {
        "name": "prime sieve by trial division",
        "src": """
int is_prime(int n) {
    if (n < 2) return 0;
    for (int d = 2; d * d <= n; d++) {
        if (n % d == 0) return 0;
    }
    return 1;
}
int main(void) {
    int count = 0;
    int sum = 0;
    for (int i = 0; i < 1000; i++) {
        if (is_prime(i)) { count++; sum += i; }
    }
    printf("%d %d\\n", count, sum);
    return 0;
}
""",
        # 168 primes below 1000, summing to 76127
        "out": "168 76127\n",
        "exit": 0,
    },
]


# ---------------------------------------------------------------------------

def test_programs(tmp, verbose):
    for case in PROGRAMS:
        base = case["name"].replace(" ", "_").replace("(", "").replace(")", "")
        cpath = os.path.join(tmp, base + ".c")
        epath = os.path.join(tmp, base + ".exe")
        with open(cpath, "w", encoding="utf-8") as fh:
            fh.write(case["src"].strip() + "\n")

        r = run([PY, ACC, cpath, "-o", epath])
        if r.returncode != 0:
            record("programs", case["name"], False,
                   "compile failed", r.stderr.strip()[:400])
            continue

        r = run([PY, ACCRUN, epath])
        ok_out = r.stdout == case["out"]
        ok_exit = r.returncode == case["exit"]
        if verbose:
            sys.stderr.write("--- %s ---\n%s" % (case["name"], r.stdout))
        record("programs", case["name"], ok_out and ok_exit,
               "" if (ok_out and ok_exit) else
               ("stdout mismatch" if not ok_out else
                "exit code %d, expected %d" % (r.returncode, case["exit"])),
               repr(r.stdout), repr(case["out"]))

        # Run the same binary on the real processor and require it to agree
        # with the interpreter. Some machines refuse to launch freshly built
        # unsigned binaries (Smart App Control, WinError 4551); that is an OS
        # policy, not a compiler failure, so it is reported as "skipped".
        try:
            n = subprocess.run([epath], capture_output=True, text=True,
                               timeout=120)
        except OSError as e:
            record("native cpu", case["name"], None,
                   "OS refused to launch the binary: %s" % e)
            continue
        except subprocess.TimeoutExpired:
            record("native cpu", case["name"], False, "timed out")
            continue
        agree = (n.stdout == r.stdout and n.returncode == r.returncode)
        correct = (n.stdout == case["out"] and n.returncode == case["exit"])
        record("native cpu", case["name"], agree and correct,
               "" if (agree and correct) else
               ("cpu and interpreter disagree" if not agree
                else "wrong result on cpu"),
               repr(n.stdout), repr(case["out"]))


def test_dll(tmp, verbose):
    src = """
int add(int a, int b) { return a + b; }
int mul(int a, int b) { return a * b; }
int factorial(int n) { if (n <= 1) return 1; return n * factorial(n - 1); }
int sum_to(int n) { int t = 0; for (int i = 1; i <= n; i++) t += i; return t; }
"""
    cpath = os.path.join(tmp, "lib.c")
    dpath = os.path.join(tmp, "lib.dll")
    with open(cpath, "w", encoding="utf-8") as fh:
        fh.write(src.strip() + "\n")

    r = run([PY, ACC, cpath, "--dll", "-o", dpath])
    if not record("dll", "build a DLL", r.returncode == 0, r.stderr.strip()[:300]):
        return

    import ctypes
    try:
        lib = ctypes.CDLL(dpath)
    except OSError as e:
        record("dll", "Windows loader accepts the DLL", False, str(e)[:200])
        return
    record("dll", "Windows loader accepts the DLL", True)

    expect = {"add": ((17, 25), 42), "mul": ((6, 7), 42),
              "factorial": ((10,), 3628800), "sum_to": ((100,), 5050)}
    for fn, (args, want) in expect.items():
        try:
            f = getattr(lib, fn)
        except AttributeError:
            record("dll", "export %s" % fn, False, "not exported")
            continue
        f.restype = ctypes.c_longlong
        f.argtypes = [ctypes.c_longlong] * len(args)
        got = f(*args)
        record("dll", "%s%s == %d" % (fn, args, want), got == want,
               "" if got == want else "got %d" % got, str(got), str(want))


def test_linker(tmp, verbose):
    util = """
int call_count = 0;
int fib(int n) {
    call_count++;
    if (n < 2) return n;
    return fib(n - 1) + fib(n - 2);
}
int gcd(int a, int b) {
    while (b != 0) { int t = b; b = a % b; a = t; }
    return a;
}
"""
    prog = """
int fib(int n);
int gcd(int a, int b);
extern int call_count;
int main(void) {
    int f = fib(15);
    int calls = call_count;
    printf("%d %d %d\\n", f, calls, gcd(252, 105));
    return 0;
}
"""
    up = os.path.join(tmp, "util.c")
    pp = os.path.join(tmp, "prog.c")
    uo = os.path.join(tmp, "util.obj")
    po = os.path.join(tmp, "prog.obj")
    ex = os.path.join(tmp, "linked.exe")
    for path, text in ((up, util), (pp, prog)):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.strip() + "\n")

    ok = True
    for src, obj in ((up, uo), (pp, po)):
        r = run([PY, ACC, "-c", src, "-o", obj])
        ok &= record("linker", "compile %s to COFF" % os.path.basename(src),
                     r.returncode == 0, r.stderr.strip()[:300])
    if not ok:
        return

    r = run([PY, ACCLD, uo, po, "-o", ex])
    if not record("linker", "link two objects", r.returncode == 0,
                  r.stderr.strip()[:300]):
        return

    r = run([PY, ACCRUN, ex])
    # fib(15) = 610; the call count is 2*F(16)-1 = 1973; gcd(252,105) = 21
    want = "610 1973 21\n"
    record("linker", "cross-object calls and shared global",
           r.stdout == want, "", repr(r.stdout), repr(want))

    # linking only the half that lacks the definitions must fail loudly
    r = run([PY, ACCLD, po, "-o", os.path.join(tmp, "broken.exe")])
    record("linker", "undefined reference is reported",
           r.returncode == 1 and "undefined reference" in r.stderr,
           r.stderr.strip()[:200])

    # selective exports
    r = run([PY, ACC, "-c", up, "-o", uo])
    r = run([PY, ACCLD, uo, "--dll", "-o", os.path.join(tmp, "sel.dll"),
             "--export", "gcd"])
    if r.returncode == 0:
        import ctypes
        lib = ctypes.CDLL(os.path.join(tmp, "sel.dll"))
        lib.gcd.restype = ctypes.c_longlong
        lib.gcd.argtypes = [ctypes.c_longlong] * 2
        got = lib.gcd(252, 105)
        record("linker", "linker-built DLL export works", got == 21,
               "", str(got), "21")
        try:
            lib.fib
            record("linker", "unexported symbol stays hidden", False,
                   "fib was exported anyway")
        except AttributeError:
            record("linker", "unexported symbol stays hidden", True)
    else:
        record("linker", "linker builds a DLL", False, r.stderr.strip()[:200])


def test_preprocessor(tmp, verbose):
    hdr = os.path.join(tmp, "defs.h")
    with open(hdr, "w", encoding="utf-8") as fh:
        fh.write("#pragma once\n"
                 "#define SCALE 10\n"
                 "#define DOUBLE(x) ((x) + (x))\n"
                 "int from_header(void);\n")

    src = os.path.join(tmp, "pp.c")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write('#include "defs.h"\n'
                 '#define NAME "accpp"\n'
                 '#if SCALE > 5\n'
                 '#define BIG 1\n'
                 '#else\n'
                 '#define BIG 0\n'
                 '#endif\n'
                 'int from_header(void) { return DOUBLE(21); }\n'
                 'int main(void) {\n'
                 '    printf("%s %d %d %d\\n", NAME, SCALE, BIG, from_header());\n'
                 '#ifdef RELEASE\n'
                 '    printf("release\\n");\n'
                 '#else\n'
                 '    printf("debug\\n");\n'
                 '#endif\n'
                 '    return 0;\n'
                 '}\n')

    # the default must not change: # lines are skipped, so NAME is unknown
    r = run([PY, ACC, src, "-o", os.path.join(tmp, "nopp.exe")])
    record("preprocessor", "default leaves # lines alone",
           r.returncode == 1, "should fail without --pp")

    exe = os.path.join(tmp, "pp.exe")
    r = run([PY, ACC, "--pp", src, "-o", exe])
    if not record("preprocessor", "--pp compiles the program",
                  r.returncode == 0, r.stderr.strip()[:300]):
        return
    r = run([PY, ACCRUN, exe])
    want = "accpp 10 1 42\ndebug\n"
    record("preprocessor", "include, macros, #if and #ifdef all expand",
           r.stdout == want, "", repr(r.stdout), repr(want))

    exe2 = os.path.join(tmp, "pp2.exe")
    r = run([PY, ACC, "--pp", "-D", "RELEASE", src, "-o", exe2])
    r = run([PY, ACCRUN, exe2])
    record("preprocessor", "-D flips a conditional",
           r.stdout.endswith("release\n"), "", repr(r.stdout), "release")

    # a diagnostic inside an included header must name the header
    badh = os.path.join(tmp, "bad.h")
    with open(badh, "w", encoding="utf-8") as fh:
        fh.write("int broken(void) {\n    return no_such_variable;\n}\n")
    badc = os.path.join(tmp, "usebad.c")
    with open(badc, "w", encoding="utf-8") as fh:
        fh.write('#include "bad.h"\nint main(void) { return broken(); }\n')
    r = run([PY, ACC, "--pp", badc, "-o", os.path.join(tmp, "bad.exe"),
             "--json"])
    ok = False
    try:
        d = json.loads(r.stdout)["diagnostics"][0]
        ok = (d["code"] == "E0110" and d["file"].endswith("bad.h")
              and d["line"] == 2)
    except Exception:
        pass
    record("preprocessor", "errors map back to the header and line", ok,
           "", r.stdout[:200], "bad.h line 2")

    # #error must surface
    errc = os.path.join(tmp, "err.c")
    with open(errc, "w", encoding="utf-8") as fh:
        fh.write("#error this build is not supported\nint main(void){return 0;}\n")
    r = run([PY, ACC, "--pp", errc, "-o", os.path.join(tmp, "e.exe"),
             "--json"])
    ok = False
    try:
        d = json.loads(r.stdout)["diagnostics"][0]
        ok = d["code"] == "E0201" and "not supported" in d["message"]
    except Exception:
        pass
    record("preprocessor", "#error is reported", ok, "", r.stdout[:200])

    # --save-pp writes the expanded source
    saved = os.path.join(tmp, "expanded.c")
    run([PY, ACC, "--pp", src, "-o", os.path.join(tmp, "s.exe"),
         "--save-pp", saved])
    ok = os.path.exists(saved)
    if ok:
        text = open(saved, encoding="utf-8").read()
        ok = "accpp" in text and "#define" not in text
    record("preprocessor", "--save-pp writes expanded source", ok)

    # external mode, only if a system preprocessor exists
    if shutil.which("gcc"):
        exe3 = os.path.join(tmp, "ext.exe")
        r = run([PY, ACC, "--cpp", "gcc -E", src, "-o", exe3])
        if r.returncode == 0:
            r = run([PY, ACCRUN, exe3])
            record("preprocessor", "external cpp (gcc -E) produces the same result",
                   r.stdout == want, "", repr(r.stdout), repr(want))
        else:
            record("preprocessor", "external cpp (gcc -E)", False,
                   r.stderr.strip()[:200])
    else:
        record("preprocessor", "external cpp (gcc -E)", None,
               "no gcc on this machine")


def test_diagnostics(tmp, verbose):
    cases = [
        ("undefined variable", "int main(void) { return nope; }", "E0110"),
        ("undefined function", "int main(void) { return nope(); }", "E0113"),
        ("wrong argument count",
         "int f(int a) { return a; }\nint main(void) { return f(1, 2); }",
         "E0112"),
        ("missing main", "int f(void) { return 1; }", "E0121"),
        ("unterminated string", 'int main(void) { printf("oops); }', "E0002"),
        ("bad dereference", "int main(void) { int x = 1; return *x; }", "E0130"),
        ("break outside loop", "int main(void) { break; }", "E0122"),
        ("assign to a literal", "int main(void) { 3 = 4; return 0; }", "E0106"),
    ]
    for name, src, code in cases:
        p = os.path.join(tmp, "diag.c")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(src)
        r = run([PY, ACC, p, "-o", os.path.join(tmp, "diag.exe"), "--json"])
        ok = r.returncode == 1
        got_code = ""
        try:
            doc = json.loads(r.stdout)
            got_code = doc["diagnostics"][0]["code"]
            ok = ok and got_code == code and not doc["ok"]
            ok = ok and doc["diagnostics"][0]["line"] >= 1
        except Exception:
            ok = False
        record("diagnostics", "%s -> %s" % (name, code), ok,
               "", got_code, code)


def test_properties(tmp, verbose):
    src = os.path.join(tmp, "prop.c")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write('int main(void) { printf("x%dy\\n", 7); return 0; }\n')

    a = os.path.join(tmp, "a.exe")
    b = os.path.join(tmp, "b.exe")
    run([PY, ACC, src, "-o", a])
    run([PY, ACC, src, "-o", b])
    ha = hashlib.sha256(open(a, "rb").read()).hexdigest()
    hb = hashlib.sha256(open(b, "rb").read()).hexdigest()
    record("properties", "builds are byte-identical", ha == hb,
           "", ha[:16], hb[:16])

    # the image must be structurally valid PE32+
    sys.path.insert(0, ROOT)
    from accrun import PE
    pe = PE(a)
    record("properties", "output is a valid PE32+ image",
           pe.machine == 0x8664 and pe.entry > 0 and len(pe.sections) == 2)
    record("properties", "import directory is present",
           pe.dirs[1][0] != 0 and pe.dirs[1][1] >= 40)
    raw = open(a, "rb").read()
    record("properties", "TimeDateStamp is zero (reproducible)",
           struct.unpack_from("<I", raw, struct.unpack_from("<I", raw, 0x3C)[0] + 8)[0] == 0)

    # a DLL must carry the DLL characteristic and an export directory
    d = os.path.join(tmp, "p.dll")
    with open(os.path.join(tmp, "p.c"), "w", encoding="utf-8") as fh:
        fh.write("int one(void) { return 1; }\n")
    run([PY, ACC, os.path.join(tmp, "p.c"), "--dll", "-o", d])
    pe = PE(d)
    record("properties", "DLL sets IMAGE_FILE_DLL and exports",
           bool(pe.characteristics & 0x2000) and pe.dirs[0][0] != 0)
    record("properties", "DLL has no entry point", pe.entry == 0)


def main(argv):
    verbose = "-v" in argv
    as_json = "--json" in argv
    tmp = tempfile.mkdtemp(prefix="acctest_")
    try:
        test_programs(tmp, verbose)
        test_dll(tmp, verbose)
        test_linker(tmp, verbose)
        test_preprocessor(tmp, verbose)
        test_diagnostics(tmp, verbose)
        test_properties(tmp, verbose)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in RESULTS if r["ok"] is False]
    skipped = [r for r in RESULTS if r["ok"] is None]
    passed = [r for r in RESULTS if r["ok"] is True]
    if as_json:
        json.dump({"total": len(RESULTS), "passed": len(passed),
                   "failed": len(failed), "skipped": len(skipped),
                   "results": RESULTS}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return len(failed)

    order = []
    for r in RESULTS:
        if r["group"] not in order:
            order.append(r["group"])
    grouped = [r for g in order for r in RESULTS if r["group"] == g]

    group = None
    for r in grouped:
        if r["group"] != group:
            group = r["group"]
            sys.stdout.write("\n  %s\n" % group.upper())
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[r["ok"]]
        sys.stdout.write("    [%s] %s\n" % (mark, r["name"]))
        if r["ok"] is None and r["detail"]:
            sys.stdout.write("           %s\n" % r["detail"])
        if r["ok"] is False:
            if r["detail"]:
                sys.stdout.write("           %s\n" % r["detail"])
            if r["want"]:
                sys.stdout.write("           expected %s\n" % r["want"])
                sys.stdout.write("           got      %s\n" % r["got"])
    sys.stdout.write("\n  %d of %d passed" % (len(passed), len(RESULTS)))
    if failed:
        sys.stdout.write(", %d failed" % len(failed))
    if skipped:
        sys.stdout.write(", %d skipped by OS policy" % len(skipped))
    sys.stdout.write("\n")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
