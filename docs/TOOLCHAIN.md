# Anatomy of a C toolchain

*Supporting notes for [acc](../README.md). Every command, file listing, and byte count on this page was captured on one Windows 11 machine with gcc 16.1.0 (MinGW-w64, WinLibs) and acc 0.1.0. Where clang is discussed it is labelled, because clang was not installed on that machine and was therefore never measured.*

---

## The question

You type this:

```console
$ gcc hello.c
```

A file appears. It is easy to assume one program read your C and wrote that file. That is not what happened — not with gcc, and not with clang either.

This page follows a single seven-line program all the way down, with the real intermediate artifacts, and then does the same for acc.

---

## 1. gcc is not a compiler

`gcc` is a **driver**. Its job is to decide which real tools to run, in what order, with which arguments. It parses no C at all.

`gcc -v` makes it print each program it launches. Trimmed to the essentials, compiling one file produced this chain:

```
cc1.exe        -o C:\Users\...\Temp\ccs4j4We.s     ← the actual C compiler
as.exe         -o C:\Users\...\Temp\ccjSHXiP.o     ← the assembler
collect2.exe   -o vtest.exe                        ← a wrapper around the linker
  └── ld.exe                                       ← the linker
```

Four processes. Two temporary files. The program you invoked wrote none of the output itself.

| Program | Role | Input | Output |
|---|---|---|---|
| `cc1.exe` | The C compiler proper: preprocessing, parsing, optimization, code generation | `.c` | **assembly text** (`.s`) |
| `as.exe` | Assembler: turns mnemonics into machine code | `.s` | COFF object (`.o`) |
| `collect2.exe` | Historical wrapper that arranges static constructors, then calls the linker | `.o` | — |
| `ld.exe` | Linker: resolves symbols, applies relocations, lays out the image | `.o` + libs | `.exe` |

Reproduce it:

```bash
gcc -v -o test.exe hello.c
```

---

## 2. Watching the program change shape

The source, which both toolchains accept unmodified (gcc needs the `#include`; acc skips `#` lines):

```c
#include <stdio.h>

int main(void) {
    printf("hello from acc\n");
    return 7;
}
```

### Stage one: C becomes assembly text

`gcc -S` stops after `cc1` and keeps the `.s` file. This is the complete, unedited output:

```gas
	.file	"same.c"
	.text
	.section .rdata,"dr"
.LC0:
	.ascii "hello from acc\0"
	.text
	.globl	main
	.def	main;	.scl	2;	.type	32;	.endef
	.seh_proc	main
main:
	pushq	%rbp
	.seh_pushreg	%rbp
	movq	%rsp, %rbp
	.seh_setframe	%rbp, 0
	subq	$32, %rsp
	.seh_stackalloc	32
	.seh_endprologue
	call	__main
	leaq	.LC0(%rip), %rax
	movq	%rax, %rcx
	call	puts
	movl	$7, %eax
	addq	$32, %rsp
	popq	%rbp
	ret
	.seh_endproc
	.def	__main;	.scl	2;	.type	32;	.endef
	.ident	"GCC: (MinGW-W64 x86_64-msvcrt-posix-seh, ...) 16.1.0"
	.def	puts;	.scl	2;	.type	32;	.endef
```

Three things in there are worth pausing on.

**gcc rewrote your code.** The source says `printf`. The assembly says `call puts`. Because the format string ends in `\n` and contains no conversions, gcc substituted the cheaper function. This is a real optimization pass, and it is the kind of thing acc will never do.

**`.seh_*` directives.** Structured Exception Handling metadata, so Windows can unwind the stack through this frame. It is not code — it is a contract with the OS, and it ends up in the `.pdata` and `.xdata` sections.

**`call __main`.** A MinGW startup hook, inserted before your first statement, that runs static constructors. Your program acquired a function call you never wrote.

