#!/usr/bin/env python3
"""
acc -- the Agent C Compiler
===========================

A from-scratch C compiler that emits Windows x86-64 PE executables *directly*.

  no gcc.  no msvc.  no assembler.  no linker.  python stdlib only.

It contains its own lexer, parser, x86-64 machine-code encoder, and PE32+
executable writer. The bytes that land in the .exe are produced here.

Designed to be driven by an AI agent rather than a human at a terminal:

  --json        every diagnostic as machine-readable JSON (code/line/col/hint)
  --listing     annotated machine-code listing: offset, bytes, mnemonic, src line
  --dump-ast    the parse tree as JSON
  --run         compile then execute, reporting the real exit code
  deterministic same source in  ->  byte-identical .exe out (no timestamps)
  single file   zero dependencies; copy it anywhere and it works

Exit codes:  0 success   1 compile error   2 usage error   3 internal error
"""

import sys
import os
import json
import struct
import subprocess

VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

KEYWORDS = {"int", "char", "void", "if", "else", "while",
            "for", "return", "break", "continue", "extern"}

PUNCT = ("++", "--", "<=", ">=", "==", "!=", "&&", "||",
         "+=", "-=", "*=", "/=", "%=",
         "+", "-", "*", "/", "%", "=", "<", ">", "!", "&",
         "(", ")", "{", "}", ";", ",")


class CompileError(Exception):
    """One precise, non-cascading diagnostic."""

    def __init__(self, code, msg, line, col, hint=None):
        Exception.__init__(self, msg)
        self.code = code
        self.msg = msg
        self.line = line
        self.col = col
        self.hint = hint

    def to_dict(self, path):
        return {
            "severity": "error",
            "code": self.code,
            "file": path,
            "line": self.line,
            "col": self.col,
            "message": self.msg,
            "hint": self.hint,
        }

    def render(self, path, src):
        out = ["%s:%d:%d: error[%s]: %s" % (path, self.line, self.col,
                                            self.code, self.msg)]
        lines = src.splitlines()
        if 0 < self.line <= len(lines):
            out.append("  %4d | %s" % (self.line, lines[self.line - 1]))
            out.append("       | " + " " * max(0, self.col - 1) + "^")
        if self.hint:
            out.append("  hint: " + self.hint)
        return "\n".join(out)


# ---------------------------------------------------------------------------
# lexer
# ---------------------------------------------------------------------------

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\",
           "'": "'", '"': '"', "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def lex(src, path):
    toks = []
    i = 0
    line = 1
    col = 1
    n = len(src)

    def err(code, msg, hint=None):
        raise CompileError(code, msg, line, col, hint)

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            col = 1
            i += 1
            continue
        if c in " \t\r\f\v":
            i += 1
            col += 1
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                err("E0001", "unterminated block comment")
            line += src.count("\n", i, j)
            i = j + 2
            col = 1
            continue
        if src.startswith("#", i):
            # acc has no preprocessor; skip the directive line but say so later
            while i < n and src[i] != "\n":
                i += 1
            continue

        sl, sc = line, col

        if c.isdigit():
            j = i
            decimal = False
            if src[i:i + 2].lower() == "0x":
                j = i + 2
                while j < n and src[j] in "0123456789abcdefABCDEF":
                    j += 1
                if j == i + 2:
                    err("E0003", "hex literal has no digits")
                val = int(src[i + 2:j], 16)
            else:
                while j < n and src[j].isdigit():
                    j += 1
                text = src[i:j]
                if len(text) > 1 and text[0] == "0":
                    # a leading 0 makes the constant octal (C99 6.4.4.1)
                    for d in text:
                        if d in "89":
                            err("E0007", "invalid digit '%s' in octal "
                                "constant %s" % (d, text),
                                "a leading 0 means octal; drop it for a "
                                "decimal number")
                    val = int(text, 8)
                else:
                    decimal = True
                    val = int(text)
            # C99 6.4.4p2: a constant must be representable in some type.
            # acc has only signed 64-bit integers until its type system is
            # rebuilt, so a decimal constant must fit in a signed 64-bit
            # value, and a hex or octal one in 64 bits (kept as its bit
            # pattern, as unsigned long long would be).
            if (decimal and val > 0x7FFFFFFFFFFFFFFF) or val > 0xFFFFFFFFFFFFFFFF:
                err("E0008", "integer constant %s is too large for any "
                    "integer type" % src[i:j])
            if val > 0x7FFFFFFFFFFFFFFF:
                val -= 1 << 64
            col += j - i
            i = j
            toks.append(("num", val, sl, sc))
            continue

        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            col += j - i
            i = j
            toks.append(("kw" if word in KEYWORDS else "id", word, sl, sc))
            continue

        if c == '"':
            i += 1
            col += 1
            out = []
            while True:
                if i >= n or src[i] == "\n":
                    line, col = sl, sc
                    err("E0002", "unterminated string literal")
                if src[i] == '"':
                    i += 1
                    col += 1
                    break
                if src[i] == "\\":
                    if i + 1 >= n:
                        err("E0002", "unterminated escape sequence")
                    e = src[i + 1]
                    if e not in ESCAPES:
                        err("E0004", "unknown escape sequence \\%s" % e,
                            "acc supports: \n \t \r \0 \\ \' \\\" \a \b \f \v")
                    out.append(ESCAPES[e])
                    i += 2
                    col += 2
                else:
                    out.append(src[i])
                    i += 1
                    col += 1
            toks.append(("str", "".join(out), sl, sc))
            continue

        if c == "'":
            i += 1
            col += 1
            if i < n and src[i] == "\\":
                e = src[i + 1] if i + 1 < n else ""
                if e not in ESCAPES:
                    err("E0004", "unknown escape sequence \\%s" % e)
                ch = ESCAPES[e]
                i += 2
                col += 2
            else:
                if i >= n:
                    err("E0005", "unterminated character literal")
                ch = src[i]
                i += 1
                col += 1
            if i >= n or src[i] != "'":
                err("E0005", "unterminated character literal")
            i += 1
            col += 1
            toks.append(("num", ord(ch), sl, sc))
            continue

        for p in PUNCT:
            if src.startswith(p, i):
                i += len(p)
                col += len(p)
                toks.append(("punct", p, sl, sc))
                break
        else:
            err("E0006", "unexpected character %r" % c)

    toks.append(("eof", None, line, col))
    return toks


# ---------------------------------------------------------------------------
# types
#
# The int type in acc is 64-bit. That is a deliberate deviation from ISO C
# (where int is 32-bit) -- it keeps every value exactly one machine word, which
# makes the generated code far easier to audit. char is 1 byte, pointers are 8.
# printf("%d") still works, because it reads the low half of the word.
# ---------------------------------------------------------------------------

def T(kind, base=None):
    return {"k": kind, "base": base}


T_INT = T("int")
T_CHAR = T("char")
T_VOID = T("void")


def sizeof(t):
    if t["k"] == "char":
        return 1
    if t["k"] == "void":
        return 1
    return 8


def type_str(t):
    if t["k"] == "ptr":
        return type_str(t["base"]) + "*"
    return t["k"]


# ---------------------------------------------------------------------------
# parser  (recursive descent)
# ---------------------------------------------------------------------------

