# Changelog

All notable changes to acc, including the bugs found and the claims corrected.

This file records mistakes as well as features. A compiler asks you to trust that the bytes it emits mean what your source said, and that trust is worth more than a clean-looking history. Where a published number turned out to be wrong, the old value and the correction are both here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [semantic versioning](https://semver.org/).

---

## [Unreleased]

### Added

- **Continuous integration.** `.github/workflows/ci.yml` runs the test suite and the claims audit on GitHub's `windows-latest` runner for every pull request and every push to `main`. It is a required status check: branch protection refuses to merge a pull request unless it passes. Actions are pinned to full commit SHAs rather than tags, and the job runs with read-only permissions.

- **An optional preprocessor, `accpp.py`.** Off by default: without a flag, `#` lines are still skipped exactly as before, so existing sources and the acc/gcc dual-compilable `examples/same.c` are unaffected.

  - `--pp` runs the bundled preprocessor: `#define` (object-like and function-like), `#undef`, `#include` with a search path, `#ifdef` / `#ifndef` / `#if` / `#elif` / `#else` / `#endif` with integer constant expressions, `#error`, `#pragma once`, line continuations, and `__FILE__` / `__LINE__`.
  - `-D NAME[=VAL]` and `-I DIR` define macros and add search paths, and imply `--pp`.
  - `--cpp "gcc -E"` hands the file to an external preprocessor instead, reading back its line markers.
  - `--save-pp FILE` writes the expanded source out for inspection.
  - Diagnostics map back through expansion: an error inside an included header reports that header and its own line number, with the expanded line also available as `expanded_line` in JSON output.
  - Unsupported constructs — `#` stringize, `##` paste, variadic macros — raise a clear error rather than being silently ignored.
  - **Known limit:** system headers such as `<stdio.h>` still cannot be compiled in either mode. They contain inline assembly, `__attribute__`, structs and typedefs that acc cannot parse. The preprocessor is for your own headers and build-time conditionals.

- **Native-CPU differential testing.** Every program test now also runs its binary on the real processor and requires the result to match both the hand-derived expectation and the interpreter's output. A disagreement between silicon and `accrun` fails the test, so neither can cover for a bug in the other. Test count rose from 43 to 59, and to 67 once the preprocessor tests landed.
- **Skip reporting.** Where the host refuses to launch freshly built unsigned binaries — Windows Smart App Control, `WinError 4551` — the native cases report `SKIP`, and every skip is listed with its own reason (an earlier version of the summary line attributed every skip to OS policy, including skips caused by gcc being absent). Skips are never counted as passing.
- `docs/VERIFICATION.md`, `docs/TOOLCHAIN.md`, `docs/demo.gif`, and this changelog.

### Fixed

Compiler bugs that shipped in 0.1.0. Each has a regression test in the new `regressions` group, written before the fix and confirmed failing against 0.1.0. Every expected value was also cross-checked by building the same program with `gcc -std=c99 -pedantic-errors`.

- **Any declaration with more than one declarator was broken.** `int a = 1, b = 2; return a + b;` failed with `undefined variable 'a'`. The parser wrapped the declarators in a synthetic block, and a block opens its own scope, so every variable vanished as soon as it was declared. No existing test declared two variables in one statement. Found while writing the test for the next item, which failed for this reason instead of its own.
- **`int *a, b;` made `b` a pointer too.** The `*` was attached to the shared base type instead of the one declarator (C99 6.7.5).
- **Constants with a leading zero were read as decimal.** `0777` was 777; it is now octal, 511 (C99 6.4.4.1). `08` is now error `E0007` rather than a silent 8.
- **An integer constant too large for any type crashed acc** with a Python `struct.error` traceback. It is now error `E0008` (C99 6.4.4p2).
- **Negative `int` results from the C library compared as positive.** Library functions return a 32-bit `int` in `eax`, and the upper half of `rax` is not part of the value, but acc read all of `rax`. `atoi("-5") < 0` was false, so a `getchar() == EOF` loop could never end. `strcmp` happened to work only because msvcrt's implementation leaves `rax` fully sign-extended — the ABI promises neither. acc now sign-extends after every imported function that returns `int` or `long`. The interpreter did **not** show this bug (its emulated library returned full 64-bit values); the native-CPU run did, as a "cpu and interpreter disagree" failure.
- **`char` objects held 8 bytes.** `char c = 300;` kept 300, and so did `char` globals, parameters and return values. A `char` read through a pointer was unsigned (200 instead of -56), though plain `char` is signed on Windows. Every store into a `char` now keeps one signed byte (C99 6.3.1.3), and reads sign-extend. The interpreter learned `movsx` so it can still run this code.
- **Internal compiler errors escaped as Python tracebacks.** Any failure that is not a diagnosable error in the program now reports `E9999 internal compiler error` and exits 3, in text and `--json` output.

- **The claims audit would have failed in CI on a correct repo.** It compared gcc's output against the 54,465 bytes measured under gcc 16.1.0, but gcc's output size depends on which gcc built it, so any other version would report a true claim as stale. It now re-measures only when the local gcc matches the version the README quotes, and says so when it skips. It also crashed with `FileNotFoundError` when gcc was not on `PATH` at all; that now skips cleanly too. Both found while writing the CI workflow, before it ever ran.

- **DLLs could not coexist in one process.** Every acc DLL declared `IMAGE_FILE_RELOCS_STRIPPED` and requested image base `0x180000000`. Because a DLL with relocations stripped cannot be rebased, the first acc DLL loaded into a process took that address and every subsequent one failed with `WinError 487` — permanently, for that process. Manual testing never caught it because it had only ever loaded one DLL at a time; the test suite loads several and failed immediately.

  Fixed by emitting a `.reloc` section holding one block of `IMAGE_REL_BASED_ABSOLUTE` entries, which are defined as no-ops, and setting `DYNAMIC_BASE`. The image now says "I can be moved" without listing fixups, because acc's rip-relative code genuinely needs none. Three separately built DLLs now load simultaneously, relocated by ASLR far from the preferred base.

  Side effect: DLL output grew by one 512-byte section. `mathlib.dll` went from 1,536 to 2,048 bytes.

- **The demo GIF showed a shell error.** The generator ran commands through `cmd.exe`, which rejects `./hello.exe`, so the recording displayed `'.' is not recognized as an internal or external command` where the program output belonged. The generator now uses bash when one is present. The compiler was never involved.

- **`drawtext` ate backslashes.** `printf("hello from acc\n")` rendered in the GIF as `"hello from accn"`, because ffmpeg's `drawtext` interprets escape sequences even from a text file. Backslashes are now doubled before rendering.

### Changed

- **The size comparison now uses one source file for both compilers.** It previously reported gcc at 54,501 bytes against acc at 1,536 — but those were *different* source files, which is not a benchmark. Both now compile `examples/same.c`: gcc 54,465 bytes, acc 1,536 bytes, a 35.5× difference. The two caveats that make the comparison fair to gcc are stated alongside it.

- **Line counts refreshed** twice: after the relocation fix (`acc.py` 1,906 → 1,928, `accld.py` 529 → 550) and again after the preprocessor was wired in (`acc.py` 1,928 → 2,024, core total 3,170 → 3,266, plus the optional `accpp.py` at 448 lines).

- **The claims audit had a false positive.** It flagged `import accpp` in `acc.py` as a non-stdlib dependency. `accpp.py` ships with acc, so the check now distinguishes project modules from third-party ones. It also pins `accpp.py`'s published line count.

- **Test report grouped by category.** Results were recorded per case, so group headers interleaved (`PROGRAMS`, `NATIVE CPU`, `PROGRAMS`, …). Output is now grouped.

---

## [0.1.0]

The first working toolchain.

### Added — compiler (`acc.py`)

- Lexer, recursive-descent parser, and a code generator that walks the AST and emits x86-64 machine code directly. No intermediate representation, no assembly text, no assembler.
- Hand-encoded instruction emitter covering roughly 40 instruction forms, with a listing mode (`--listing`) that prints every emitted byte next to the source line that produced it.
- PE32+ writer: DOS header, COFF header, optional header, section table, and an import directory bound to `msvcrt.dll`. No linker involved.
- COFF object writer (`-c`) with a symbol table, relocations, and `__imp_NAME` undefined symbols for C library calls, following MSVC's convention.
- DLL output (`--dll`) with an export directory.
- **Language:** `int` (64-bit), `char`, `void`, pointers to any depth; functions with up to 4 parameters, prototypes, recursion; `if`/`else`, `while`, `for`, `return`, `break`, `continue`, nested scopes; the usual arithmetic, comparison, and logical operators with short-circuit evaluation; `++`/`--` in both positions; compound assignment; `&`, `*`, pointer arithmetic scaled by element size, and `ptr - ptr`; globals with constant initializers and `extern` declarations; 25 C library functions imported from `msvcrt.dll`.
- **Win64 calling convention:** first four arguments in `rcx`/`rdx`/`r8`/`r9`, the rest above a 32-byte shadow space, with 16-byte stack alignment maintained at every call site through compile-time push-depth tracking.
- **Agent-facing features:** `--json` diagnostics with stable error codes and hints, `--dump-ast`, and reproducible output — `TimeDateStamp` is zeroed, so identical source produces byte-identical binaries.

### Added — linker (`accld.py`)

- Merges the `.text` and `.data` sections of multiple COFF objects, builds one global symbol table, and reports duplicate definitions.
- Resolves `IMAGE_REL_AMD64_REL32` relocations, and names the symbol when it cannot: `undefined reference to 'call_count' / referenced from prog.obj`.
- Turns undefined `__imp_NAME` symbols into a real import table.
- Synthesizes the 21-byte startup stub that calls `main` and then `exit`, filling the role of `crt2.o`.
- Builds executables or DLLs, with `--export` for selective exports and `--map` for the link map.

### Added — loader (`accrun.py`)

- Parses a PE32+ image, maps its sections at their RVAs, binds imports to Python implementations of the C library, and interprets the x86-64 machine code.
- Decodes instructions from raw bytes using REX/ModRM/SIB rules rather than pattern-matching acc's output, so an encoding error surfaces as a wrong effective address rather than agreeing with the compiler.
- Written because Smart App Control blocked every freshly built unsigned binary on the development machine. The same problem applies to CI containers, locked-down build machines, and cross-architecture hosts.

### Fixed during initial development

- **Calls were limited to four arguments.** The first version rejected a five-argument `printf`. Fixed by evaluating arguments right to left and passing the overflow above the shadow space, per the Win64 convention.
- **The linker's startup stub did not fit.** Reserved 16 bytes for a stub that assembles to 21.

### Known corrections to earlier test expectations

Three tests failed on first run. All three were wrong tests, not compiler bugs — recorded because a suite's failures only mean something if you say what they were.

- `*(s + 5)` on `"abcdef"` is `'f'`, not `'e'`. Arithmetic error on my part.
- `printf("%d %d", i++, i)` is undefined behaviour — an unsequenced modification and read of `i`. Rewritten to bind each result to a variable first.
- `printf("%d %d %d", fib(15), call_count, gcd(...))` read the counter before `fib` ran, because acc evaluates arguments right to left. C leaves that order unspecified, so the test was wrong. Rewritten to sequence the calls, and a separate test now pins the right-to-left order deliberately rather than leaving it accidental.

### Not implemented

No preprocessor (`#` lines are skipped), no structs, unions, enums, or arrays, no `switch`, `goto`, `do`/`while`, or ternary, no floating point, no unsigned types, no casts, no bitwise operators, no `sizeof`, and no optimizer. `int` is 64 bits rather than 32. Function definitions accept at most 4 parameters. Windows x86-64 only.
