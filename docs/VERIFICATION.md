# Verification

*How acc is tested, what the tests actually prove, and where the gaps are. Supporting notes for [the README](../README.md).*

```console
$ python tests/run_tests.py
  67 of 67 passed
```

---

## The rule the suite follows

Every expected value was derived from C semantics **before** running the compiler, never recorded from whatever acc happened to print. A test that asserts "the output is what the compiler produced" proves only that the compiler is deterministic.

That rule has teeth. Writing this suite produced one genuine compiler bug and three wrong expectations of my own, all documented below.

---

## Full output

```
  PROGRAMS
    [PASS] arithmetic and precedence
    [PASS] signed division truncates toward zero
    [PASS] control flow
    [PASS] recursion and call depth
    [PASS] mutual recursion
    [PASS] short-circuit evaluation
    [PASS] increment and decrement semantics
    [PASS] argument evaluation order is right to left
    [PASS] pointers and address-of
    [PASS] pointer arithmetic scales by element size
    [PASS] globals and nested scope shadowing
    [PASS] C library calls
    [PASS] more than four call arguments (stack passing)
    [PASS] comparison operators
    [PASS] exit code propagation
    [PASS] prime sieve by trial division

  NATIVE CPU
    [PASS] arithmetic and precedence
    [PASS] signed division truncates toward zero
    [PASS] control flow
    [PASS] recursion and call depth
    [PASS] mutual recursion
    [PASS] short-circuit evaluation
    [PASS] increment and decrement semantics
    [PASS] argument evaluation order is right to left
    [PASS] pointers and address-of
    [PASS] pointer arithmetic scales by element size
    [PASS] globals and nested scope shadowing
    [PASS] C library calls
    [PASS] more than four call arguments (stack passing)
    [PASS] comparison operators
    [PASS] exit code propagation
    [PASS] prime sieve by trial division

  DLL
    [PASS] build a DLL
    [PASS] Windows loader accepts the DLL
    [PASS] add(17, 25) == 42
    [PASS] mul(6, 7) == 42
    [PASS] factorial(10,) == 3628800
    [PASS] sum_to(100,) == 5050

  LINKER
    [PASS] compile util.c to COFF
    [PASS] compile prog.c to COFF
    [PASS] link two objects
    [PASS] cross-object calls and shared global
    [PASS] undefined reference is reported
    [PASS] linker-built DLL export works
    [PASS] unexported symbol stays hidden

  PREPROCESSOR
    [PASS] default leaves # lines alone
    [PASS] --pp compiles the program
    [PASS] include, macros, #if and #ifdef all expand
    [PASS] -D flips a conditional
    [PASS] errors map back to the header and line
    [PASS] #error is reported
    [PASS] --save-pp writes expanded source
    [PASS] external cpp (gcc -E) produces the same result

  DIAGNOSTICS
    [PASS] undefined variable -> E0110
    [PASS] undefined function -> E0113
    [PASS] wrong argument count -> E0112
    [PASS] missing main -> E0121
    [PASS] unterminated string -> E0002
    [PASS] bad dereference -> E0130
    [PASS] break outside loop -> E0122
    [PASS] assign to a literal -> E0106

  PROPERTIES
    [PASS] builds are byte-identical
    [PASS] output is a valid PE32+ image
    [PASS] import directory is present
    [PASS] TimeDateStamp is zero (reproducible)
    [PASS] DLL sets IMAGE_FILE_DLL and exports
    [PASS] DLL has no entry point

  67 of 67 passed
```

The runner exits with the number of failures, so CI can gate on it. `--json` emits the same results as structured data.

---

## Three independent paths

A compiler that only checks its own work is checking nothing. Each of these routes to "the program ran" is independent of the others.

### 1. The real CPU

All 16 program binaries are executed directly by Windows. No emulation anywhere in that path — the processor decodes acc's bytes.

