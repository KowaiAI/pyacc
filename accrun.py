#!/usr/bin/env python3
"""
accrun -- load and execute a PE32+ executable without the Windows loader.

This is acc companion tooling, and it exists for a concrete reason: an agent
that just generated a binary usually cannot run it (code-signing policy,
sandbox, wrong host architecture, no OS at all). accrun parses the PE that acc
produced, maps its sections, binds the imports to Python implementations of the
C library, and interprets the x86-64 machine code.

It decodes instructions from the raw bytes using REX/ModRM/SIB rules rather
than pattern-matching acc output, so a wrong encoding shows up as a wrong
effective address or an unknown opcode instead of quietly agreeing with the
compiler.

usage: accrun PROGRAM.exe [--trace] [--max-steps N]
"""

import sys
import struct

STACK_BASE = 0x200000000
STACK_SIZE = 1 << 20
HEAP_BASE = 0x300000000
HEAP_SIZE = 16 << 20
THUNK_BASE = 0xF0000000

REGNAMES = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
            "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]

MASK = (1 << 64) - 1


def to_signed(v, bits=64):
    v &= (1 << bits) - 1
    if v >> (bits - 1):
        v -= 1 << bits
    return v


class Segment:
    def __init__(self, base, size, name):
        self.base = base
        self.size = size
        self.name = name
        self.data = bytearray(size)


class Memory:
    def __init__(self):
        self.segs = []

    def add(self, seg):
        self.segs.append(seg)
        return seg

    def find(self, addr, size):
        for s in self.segs:
            if s.base <= addr and addr + size <= s.base + s.size:
                return s
        raise MemoryError("unmapped access at 0x%X (%d bytes)" % (addr, size))

    def read(self, addr, size):
        s = self.find(addr, size)
        off = addr - s.base
        return bytes(s.data[off:off + size])

    def write(self, addr, blob):
        s = self.find(addr, len(blob))
        off = addr - s.base
        s.data[off:off + len(blob)] = blob

    def read_u64(self, addr):
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def write_u64(self, addr, v):
        self.write(addr, struct.pack("<Q", v & MASK))

    def read_cstr(self, addr, limit=1 << 20):
        out = bytearray()
        while len(out) < limit:
            b = self.read(addr + len(out), 1)[0]
            if b == 0:
                break
            out.append(b)
        return bytes(out)