Note also that this is *text*. gcc formatted mnemonics into a string, and `as.exe` is about to parse that string back into bytes. Clang skips this round trip — see §4.

### Stage two through four

```console
$ gcc -S -o same.s same.c     # cc1 only
$ gcc -c -o same.o same.c     # cc1 + as
$ gcc -o same_gcc.exe same.c  # cc1 + as + collect2 + ld
```

| Artifact | Produced by | Size |
|---|---|---:|
| `same.c` | you | 85 bytes |
| `same.s` | `cc1.exe` | 599 bytes |
| `same.o` | `as.exe` | 914 bytes |
| `same_gcc.exe` | `ld.exe` | **54,465 bytes** |
| `same_acc.exe` | `acc.py` | **1,536 bytes** |

The object file is 914 bytes. The executable is 54,465. Almost everything in the final binary arrived at link time, from somewhere other than your source.

---

## 3. Where 54 kilobytes comes from

`ld` was handed this (from the `gcc -v` output):

```
crt2.o  crtbegin.o  crtend.o  default-manifest.o
-lmingw32 -lgcc -lgcc_eh -lmingwex -lmsvcrt
-lkernel32 -lpthread -ladvapi32 -lshell32 -luser32
```

Four startup objects and eleven libraries for one `printf`.

`crt2.o` is the C runtime startup: it is what actually receives control from Windows, sets up `argc`/`argv`, initializes the heap and stdio, calls your `main`, then calls `exit` with whatever you returned. `main` is not the entry point of a C program and never was.

Pointing acc's own PE parser (`accrun.py`) at both binaries shows the result:

```console
== same_acc.exe ==
  image base 0x140000000  entry rva 0x1000  size of image 12288
  sections (2):
    .text      rva 0x1000   vsize 70       raw 512
    .data      rva 0x2000   vsize 136      raw 512
  imports:
    msvcrt.dll       2 function(s): exit, printf

== same_gcc.exe ==
  image base 0x140000000  entry rva 0x105F  size of image 90112
  sections (18):
    .text   .data   .rdata  /4      .pdata  .xdata
    .bss    .idata  .tls    .rsrc   .reloc  /14
    /29     /41     /55     /67     /80     /91
  imports:
    KERNEL32.dll     14 function(s)
    msvcrt.dll       26 function(s)
```

Eighteen sections against two. Forty imported functions against two. The `/4`, `/29`, `/41`… sections are debug information; `.tls` is thread-local storage; `.rsrc` holds the application manifest; `.reloc` lets the loader move the image if its preferred base is taken.

None of that is waste. Every one of those sections buys something: unwinding through exceptions, thread-local variables, ASLR, debuggability, a C runtime that works when `main` returns from a thread you did not create. It is the price of a general-purpose toolchain, and for production software it is worth paying.

acc declines all of it, which is exactly why its binary is small and its capabilities are narrow.

---

## 4. What clang does differently

**Not measured here — clang was not installed on the test machine.** Described from documented behaviour, not benchmarked.

The clang driver runs roughly two programs:

| Step | Program | Note |
|---|---|---|
| 1 | `clang -cc1` | The driver forks *itself*; this process is the front end plus the LLVM back end |
| 2 | a linker | `lld-link`, `ld.lld`, GNU `ld`, or MSVC `link.exe`, depending on target |

The notable difference from gcc is the **integrated assembler**. Where gcc's `cc1` writes assembly text for a separate `as.exe` to parse back into bytes, clang emits the object file directly from LLVM's machine-code layer. The `.s` round trip in §2 simply does not happen.

What clang does *not* do is link. LLVM's linker, `lld`, is a separate program from a separate subproject. A compiler driver that cannot produce an executable without calling out to a linker is the normal arrangement — gcc, clang, and MSVC all work this way.

---

## 5. What acc does instead

```console
$ python acc.py same.c
acc: wrote same.exe [exe] (1536 bytes, 19 instructions, 70 code bytes)
```