Each native run must satisfy two conditions: match the hand-derived expected output, **and** match what the interpreter produced for the same file. That makes this group differential. A disagreement between silicon and interpreter fails the test, so neither can quietly cover for a bug in the other.

One caveat, stated plainly because it changes what this group proves on other machines. Windows **Smart App Control blocked every freshly built unsigned binary** through most of this project's development (`WinError 4551`) — that is why `accrun` exists at all. The machine's owner has since switched Smart App Control off, which is why all 16 now run natively here. Where it is enabled, these cases report `SKIP` and the summary states how many were skipped by OS policy. They are never silently counted as passing.

### 2. The real Windows loader

DLL tests go through `ctypes.CDLL`, which is `LoadLibrary`. Windows parses acc's PE headers, maps its sections, applies relocations, resolves its imports, and hands back function pointers that get called with real arguments:

```console
add(17, 25)     = 42
mul(6, 7)       = 42
factorial(10)   = 3628800
sum_to(100)     = 5050
```

If the export directory, the section table, the relocation directory, or the calling convention were wrong, `LoadLibrary` would reject the file or the calls would crash.

Negative control included: a DLL linked with `--export gcd` must **not** expose `fib`, and the test fails if `getattr(lib, "fib")` succeeds.

### 3. An independent interpreter

`accrun.py` decodes instructions from raw bytes using REX/ModRM/SIB rules rather than pattern-matching acc's output. This distinction is the entire point: if the encoder writes a wrong ModRM byte, the interpreter computes a wrong effective address and the test fails, instead of the two quietly agreeing on the same mistake.

It was calibrated against route 1 before being trusted — same binary, same output, same exit code:

```console
$ ./hello.exe                    # real CPU
hello from acc
exit 7

$ python accrun.py hello.exe     # interpreter
hello from acc
[accrun] hello.exe exited with 7 after 15 instructions
```

---

## What individual tests are pinning down

Most of these exist because they are places a naive code generator gets it wrong.

**Signed division truncates toward zero.** C99 requires `-7 / 2 == -3` and `-7 % 2 == -1`. Python's `//` floors, giving `-4`, so a code generator written in Python will get this wrong unless it handles sign explicitly. All six combinations of operand signs are checked.

**Short-circuit evaluation.** A global counter records whether the right operand ran. `0 && bump()` and `1 || bump()` must leave it untouched; `1 && bump()` and `0 || bump()` must increment it. This catches a code generator that evaluates both sides before branching.

**Argument evaluation order.** C leaves the order unspecified. acc evaluates right to left, because popping arguments in that order lands them exactly where the Win64 convention wants them. The test pins it at `321` so a future change to the calling sequence cannot alter observable behaviour silently.

**Increment and decrement.** Each result is bound to its own variable first. Writing `printf("%d %d", i++, i)` would be undefined behaviour, not a compiler test — see §4.

**Pointer arithmetic scales by element size.** `int*` steps by 8, `char*` by 1, and `ptr - ptr` divides by the element size. A compiler that treats every pointer as a byte pointer passes half of this and fails the rest.

**More than four arguments.** Win64 puts the first four in `rcx/rdx/r8/r9` and the rest above a 32-byte shadow space. A seven-argument `printf` is checked, because getting this wrong usually corrupts the stack rather than printing wrong numbers.

**Stack alignment.** Not a named test, but every call in the suite depends on it. `rsp` must be 16-byte aligned at each `call`; acc tracks push depth at compile time and inserts an 8-byte pad when it is odd. Misalignment does not show up until a callee touches SSE, so `printf` exercises it constantly.

**Prime sieve.** 168 primes below 1000 summing to 76127 — a value with an external source of truth, exercising nested loops, early return, and modulo across ~1000 iterations.

**Diagnostics.** Eight malformed programs must each produce their specific error code, exit 1, and report a line number of at least 1. This keeps error handling from degrading into a stack trace.

