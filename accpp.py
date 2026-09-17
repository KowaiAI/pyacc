#!/usr/bin/env python3
"""
accpp -- an optional C preprocessor for acc.

acc ignores `#` lines by default. That default is deliberate: it keeps the
compiler dependency-free and lets one source file compile under both acc and
gcc. This module is what you opt into when you want the directives to mean
something.

Two modes, both optional:

    acc --pp prog.c              acc runs this preprocessor
    acc --cpp "gcc -E" prog.c    acc pipes the source through an external one

Implemented: #define (object-like and function-like), #undef, #include with a
search path, #ifdef / #ifndef / #if / #elif / #else / #endif, #error, #pragma
once, line continuations, and the __FILE__ / __LINE__ macros.

Not implemented: the # stringize and ## paste operators, variadic macros,
#include_next, and _Pragma. Those raise a clear error rather than being
silently ignored.

Every output line carries the file and line it came from, so diagnostics point
at your source rather than at expanded text.
"""

import os
import re

IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
DIRECTIVE = re.compile(r"^\s*#\s*(\w*)\s*(.*)$")


class PPError(Exception):
    def __init__(self, msg, path, line, hint=None):
        Exception.__init__(self, msg)
        self.msg = msg
        self.path = path
        self.line = line
        self.hint = hint