class Parser:
    def __init__(self, toks, path):
        self.toks = toks
        self.path = path
        self.p = 0
        self.strings = []

    # -- token helpers ------------------------------------------------------
    def peek(self, k=0):
        return self.toks[min(self.p + k, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.p]
        if t[0] != "eof":
            self.p += 1
        return t

    def at(self, kind, val=None):
        t = self.peek()
        return t[0] == kind and (val is None or t[1] == val)

    def accept(self, kind, val=None):
        if self.at(kind, val):
            return self.next()
        return None

    def expect(self, kind, val=None, what=None):
        t = self.peek()
        if t[0] == kind and (val is None or t[1] == val):
            return self.next()
        got = "end of file" if t[0] == "eof" else repr(str(t[1]))
        want = what or (repr(val) if val else kind)
        raise CompileError("E0100", "expected %s, found %s" % (want, got),
                           t[2], t[3])

    def err(self, code, msg, tok=None, hint=None):
        t = tok or self.peek()
        raise CompileError(code, msg, t[2], t[3], hint)

    # -- top level ----------------------------------------------------------
    def parse_program(self):
        funcs = []
        globs = []
        externs = []
        extern_globals = []
        while not self.at("eof"):
            is_extern = bool(self.accept("kw", "extern"))
            ty = self.parse_type()
            nt = self.expect("id", what="a name")
            name = nt[1]
            if self.at("punct", "("):
                f = self.parse_function(ty, name, nt)
                (externs if f.get("proto") else funcs).append(f)
            elif is_extern:
                self.expect("punct", ";")
                extern_globals.append({"name": name, "type": ty,
                                       "line": nt[2]})
            else:
                init = 0
                if self.accept("punct", "="):
                    neg = bool(self.accept("punct", "-"))
                    vt = self.peek()
                    if vt[0] != "num":
                        self.err("E0101",
                                 "global initializer must be an integer constant",
                                 vt,
                                 "acc does not evaluate static initializers yet")
                    self.next()
                    init = -vt[1] if neg else vt[1]
                self.expect("punct", ";")
                globs.append({"name": name, "type": ty, "init": init,
                              "line": nt[2]})
        return {"funcs": funcs, "globals": globs, "externs": externs,
                "extern_globals": extern_globals, "strings": self.strings}

    def parse_base_type(self):
        t = self.peek()
        if not (t[0] == "kw" and t[1] in ("int", "char", "void")):
            self.err("E0102",
                     "expected a type (int, char or void), found %r"
                     % (str(t[1]),), t)
        self.next()
        return {"int": T_INT, "char": T_CHAR, "void": T_VOID}[t[1]]

    def parse_pointers(self, ty):
        while self.accept("punct", "*"):
            ty = T("ptr", ty)
        return ty

    def parse_type(self):
        return self.parse_pointers(self.parse_base_type())

    def parse_function(self, ret, name, nt):
        self.expect("punct", "(")
        params = []
        if not self.at("punct", ")"):
            if self.at("kw", "void") and self.peek(1)[1] == ")":
                self.next()
            else:
                while True:
                    pty = self.parse_type()
                    pn = self.expect("id", what="a parameter name")
                    params.append({"type": pty, "name": pn[1]})
                    if not self.accept("punct", ","):
                        break
        self.expect("punct", ")")
        if len(params) > 4:
            self.err("E0103",
                     "function %r takes %d parameters; acc supports at most 4"
                     % (name, len(params)), nt,
                     "Win64 passes 4 arguments in rcx/rdx/r8/r9; acc does not "
                     "spill the rest onto the stack yet")
        if self.accept("punct", ";"):
            # a prototype: the definition lives in another object
            return {"proto": True, "name": name, "ret": ret,
                    "params": params, "line": nt[2], "col": nt[3]}
        body = self.parse_block()
        return {"name": name, "ret": ret, "params": params,
                "body": body, "line": nt[2], "col": nt[3]}

    # -- statements ---------------------------------------------------------
    def parse_block(self):
        lb = self.expect("punct", "{")
        body = []
        while not self.at("punct", "}"):
            if self.at("eof"):
                raise CompileError("E0104", "unterminated block", lb[2], lb[3],
                                   "this brace is never closed")
            body.append(self.parse_stmt())
        self.next()
        return {"k": "block", "body": body, "line": lb[2]}

    def parse_stmt(self):
        t = self.peek()
        if t[0] == "punct" and t[1] == "{":
            return self.parse_block()
        if t[0] == "punct" and t[1] == ";":
            self.next()
            return {"k": "empty", "line": t[2]}
        if t[0] == "kw":
            if t[1] == "if":
                return self.parse_if()
            if t[1] == "while":
                return self.parse_while()
            if t[1] == "for":
                return self.parse_for()
            if t[1] == "return":
                self.next()
                e = None
                if not self.at("punct", ";"):
                    e = self.parse_expr()
                self.expect("punct", ";")
                return {"k": "ret", "e": e, "line": t[2]}
            if t[1] in ("break", "continue"):
                self.next()
                self.expect("punct", ";")
                return {"k": t[1], "line": t[2]}
            if t[1] in ("int", "char", "void"):
                return self.parse_decl()
        e = self.parse_expr()
        self.expect("punct", ";")
        return {"k": "expr", "e": e, "line": t[2]}

    def parse_decl(self):
        t = self.peek()
        base = self.parse_base_type()
        decls = []
        while True:
            # the * belongs to each declarator, not to the shared base type:
            # in "int *a, b;" only a is a pointer (C99 6.7.5)
            ty = self.parse_pointers(base)
            nt = self.expect("id", what="a variable name")
            init = None
            if self.accept("punct", "="):
                init = self.parse_expr()
            decls.append({"k": "decl", "type": ty, "name": nt[1],
                          "init": init, "line": nt[2], "col": nt[3]})
            if not self.accept("punct", ","):
                break
        self.expect("punct", ";")
        if len(decls) == 1:
            return decls[0]
        # Several declarators in one declaration. This is deliberately not a
        # "block": a block opens its own scope, which made every variable in
        # "int a, b;" vanish the moment it was declared.
        return {"k": "decls", "body": decls, "line": t[2]}

    def parse_if(self):
        t = self.next()
        self.expect("punct", "(")
        c = self.parse_expr()
        self.expect("punct", ")")
        then = self.parse_stmt()
        els = None
        if self.accept("kw", "else"):
            els = self.parse_stmt()
        return {"k": "if", "c": c, "t": then, "e": els, "line": t[2]}

    def parse_while(self):
        t = self.next()
        self.expect("punct", "(")
        c = self.parse_expr()
        self.expect("punct", ")")
        return {"k": "while", "c": c, "b": self.parse_stmt(), "line": t[2]}

    def parse_for(self):
        t = self.next()
        self.expect("punct", "(")
        init = None
        if not self.at("punct", ";"):
            if self.at("kw") and self.peek()[1] in ("int", "char"):
                init = self.parse_decl()          # this consumes the ';'
            else:
                init = {"k": "expr", "e": self.parse_expr(), "line": t[2]}
                self.expect("punct", ";")
        else:
            self.next()
        cond = None
        if not self.at("punct", ";"):
            cond = self.parse_expr()
        self.expect("punct", ";")
        step = None
        if not self.at("punct", ")"):
            step = self.parse_expr()
        self.expect("punct", ")")
        return {"k": "for", "init": init, "c": cond, "step": step,
                "b": self.parse_stmt(), "line": t[2]}

    # -- expressions --------------------------------------------------------
    def parse_expr(self):
        return self.parse_assign()

    def parse_assign(self):
        left = self.parse_logor()
        t = self.peek()
        if t[0] == "punct" and t[1] == "=":
            self.next()
            right = self.parse_assign()
            if left["k"] not in ("var", "deref"):
                self.err("E0106", "left side of assignment is not assignable",
                         t, "assign to a variable, or through a pointer: *p = x")
            return {"k": "assign", "t": left, "v": right, "line": t[2]}
        if t[0] == "punct" and t[1] in ("+=", "-=", "*=", "/=", "%="):
            self.next()
            right = self.parse_assign()
            if left["k"] != "var":
                self.err("E0105",
                         "compound assignment needs a plain variable on the left",
                         t, "acc would otherwise evaluate the target twice")
            binop = {"k": "bin", "op": t[1][0], "l": left, "r": right,
                     "line": t[2]}
            return {"k": "assign", "t": left, "v": binop, "line": t[2]}
        return left

    def parse_logor(self):
        l = self.parse_logand()
        while self.at("punct", "||"):
            t = self.next()
            l = {"k": "or", "l": l, "r": self.parse_logand(), "line": t[2]}
        return l

    def parse_logand(self):
        l = self.parse_equality()
        while self.at("punct", "&&"):
            t = self.next()
            l = {"k": "and", "l": l, "r": self.parse_equality(), "line": t[2]}
        return l

    def parse_equality(self):
        l = self.parse_rel()
        while self.at("punct", "==") or self.at("punct", "!="):
            t = self.next()
            l = {"k": "bin", "op": t[1], "l": l, "r": self.parse_rel(),
                 "line": t[2]}
        return l

    def parse_rel(self):
        l = self.parse_add()
        while (self.at("punct", "<") or self.at("punct", ">")
               or self.at("punct", "<=") or self.at("punct", ">=")):
            t = self.next()
            l = {"k": "bin", "op": t[1], "l": l, "r": self.parse_add(),
                 "line": t[2]}
        return l

    def parse_add(self):
        l = self.parse_mul()
        while self.at("punct", "+") or self.at("punct", "-"):
            t = self.next()
            l = {"k": "bin", "op": t[1], "l": l, "r": self.parse_mul(),
                 "line": t[2]}
        return l

    def parse_mul(self):
        l = self.parse_unary()
        while (self.at("punct", "*") or self.at("punct", "/")
               or self.at("punct", "%")):
            t = self.next()
            l = {"k": "bin", "op": t[1], "l": l, "r": self.parse_unary(),
                 "line": t[2]}
        return l

    def parse_unary(self):
        t = self.peek()
        if t[0] == "punct":
            if t[1] == "-":
                self.next()
                return {"k": "neg", "e": self.parse_unary(), "line": t[2]}
            if t[1] == "+":
                self.next()
                return self.parse_unary()
            if t[1] == "!":
                self.next()
                return {"k": "not", "e": self.parse_unary(), "line": t[2]}
            if t[1] == "*":
                self.next()
                return {"k": "deref", "e": self.parse_unary(), "line": t[2]}
            if t[1] == "&":
                self.next()
                e = self.parse_unary()
                if e["k"] != "var":
                    self.err("E0107", "address-of needs a variable", t)
                return {"k": "addr", "e": e, "line": t[2]}
            if t[1] in ("++", "--"):
                self.next()
                e = self.parse_unary()
                if e["k"] != "var":
                    self.err("E0108", "%s needs a variable" % t[1], t)
                return {"k": "preinc", "op": t[1], "e": e, "line": t[2]}
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_primary()
        while True:
            t = self.peek()
            if t[0] == "punct" and t[1] in ("++", "--"):
                self.next()
                if e["k"] != "var":
                    self.err("E0108", "%s needs a variable" % t[1], t)
                e = {"k": "postinc", "op": t[1], "e": e, "line": t[2]}
                continue
            return e

    def parse_primary(self):
        t = self.next()
        if t[0] == "num":
            return {"k": "num", "v": t[1], "line": t[2]}
        if t[0] == "str":
            if t[1] in self.strings:
                idx = self.strings.index(t[1])
            else:
                idx = len(self.strings)
                self.strings.append(t[1])
            return {"k": "str", "v": idx, "line": t[2]}
        if t[0] == "id":
            if self.at("punct", "("):
                self.next()
                args = []
                if not self.at("punct", ")"):
                    while True:
                        args.append(self.parse_expr())
                        if not self.accept("punct", ","):
                            break
                self.expect("punct", ")")
                return {"k": "call", "name": t[1], "args": args,
                        "line": t[2], "col": t[3]}
            return {"k": "var", "name": t[1], "line": t[2], "col": t[3]}
        if t[0] == "punct" and t[1] == "(":
            e = self.parse_expr()
            self.expect("punct", ")")
            return e
        got = "end of file" if t[0] == "eof" else repr(str(t[1]))
        raise CompileError("E0109", "expected an expression, found %s" % got,
                           t[2], t[3])


# ---------------------------------------------------------------------------
# x86-64 machine code emitter
#
# Every instruction acc can produce is encoded by hand below. No assembler is
# involved at any point: these are the literal bytes that end up in .text.
# Each emit() also records a listing entry so --listing can show you exactly
# what was generated and which source line produced it.
# ---------------------------------------------------------------------------

def i32(v):
    return struct.pack("<i", v)


def i64(v):
    return struct.pack("<q", v)


class Emitter:
    def __init__(self):
        self.buf = bytearray()
        self.rel = []        # (kind, key, pos)  kind: label | func
        self.rip = []        # (kind, key, pos)  kind: str | glob | iat
        self.listing = []
        self.depth = 0       # 8-byte slots currently pushed (stack alignment)
        self.line = 0

    def emit(self, bs, text):
        self.listing.append((len(self.buf), bytes(bs), text, self.line))
        self.buf += bs

    def here(self):
        return len(self.buf)

    # -- prologue / epilogue ------------------------------------------------
    def push_rbp(self):
        self.emit(b"\x55", "push rbp")

    def mov_rbp_rsp(self):
        self.emit(b"\x48\x89\xE5", "mov rbp, rsp")

    def sub_rsp(self, n):
        self.emit(b"\x48\x81\xEC" + i32(n), "sub rsp, %d" % n)

    def add_rsp(self, n):
        self.emit(b"\x48\x81\xC4" + i32(n), "add rsp, %d" % n)

    def leave(self):
        self.emit(b"\xC9", "leave")

    def ret(self):
        self.emit(b"\xC3", "ret")

    # -- moves --------------------------------------------------------------
    def mov_rax_imm(self, v):
        self.emit(b"\x48\xB8" + i64(v), "mov rax, %d" % v)

    def xor_eax_eax(self):
        self.emit(b"\x31\xC0", "xor eax, eax")

    def mov_rcx_rax(self):
        self.emit(b"\x48\x89\xC1", "mov rcx, rax")

    def mov_rax_rcx(self):
        self.emit(b"\x48\x89\xC8", "mov rax, rcx")

    def mov_rax_rdx(self):
        self.emit(b"\x48\x89\xD0", "mov rax, rdx")

    def mov_ecx_eax(self):
        self.emit(b"\x89\xC1", "mov ecx, eax")

    # -- locals (rbp-relative) ---------------------------------------------
    def load_local(self, off, name):
        self.emit(b"\x48\x8B\x85" + i32(-off), "mov rax, [rbp-%d]  ; %s" % (off, name))

    def store_local_rax(self, off, name):
        self.emit(b"\x48\x89\x85" + i32(-off), "mov [rbp-%d], rax  ; %s" % (off, name))

    def store_local_rcx(self, off, name):
        self.emit(b"\x48\x89\x8D" + i32(-off), "mov [rbp-%d], rcx  ; %s" % (off, name))

    def lea_local(self, off, name):
        self.emit(b"\x48\x8D\x85" + i32(-off), "lea rax, [rbp-%d]  ; &%s" % (off, name))

    def store_arg_reg(self, idx, off, name):
        # mov [rbp-off], <rcx|rdx|r8|r9>
        enc = [b"\x48\x89\x8D", b"\x48\x89\x95", b"\x4C\x89\x85", b"\x4C\x89\x8D"]
        reg = ["rcx", "rdx", "r8", "r9"][idx]
        self.emit(enc[idx] + i32(-off), "mov [rbp-%d], %s  ; param %s" % (off, reg, name))

    # -- globals / strings / imports (rip-relative) -------------------------
    def load_global(self, name):
        self.emit(b"\x48\x8B\x05" + i32(0), "mov rax, [rip+%s]" % name)
        self.rip.append(("glob", name, self.here() - 4))

    def store_global_rax(self, name):
        self.emit(b"\x48\x89\x05" + i32(0), "mov [rip+%s], rax" % name)
        self.rip.append(("glob", name, self.here() - 4))

    def store_global_rcx(self, name):
        self.emit(b"\x48\x89\x0D" + i32(0), "mov [rip+%s], rcx" % name)
        self.rip.append(("glob", name, self.here() - 4))

    def lea_global(self, name):
        self.emit(b"\x48\x8D\x05" + i32(0), "lea rax, [rip+%s]" % name)
        self.rip.append(("glob", name, self.here() - 4))

    def lea_string(self, idx):
        self.emit(b"\x48\x8D\x05" + i32(0), "lea rax, [rip+str%d]" % idx)
        self.rip.append(("str", idx, self.here() - 4))

    def call_import(self, name):
        self.emit(b"\xFF\x15" + i32(0), "call [rip+iat.%s]" % name)
        self.rip.append(("iat", name, self.here() - 4))

    def call_func(self, name):
        self.emit(b"\xE8" + i32(0), "call %s" % name)
        self.rel.append(("func", name, self.here() - 4))

    # -- pointer memory -----------------------------------------------------
    def load_qword_at_rax(self):
        self.emit(b"\x48\x8B\x00", "mov rax, [rax]")

    def load_byte_at_rax(self):
        # plain char is signed on Windows, so a char read sign-extends
        self.emit(b"\x48\x0F\xBE\x00", "movsx rax, byte [rax]")

    def movsx_rax_al(self):
        self.emit(b"\x48\x0F\xBE\xC0", "movsx rax, al")

    def movsx_rcx_cl(self):
        self.emit(b"\x48\x0F\xBE\xC9", "movsx rcx, cl")

    def store_qword_at_rcx(self):
        self.emit(b"\x48\x89\x01", "mov [rcx], rax")

    def store_byte_at_rcx(self):
        self.emit(b"\x88\x01", "mov [rcx], al")

    # -- stack --------------------------------------------------------------
    def push_rax(self):
        self.emit(b"\x50", "push rax")
        self.depth += 1

    def pop_rax(self):
        self.emit(b"\x58", "pop rax")
        self.depth -= 1

    def pop_rcx(self):
        self.emit(b"\x59", "pop rcx")
        self.depth -= 1

    def pop_arg(self, idx):
        enc = [b"\x59", b"\x5A", b"\x41\x58", b"\x41\x59"]
        reg = ["rcx", "rdx", "r8", "r9"][idx]
        self.emit(enc[idx], "pop %s" % reg)
        self.depth -= 1

    # -- arithmetic ---------------------------------------------------------
    def add_rax_rcx(self):
        self.emit(b"\x48\x01\xC8", "add rax, rcx")

    def sub_rax_rcx(self):
        self.emit(b"\x48\x29\xC8", "sub rax, rcx")

    def imul_rax_rcx(self):
        self.emit(b"\x48\x0F\xAF\xC1", "imul rax, rcx")

    def imul_rax_imm(self, v):
        self.emit(b"\x48\x69\xC0" + i32(v), "imul rax, rax, %d" % v)

    def add_rcx_imm(self, v):
        self.emit(b"\x48\x81\xC1" + i32(v), "add rcx, %d" % v)

    def cqo(self):
        self.emit(b"\x48\x99", "cqo")

    def movsxd_rax_eax(self):
        self.emit(b"\x48\x63\xC0", "movsxd rax, eax")

    def idiv_rcx(self):
        self.emit(b"\x48\xF7\xF9", "idiv rcx")

    def neg_rax(self):
        self.emit(b"\x48\xF7\xD8", "neg rax")

    def cmp_rax_rcx(self):
        self.emit(b"\x48\x39\xC8", "cmp rax, rcx")

    def test_rax_rax(self):
        self.emit(b"\x48\x85\xC0", "test rax, rax")

    SETCC = {"<": (0x9C, "setl"), ">": (0x9F, "setg"),
             "<=": (0x9E, "setle"), ">=": (0x9D, "setge"),
             "==": (0x94, "sete"), "!=": (0x95, "setne")}

    def setcc_rax(self, op):
        code, mn = self.SETCC[op]
        self.emit(bytes([0x0F, code, 0xC0]), "%s al" % mn)
        self.emit(b"\x48\x0F\xB6\xC0", "movzx rax, al")

    def sete_rax(self):
        self.emit(b"\x0F\x94\xC0", "sete al")
        self.emit(b"\x48\x0F\xB6\xC0", "movzx rax, al")

    # -- jumps --------------------------------------------------------------
    def jmp(self, label):
        self.emit(b"\xE9" + i32(0), "jmp L%d" % label)
        self.rel.append(("label", label, self.here() - 4))

    def je(self, label):
        self.emit(b"\x0F\x84" + i32(0), "je L%d" % label)
        self.rel.append(("label", label, self.here() - 4))

    def jne(self, label):
        self.emit(b"\x0F\x85" + i32(0), "jne L%d" % label)
        self.rel.append(("label", label, self.here() - 4))


# ---------------------------------------------------------------------------
# the C library surface acc knows how to import from msvcrt.dll
# ---------------------------------------------------------------------------

LIBC_RET = {
    "printf": T_INT, "puts": T_INT, "putchar": T_INT, "getchar": T_INT,
    "exit": T_VOID, "abort": T_VOID, "free": T_VOID, "srand": T_VOID,
    "malloc": T("ptr", T_VOID), "calloc": T("ptr", T_VOID),
    "realloc": T("ptr", T_VOID), "memset": T("ptr", T_VOID),
    "memcpy": T("ptr", T_VOID), "strcpy": T("ptr", T_CHAR),
    "strlen": T_INT, "strcmp": T_INT, "atoi": T_INT, "abs": T_INT,
    "rand": T_INT, "fflush": T_INT, "time": T_INT, "clock": T_INT,
    "system": T_INT, "toupper": T_INT, "tolower": T_INT,
}

# Library functions whose C return type is int or long: 32 bits on Win64,
# returned in eax with the upper half of rax undefined. acc's own int is
# still 64-bit, so these results are sign-extended after the call. strlen
# (size_t) and time (time_t) really are 64-bit and must not be.
LIBC_RET_INT32 = {
    "printf", "puts", "putchar", "getchar", "strcmp", "atoi", "abs",
    "rand", "fflush", "clock", "system", "toupper", "tolower",
}


# ---------------------------------------------------------------------------
# code generator
# ---------------------------------------------------------------------------

class CodeGen:
    def __init__(self, prog, path, dll=False, obj=False):
        self.prog = prog
        self.path = path
        self.dll = dll
        self.obj = obj
        self.funcs = {}
        for f in prog["funcs"]:
            if f["name"] in self.funcs:
                raise CompileError("E0120",
                                   "function %r is defined more than once"
                                   % f["name"], f["line"], f["col"])
            self.funcs[f["name"]] = f
        self.globals = {}
        for g in prog["globals"]:
            self.globals[g["name"]] = g
        self.externs = {}
        for f in prog.get("externs", []):
            if f["name"] not in self.funcs:
                self.externs[f["name"]] = f
        self.extern_globals = {}
        for g in prog.get("extern_globals", []):
            if g["name"] not in self.globals:
                self.extern_globals[g["name"]] = g
        self.em = Emitter()
        self.imports = []
        self.func_off = {}
        self.labels = {}
        self.nlabel = 0
        self.scopes = []
        self.loops = []

    # -- helpers ------------------------------------------------------------
    def err(self, code, msg, node, hint=None):
        raise CompileError(code, msg, node.get("line", 0),
                           node.get("col", 1), hint)

    def new_label(self):
        self.nlabel += 1
        return self.nlabel

    def place(self, label):
        self.labels[label] = self.em.here()
        self.em.listing.append((self.em.here(), b"", "L%d:" % label,
                                self.em.line))

    def need_import(self, name):
        if name not in self.imports:
            self.imports.append(name)

    def lookup(self, name, node):
        for sc in reversed(self.scopes):
            if name in sc:
                return sc[name]
        if name in self.globals:
            g = self.globals[name]
            return {"where": "global", "name": name, "type": g["type"]}
        if name in self.extern_globals:
            if not self.obj:
                self.err("E0115",
                         "extern variable %r has no definition in this file"
                         % name, node,
                         "compile with -c and link the objects with accld")
            g = self.extern_globals[name]
            return {"where": "global", "name": name, "type": g["type"]}
        hint = None
        if name in self.funcs or name in LIBC_RET:
            hint = "%r is a function; did you mean %s(...)?" % (name, name)
        self.err("E0110", "undefined variable %r" % name, node, hint)

    # -- driver -------------------------------------------------------------
    def generate(self):
        if not self.dll and not self.obj:
            if "main" not in self.funcs:
                raise CompileError("E0121", "no main function", 1, 1,
                                   "every acc program needs: "
                                   "int main(void) { ... }")
            self.need_import("exit")
            self.gen_entry()
        for f in self.prog["funcs"]:
            self.gen_func(f)
        self.resolve_internal()
        return self.em

    def gen_entry(self):
        em = self.em
        em.line = 0
        em.listing.append((0, b"", "_start:  ; entry stub written by acc", 0))
        em.sub_rsp(40)                 # 32 shadow + 8 to realign the stack
        em.call_func("main")
        em.mov_ecx_eax()               # exit code = main return value
        em.call_import("exit")         # msvcrt exit flushes stdio, then ends
        em.ret()                       # unreachable, kept for disassemblers

    # -- functions ----------------------------------------------------------
    def assign_slots(self, node, counter):
        """Give every declaration in the function its own stack slot."""
        if not isinstance(node, dict):
            return counter
        k = node.get("k")
        if k == "decl":
            node["slot"] = counter
            counter += 1
        for key in ("body", "t", "e", "b", "init", "step", "c"):
            v = node.get(key)
            if isinstance(v, list):
                for item in v:
                    counter = self.assign_slots(item, counter)
            elif isinstance(v, dict):
                counter = self.assign_slots(v, counter)
        return counter

    def gen_func(self, f):
        em = self.em
        em.line = f["line"]
        self.func_off[f["name"]] = em.here()
        em.listing.append((em.here(), b"", "%s:" % f["name"], f["line"]))

        nslots = self.assign_slots(f["body"], len(f["params"]))
        frame = nslots * 8
        if frame % 16:
            frame += 16 - (frame % 16)
        self.frame = frame
        self.cur_func = f

        em.push_rbp()
        em.mov_rbp_rsp()
        if frame:
            em.sub_rsp(frame)
        em.depth = 0

        self.scopes = [{}]
        for i, p in enumerate(f["params"]):
            off = (i + 1) * 8
            self.scopes[0][p["name"]] = {"where": "local", "off": off,
                                         "type": p["type"], "name": p["name"]}
            em.store_arg_reg(i, off, p["name"])
            if p["type"]["k"] == "char":
                # a prototyped call converts the argument to the parameter
                # type (C99 6.5.2.2p7); do it here so every caller is covered
                em.load_local(off, p["name"])
                self.narrow(p["type"])
                em.store_local_rax(off, p["name"])

        self.gen_stmt(f["body"])

        # implicit return 0 if control reaches the end
        em.xor_eax_eax()
        em.leave()
        em.ret()
        self.scopes = []

    # -- statements ---------------------------------------------------------
    def gen_stmt(self, s):
        em = self.em
        k = s["k"]
        em.line = s.get("line", em.line)

        if k == "block":
            self.scopes.append({})
            for st in s["body"]:
                self.gen_stmt(st)
            self.scopes.pop()
            return

        if k == "decls":
            # declarators that share one declaration live in the enclosing
            # scope, so no scope is opened here
            for st in s["body"]:
                self.gen_stmt(st)
            return

        if k == "empty":
            return

        if k == "decl":
            off = (s["slot"] + 1) * 8
            if s["init"] is not None:
                self.gen_expr(s["init"])
                self.narrow(s["type"])
            else:
                em.xor_eax_eax()
            em.store_local_rax(off, s["name"])
            self.scopes[-1][s["name"]] = {"where": "local", "off": off,
                                          "type": s["type"], "name": s["name"]}
            return

        if k == "expr":
            self.gen_expr(s["e"])
            return

        if k == "ret":
            if s["e"] is not None:
                self.gen_expr(s["e"])
                # return converts to the function's return type (6.8.6.4p3)
                self.narrow(self.cur_func["ret"])
            else:
                em.xor_eax_eax()
            em.leave()
            em.ret()
            return

        if k == "if":
            lelse = self.new_label()
            lend = self.new_label()
            self.gen_expr(s["c"])
            em.test_rax_rax()
            em.je(lelse)
            self.gen_stmt(s["t"])
            if s["e"] is not None:
                em.jmp(lend)
                self.place(lelse)
                self.gen_stmt(s["e"])
                self.place(lend)
            else:
                self.place(lelse)
            return

        if k == "while":
            ltop = self.new_label()
            lend = self.new_label()
            self.place(ltop)
            self.gen_expr(s["c"])
            em.test_rax_rax()
            em.je(lend)
            self.loops.append((lend, ltop))
            self.gen_stmt(s["b"])
            self.loops.pop()
            em.jmp(ltop)
            self.place(lend)
            return

        if k == "for":
            self.scopes.append({})
            if s["init"] is not None:
                self.gen_stmt(s["init"])
            ltop = self.new_label()
            lstep = self.new_label()
            lend = self.new_label()
            self.place(ltop)
            if s["c"] is not None:
                self.gen_expr(s["c"])
                em.test_rax_rax()
                em.je(lend)
            self.loops.append((lend, lstep))
            self.gen_stmt(s["b"])
            self.loops.pop()
            self.place(lstep)
            if s["step"] is not None:
                self.gen_expr(s["step"])
            em.jmp(ltop)
            self.place(lend)
            self.scopes.pop()
            return

        if k in ("break", "continue"):
            if not self.loops:
                self.err("E0122", "%s outside of a loop" % k, s)
            lend, lcont = self.loops[-1]
            em.jmp(lend if k == "break" else lcont)
            return

        raise CompileError("E9001", "internal: unknown statement %r" % k,
                           s.get("line", 0), 1)

    # -- expressions --------------------------------------------------------
    def gen_expr(self, e):
        em = self.em
        k = e["k"]
        em.line = e.get("line", em.line)

        if k == "num":
            em.mov_rax_imm(e["v"])
            return T_INT

        if k == "str":
            em.lea_string(e["v"])
            return T("ptr", T_CHAR)

        if k == "var":
            sym = self.lookup(e["name"], e)
            if sym["where"] == "local":
                em.load_local(sym["off"], sym["name"])
            else:
                em.load_global(sym["name"])
            return sym["type"]

        if k == "addr":
            sym = self.lookup(e["e"]["name"], e["e"])
            if sym["where"] == "local":
                em.lea_local(sym["off"], sym["name"])
            else:
                em.lea_global(sym["name"])
            return T("ptr", sym["type"])

        if k == "deref":
            t = self.gen_expr(e["e"])
            if t["k"] != "ptr":
                self.err("E0130", "cannot dereference a value of type %s"
                         % type_str(t), e)
            base = t["base"]
            if base["k"] == "void":
                self.err("E0131", "cannot dereference void*", e,
                         "cast is not supported; declare the pointer as "
                         "int* or char*")
            if sizeof(base) == 1:
                em.load_byte_at_rax()
            else:
                em.load_qword_at_rax()
            return base

        if k == "neg":
            t = self.gen_expr(e["e"])
            em.neg_rax()
            return t

        if k == "not":
            self.gen_expr(e["e"])
            em.test_rax_rax()
            em.sete_rax()
            return T_INT

        if k in ("preinc", "postinc"):
            return self.gen_inc(e, k == "postinc")

        if k == "assign":
            return self.gen_assign(e)

        if k == "call":
            return self.gen_call(e)

        if k in ("and", "or"):
            return self.gen_logical(e)

        if k == "bin":
            return self.gen_bin(e)

        raise CompileError("E9002", "internal: unknown expression %r" % k,
                           e.get("line", 0), 1)

    def narrow(self, ty):
        """Convert the value in rax to ty's range before storing it.

        acc still keeps every object in an 8-byte slot, so a char has to be
        cut down to one signed byte on the way in (C99 6.3.1.3); otherwise
        "char c = 300;" kept 300.
        """
        if ty["k"] == "char":
            self.em.movsx_rax_al()

    def gen_inc(self, e, post):
        em = self.em
        sym = self.lookup(e["e"]["name"], e["e"])
        ty = sym["type"]
        step = sizeof(ty["base"]) if ty["k"] == "ptr" else 1
        if e["op"] == "--":
            step = -step
        if sym["where"] == "local":
            em.load_local(sym["off"], sym["name"])
        else:
            em.load_global(sym["name"])
        em.mov_rcx_rax()
        em.add_rcx_imm(step)
        if ty["k"] == "char":
            em.movsx_rcx_cl()              # a char holds one signed byte
        if sym["where"] == "local":
            em.store_local_rcx(sym["off"], sym["name"])
        else:
            em.store_global_rcx(sym["name"])
        if not post:
            em.mov_rax_rcx()
        return ty

    def gen_assign(self, e):
        em = self.em
        target = e["t"]
        if target["k"] == "var":
            sym = self.lookup(target["name"], target)
            self.gen_expr(e["v"])
            self.narrow(sym["type"])
            if sym["where"] == "local":
                em.store_local_rax(sym["off"], sym["name"])
            else:
                em.store_global_rax(sym["name"])
            return sym["type"]
        # *p = value
        t = self.gen_expr(target["e"])
        if t["k"] != "ptr":
            self.err("E0130", "cannot assign through a value of type %s"
                     % type_str(t), target)
        em.push_rax()
        vt = self.gen_expr(e["v"])
        em.pop_rcx()
        if sizeof(t["base"]) == 1:
            em.store_byte_at_rcx()
        else:
            em.store_qword_at_rcx()
        return vt

    def gen_logical(self, e):
        em = self.em
        lfalse = self.new_label()
        lend = self.new_label()
        if e["k"] == "and":
            self.gen_expr(e["l"])
            em.test_rax_rax()
            em.je(lfalse)
            self.gen_expr(e["r"])
            em.test_rax_rax()
            em.je(lfalse)
            em.mov_rax_imm(1)
            em.jmp(lend)
            self.place(lfalse)
            em.xor_eax_eax()
            self.place(lend)
        else:
            ltrue = self.new_label()
            self.gen_expr(e["l"])
            em.test_rax_rax()
            em.jne(ltrue)
            self.gen_expr(e["r"])
            em.test_rax_rax()
            em.jne(ltrue)
            em.xor_eax_eax()
            em.jmp(lend)
            self.place(ltrue)
            em.mov_rax_imm(1)
            self.place(lend)
        return T_INT

    def gen_bin(self, e):
        em = self.em
        op = e["op"]
        lt = self.gen_expr(e["l"])
        em.push_rax()
        rt = self.gen_expr(e["r"])

        lptr = lt["k"] == "ptr"
        rptr = rt["k"] == "ptr"

        if op in ("+", "-") and lptr and not rptr:
            scale = sizeof(lt["base"])
            if scale != 1:
                em.imul_rax_imm(scale)
        em.mov_rcx_rax()
        em.pop_rax()
        if op == "+" and rptr and not lptr:
            scale = sizeof(rt["base"])
            if scale != 1:
                em.imul_rax_imm(scale)

        if op == "+":
            em.add_rax_rcx()
            return lt if lptr else (rt if rptr else T_INT)
        if op == "-":
            em.sub_rax_rcx()
            if lptr and rptr:
                scale = sizeof(lt["base"])
                if scale != 1:
                    em.emit(b"\x48\xC7\xC1" + i32(scale), "mov rcx, %d" % scale)
                    em.cqo()
                    em.idiv_rcx()
                return T_INT
            return lt if lptr else T_INT
        if op == "*":
            em.imul_rax_rcx()
            return T_INT
        if op in ("/", "%"):
            em.cqo()
            em.idiv_rcx()
            if op == "%":
                em.mov_rax_rdx()
            return T_INT
        if op in Emitter.SETCC:
            em.cmp_rax_rcx()
            em.setcc_rax(op)
            return T_INT
        raise CompileError("E9003", "internal: unknown operator %r" % op,
                           e.get("line", 0), 1)

    def gen_call(self, e):
        em = self.em
        name = e["name"]
        args = e["args"]

        if name in self.funcs:
            f = self.funcs[name]
            if len(args) != len(f["params"]):
                self.err("E0112",
                         "%r expects %d argument(s), got %d"
                         % (name, len(f["params"]), len(args)), e)
            ret = f["ret"]
            direct = True
        elif name in self.externs:
            f = self.externs[name]
            if len(args) != len(f["params"]):
                self.err("E0112",
                         "%r expects %d argument(s), got %d"
                         % (name, len(f["params"]), len(args)), e)
            ret = f["ret"]
            direct = True
        elif name in LIBC_RET:
            ret = LIBC_RET[name]
            direct = False
            self.need_import(name)
        else:
            known = ", ".join(sorted(LIBC_RET))
            self.err("E0113", "unknown function %r" % name, e,
                     "define it above, or use a C library function acc can "
                     "import: " + known)

        # Win64 argument passing: the first four go in rcx/rdx/r8/r9, the rest
        # sit above the 32-byte shadow space. Arguments are evaluated right to
        # left so that popping them yields exactly that layout -- C leaves the
        # evaluation order unspecified, so this is a legal choice.
        n = len(args)
        depth0 = em.depth
        nstack = max(0, n - 4)

        # rsp must be 16-byte aligned at the call instruction
        pad = (depth0 + nstack) % 2
        if pad:
            em.sub_rsp(8)
            em.depth += 1

        for a in reversed(args):
            self.gen_expr(a)
            em.push_rax()
        for i in range(min(n, 4)):
            em.pop_arg(i)

        em.sub_rsp(32)                     # shadow space
        if direct:
            em.call_func(name)
        else:
            em.call_import(name)
            if name in LIBC_RET_INT32:
                em.movsxd_rax_eax()
        em.add_rsp(32 + 8 * nstack + 8 * pad)
        em.depth = depth0
        return ret

    # -- fixups that stay inside .text --------------------------------------
    def resolve_internal(self):
        em = self.em
        for kind, key, pos in em.rel:
            if kind == "label":
                target = self.labels[key]
            else:
                if self.obj:
                    continue          # accld resolves calls across objects
                if key not in self.func_off:
                    raise CompileError("E0114", "call to undefined function %r"
                                       % key, 0, 1)
                target = self.func_off[key]
            struct.pack_into("<i", em.buf, pos, target - (pos + 4))


# ---------------------------------------------------------------------------
# PE32+ executable writer
#
# acc builds the whole file itself: DOS header, COFF header, optional header,
# section table, an import directory bound to msvcrt.dll, and the two sections.
# There is no linker in this pipeline.
# ---------------------------------------------------------------------------

IMAGE_BASE = 0x140000000
SECT_ALIGN = 0x1000
FILE_ALIGN = 0x200


def align_up(v, a):
    return (v + a - 1) // a * a


def global_init_value(g):
    """A global's initial value, converted to the range of its type.

    Globals still occupy 8 bytes each, so a char global is stored as its
    sign-extended byte: "char g = 200;" holds -56, as it does under gcc.
    """
    v = g["init"]
    if g["type"]["k"] == "char":
        v = ((v + 128) & 0xFF) - 128
    return v


def build_pe(em, strings, globals_list, imports, entry_off=0,
             dll=False, exports=None, module_name="module.dll"):
    text = bytearray(em.buf)
    image_base = 0x180000000 if dll else IMAGE_BASE
    exports = sorted(exports or [])     # the loader binary-searches names
    text_rva = SECT_ALIGN
    data_rva = text_rva + align_up(len(text), SECT_ALIGN)

    data = bytearray()

    str_rva = {}
    for i, s in enumerate(strings):
        str_rva[i] = data_rva + len(data)
        data += s.encode("utf-8") + b"\x00"

    while len(data) % 8:
        data += b"\x00"
    glob_rva = {}
    for g in globals_list:
        glob_rva[g["name"]] = data_rva + len(data)
        data += struct.pack("<q", global_init_value(g))

    while len(data) % 8:
        data += b"\x00"
    name_rva = {}
    for fn in imports:
        if len(data) % 2:
            data += b"\x00"
        name_rva[fn] = data_rva + len(data)
        data += struct.pack("<H", 0) + fn.encode("ascii") + b"\x00"

    if len(data) % 2:
        data += b"\x00"
    dll_rva = data_rva + len(data)
    data += b"msvcrt.dll\x00"

    while len(data) % 8:
        data += b"\x00"
    ilt_rva = data_rva + len(data)
    for fn in imports:
        data += struct.pack("<Q", name_rva[fn])
    data += struct.pack("<Q", 0)

    iat_rva = data_rva + len(data)
    iat = {}
    for fn in imports:
        iat[fn] = data_rva + len(data)
        data += struct.pack("<Q", name_rva[fn])
    data += struct.pack("<Q", 0)
    iat_size = 8 * (len(imports) + 1)

    desc_rva = data_rva + len(data)
    data += struct.pack("<IIIII", ilt_rva, 0, 0, dll_rva, iat_rva)
    data += b"\x00" * 20
    desc_size = 40

    # export directory (DLLs only)
    exp_dir_rva = 0
    exp_dir_size = 0
    if exports:
        while len(data) % 4:
            data += b"\x00"
        func_arr_rva = data_rva + len(data)
        for _, off in exports:
            data += struct.pack("<I", text_rva + off)
        name_arr_rva = data_rva + len(data)
        name_arr_pos = len(data)
        data += b"\x00" * (4 * len(exports))        # patched below
        ord_arr_rva = data_rva + len(data)
        for i in range(len(exports)):
            data += struct.pack("<H", i)
        name_rvas = []
        for nm, _ in exports:
            name_rvas.append(data_rva + len(data))
            data += nm.encode("ascii") + b"\x00"
        modname_rva = data_rva + len(data)
        data += module_name.encode("ascii") + b"\x00"
        for i, r in enumerate(name_rvas):
            struct.pack_into("<I", data, name_arr_pos + 4 * i, r)
        while len(data) % 4:
            data += b"\x00"
        exp_dir_rva = data_rva + len(data)
        data += struct.pack("<IIHHIIIIIII", 0, 0, 0, 0, modname_rva, 1,
                            len(exports), len(exports),
                            func_arr_rva, name_arr_rva, ord_arr_rva)
        exp_dir_size = 40

    for kind, key, pos in em.rip:
        if kind == "str":
            target = str_rva[key]
        elif kind == "glob":
            target = glob_rva[key]
        else:
            target = iat[key]
        struct.pack_into("<i", text, pos, target - (text_rva + pos + 4))

    # Base relocations. Code acc emits is entirely rip-relative, so there is
    # genuinely nothing to fix up -- but a DLL with no relocation directory
    # cannot be rebased, and two such DLLs sharing a preferred image base then
    # refuse to coexist in one process (WinError 487). One block of ABSOLUTE
    # entries, which are defined as no-ops, is how an image says
    # "relocatable, nothing to do".
    reloc = b""
    if dll:
        reloc = struct.pack("<II", text_rva, 12) + struct.pack("<HH", 0, 0)

    text_raw = align_up(len(text), FILE_ALIGN)
    data_raw = align_up(len(data), FILE_ALIGN)
    reloc_raw = align_up(len(reloc), FILE_ALIGN) if reloc else 0
    headers_size = FILE_ALIGN
    text_off = headers_size
    data_off = text_off + text_raw
    reloc_off = data_off + data_raw if reloc else 0
    reloc_rva = data_rva + align_up(len(data), SECT_ALIGN) if reloc else 0
    nsections = 3 if reloc else 2
    size_of_image = (reloc_rva + align_up(len(reloc), SECT_ALIGN) if reloc
                     else data_rva + align_up(len(data), SECT_ALIGN))

    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<H", dos, 0x02, 0x90)
    struct.pack_into("<H", dos, 0x04, 0x03)
    struct.pack_into("<H", dos, 0x08, 0x04)
    struct.pack_into("<H", dos, 0x18, 0x40)
    struct.pack_into("<I", dos, 0x3C, 0x80)
    msg = b"This program cannot be run in DOS mode.\r\n$"
    dos[0x4E:0x4E + len(msg)] = msg

    # TimeDateStamp is deliberately 0 so builds are reproducible.
    # 0x2000 = IMAGE_FILE_DLL. Executables keep 0x0001 (relocations stripped)
    # because they get their own address space; DLLs must stay relocatable.
    chars = 0x2022 if dll else (0x0022 | 0x0001)
    coff = struct.pack("<HHIIIHH", 0x8664, nsections, 0, 0, 0, 240, chars)

    opt = b""
    opt += struct.pack("<H", 0x20B)            # PE32+
    opt += struct.pack("<BB", 0, 1)            # linker version (acc 0.1)
    opt += struct.pack("<I", text_raw)         # SizeOfCode
    opt += struct.pack("<I", data_raw)         # SizeOfInitializedData
    opt += struct.pack("<I", 0)                # SizeOfUninitializedData
    opt += struct.pack("<I", 0 if dll else text_rva + entry_off)
    opt += struct.pack("<I", text_rva)         # BaseOfCode
    opt += struct.pack("<Q", image_base)
    opt += struct.pack("<I", SECT_ALIGN)
    opt += struct.pack("<I", FILE_ALIGN)
    opt += struct.pack("<HH", 6, 0)            # OS version
    opt += struct.pack("<HH", 0, 0)            # image version
    opt += struct.pack("<HH", 6, 0)            # subsystem version
    opt += struct.pack("<I", 0)
    opt += struct.pack("<I", size_of_image)
    opt += struct.pack("<I", headers_size)
    opt += struct.pack("<I", 0)                # checksum (unused for EXEs)
    opt += struct.pack("<H", 3)                # subsystem: console
    # 0x40 DYNAMIC_BASE, 0x100 NX_COMPAT, 0x8000 TERMINAL_SERVER_AWARE
    opt += struct.pack("<H", 0x8140 if dll else 0x8100)
    opt += struct.pack("<Q", 0x100000)         # stack reserve
    opt += struct.pack("<Q", 0x1000)           # stack commit
    opt += struct.pack("<Q", 0x100000)         # heap reserve
    opt += struct.pack("<Q", 0x1000)           # heap commit
    opt += struct.pack("<I", 0)
    opt += struct.pack("<I", 16)
    dirs = [(0, 0)] * 16
    dirs[0] = (exp_dir_rva, exp_dir_size)      # export table
    dirs[1] = (desc_rva, desc_size)            # import table
    dirs[5] = (reloc_rva, len(reloc))          # base relocations
    dirs[12] = (iat_rva, iat_size)             # IAT
    for rva, size in dirs:
        opt += struct.pack("<II", rva, size)

    def sect(name, vsize, rva, rawsize, rawoff, chars):
        nm = (name.encode("ascii") + b"\x00" * 8)[:8]
        return nm + struct.pack("<IIIIIIHHI", vsize, rva, rawsize, rawoff,
                                0, 0, 0, 0, chars)

    out = bytearray()
    out += dos
    out += b"PE\x00\x00"
    out += coff
    out += opt
    out += sect(".text", len(text), text_rva, text_raw, text_off, 0x60000020)
    out += sect(".data", len(data), data_rva, data_raw, data_off, 0xC0000040)
    if reloc:
        out += sect(".reloc", len(reloc), reloc_rva, reloc_raw, reloc_off,
                    0x42000040)   # INITIALIZED_DATA | DISCARDABLE | READ
    if len(out) > headers_size:
        raise CompileError("E9004", "internal: headers overflow", 0, 1)
    out += b"\x00" * (headers_size - len(out))
    out += text + b"\x00" * (text_raw - len(text))
    out += data + b"\x00" * (data_raw - len(data))
    if reloc:
        out += reloc + b"\x00" * (reloc_raw - len(reloc))
    return bytes(out)


# ---------------------------------------------------------------------------
# COFF object writer
#
# With -c, acc stops before layout and emits a real COFF object: two sections,
# a symbol table, and relocations. accld consumes these. Calls to the C library
# become undefined __imp_NAME symbols, exactly the convention MSVC uses, so the
# linker knows to build an import thunk rather than look for a definition.
# ---------------------------------------------------------------------------

IMAGE_REL_AMD64_REL32 = 4


def build_coff(em, cg, prog):
    text = bytearray(em.buf)
    data = bytearray()

    str_off = {}
    for i, s in enumerate(prog["strings"]):
        str_off[i] = len(data)
        data += s.encode("utf-8") + b"\x00"
    while len(data) % 8:
        data += b"\x00"
    glob_off = {}
    for g in prog["globals"]:
        glob_off[g["name"]] = len(data)
        data += struct.pack("<q", global_init_value(g))

    relocs = []
    for kind, key, pos in em.rip:
        if kind == "str":
            struct.pack_into("<i", text, pos, str_off[key])   # addend in field
            relocs.append((pos, ".data"))
        elif kind == "glob":
            relocs.append((pos, key))
        else:
            relocs.append((pos, "__imp_" + key))
    for kind, key, pos in em.rel:
        if kind == "func":
            relocs.append((pos, key))

    strtab = bytearray()
    syms = bytearray()
    index = {}
    state = {"n": 0}

    def name_field(nm):
        if len(nm) <= 8:
            return nm.encode("ascii") + b"\x00" * (8 - len(nm))
        off = 4 + len(strtab)
        strtab.extend(nm.encode("ascii") + b"\x00")
        return struct.pack("<II", 0, off)

    def add_sym(nm, value, sect, typ, cls, aux=b""):
        index[nm] = state["n"]
        syms.extend(name_field(nm))
        syms.extend(struct.pack("<IhHBB", value, sect, typ, cls,
                                1 if aux else 0))
        state["n"] += 1
        if aux:
            syms.extend(aux)
            state["n"] += 1

    def sect_aux(length, nrel):
        return struct.pack("<IHHIHB", length, nrel, 0, 0, 1, 0) + b"\x00" * 3

    add_sym(".text", 0, 1, 0, 3, sect_aux(len(text), len(relocs)))
    add_sym(".data", 0, 2, 0, 3, sect_aux(len(data), 0))
    for f in prog["funcs"]:
        add_sym(f["name"], cg.func_off[f["name"]], 1, 0x20, 2)
    for g in prog["globals"]:
        add_sym(g["name"], glob_off[g["name"]], 2, 0, 2)
    for _, nm in relocs:
        if nm not in index:
            add_sym(nm, 0, 0, 0, 2)               # undefined external

    rel_bytes = bytearray()
    for pos, nm in relocs:
        rel_bytes += struct.pack("<IIH", pos, index[nm], IMAGE_REL_AMD64_REL32)

    text_off = 20 + 80
    data_off = text_off + len(text)
    rel_off = data_off + len(data)
    sym_off = rel_off + len(rel_bytes)

    out = bytearray()
    out += struct.pack("<HHIIIHH", 0x8664, 2, 0, sym_off, state["n"], 0, 0)

    def sect_hdr(nm, size, raw, relptr, nrel, chars):
        return ((nm.encode("ascii") + b"\x00" * 8)[:8]
                + struct.pack("<IIIIIIHHI", 0, 0, size, raw if size else 0,
                              relptr if nrel else 0, 0, nrel, 0, chars))

    out += sect_hdr(".text", len(text), text_off, rel_off, len(relocs),
                    0x60500020)
    out += sect_hdr(".data", len(data), data_off, 0, 0, 0xC0500040)
    out += text
    out += data
    out += rel_bytes
    out += syms
    out += struct.pack("<I", 4 + len(strtab)) + strtab
    return bytes(out)


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

def render_listing(em, src):
    lines = src.splitlines()
    out = []
    out.append("; acc %s -- machine code listing" % VERSION)
    out.append("; offset  bytes                    instruction")
    last_line = None
    for off, bs, text, line in em.listing:
        if line and line != last_line and 0 < line <= len(lines):
            out.append("")
            out.append(";  %d | %s" % (line, lines[line - 1].strip()))
            last_line = line
        if not bs:
            out.append("%s" % text)
            continue
        hexed = " ".join("%02X" % b for b in bs)
        out.append("  %04X  %-24s %s" % (off, hexed, text))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

USAGE = """acc %s -- the Agent C Compiler

usage: acc [options] SOURCE.c

options:
  -o FILE        write the output here (default: SOURCE.exe)
  --dll          build a DLL exporting every function, instead of an exe
  -c             stop after codegen and write a COFF object for accld

preprocessing (optional; without these, # lines are skipped as always):
  --pp           run accpp, the bundled preprocessor
  -D NAME[=VAL]  define a macro (implies --pp)
  -I DIR         add an include search directory (implies --pp)
  --cpp CMD      preprocess with an external command instead, e.g. "gcc -E"
  --save-pp FILE write the preprocessed source out for inspection
  --json         emit results and diagnostics as JSON on stdout
  --listing      write an annotated machine-code listing next to the exe
  --dump-ast     write the parse tree as JSON next to the exe
  --run          execute the program after building it
  --version      print version and exit
  -h, --help     print this help

acc compiles a subset of C straight to a Windows x86-64 .exe. It does not use
gcc, msvc, an assembler, or a linker: it encodes the instructions and writes
the PE file itself.
""" % VERSION


def preprocess(path, pp, state):
    """Optional preprocessing. Returns (source_text, linemap_or_None)."""
    if not pp:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read(), None
    try:
        import accpp
    except ImportError:
        raise CompileError("E0200", "preprocessing was requested but "
                           "accpp.py is missing", 1, 1,
                           "accpp.py belongs next to acc.py; without it, drop "
                           "--pp and acc skips # lines as usual")
    try:
        if pp["mode"] == "external":
            src, linemap = accpp.external(pp["command"], path)
        else:
            src, linemap = accpp.Preprocessor(
                includes=pp["includes"], defines=pp["defines"]).run(path)
    except accpp.PPError as e:
        raise CompileError("E0201", e.msg, e.line or 1, 1, e.hint)
    if pp.get("save"):
        with open(pp["save"], "w", encoding="utf-8") as fh:
            fh.write(src)
    state["linemap"] = linemap
    return src, linemap


def compile_file(path, out_path, want_listing, want_ast, dll=False, obj=False,
                 pp=None, state=None):
    state = {} if state is None else state
    src, linemap = preprocess(path, pp, state)
    state["src"] = src

    toks = lex(src, path)
    parser = Parser(toks, path)
    prog = parser.parse_program()

    if want_ast:
        with open(want_ast, "w", encoding="utf-8") as fh:
            json.dump(prog, fh, indent=2)

    cg = CodeGen(prog, path, dll=dll, obj=obj)
    em = cg.generate()

    if obj:
        blob = build_coff(em, cg, prog)
        with open(out_path, "wb") as fh:
            fh.write(blob)
        ninstr = sum(1 for e in em.listing if e[1])
        if want_listing:
            with open(want_listing, "w", encoding="utf-8") as fh:
                fh.write(render_listing(em, src))
        return {
            "ok": True, "source": path, "output": out_path, "kind": "obj",
            "bytes": len(blob), "code_bytes": len(em.buf),
            "instructions": ninstr,
            "functions": [f["name"] for f in prog["funcs"]],
            "globals": [g["name"] for g in prog["globals"]],
            "strings": len(prog["strings"]), "imports": cg.imports,
            "exports": [], "listing": want_listing, "ast": want_ast,
        }, src

    exports = None
    if dll:
        exports = [(f["name"], cg.func_off[f["name"]]) for f in prog["funcs"]]
        if not exports:
            raise CompileError("E0123", "a DLL must define at least one "
                               "function to export", 1, 1)
    exe = build_pe(em, prog["strings"], prog["globals"], cg.imports, 0,
                   dll=dll, exports=exports,
                   module_name=os.path.basename(out_path))

    with open(out_path, "wb") as fh:
        fh.write(exe)

    if want_listing:
        with open(want_listing, "w", encoding="utf-8") as fh:
            fh.write(render_listing(em, src))

    ninstr = sum(1 for e in em.listing if e[1])
    return {
        "ok": True,
        "source": path,
        "output": out_path,
        "kind": "dll" if dll else "exe",
        "exports": [n for n, _ in (exports or [])],
        "bytes": len(exe),
        "code_bytes": len(em.buf),
        "instructions": ninstr,
        "functions": [f["name"] for f in prog["funcs"]],
        "globals": [g["name"] for g in prog["globals"]],
        "strings": len(prog["strings"]),
        "imports": cg.imports,
        "listing": want_listing,
        "ast": want_ast,
    }, src


def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0 if args else 2
    if args[0] == "--version":
        sys.stdout.write("acc %s\n" % VERSION)
        return 0

    src_path = None
    out_path = None
    as_json = False
    want_listing = False
    want_ast = False
    do_run = False
    make_dll = False
    make_obj = False
    use_pp = False
    pp_defines = []
    pp_includes = []
    pp_command = None
    pp_save = None

    i = 0
    while i < len(args):
        a = args[i]
        if a == "-o":
            i += 1
            if i >= len(args):
                sys.stderr.write("acc: -o needs a filename\n")
                return 2
            out_path = args[i]
        elif a == "--json":
            as_json = True
        elif a == "--listing":
            want_listing = True
        elif a == "--dump-ast":
            want_ast = True
        elif a == "--run":
            do_run = True
        elif a == "--dll":
            make_dll = True
        elif a == "-c":
            make_obj = True
        elif a == "--pp":
            use_pp = True
        elif a == "-D" or a.startswith("-D") and len(a) > 2:
            use_pp = True
            if a == "-D":
                i += 1
                if i >= len(args):
                    sys.stderr.write("acc: -D needs NAME[=VALUE]\n")
                    return 2
                pp_defines.append(args[i])
            else:
                pp_defines.append(a[2:])
        elif a == "-I" or a.startswith("-I") and len(a) > 2:
            use_pp = True
            if a == "-I":
                i += 1
                if i >= len(args):
                    sys.stderr.write("acc: -I needs a directory\n")
                    return 2
                pp_includes.append(args[i])
            else:
                pp_includes.append(a[2:])
        elif a == "--cpp":
            i += 1
            if i >= len(args):
                sys.stderr.write("acc: --cpp needs a command\n")
                return 2
            pp_command = args[i]
        elif a == "--save-pp":
            i += 1
            if i >= len(args):
                sys.stderr.write("acc: --save-pp needs a filename\n")
                return 2
            pp_save = args[i]
        elif a.startswith("-"):
            sys.stderr.write("acc: unknown option %r\n" % a)
            return 2
        else:
            if src_path is not None:
                sys.stderr.write("acc: only one source file at a time\n")
                return 2
            src_path = a
        i += 1

    if src_path is None:
        sys.stderr.write("acc: no source file\n")
        return 2
    if not os.path.exists(src_path):
        sys.stderr.write("acc: cannot open %s\n" % src_path)
        return 2

    base = os.path.splitext(src_path)[0]
    ext = ".obj" if make_obj else (".dll" if make_dll else ".exe")
    out_path = out_path or (base + ext)
    listing_path = (base + ".lst") if want_listing else False
    ast_path = (base + ".ast.json") if want_ast else False

    pp = None
    if pp_command:
        pp = {"mode": "external", "command": pp_command, "save": pp_save,
              "includes": pp_includes, "defines": pp_defines}
    elif use_pp:
        pp = {"mode": "builtin", "includes": pp_includes,
              "defines": pp_defines, "save": pp_save}

    state = {}
    try:
        info, src = compile_file(src_path, out_path, listing_path, ast_path,
                                 dll=make_dll, obj=make_obj, pp=pp,
                                 state=state)
    except CompileError as e:
        # After preprocessing, the line the parser saw is a line of expanded
        # text. Map it back so the message points at the file you wrote.
        lm = state.get("linemap")
        rep_path, rep_line = src_path, e.line
        if lm and 1 <= e.line <= len(lm):
            rep_path, rep_line = lm[e.line - 1]
        if as_json:
            d = e.to_dict(src_path)
            d["file"], d["line"] = rep_path, rep_line
            if lm:
                d["expanded_line"] = e.line
            json.dump({"ok": False, "diagnostics": [d]}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            try:
                with open(rep_path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except Exception:
                text = state.get("src", "")
            shown, e.line = e.line, rep_line
            sys.stderr.write(e.render(rep_path, text) + "\n")
            e.line = shown
        return 1
    except Exception as e:
        # Anything that is not a CompileError is a bug in acc, not in the
        # program being compiled. Report it as a diagnostic (exit 3) instead
        # of letting a Python traceback escape.
        if isinstance(e, RecursionError):
            msg = "expression or statement nested too deeply"
        else:
            msg = "internal compiler error: %s: %s" % (type(e).__name__, e)
        hint = ("this is a bug in acc; please report it with the source "
                "file that triggered it")
        if as_json:
            json.dump({"ok": False, "diagnostics": [{
                "severity": "error", "code": "E9999", "file": src_path,
                "line": 0, "col": 0, "message": msg, "hint": hint}]},
                sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stderr.write("%s: error[E9999]: %s\n  hint: %s\n"
                             % (src_path, msg, hint))
        return 3

    if do_run:
        proc = subprocess.run([os.path.abspath(out_path)],
                              capture_output=True)
        info["run"] = {
            "exit_code": proc.returncode,
            "stdout": proc.stdout.decode("utf-8", "replace"),
            "stderr": proc.stderr.decode("utf-8", "replace"),
        }

    if as_json:
        json.dump(info, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write("acc: wrote %s [%s] (%d bytes, %d instructions, "
                         "%d code bytes)\n"
                         % (info["output"], info["kind"], info["bytes"],
                            info["instructions"], info["code_bytes"]))
        if info.get("exports"):
            sys.stdout.write("acc: exports: %s\n" % ", ".join(info["exports"]))
        if info["imports"]:
            sys.stdout.write("acc: imports from msvcrt.dll: %s\n"
                             % ", ".join(info["imports"]))
        if listing_path:
            sys.stdout.write("acc: listing -> %s\n" % listing_path)
        if ast_path:
            sys.stdout.write("acc: ast -> %s\n" % ast_path)
        if do_run:
            sys.stdout.write("--- program output ---\n")
            sys.stdout.write(info["run"]["stdout"])
            if info["run"]["stderr"]:
                sys.stdout.write(info["run"]["stderr"])
            sys.stdout.write("--- exit code %d ---\n" % info["run"]["exit_code"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