**Reproducibility.** Two builds of one source are hashed and compared. `TimeDateStamp` is read directly out of the COFF header and asserted to be zero.

---

## The bug this suite caught

Worth recording in full, because it is exactly the class of defect that hides from casual testing.

Every acc DLL declared `IMAGE_FILE_RELOCS_STRIPPED`. The reasoning seemed sound: acc's generated code is entirely rip-relative, so there is nothing to relocate. Manual testing agreed — DLLs loaded, exports returned correct values.

The suite loads **several** DLLs, and the second one failed:

```
OSError: [WinError 487] Attempt to access invalid address
```

Every acc DLL asks for image base `0x180000000`. A DLL with relocations stripped cannot be moved. So the first DLL loaded into a process takes that address and every subsequent acc DLL is unloadable — in that process, forever. Every earlier manual test had loaded exactly one.

The fix is a `.reloc` section containing a single block of `IMAGE_REL_BASED_ABSOLUTE` entries, which are defined as no-ops, plus the `DYNAMIC_BASE` characteristic. The image says "I can be moved" without listing any fixups, because it genuinely needs none.

Proof, three separately built DLLs in one process:

```console
  A.dll   loaded at 0x7FFC7A0B0000
  B.dll   loaded at 0x7FFC72E50000
  C.dll   loaded at 0x7FFC72880000

  A.dll add(17,25)      = 42
  B.dll gcd(252,105)    = 21
  C.dll factorial(12)   = 479001600
```

Note the addresses: nowhere near the requested `0x180000000`. Windows relocated all three under ASLR, and the rip-relative code computed correctly at its new home — which incidentally proves the position independence that made the fix safe.

---

## Three expectations I got wrong

Recorded because a test suite's failures are only useful if you say what they were.

**`"abcdef"` indexing.** I expected `*(s + 5)` to be `'e'`. It is `'f'`. Plain arithmetic error on my part; the compiler was right and the test was wrong.

**`printf("%d %d", i++, i)`.** I expected `5 6`. acc printed `5 5`. This is undefined behaviour — an unsequenced modification and read of `i` — so neither answer is "correct" and the test had no business existing. Rewritten to bind each result to a variable first.

**`printf("%d %d %d", fib(15), call_count, gcd(...))`.** I expected the call counter to read 1973. It read 0, because acc evaluates arguments right to left and read the counter before `fib` ran. Unspecified order, so again the test was wrong, not the compiler. Rewritten to sequence the calls, and a separate test now pins the evaluation order deliberately.

Two of three were mine assuming an order C does not promise. That is worth knowing about acc: **its argument evaluation order is right to left, and it is documented rather than accidental.**

---

## What is not tested

Being explicit about the boundary:

- **No fuzzing.** No randomized program generation, no differential testing against gcc on generated inputs. This is the largest gap.
- **No optimizer tests**, because there is no optimizer.
- **Windows only.** CI runs the suite and the claims audit on GitHub's `windows-latest` runner for every pull request, and a failing run blocks the merge. There is no OS matrix: acc emits Windows PE images, and the compiler and interpreter have not been tested on Linux or macOS.
- **Native execution depends on the host's policy.** Every program is run on the real CPU here, but on a machine with Smart App Control enabled those 16 cases are skipped rather than run. The interpreter and DLL routes are unaffected.
- **No performance benchmarks.** Compile time and runtime speed are unmeasured.
- **Unsupported language features are not tested for graceful failure.** Feeding acc a `struct` produces a parse error, but the quality of that error is not asserted.
- **The interpreter is not itself verified** beyond agreeing with the CPU on the binaries Windows would run. A shared blind spot between `accrun` and the real hardware is possible, though the independent decoding path makes it unlikely.

---

## Running it

```bash
python tests/run_tests.py            # summary
python tests/run_tests.py -v         # per-case output
python tests/run_tests.py --json     # structured results
```

Every case builds real binaries in a temp directory that is removed afterwards. Nothing in the repo is modified.