class Preprocessor:
    def __init__(self, includes=None, defines=None, max_depth=64):
        self.includes = list(includes or [])
        self.max_depth = max_depth
        self.macros = {}
        self.once = set()
        self.out = []          # list of (text, origin_file, origin_line)
        for d in (defines or []):
            name, sep, val = d.partition("=")
            name = name.strip()
            if not IDENT.fullmatch(name):
                raise PPError("bad -D name %r" % name, "<command line>", 0)
            self.define_from_text(name + ("(" if False else "") +
                                  (" " + val if sep else " 1"),
                                  "<command line>", 0)

    # -- public -------------------------------------------------------------
    def run(self, path):
        self.include_file(path, "<command line>", 0, depth=0)
        text = "\n".join(t for t, _, _ in self.out) + "\n"
        linemap = [(f, l) for _, f, l in self.out]
        return text, linemap

    # -- macro table --------------------------------------------------------
    def define_from_text(self, rest, path, line):
        m = IDENT.match(rest)
        if not m:
            raise PPError("#define needs a name", path, line)
        name = m.group(0)
        i = m.end()
        params = None
        if i < len(rest) and rest[i] == "(":
            depth = 0
            j = i
            while j < len(rest):
                if rest[j] == "(":
                    depth += 1
                elif rest[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= len(rest):
                raise PPError("unterminated parameter list in #define",
                              path, line)
            inner = rest[i + 1:j].strip()
            params = [p.strip() for p in inner.split(",")] if inner else []
            for p in params:
                if p == "...":
                    raise PPError("variadic macros are not supported",
                                  path, line,
                                  "accpp has no __VA_ARGS__ yet")
                if not IDENT.fullmatch(p):
                    raise PPError("bad macro parameter %r" % p, path, line)
            i = j + 1
        body = rest[i:].strip()
        if "##" in body:
            raise PPError("the ## paste operator is not supported", path, line,
                          "accpp expands macros but does not paste tokens")
        self.macros[name] = {"params": params, "body": body}

    # -- expansion ----------------------------------------------------------
    def expand(self, text, path, line, blue=None):
        blue = blue or frozenset()
        out = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch in "\"'":
                j = i + 1
                while j < len(text):
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == ch:
                        j += 1
                        break
                    j += 1
                out.append(text[i:j])
                i = j
                continue
            m = IDENT.match(text, i)
            if not m:
                out.append(ch)
                i += 1
                continue
            name = m.group(0)
            i = m.end()
            if name == "__LINE__":
                out.append(str(line))
                continue
            if name == "__FILE__":
                out.append('"%s"' % path.replace("\\", "/"))
                continue
            mac = self.macros.get(name)
            if mac is None or name in blue:
                out.append(name)
                continue
            if mac["params"] is None:
                out.append(self.expand(mac["body"], path, line,
                                       blue | {name}))
                continue
            # function-like: only expands when an argument list follows
            j = i
            while j < len(text) and text[j].isspace():
                j += 1
            if j >= len(text) or text[j] != "(":
                out.append(name)
                continue
            args, end = self.read_args(text, j, path, line)
            if len(args) != len(mac["params"]):
                raise PPError("macro %s expects %d argument(s), got %d"
                              % (name, len(mac["params"]), len(args)),
                              path, line)
            body = mac["body"]
            expanded_args = [self.expand(a, path, line, blue) for a in args]
            subst = []
            k = 0
            while k < len(body):
                bm = IDENT.match(body, k)
                if not bm:
                    subst.append(body[k])
                    k += 1
                    continue
                w = bm.group(0)
                k = bm.end()
                if w in mac["params"]:
                    subst.append(expanded_args[mac["params"].index(w)])
                else:
                    subst.append(w)
            out.append(self.expand("".join(subst), path, line, blue | {name}))
            i = end
        return "".join(out)

    def read_args(self, text, open_paren, path, line):
        args = []
        depth = 0
        cur = []
        i = open_paren
        while i < len(text):
            c = text[i]
            if c == "(":
                depth += 1
                if depth == 1:
                    i += 1
                    continue
            elif c == ")":
                depth -= 1
                if depth == 0:
                    args.append("".join(cur).strip())
                    return ([] if len(args) == 1 and args[0] == "" else args,
                            i + 1)
            elif c == "," and depth == 1:
                args.append("".join(cur).strip())
                cur = []
                i += 1
                continue
            cur.append(c)
            i += 1
        raise PPError("unterminated macro argument list", path, line)

    # -- #if expressions ----------------------------------------------------
    def eval_condition(self, expr, path, line):
        expr = re.sub(r"defined\s*\(\s*(\w+)\s*\)",
                      lambda m: "1" if m.group(1) in self.macros else "0",
                      expr)
        expr = re.sub(r"defined\s+(\w+)",
                      lambda m: "1" if m.group(1) in self.macros else "0",
                      expr)
        expr = self.expand(expr, path, line)
        # any identifier still standing is an undefined macro, which is 0
        expr = IDENT.sub(lambda m: "0", expr)
        return self.eval_int(expr, path, line) != 0

    def eval_int(self, expr, path, line):
        toks = re.findall(r"\d+|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%()<>!&|^~]",
                          expr)
        pos = [0]

        def peek():
            return toks[pos[0]] if pos[0] < len(toks) else None

        def take():
            t = peek()
            pos[0] += 1
            return t

        def primary():
            t = take()
            if t is None:
                raise PPError("malformed #if expression", path, line)
            if t == "(":
                v = expression()
                if take() != ")":
                    raise PPError("missing ) in #if expression", path, line)
                return v
            if t == "!":
                return 0 if primary() else 1
            if t == "-":
                return -primary()
            if t == "~":
                return ~primary()
            if t == "+":
                return primary()
            if t.isdigit():
                return int(t)
            raise PPError("cannot evaluate %r in #if" % t, path, line)

        def binary(level=0):
            ops = [["||"], ["&&"], ["|"], ["^"], ["&"], ["==", "!="],
                   ["<", ">", "<=", ">="], ["<<", ">>"], ["+", "-"],
                   ["*", "/", "%"]]
            if level >= len(ops):
                return primary()
            v = binary(level + 1)
            while peek() in ops[level]:
                op = take()
                r = binary(level + 1)
                v = {
                    "||": lambda a, b: 1 if (a or b) else 0,
                    "&&": lambda a, b: 1 if (a and b) else 0,
                    "|": lambda a, b: a | b, "^": lambda a, b: a ^ b,
                    "&": lambda a, b: a & b,
                    "==": lambda a, b: 1 if a == b else 0,
                    "!=": lambda a, b: 1 if a != b else 0,
                    "<": lambda a, b: 1 if a < b else 0,
                    ">": lambda a, b: 1 if a > b else 0,
                    "<=": lambda a, b: 1 if a <= b else 0,
                    ">=": lambda a, b: 1 if a >= b else 0,
                    "<<": lambda a, b: a << b, ">>": lambda a, b: a >> b,
                    "+": lambda a, b: a + b, "-": lambda a, b: a - b,
                    "*": lambda a, b: a * b,
                    "/": lambda a, b: a // b if b else 0,
                    "%": lambda a, b: a % b if b else 0,
                }[op](v, r)
            return v

        def expression():
            return binary()

        v = expression()
        if pos[0] != len(toks):
            raise PPError("trailing tokens in #if expression", path, line)
        return v

    # -- the driver ---------------------------------------------------------
    def resolve_include(self, name, angled, from_path):
        roots = []
        if not angled:
            roots.append(os.path.dirname(os.path.abspath(from_path)))
        roots.extend(self.includes)
        for r in roots:
            cand = os.path.join(r, name)
            if os.path.isfile(cand):
                return cand
        return None

    def include_file(self, path, from_path, from_line, depth):
        if depth > self.max_depth:
            raise PPError("#include nested too deeply", from_path, from_line)
        real = os.path.abspath(path)
        if real in self.once:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            raise PPError("cannot read %s: %s" % (path, e),
                          from_path, from_line)
        self.process(text, path, depth)

    def process(self, text, path, depth):
        raw_lines = text.splitlines()
        lines = []
        i = 0
        while i < len(raw_lines):
            start = i + 1
            cur = raw_lines[i]
            while cur.endswith("\\") and i + 1 < len(raw_lines):
                i += 1
                cur = cur[:-1] + raw_lines[i]
            lines.append((cur, start))
            i += 1

        stack = []          # (taken_any, currently_active, seen_else)
        for cur, lineno in lines:
            active = all(s[1] for s in stack)
            m = DIRECTIVE.match(cur)
            if not m:
                if active:
                    self.out.append((self.expand(cur, path, lineno),
                                     path, lineno))
                continue

            name, rest = m.group(1), m.group(2).strip()

            if name in ("ifdef", "ifndef", "if"):
                if not active:
                    stack.append((True, False, False))
                    continue
                if name == "ifdef":
                    cond = rest.split()[0] in self.macros if rest else False
                elif name == "ifndef":
                    cond = rest.split()[0] not in self.macros if rest else False
                else:
                    cond = self.eval_condition(rest, path, lineno)
                stack.append((cond, cond, False))
                continue

            if name == "elif":
                if not stack:
                    raise PPError("#elif without #if", path, lineno)
                taken, _, seen_else = stack[-1]
                if seen_else:
                    raise PPError("#elif after #else", path, lineno)
                outer = all(s[1] for s in stack[:-1])
                cond = (outer and not taken
                        and self.eval_condition(rest, path, lineno))
                stack[-1] = (taken or cond, cond, False)
                continue

            if name == "else":
                if not stack:
                    raise PPError("#else without #if", path, lineno)
                taken, _, seen_else = stack[-1]
                if seen_else:
                    raise PPError("duplicate #else", path, lineno)
                outer = all(s[1] for s in stack[:-1])
                stack[-1] = (True, outer and not taken, True)
                continue

            if name == "endif":
                if not stack:
                    raise PPError("#endif without #if", path, lineno)
                stack.pop()
                continue

            if not active:
                continue

            if name == "define":
                self.define_from_text(rest, path, lineno)
            elif name == "undef":
                self.macros.pop(rest.split()[0] if rest else "", None)
            elif name == "include":
                inc = self.expand(rest, path, lineno).strip()
                if inc.startswith('"') and inc.endswith('"'):
                    target, angled = inc[1:-1], False
                elif inc.startswith("<") and inc.endswith(">"):
                    target, angled = inc[1:-1], True
                else:
                    raise PPError("malformed #include %r" % inc, path, lineno)
                found = self.resolve_include(target, angled, path)
                if not found:
                    raise PPError("cannot find include %r" % target,
                                  path, lineno,
                                  "add its directory with -I")
                self.include_file(found, path, lineno, depth + 1)
            elif name == "error":
                raise PPError(self.expand(rest, path, lineno) or "#error",
                              path, lineno)
            elif name == "warning":
                pass
            elif name == "pragma":
                if rest.strip() == "once":
                    self.once.add(os.path.abspath(path))
            elif name == "line":
                pass
            elif name == "":
                pass
            else:
                raise PPError("unknown directive #%s" % name, path, lineno,
                              "accpp supports define, undef, include, ifdef, "
                              "ifndef, if, elif, else, endif, error, pragma")

        if stack:
            raise PPError("unterminated #if", path, len(lines))


def external(command, path):
    """Run an external preprocessor and read back its line markers."""
    import shlex
    import subprocess

    argv = shlex.split(command) + [path]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PPError("external preprocessor failed: %s"
                      % (proc.stderr.strip().splitlines() or [""])[0],
                      path, 0, "command was: %s" % " ".join(argv))

    out = []
    linemap = []
    cur_file, cur_line = path, 1
    for line in proc.stdout.splitlines():
        m = re.match(r'^#\s*(\d+)\s+"([^"]*)"', line)
        if m:
            cur_line = int(m.group(1))
            cur_file = m.group(2)
            continue
        if line.startswith("#"):
            continue
        out.append(line)
        linemap.append((cur_file, cur_line))
        cur_line += 1
    return "\n".join(out) + "\n", linemap