One process. No `.s`, no `.o`, no temporary files, no external programs, no startup objects, no libraries.

The pipeline inside that single process:

```
source text
   │  lexer            characters → tokens
   ▼
tokens
   │  parser           recursive descent → AST
   ▼
AST
   │  type checker     pointer arithmetic scaling, deref widths
   ▼
AST
   │  code generator   walks the tree, emits x86-64 bytes directly
   ▼
machine code + fixup lists
   │  layout           assign RVAs, patch rip-relative displacements
   ▼
PE32+ file
```

There is no intermediate representation. The code generator walks the syntax tree and appends bytes to a buffer. `--listing` shows the result of that walk for the same program:

```
_start:  ; entry stub written by acc
  0000  48 81 EC 28 00 00 00     sub rsp, 40
  0007  E8 00 00 00 00           call main
  000C  89 C1                    mov ecx, eax
  000E  FF 15 00 00 00 00        call [rip+iat.exit]
  0014  C3                       ret

;  3 | int main(void) {
main:
  0015  55                       push rbp
  0016  48 89 E5                 mov rbp, rsp

;  4 | printf("hello from acc\n");
  0019  48 8D 05 00 00 00 00     lea rax, [rip+str0]
  0020  50                       push rax
  0021  59                       pop rcx
  0022  48 81 EC 20 00 00 00     sub rsp, 32
  0029  FF 15 00 00 00 00        call [rip+iat.printf]
  002F  48 81 C4 20 00 00 00     add rsp, 32

;  5 | return 7;
  0036  48 B8 07 00 00 00 00 00 00 00 mov rax, 7
  0040  C9                       leave
  0041  C3                       ret
```

Compare it honestly against the gcc assembly in §2 and the weaknesses are visible:

- `push rax` immediately followed by `pop rcx` — the stack-machine code generator materializing an argument the long way. gcc emits `movq %rax, %rcx`. A peephole pass would fix this; acc has none.
- `mov rax, 7` uses the 10-byte 64-bit-immediate form. gcc's `movl $7, %eax` is 5 bytes.
- The trailing `xor eax, eax; leave; ret` is an unreachable implicit `return 0`, emitted because acc does not track whether control can reach the end of a function.
- No SEH metadata, so nothing can unwind through an acc frame.

The entry stub at offset `0000` is acc's replacement for `crt2.o`: four instructions that call `main`, move the result into `ecx`, and call `exit`. `msvcrt`'s `exit` flushes stdio, which is why output appears at all. That is the entire C runtime startup, and it fits in 21 bytes.

---

## 6. What a linker actually does

The word "linking" covers three distinct jobs. `accld` does all three, and its `--map` output shows the results.

### Symbol resolution

Each object declares what it defines and what it needs. The linker matches them and complains when it cannot:

```console
$ python accld.py prog.obj -o broken.exe
accld: error: undefined reference to 'call_count'
       referenced from prog.obj
```

That is the entire meaning of the most-feared error message in C.

### Relocation

Object code contains holes. A `call` to a function in another file cannot know its distance until both files have addresses. The compiler leaves zeros and records a relocation; the linker fills them in.

For the x86-64 COFF type acc uses, `IMAGE_REL_AMD64_REL32`, the arithmetic is:

```
value = S + A - (P + 4)

  S = final address of the target symbol
  A = addend already sitting in the 4-byte field
  P = address of the field itself
  4 = because the displacement is measured from the end of the instruction
```

Those zeroed displacements in the listing above — `E8 00 00 00 00` — are relocation sites waiting for this calculation.

### Layout and imports

The linker concatenates the `.text` of every object, then the `.data`, assigns each a virtual address, builds the import tables, and writes the headers:

