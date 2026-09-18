# acc — the Agent C Compiler

![language](https://img.shields.io/badge/written%20in-Python%203-3776AB)
![target](https://img.shields.io/badge/target-x86--64%20PE32%2B-0078D6)
![deps](https://img.shields.io/badge/dependencies-none-success)
![toolchain](https://img.shields.io/badge/gcc%20%7C%20clang%20%7C%20msvc-not%20required-critical)
![builds](https://img.shields.io/badge/builds-reproducible-blueviolet)
![tests](https://img.shields.io/badge/tests-67%2F67%20passing-brightgreen)
[![CI](https://github.com/KowaiAI/pyacc/actions/workflows/ci.yml/badge.svg)](https://github.com/KowaiAI/pyacc/actions/workflows/ci.yml)

**A C compiler, linker, and loader that produce native Windows executables without gcc, without clang, without MSVC, without an assembler, and without an external linker.**

Not a wrapper. Not a transpiler. `acc` reads your C, encodes x86-64 machine code byte by byte, and writes the PE file itself. The only thing between your source and a running `.exe` is about 3,000 lines of dependency-free Python.

```console
$ python acc.py hello.c
acc: wrote hello.exe [exe] (1536 bytes, 19 instructions, 70 code bytes)
acc: imports from msvcrt.dll: exit, printf

$ ./hello.exe
hello from acc
```

That `.exe` is real machine code. No interpreter, no runtime, no trace of Python. Delete Python afterwards and the binary still runs.

<p align="center">
  <img src="docs/demo.gif" alt="acc compiling a C file to a 1.5 KB exe, building a DLL that the Windows loader accepts, and passing its test suite" width="100%">
</p>

---

## Proof

Everything above is checked by a suite that builds real binaries and runs them:

```console
$ python tests/run_tests.py
  67 of 67 passed
```

| Group | Tests | What it establishes |
|---|---:|---|
| Programs | 16 | Arithmetic, control flow, recursion, pointers, `malloc`, short-circuit evaluation, stack-passed arguments — expected output derived from C semantics by hand, never recorded from the compiler |
| Native CPU | 16 | The same 16 binaries executed by the processor itself, each required to match both the hand-derived expectation **and** the interpreter |
| DLL | 6 | The **real Windows loader** accepts acc DLLs via `LoadLibrary`, and every export returns the right answer |
| Linker | 7 | Cross-object calls, a global written in one object and read in another, undefined-reference detection, selective exports |
| Preprocessor | 8 | The optional preprocessor expands includes, macros and conditionals, `-D` flips a build, `#error` surfaces, and diagnostics map back to the header that caused them |
| Diagnostics | 8 | Eight malformed programs each produce their specific error code and a real line number |
| Properties | 6 | Byte-identical rebuilds, valid PE32+ structure, zeroed `TimeDateStamp`, correct DLL characteristics |

Three independent routes confirm a binary actually runs, and the suite uses all three: the **real CPU**, the **real Windows loader** (`ctypes.CDLL` calling exports with live arguments), and an **independent interpreter** that decodes instructions from raw bytes using REX/ModRM/SIB rules rather than recognizing acc's own output. The native-CPU group is differential — a disagreement between silicon and interpreter fails the test, so neither can quietly cover for the other.

If your machine refuses to launch freshly built unsigned binaries — Windows Smart App Control does this, `WinError 4551` — those 16 cases report `SKIP` rather than passing silently, and the run tells you how many were skipped by OS policy.

The suite earns its keep. It found a bug that manual testing had missed completely: every acc DLL declared its relocations stripped, so **only the first one loaded into a process could ever map** — the second failed with `WinError 487`. Three separately built DLLs now coexist, relocated by ASLR far from their preferred base:

```console
  A.dll   loaded at 0x7FFC7A0B0000     add(17,25)     = 42
  B.dll   loaded at 0x7FFC72E50000     gcd(252,105)   = 21
  C.dll   loaded at 0x7FFC72880000     factorial(12)  = 479001600
```

Documentation drifts, so the numbers in this README are themselves tested:

```console
$ python tests/audit_claims.py
  0 stale claim(s)
```

It re-measures every figure quoted here — line counts, binary sizes, the DLL banner, the test total — and fails if any no longer matches reality. The one exception is gcc's binary size, which is only re-measured under the gcc version this README names: a different gcc legitimately produces a different size.

Both run on every pull request in [CI](.github/workflows/ci.yml), and `main` is branch-protected so a pull request cannot merge unless they pass.

> **→ [Verification](docs/VERIFICATION.md)** — the full test output, what each test pins down and why, the three expectations *I* got wrong (two of them assuming an evaluation order C never promised), and an explicit list of what is **not** tested.

---

## Why this exists

Ask a machine to compile C and it reaches for gcc. But gcc is 15 million lines of someone else's decisions, and when you only need a subset of C, most of that is ceremony. `acc` is the other answer: own the whole pipeline, from the first character of source to the last byte of the PE header.

It is also built for a specific kind of user — an AI agent, which has different needs than a human at a terminal. Agents need machine-readable diagnostics, deterministic output they can hash, and a way to *verify what they just built* even when the host operating system refuses to run it. All three are first-class features here.

---

## Size

The identical source file — [`examples/same.c`](examples/same.c), a `printf` and a `return` — compiled by both toolchains on the same machine:

| Compiler | Size |
|---|---|
| gcc 16.1.0 (MinGW-w64) | 54,465 bytes |
| **acc 0.1.0** | **1,536 bytes** |

35.5× smaller. Two caveats, because the bare number flatters acc. gcc links a full CRT startup and eleven libraries where acc calls straight into `msvcrt.dll`; and gcc's binary carries exception-unwinding tables, thread-local storage, an application manifest, and debug sections that acc does not emit at all. What gcc produces is the more capable artifact. [Where the other 53 KB goes](docs/TOOLCHAIN.md) breaks it down section by section.

---

## How it compares to clang and gcc

Only gcc was benchmarked above, because gcc is what was installed on the test machine. **clang was not installed and therefore not measured** — no byte counts for it appear in this README, and none are guessed. What follows is a comparison of shape rather than speed or size.

| | **acc** | **clang / LLVM** | **gcc** |
|---|---|---|---|
| Implementation | ~3,200 lines of Python | millions of lines of C++ | millions of lines of C |
| Dependencies | none (stdlib only) | LLVM; a C++ toolchain to build it | a C toolchain to build it |
| Pipeline | source → AST → machine code | source → AST → LLVM IR → optimized IR → machine code | source → AST → GENERIC → GIMPLE → RTL → machine code |
| Intermediate representation | none | LLVM IR, the heart of the project | GIMPLE and RTL |
| Optimizer | none | among the most advanced in production | among the most advanced in production |
| Targets | x86-64 Windows PE | dozens of architectures and object formats | dozens of architectures and object formats |
| Language coverage | a subset of C | C, C++, Objective-C, current standards, full preprocessor | C, C++, Fortran, Ada, others, full preprocessor |
| Assembler | its own encoder, ~40 instruction forms | integrated assembler, the full ISA | emits assembly text for `as` by default |
| Linker | included (`accld`) | external (`lld`, `link.exe`, `ld`) | external (`ld`) |
| Way to run the output without the OS | included (`accrun`) | none for native binaries | none |
| Time to read the whole implementation | an afternoon | a career | a career |

The honest summary: clang and gcc win every axis that matters for shipping production software — optimization, language coverage, portability, standards conformance, decades of hardening. acc wins exactly one axis, and it is a narrow one: **the entire path from C source to a running process fits in three files you can read in a sitting.**

That difference is structural, not incidental. clang and gcc are built around an intermediate representation so that many front ends can meet many back ends, and so an optimizer has something principled to chew on. acc walks the AST and emits bytes, which is why it has no optimizer and never will without a redesign.

One difference cuts the other way, though: `gcc hello.c` does not compile anything — it is a *driver* that launches four separate programs (`cc1`, `as`, `collect2`, `ld`) and writes two temp files along the way. clang launches two. acc runs as one process and writes one file.

> **→ [Anatomy of a C toolchain](docs/TOOLCHAIN.md)** — the long version, with the real intermediate assembly gcc generates, a stage-by-stage size breakdown, a section-and-import comparison of both binaries, where the other 53 KB actually comes from, and what a linker does in the three jobs that word hides.

---

## The toolchain

```
        your C source
              │
              ▼
   ┌─────────────────────┐
   │       acc.py        │   lexer → parser → type check → x86-64 encoder
   │   the compiler      │
   └─────────────────────┘
        │            │
   -c   │            │  (default)
        ▼            ▼
   ┌─────────┐   ┌──────────────┐
   │  .obj   │   │  .exe / .dll │
   │  COFF   │   │    PE32+     │
   └─────────┘   └──────────────┘
        │
        ▼
   ┌─────────────────────┐
   │      accld.py       │   symbol resolution, relocation, imports, exports
   │      the linker     │
   └─────────────────────┘
              │
              ▼
        .exe / .dll
              │
              ▼
   ┌─────────────────────┐
   │     accrun.py       │   PE loader + x86-64 interpreter
   │      the loader     │
   └─────────────────────┘
```

Three tools, 3,266 lines, zero dependencies — plus an optional preprocessor:

| Tool | Lines | What it is |
|---|---:|---|
| `acc.py` | 2,024 | C compiler: lexer, recursive-descent parser, type checker, x86-64 instruction encoder, PE and COFF writers |
| `accld.py` | 550 | Linker: merges objects, resolves symbols, applies relocations, builds import/export tables, synthesizes the startup stub |
| `accrun.py` | 692 | Loader: parses PE, maps sections, binds imports, interprets the machine code |
| `accpp.py` | 448 | Preprocessor, **optional and off by default**: macros, includes, conditionals |

---

## Everything it can do

### Compile straight to an executable

```console
$ python acc.py program.c
acc: wrote program.exe [exe] (3072 bytes, 448 instructions, 1932 code bytes)
acc: imports from msvcrt.dll: exit, printf, malloc, free, strlen
```

### Build a DLL

Every function becomes an export, and the real Windows loader accepts it:

```console
$ python acc.py mathlib.c --dll
acc: wrote mathlib.dll [dll] (2048 bytes, 107 instructions, 414 code bytes)
acc: exports: add, mul, factorial, sum_to

$ python -c "import ctypes; print(ctypes.CDLL('./mathlib.dll').factorial(10))"
3628800
```

### Compile separately, then link

```console
$ python acc.py -c util.c
$ python acc.py -c prog.c
$ python accld.py util.obj prog.obj -o prog.exe --map
accld: wrote prog.exe [exe] (2048 bytes) from 2 object(s), 4 symbol(s)
accld: imports: printf, exit

  RVA         SECTION  SYMBOL
  0x1020      .text    fib  (util.obj)
  0x10ED      .text    gcd  (util.obj)
  0x1180      .text    main  (prog.obj)
  0x2000      .data    call_count  (util.obj)
```

The objects are genuine COFF — two sections, a symbol table, relocations, and `__imp_NAME` undefined symbols for C library calls, the same convention MSVC uses.

And when a symbol is missing, you get the error a linker should give you:

```console
$ python accld.py prog.obj -o broken.exe
accld: error: undefined reference to 'call_count'
       referenced from prog.obj
```

### Run the result without the OS

```console
$ python accrun.py prog.exe
linked across two objects
  fib(15)       = 610
  fib call count= 1973
  gcd(252, 105) = 21
[accrun] prog.exe exited with 0 after 67214 instructions
```

`accrun` parses the PE, maps the sections at their RVAs, rewrites the IAT, and interprets x86-64 directly. It decodes instructions from raw bytes using REX/ModRM/SIB rules rather than pattern-matching acc's output, so a bad encoding surfaces as a wrong effective address instead of quietly agreeing with the compiler.

This turned out to matter. Halfway through development, Windows Smart App Control started blocking every freshly built unsigned binary on the test machine:

```
OSError: [WinError 4551] An Application Control policy has blocked this file
```

The compiler was fine — the OS simply refused to launch new unknown executables. `accrun` is the answer to that class of problem, and it applies equally to sandboxes, CI containers, and cross-architecture hosts.

---

## Diagnostics built for machines

Errors carry a stable code, an exact location, and a hint that says what to do:

```console
$ python acc.py bad.c
bad.c:51:5: error[E0111]: call to 'printf' passes 5 arguments; acc supports at most 4
    51 |     printf("%s %d %c %c\n", s, len, *s, *(s + len - 1));
       |     ^
  hint: Win64 passes the first four arguments in rcx/rdx/r8/r9
```

The same thing as JSON, for a caller that needs to parse it:

```console
$ python acc.py bad.c --json
{
  "ok": false,
  "diagnostics": [
    {
      "severity": "error",
      "code": "E0111",
      "file": "bad.c",
      "line": 51,
      "col": 5,
      "message": "call to 'printf' passes 5 arguments; acc supports at most 4",
      "hint": "Win64 passes the first four arguments in rcx/rdx/r8/r9"
    }
  ]
}
```

### Read the machine code it wrote

`--listing` maps every emitted byte back to the source line that caused it. No disassembler needed:

```console
$ python acc.py hello.c --listing && cat hello.lst
; acc 0.1.0 -- machine code listing
; offset  bytes                    instruction
_start:  ; entry stub written by acc
  0000  48 81 EC 28 00 00 00     sub rsp, 40
  0007  E8 00 00 00 00           call main
  000C  89 C1                    mov ecx, eax
  000E  FF 15 00 00 00 00        call [rip+iat.exit]
  0014  C3                       ret

;  1 | int main(void) {
main:
  0015  55                       push rbp
  0016  48 89 E5                 mov rbp, rsp

;  2 | printf("hello from acc\n");
  0019  48 8D 05 00 00 00 00     lea rax, [rip+str0]
  0020  50                       push rax
  0021  59                       pop rcx
  0022  48 81 EC 20 00 00 00     sub rsp, 32
  0029  FF 15 00 00 00 00        call [rip+iat.printf]
  002F  48 81 C4 20 00 00 00     add rsp, 32
```

The zeroed displacements are relocation sites — `acc` patches them during layout, or hands them to `accld` as COFF relocations.

### Other flags

| Flag | Effect |
|---|---|
| `--json` | results and diagnostics as JSON |
| `--listing` | annotated machine-code listing |
| `--dump-ast` | the parse tree as JSON |
| `--dll` | build a DLL exporting every function |
| `-c` | stop at a COFF object |
| `--run` | build, then execute |
| `--pp` / `--cpp CMD` | optional preprocessing, off by default |
| `accld --map` | print the link map |

### Reproducible by construction

No timestamps anywhere in the output — `TimeDateStamp` is deliberately zero:

```console
$ python acc.py t2.c -o r1.exe && python acc.py t2.c -o r2.exe
$ sha256sum r1.exe r2.exe
d34655a18e0530d5...  r1.exe
d34655a18e0530d5...  r2.exe
```

Same source in, byte-identical binary out, every time.

---

## Preprocessing (optional)

acc skips `#` lines by default. That default is deliberate — it is what lets [`examples/same.c`](examples/same.c) compile under acc *and* gcc, and it keeps the compiler dependency-free. Turn the directives on when you want them:

```console
$ python acc.py --pp program.c
```

Two modes, neither of them required:

| | |
|---|---|
| `--pp` | Use `accpp.py`, bundled, no external tools |
| `-D NAME[=VAL]` | Define a macro (implies `--pp`) |
| `-I DIR` | Add an include search directory (implies `--pp`) |
| `--cpp "gcc -E"` | Hand the file to an external preprocessor instead |
| `--save-pp FILE` | Write the expanded source out to read |

`accpp` implements `#define` (object-like and function-like), `#undef`, `#include` with a search path, `#ifdef` / `#ifndef` / `#if` / `#elif` / `#else` / `#endif` with integer constant expressions, `#error`, `#pragma once`, line continuations, and `__FILE__` / `__LINE__`.

It does **not** implement the `#` stringize operator, `##` token pasting, variadic macros, or `#include_next`. Those raise a clear error rather than being silently ignored.

Diagnostics survive expansion. An error inside an included header names the header and its own line number, not a line of expanded text:

```console
$ python acc.py --pp usebad.c
examples/bad.h:4:12: error[E0110]: undefined variable 'undefined_symbol_in_header'
     4 |     return undefined_symbol_in_header;
       |            ^
```

The JSON form carries both, as `line` plus `expanded_line`.

**One limit worth stating plainly:** `#include <stdio.h>` still will not work, in either mode. System headers are full of declarations acc cannot parse — inline assembly, `__attribute__`, structs, typedefs — so pointing `--cpp "gcc -E"` at one produces an error inside the header rather than a working build. The preprocessor is useful for *your* headers, macros, and build-time conditionals. Declare library functions with prototypes, as the examples do.

---

## The language

acc compiles a subset of C. What it handles:

| | |
|---|---|
| **Types** | `int` (64-bit), `char`, `void`, pointers to any depth |
| **Functions** | definitions, prototypes, recursion, up to 4 parameters |
| **Statements** | `if` / `else`, `while`, `for`, `return`, `break`, `continue`, blocks, nested scopes |
| **Operators** | `+ - * / %`, `< > <= >= == !=`, `&& \|\|` (short-circuit), `!`, unary `-`, `++` / `--` (prefix and postfix), `= += -= *= /= %=` |
| **Pointers** | `&x`, `*p`, assignment through pointers, pointer arithmetic scaled by element size, `ptr - ptr` |
| **Storage** | locals, globals with constant initializers, `extern` declarations across objects |
| **Literals** | decimal, hex, character literals, string literals with escapes |
| **Library** | 25 functions imported from `msvcrt.dll` — `printf`, `malloc`, `free`, `strlen`, `strcmp`, `memset`, `atoi`, `rand`, and friends |
| **Calls** | variadic calls with arguments passed on the stack beyond the fourth |

A program it compiles today:

```c
int prime_sum = 0;

int is_prime(int n) {
    if (n < 2) return 0;
    for (int d = 2; d * d <= n; d++) {
        if (n % d == 0) return 0;
    }
    return 1;
}

int main(void) {
    int count = 0;
    for (int i = 0; i < 100; i++) {
        if (is_prime(i)) { count++; prime_sum += i; }
    }
    printf("primes < 100 = %d (sum %d)\n", count, prime_sum);

    int *buf = malloc(10 * 8);
    for (int i = 0; i < 10; i++) *(buf + i) = i * i;
    int total = 0;
    for (int i = 0; i < 10; i++) total += *(buf + i);
    free(buf);
    printf("sum of squares = %d\n", total);
    return 0;
}
```

```
primes < 100 = 25 (sum 1060)
sum of squares = 285
```

---

## What it does not do

Read this part. The subset is real and the gaps are real:

- **The preprocessor is opt-in.** By default `#` lines are skipped, not processed. Pass `--pp` (or `--cpp "gcc -E"`) to turn it on — see [Preprocessing](#preprocessing-optional). System headers such as `<stdio.h>` still will not compile: they contain declarations acc cannot parse.
- **No structs, unions, enums, or arrays.** Use pointers and `malloc` for now. This is the biggest gap.
- **No `switch`, `goto`, `do/while`, or the ternary operator.**
- **No floating point.** No `float`, no `double`, no SSE.
- **`int` is 64 bits**, not 32. Deliberate: every value is one machine word, which makes the generated code auditable. `printf("%d")` still works because it reads the low half.
- **Functions take at most 4 parameters.** Calls can pass more (they go on the stack), but definitions cannot receive them yet.
- **No optimizer.** Codegen is a straightforward stack machine. Expect roughly 3× the instruction count of `gcc -O2`.
- **No unsigned types, casts, bitwise operators, or `sizeof`.**
- **Windows x86-64 only.** PE32+ output, Win64 calling convention.

---

## How it is verified

Three independent checks, all of which currently pass:

1. **Real CPU.** `t1.exe` was executed by Windows directly — printed correct output, returned exit code 7.
2. **Real Windows loader.** `mathlib.dll` is loaded through `ctypes.CDLL`, and every export returns the right answer: `add(17,25) = 42`, `factorial(10) = 3628800`, `sum_to(100) = 5050`. Selective exports are honored — a symbol left out of `--export` is genuinely absent.
3. **Independent interpreter.** `accrun.py` decodes the machine code from scratch and agrees with the real CPU byte for byte on the same binary, then validates the larger programs the OS refused to launch.

Cross-object correctness gets its own check: linking `util.obj` and `prog.obj` reports `fib call count = 1973` for `fib(15)`, which is exactly `2·F(16) − 1`. A cross-object global write landing at the wrong address would not produce that number.

---

## Install

```console
$ git clone <this repo>
$ cd acc
$ python acc.py --version
acc 0.1.0
```

Python 3.8+. That is the entire installation. No packages, no build step, no compiler.

---

## Documentation

- **[CHANGELOG.md](CHANGELOG.md)** — Every change, including the bugs found and the published numbers that turned out to be wrong. Mistakes are recorded with their corrections rather than quietly edited away.
- **[docs/VERIFICATION.md](docs/VERIFICATION.md)** — How acc is tested and what the tests prove. Full results, the three independent verification routes, the DLL relocation bug the suite caught, the expectations I got wrong, and an honest list of what is not covered.
- **[docs/TOOLCHAIN.md](docs/TOOLCHAIN.md)** — Anatomy of a C toolchain. What `gcc` actually runs when you invoke it, the assembly text it hands to the assembler, why a hello world costs 54 KB, what a linker really does, and how each classic tool maps onto a piece of acc.

The demo GIF is generated, not hand-made: `docs/make_demo.py` runs each command for real and renders the captured transcript with ffmpeg, so stale output cannot survive a regeneration. `docs/demo.tape` drives [VHS](https://github.com/charmbracelet/vhs) for a true terminal recording where VHS can run.

---

## Roadmap

- [ ] Arrays and `struct` — the gap that matters most
- [ ] A rewrite of the compiler in C, so it can eventually compile itself
- [ ] Register allocation to replace the push/pop stack machine
- [ ] `switch`, `goto`, ternary, bitwise operators
- [ ] ELF output so the same front end targets Linux
- [ ] A minimal preprocessor

---

## License

GNU General Public License v2.0. See [LICENSE](LICENSE).