class PE:
    """Just enough PE32+ parsing to load what acc emits."""

    def __init__(self, path):
        with open(path, "rb") as fh:
            self.raw = fh.read()
        if self.raw[:2] != b"MZ":
            raise ValueError("not a PE file: missing MZ signature")
        e_lfanew = struct.unpack_from("<I", self.raw, 0x3C)[0]
        if self.raw[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
            raise ValueError("not a PE file: missing PE signature")
        coff = e_lfanew + 4
        (self.machine, self.nsections, _, _, _, self.opt_size,
         self.characteristics) = struct.unpack_from("<HHIIIHH", self.raw, coff)
        if self.machine != 0x8664:
            raise ValueError("not an x86-64 image (machine=0x%X)" % self.machine)
        opt = coff + 20
        magic = struct.unpack_from("<H", self.raw, opt)[0]
        if magic != 0x20B:
            raise ValueError("not PE32+ (magic=0x%X)" % magic)
        self.entry = struct.unpack_from("<I", self.raw, opt + 16)[0]
        self.image_base = struct.unpack_from("<Q", self.raw, opt + 24)[0]
        self.size_of_image = struct.unpack_from("<I", self.raw, opt + 56)[0]
        ndirs = struct.unpack_from("<I", self.raw, opt + 108)[0]
        self.dirs = []
        for i in range(ndirs):
            rva, size = struct.unpack_from("<II", self.raw, opt + 112 + 8 * i)
            self.dirs.append((rva, size))
        sect = opt + self.opt_size
        self.sections = []
        for i in range(self.nsections):
            off = sect + 40 * i
            name = self.raw[off:off + 8].rstrip(b"\x00").decode("ascii", "replace")
            vsize, rva, rawsize, rawoff = struct.unpack_from("<IIII", self.raw, off + 8)
            chars = struct.unpack_from("<I", self.raw, off + 36)[0]
            self.sections.append({"name": name, "vsize": vsize, "rva": rva,
                                  "rawsize": rawsize, "rawoff": rawoff,
                                  "chars": chars})


# ---------------------------------------------------------------------------
# emulated C library
# ---------------------------------------------------------------------------

class Libc:
    def __init__(self, cpu):
        self.cpu = cpu
        self.brk = HEAP_BASE + 16
        self.seed = 1
        self.out = []

    def arg(self, i):
        if i < 4:
            return self.cpu.regs[[1, 2, 8, 9][i]]      # rcx, rdx, r8, r9
        # stack arguments sit above the 32-byte shadow space
        return self.cpu.mem.read_u64(self.cpu.regs[4] + 32 + 8 * (i - 4))

    def write(self, text):
        self.out.append(text)
        sys.stdout.write(text)

    def do_printf(self):
        fmt = self.cpu.mem.read_cstr(self.arg(0)).decode("utf-8", "replace")
        out = []
        ai = 1
        i = 0
        while i < len(fmt):
            c = fmt[i]
            if c != "%":
                out.append(c)
                i += 1
                continue
            i += 1
            if i >= len(fmt):
                break
            spec = fmt[i]
            while spec in "-+ #0123456789.":       # flags/width are parsed,
                i += 1                             # then ignored
                if i >= len(fmt):
                    break
                spec = fmt[i]
            if spec == "l":
                i += 1
                spec = fmt[i] if i < len(fmt) else "d"
                if spec == "l":
                    i += 1
                    spec = fmt[i] if i < len(fmt) else "d"
            if spec == "%":
                out.append("%")
            elif spec in "di":
                out.append(str(to_signed(self.arg(ai) & 0xFFFFFFFF, 32)))
                ai += 1
            elif spec == "u":
                out.append(str(self.arg(ai) & 0xFFFFFFFF))
                ai += 1
            elif spec == "x":
                out.append("%x" % (self.arg(ai) & 0xFFFFFFFF))
                ai += 1
            elif spec == "c":
                out.append(chr(self.arg(ai) & 0xFF))
                ai += 1
            elif spec == "s":
                out.append(self.cpu.mem.read_cstr(self.arg(ai)).decode("utf-8", "replace"))
                ai += 1
            elif spec == "p":
                out.append("0x%X" % self.arg(ai))
                ai += 1
            else:
                out.append("%" + spec)
            i += 1
        text = "".join(out)
        self.write(text)
        return len(text)

    def call(self, name):
        if name == "printf":
            return self.do_printf()
        if name == "puts":
            s = self.cpu.mem.read_cstr(self.arg(0)).decode("utf-8", "replace")
            self.write(s + "\n")
            return len(s) + 1
        if name == "putchar":
            self.write(chr(self.arg(0) & 0xFF))
            return self.arg(0) & 0xFF
        if name == "exit":
            raise ProgramExit(to_signed(self.arg(0) & 0xFFFFFFFF, 32))
        if name == "abort":
            raise ProgramExit(3)
        if name == "malloc":
            n = self.arg(0)
            p = self.brk
            self.brk = (self.brk + n + 15) & ~15
            if self.brk > HEAP_BASE + HEAP_SIZE:
                raise MemoryError("emulated heap exhausted")
            return p
        if name == "calloc":
            n = self.arg(0) * self.arg(1)
            p = self.brk
            self.brk = (self.brk + n + 15) & ~15
            self.cpu.mem.write(p, b"\x00" * n)
            return p
        if name == "free":
            return 0
        if name == "strlen":
            return len(self.cpu.mem.read_cstr(self.arg(0)))
        if name == "strcmp":
            a = self.cpu.mem.read_cstr(self.arg(0))
            b = self.cpu.mem.read_cstr(self.arg(1))
            return 0 if a == b else (1 if a > b else -1)
        if name == "strcpy":
            src = self.cpu.mem.read_cstr(self.arg(1))
            self.cpu.mem.write(self.arg(0), src + b"\x00")
            return self.arg(0)
        if name == "memset":
            self.cpu.mem.write(self.arg(0), bytes([self.arg(1) & 0xFF]) * self.arg(2))
            return self.arg(0)
        if name == "memcpy":
            self.cpu.mem.write(self.arg(0), self.cpu.mem.read(self.arg(1), self.arg(2)))
            return self.arg(0)
        if name == "abs":
            return abs(to_signed(self.arg(0) & 0xFFFFFFFF, 32))
        if name == "atoi":
            s = self.cpu.mem.read_cstr(self.arg(0)).decode("ascii", "replace").strip()
            num = ""
            for ch in s:
                if ch in "+-" and not num:
                    num += ch
                elif ch.isdigit():
                    num += ch
                else:
                    break
            try:
                return int(num) & MASK
            except ValueError:
                return 0
        if name == "rand":
            self.seed = (self.seed * 214013 + 2531011) & 0x7FFFFFFF
            return (self.seed >> 16) & 0x7FFF
        if name == "srand":
            self.seed = self.arg(0) & 0x7FFFFFFF
            return 0
        if name in ("fflush", "time", "clock"):
            return 0
        if name == "toupper":
            return ord(chr(self.arg(0) & 0xFF).upper())
        if name == "tolower":
            return ord(chr(self.arg(0) & 0xFF).lower())
        raise NotImplementedError("accrun has no implementation of %r" % name)


class ProgramExit(Exception):
    def __init__(self, code):
        Exception.__init__(self, "exit %d" % code)
        self.code = code


# ---------------------------------------------------------------------------
# the interpreter
# ---------------------------------------------------------------------------

class CPU:
    def __init__(self, pe, trace=False):
        self.pe = pe
        self.mem = Memory()
        self.trace = trace
        self.steps = 0

        image = self.mem.add(Segment(pe.image_base, max(pe.size_of_image, 0x10000), "image"))
        image.data[0:len(pe.raw[:0x200])] = pe.raw[:0x200]
        for s in pe.sections:
            blob = pe.raw[s["rawoff"]:s["rawoff"] + s["rawsize"]]
            off = s["rva"]
            image.data[off:off + len(blob)] = blob

        self.mem.add(Segment(STACK_BASE, STACK_SIZE, "stack"))
        self.mem.add(Segment(HEAP_BASE, HEAP_SIZE, "heap"))

        self.regs = [0] * 16
        self.regs[4] = STACK_BASE + STACK_SIZE - 0x1000   # rsp
        self.regs[5] = 0                                  # rbp
        self.rip = pe.image_base + pe.entry
        self.zf = False
        self.lt = False
        self.gt = False
        self.libc = Libc(self)
        self.thunks = {}
        self.bind_imports()

        # return address that means "the program returned from its entry point"
        self.exit_marker = 0xDEAD0000
        self.push(self.exit_marker)

    def bind_imports(self):
        rva, size = self.pe.dirs[1]
        if not rva:
            return
        base = self.pe.image_base
        off = 0
        idx = 0
        while True:
            desc = self.mem.read(base + rva + off, 20)
            ilt, _, _, name_rva, iat_rva = struct.unpack("<IIIII", desc)
            if ilt == 0 and name_rva == 0 and iat_rva == 0:
                break
            dll = self.mem.read_cstr(base + name_rva).decode("ascii")
            k = 0
            while True:
                entry = self.mem.read_u64(base + iat_rva + 8 * k)
                if entry == 0:
                    break
                fname = self.mem.read_cstr(base + entry + 2).decode("ascii")
                thunk = THUNK_BASE + 8 * idx
                self.thunks[thunk] = (dll, fname)
                self.mem.write_u64(base + iat_rva + 8 * k, thunk)
                idx += 1
                k += 1
            off += 20

    # -- stack --------------------------------------------------------------
    def push(self, v):
        self.regs[4] = (self.regs[4] - 8) & MASK
        self.mem.write_u64(self.regs[4], v)

    def pop(self):
        v = self.mem.read_u64(self.regs[4])
        self.regs[4] = (self.regs[4] + 8) & MASK
        return v

    # -- fetch --------------------------------------------------------------
    def f8(self):
        b = self.mem.read(self.rip, 1)[0]
        self.rip += 1
        return b

    def f8s(self):
        return to_signed(self.f8(), 8)

    def f32(self):
        v = struct.unpack("<i", self.mem.read(self.rip, 4))[0]
        self.rip += 4
        return v

    def f64(self):
        v = struct.unpack("<q", self.mem.read(self.rip, 8))[0]
        self.rip += 8
        return v

    # -- operand helpers ----------------------------------------------------
    def get(self, r, size=8):
        v = self.regs[r]
        return v & ((1 << (size * 8)) - 1)

    def put(self, r, v, size=8):
        if size == 8:
            self.regs[r] = v & MASK
        elif size == 4:
            self.regs[r] = v & 0xFFFFFFFF          # 32-bit writes zero-extend
        else:
            keep = self.regs[r] & ~((1 << (size * 8)) - 1)
            self.regs[r] = keep | (v & ((1 << (size * 8)) - 1))

    def read_rm(self, rm, size=8):
        kind = rm[0]
        if kind == "reg":
            return self.get(rm[1], size)
        return int.from_bytes(self.mem.read(self.ea(rm), size), "little")

    def write_rm(self, rm, v, size=8):
        if rm[0] == "reg":
            self.put(rm[1], v, size)
        else:
            self.mem.write(self.ea(rm), (v & ((1 << (size * 8)) - 1)).to_bytes(size, "little"))

    def ea(self, rm):
        if rm[0] == "rip":
            return (self.rip + rm[1]) & MASK
        if rm[0] == "mem":
            addr = self.regs[rm[1]] + rm[2]
            if rm[3] is not None:
                idx, scale = rm[3]
                addr += self.regs[idx] * scale
            return addr & MASK
        raise ValueError("not a memory operand: %r" % (rm,))

    def modrm(self, rex):
        R = (rex >> 2) & 1
        X = (rex >> 1) & 1
        B = rex & 1
        m = self.f8()
        mod = m >> 6
        reg = ((m >> 3) & 7) | (R << 3)
        rm = m & 7
        if mod == 3:
            return reg, ("reg", rm | (B << 3))
        index = None
        if rm == 4:                                  # SIB byte follows
            sib = self.f8()
            scale = 1 << (sib >> 6)
            idx = ((sib >> 3) & 7) | (X << 3)
            base = (sib & 7) | (B << 3)
            if idx != 4:
                index = (idx, scale)
            if mod == 0 and (sib & 7) == 5:
                disp = self.f32()
                return reg, ("mem", 0, disp, index)  # no base
            disp = 0
            if mod == 1:
                disp = self.f8s()
            elif mod == 2:
                disp = self.f32()
            return reg, ("mem", base, disp, index)
        if mod == 0 and rm == 5:
            return reg, ("rip", self.f32())
        disp = 0
        if mod == 1:
            disp = self.f8s()
        elif mod == 2:
            disp = self.f32()
        return reg, ("mem", rm | (B << 3), disp, index)

    # -- flags --------------------------------------------------------------
    def set_cmp(self, a, b):
        sa, sb = to_signed(a), to_signed(b)
        self.zf = sa == sb
        self.lt = sa < sb
        self.gt = sa > sb

    def cond(self, cc):
        if cc == 0x4:
            return self.zf
        if cc == 0x5:
            return not self.zf
        if cc == 0xC:
            return self.lt
        if cc == 0xD:
            return not self.lt
        if cc == 0xE:
            return self.lt or self.zf
        if cc == 0xF:
            return self.gt
        raise NotImplementedError("condition code 0x%X" % cc)

    # -- one instruction ----------------------------------------------------
    def step(self):
        start = self.rip
        rex = 0
        b = self.f8()
        if 0x40 <= b <= 0x4F:
            rex = b
            b = self.f8()
        W = (rex >> 3) & 1
        size = 8 if W else 4

        if b == 0x0F:
            b2 = self.f8()
            if b2 == 0xAF:                                   # imul r, r/m
                reg, rm = self.modrm(rex)
                v = to_signed(self.get(reg, size)) * to_signed(self.read_rm(rm, size))
                self.put(reg, v & MASK, size)
                return
            if b2 == 0xB6:                                   # movzx r, r/m8
                reg, rm = self.modrm(rex)
                self.put(reg, self.read_rm(rm, 1), size)
                return
            if b2 == 0xB7:
                reg, rm = self.modrm(rex)
                self.put(reg, self.read_rm(rm, 2), size)
                return
            if b2 in (0xBE, 0xBF):                           # movsx r, r/m8/16
                reg, rm = self.modrm(rex)
                width = 1 if b2 == 0xBE else 2
                v = to_signed(self.read_rm(rm, width), 8 * width)
                self.put(reg, v & MASK, size)
                return
            if 0x80 <= b2 <= 0x8F:                           # jcc rel32
                rel = self.f32()
                if self.cond(b2 & 0xF):
                    self.rip = (self.rip + rel) & MASK
                return
            if 0x90 <= b2 <= 0x9F:                           # setcc r/m8
                _, rm = self.modrm(rex)
                self.write_rm(rm, 1 if self.cond(b2 & 0xF) else 0, 1)
                return
            raise NotImplementedError("0F %02X at 0x%X" % (b2, start))

        if 0x50 <= b <= 0x57:
            self.push(self.regs[(b - 0x50) | ((rex & 1) << 3)])
            return
        if 0x58 <= b <= 0x5F:
            self.regs[(b - 0x58) | ((rex & 1) << 3)] = self.pop()
            return
        if 0xB8 <= b <= 0xBF:
            r = (b - 0xB8) | ((rex & 1) << 3)
            self.put(r, (self.f64() if W else self.f32()) & MASK, size)
            return

        if b in (0x88, 0x89):                                # mov r/m, r
            sz = 1 if b == 0x88 else size
            reg, rm = self.modrm(rex)
            self.write_rm(rm, self.get(reg, sz), sz)
            return
        if b in (0x8A, 0x8B):                                # mov r, r/m
            sz = 1 if b == 0x8A else size
            reg, rm = self.modrm(rex)
            self.put(reg, self.read_rm(rm, sz), sz)
            return
        if b == 0x8D:                                        # lea
            reg, rm = self.modrm(rex)
            self.put(reg, self.ea(rm), size)
            return
        if b == 0x63:                                        # movsxd
            reg, rm = self.modrm(rex)
            self.put(reg, to_signed(self.read_rm(rm, 4), 32) & MASK, size)
            return

        if b in (0x01, 0x29, 0x31, 0x39, 0x85, 0x09, 0x21):  # alu r/m, r
            reg, rm = self.modrm(rex)
            a = self.read_rm(rm, size)
            v = self.get(reg, size)
            if b == 0x01:
                self.write_rm(rm, (a + v) & MASK, size)
            elif b == 0x29:
                self.write_rm(rm, (a - v) & MASK, size)
            elif b == 0x31:
                self.write_rm(rm, a ^ v, size)
            elif b == 0x09:
                self.write_rm(rm, a | v, size)
            elif b == 0x21:
                self.write_rm(rm, a & v, size)
            elif b == 0x39:
                self.set_cmp(a, v)
            elif b == 0x85:
                r = a & v
                self.zf = r == 0
                self.lt = to_signed(r) < 0
                self.gt = to_signed(r) > 0
            return

        if b == 0x81:                                        # group1 r/m, imm32
            reg, rm = self.modrm(rex)
            a = self.read_rm(rm, size)
            imm = self.f32()
            op = reg & 7
            if op == 0:
                self.write_rm(rm, (a + imm) & MASK, size)
            elif op == 5:
                self.write_rm(rm, (a - imm) & MASK, size)
            elif op == 7:
                self.set_cmp(a, imm & MASK)
            else:
                raise NotImplementedError("group1 /%d" % op)
            return
        if b == 0xC7:                                        # mov r/m, imm32
            reg, rm = self.modrm(rex)
            self.write_rm(rm, self.f32() & MASK, size)
            return
        if b == 0x69:                                        # imul r, r/m, imm32
            reg, rm = self.modrm(rex)
            a = to_signed(self.read_rm(rm, size))
            imm = self.f32()
            self.put(reg, (a * imm) & MASK, size)
            return

        if b == 0xF7:                                        # group3
            reg, rm = self.modrm(rex)
            op = reg & 7
            a = self.read_rm(rm, size)
            if op == 3:                                      # neg
                self.write_rm(rm, (-to_signed(a)) & MASK, size)
                return
            if op == 7:                                      # idiv
                divisor = to_signed(a)
                if divisor == 0:
                    raise ZeroDivisionError("integer divide by zero at 0x%X" % start)
                num = (to_signed(self.regs[2]) << 64) | (self.regs[0] & MASK)
                num = to_signed(num & ((1 << 128) - 1), 128)
                q = abs(num) // abs(divisor)
                if (num < 0) != (divisor < 0):
                    q = -q
                r = num - q * divisor
                self.regs[0] = q & MASK
                self.regs[2] = r & MASK
                return
            raise NotImplementedError("group3 /%d" % op)

        if b == 0x99:                                        # cqo / cdq
            self.regs[2] = MASK if to_signed(self.regs[0]) < 0 else 0
            return
        if b == 0xC9:                                        # leave
            self.regs[4] = self.regs[5]
            self.regs[5] = self.pop()
            return
        if b == 0xC3:                                        # ret
            self.rip = self.pop()
            return
        if b == 0xE8:                                        # call rel32
            rel = self.f32()
            self.push(self.rip)
            self.rip = (self.rip + rel) & MASK
            return
        if b == 0xE9:                                        # jmp rel32
            rel = self.f32()
            self.rip = (self.rip + rel) & MASK
            return
        if b == 0xEB:
            rel = self.f8s()
            self.rip = (self.rip + rel) & MASK
            return
        if b == 0xFF:                                        # group5
            reg, rm = self.modrm(rex)
            op = reg & 7
            if op == 2:                                      # call r/m64
                target = self.read_rm(rm, 8)
                if target in self.thunks:
                    dll, fname = self.thunks[target]
                    self.regs[0] = self.libc.call(fname) & MASK
                    return
                self.push(self.rip)
                self.rip = target
                return
            if op == 4:
                self.rip = self.read_rm(rm, 8)
                return
            raise NotImplementedError("group5 /%d" % op)
        if b == 0x90:
            return
        raise NotImplementedError("opcode %02X at 0x%X" % (b, start))

    def run(self, max_steps=50_000_000):
        while True:
            if self.rip == self.exit_marker:
                return to_signed(self.regs[0] & 0xFFFFFFFF, 32)
            self.steps += 1
            if self.steps > max_steps:
                raise RuntimeError("step limit reached (%d)" % max_steps)
            if self.trace:
                sys.stderr.write("%08X rax=%016X rcx=%016X rsp=%016X\n"
                                 % (self.rip - self.pe.image_base,
                                    self.regs[0], self.regs[1], self.regs[4]))
            self.step()


def main(argv):
    args = argv[1:]
    if not args:
        sys.stderr.write(__doc__.strip() + "\n")
        return 2
    trace = "--trace" in args
    args = [a for a in args if a != "--trace"]
    max_steps = 50_000_000
    if "--max-steps" in args:
        i = args.index("--max-steps")
        max_steps = int(args[i + 1])
        del args[i:i + 2]
    path = args[0]

    pe = PE(path)
    cpu = CPU(pe, trace=trace)
    try:
        code = cpu.run(max_steps=max_steps)
    except ProgramExit as e:
        code = e.code
    sys.stdout.flush()
    sys.stderr.write("[accrun] %s exited with %d after %d instructions\n"
                     % (path, code, cpu.steps))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