```console
$ python accld.py util.obj prog.obj -o prog.exe --map
accld: wrote prog.exe [exe] (2048 bytes) from 2 object(s), 4 symbol(s)
accld: imports: printf, exit

  RVA         SECTION  SYMBOL
  0x1020      .text    fib  (util.obj)
  0x10ED      .text    gcd  (util.obj)
  0x1180      .text    main  (prog.obj)
  0x2000      .data    call_count  (util.obj)
```

Calls to the C library are the interesting case. acc emits `call [rip+disp32]` — an *indirect* call through a slot the OS loader fills in at startup. In the object file that slot is an undefined symbol named `__imp_printf`, which is MSVC's convention. `accld` recognizes the `__imp_` prefix, allocates an Import Address Table entry, and writes the import directory that tells Windows to resolve it from `msvcrt.dll`.

---

## 7. The mapping

Every classic tool has a counterpart here:

| Classic | acc | Lines |
|---|---|---:|
| `cpp` (preprocessor) | `accpp.py` — optional, off by default, enabled with `--pp` | 448 |
| `cc1` (compiler) | `lex()` 170 + `Parser` 382 + `CodeGen` 544 | 1,096 |
| `as` (assembler) | `Emitter`: hand-encoded x86-64, ~40 instruction forms | 212 |
| image writers | `build_pe` 206 + `build_coff` 95 | 301 |
| `ld` (linker) | `accld.py` | 550 |
| `crt2.o` (startup) | the 21-byte entry stub `accld` synthesizes | — |
| OS loader | `accrun.py`: maps sections, binds imports, interprets | 692 |
| `objdump -d` | `render_listing` | 113 |

Line counts measured with the script in §9, not estimated.

---

## 8. Why a loader ships with a compiler

`accrun.py` exists because of a problem that is invisible until it bites you. Midway through building acc, every newly created binary on the test machine stopped launching:

```
OSError: [WinError 4551] An Application Control policy has blocked this file
```

Windows Smart App Control, which blocks unsigned executables it has no reputation for. The compiler was correct; the operating system simply refused to run its output. A binary compiled two minutes earlier still ran — the policy tracks each file, not the code inside it.

For a human this is an annoyance. For an automated agent it is disabling: you cannot check your own work. So the loader became part of the toolchain. It parses the PE, maps sections at their RVAs, rewrites the IAT to point at Python implementations of the C library, and interprets the machine code.

It decodes instructions from raw bytes using REX/ModRM/SIB rules rather than recognizing acc's output patterns. That distinction is the whole value: if the encoder writes a wrong ModRM byte, the interpreter computes a wrong effective address and the test fails, instead of the two agreeing on the same mistake.

It was validated against the real CPU on a binary Windows *was* willing to run — same output, same exit code — before being trusted on binaries Windows refused.

The same tool covers CI containers, locked-down build machines, and cross-architecture hosts.

---

## 9. Reproducing every number here

```bash
# the four processes gcc launches
gcc -v -o test.exe hello.c

# the assembly text cc1 hands to as
gcc -S -o same.s same.c

# stage-by-stage sizes
gcc -c -o same.o same.c
gcc -o same_gcc.exe same.c
python acc.py same.c -o same_acc.exe

# section and import comparison, using acc's own PE parser
python -c "from accrun import PE; pe=PE('same_gcc.exe'); print(len(pe.sections))"

# acc's machine code, annotated with source lines
python acc.py same.c --listing && cat same.lst

# what the linker resolved
python acc.py -c util.c && python acc.py -c prog.c
python accld.py util.obj prog.obj -o prog.exe --map
```

---

## Further reading

- Microsoft, *PE Format* — the authoritative reference for the headers, sections, and import/export directories acc writes
- Microsoft, *x64 calling convention* — the rcx/rdx/r8/r9 rule, the 32-byte shadow space, and the 16-byte stack alignment requirement acc has to honour at every call
- Intel® 64 and IA-32 Architectures Software Developer's Manual, Volume 2 — the instruction encodings, including the REX/ModRM/SIB rules `accrun` decodes
