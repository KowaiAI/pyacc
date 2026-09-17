#!/usr/bin/env python3
"""
accld -- the Agent Linker

Takes COFF objects produced by `acc -c` and produces a Windows PE32+ image:
an executable or a DLL. It does what a linker does and nothing more:

  * concatenates the .text and .data sections of every input object
  * builds one global symbol table and reports duplicate definitions
  * resolves every relocation, and names the symbol when it cannot
  * turns undefined __imp_NAME symbols into a real import table (msvcrt.dll)
  * synthesizes the startup stub that calls main and then exit
  * writes the export directory when building a DLL

usage: accld [options] OBJECT.obj...

options:
  -o FILE          output path (default: a.exe, or a.dll with --dll)
  --dll            produce a DLL instead of an executable
  --entry NAME     entry symbol for an exe (default: main)
  --export A,B     export only these symbols from a DLL (default: all)
  --map            print the link map (every symbol and its address)
  --json           machine-readable result on stdout

exit codes: 0 linked, 1 link error, 2 usage error
"""

import sys
import os
import json
import struct

SECT_ALIGN = 0x1000
FILE_ALIGN = 0x200
IMAGE_REL_AMD64_REL32 = 4
IMAGE_REL_AMD64_ADDR64 = 1


class LinkError(Exception):
    def __init__(self, msg, detail=None):
        Exception.__init__(self, msg)
        self.msg = msg
        self.detail = detail


def align_up(v, a):
    return (v + a - 1) // a * a


# ---------------------------------------------------------------------------
# COFF object reader
# ---------------------------------------------------------------------------

class Obj:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as fh:
            raw = fh.read()
        self.raw = raw
        if len(raw) < 20:
            raise LinkError("%s: too small to be a COFF object" % path)
        (machine, nsect, _, symptr, nsyms, optsize,
         _chars) = struct.unpack_from("<HHIIIHH", raw, 0)
        if machine != 0x8664:
            raise LinkError("%s: not an x86-64 object (machine=0x%X)"
                            % (path, machine))
        if optsize:
            raise LinkError("%s: has an optional header; that is an image, "
                            "not an object" % path)

        self.text = b""
        self.data = b""
        self.text_relocs = []
        self.data_relocs = []
        self.sect_index = {}

        for i in range(nsect):
            off = 20 + 40 * i
            name = raw[off:off + 8].rstrip(b"\x00").decode("ascii", "replace")
            size, rawoff, relptr, _lineptr = struct.unpack_from("<IIII",
                                                                raw, off + 16)
            nrel = struct.unpack_from("<H", raw, off + 32)[0]
            blob = raw[rawoff:rawoff + size] if (size and rawoff) else b""
            relocs = []
            for r in range(nrel):
                ro = relptr + 10 * r
                addr, sym, typ = struct.unpack_from("<IIH", raw, ro)
                relocs.append((addr, sym, typ))
            self.sect_index[i + 1] = name
            if name == ".text":
                self.text, self.text_relocs = blob, relocs
            elif name == ".data":
                self.data, self.data_relocs = blob, relocs
            elif blob:
                raise LinkError("%s: unsupported section %r" % (path, name))

        strtab_off = symptr + 18 * nsyms
        strtab = raw[strtab_off:] if strtab_off < len(raw) else b""

        def read_name(field):
            if field[:4] == b"\x00\x00\x00\x00":
                off = struct.unpack_from("<I", field, 4)[0]
                end = strtab.find(b"\x00", off)
                return strtab[off:end].decode("ascii", "replace")
            return field.rstrip(b"\x00").decode("ascii", "replace")

        self.symbols = []
        i = 0
        while i < nsyms:
            so = symptr + 18 * i
            field = raw[so:so + 8]
            value, sect, typ, cls, naux = struct.unpack_from("<IhHBB", raw,
                                                             so + 8)
            self.symbols.append({"name": read_name(field), "value": value,
                                 "sect": sect, "type": typ, "class": cls,
                                 "index": i})
            for _ in range(naux):
                self.symbols.append(None)
            i += 1 + naux


# ---------------------------------------------------------------------------
# the link
# ---------------------------------------------------------------------------

class Linker:
    def __init__(self, objs, dll=False, entry="main", exports=None):
        self.objs = objs
        self.dll = dll
        self.entry_name = entry
        self.want_exports = exports
        self.image_base = 0x180000000 if dll else 0x140000000
        self.symbols = {}
        self.imports = []
        self.map_entries = []

    def link(self):
        text = bytearray()
        stub_len = 0
        if not self.dll:
            stub_len = 32                       # patched once main is placed
            text += b"\x00" * stub_len

        # ---- lay out .text, then .data ----------------------------------
        for o in self.objs:
            while len(text) % 16:
                text += b"\x00"
            o.text_base = len(text)
            text += o.text

        data = bytearray()
        for o in self.objs:
            while len(data) % 16:
                data += b"\x00"
            o.data_base = len(data)
            data += o.data

        self.text_rva = SECT_ALIGN
        self.data_rva = self.text_rva + align_up(len(text), SECT_ALIGN)

        # ---- collect defined symbols ------------------------------------
        for o in self.objs:
            for s in o.symbols:
                if s is None or s["sect"] <= 0:
                    continue
                sname = o.sect_index.get(s["sect"], "")
                if sname == ".text":
                    addr = self.text_rva + o.text_base + s["value"]
                elif sname == ".data":
                    addr = self.data_rva + o.data_base + s["value"]
                else:
                    continue
                if s["class"] == 3 and s["name"] in (".text", ".data"):
                    continue                    # section symbols are per-object
                if s["name"] in self.symbols:
                    prev = self.symbols[s["name"]]
                    raise LinkError("duplicate definition of %r" % s["name"],
                                    "defined in %s and %s"
                                    % (prev["from"], o.path))
                self.symbols[s["name"]] = {"addr": addr, "from": o.path,
                                           "sect": sname}

        # ---- discover imports -------------------------------------------
        for o in self.objs:
            for s in o.symbols:
                if s is None or s["sect"] != 0:
                    continue
                if s["name"].startswith("__imp_"):
                    fn = s["name"][6:]
                    if fn not in self.imports:
                        self.imports.append(fn)
        if not self.dll and "exit" not in self.imports:
            self.imports.append("exit")

        # ---- append import and export tables to .data --------------------
        self.build_tables(data, text, stub_len)

        # ---- relocate ----------------------------------------------------
        for o in self.objs:
            self.relocate(o, text, data)

        if not self.dll:
            self.write_stub(text, stub_len)

        return self.write_pe(text, data)

    # -- import / export tables -------------------------------------------
    def build_tables(self, data, text, stub_len):
        base = self.data_rva

        while len(data) % 8:
            data += b"\x00"
        name_rva = {}
        for fn in self.imports:
            if len(data) % 2:
                data += b"\x00"
            name_rva[fn] = base + len(data)
            data += struct.pack("<H", 0) + fn.encode("ascii") + b"\x00"
        if len(data) % 2:
            data += b"\x00"
        dll_name_rva = base + len(data)
        data += b"msvcrt.dll\x00"

        while len(data) % 8:
            data += b"\x00"
        ilt_rva = base + len(data)
        for fn in self.imports:
            data += struct.pack("<Q", name_rva[fn])
        data += struct.pack("<Q", 0)

        self.iat_rva = base + len(data)
        self.iat = {}
        for fn in self.imports:
            self.iat[fn] = base + len(data)
            data += struct.pack("<Q", name_rva[fn])
        data += struct.pack("<Q", 0)
        self.iat_size = 8 * (len(self.imports) + 1)

        self.desc_rva = base + len(data)
        data += struct.pack("<IIIII", ilt_rva, 0, 0, dll_name_rva,
                            self.iat_rva)
        data += b"\x00" * 20

        self.exp_rva = 0
        self.exp_size = 0
        if self.dll:
            names = self.want_exports
            if names is None:
                names = [n for n, s in self.symbols.items()
                         if s["sect"] == ".text"]
            missing = [n for n in names if n not in self.symbols]
            if missing:
                raise LinkError("cannot export undefined symbol(s): %s"
                                % ", ".join(sorted(missing)))
            names = sorted(names)
            if not names:
                raise LinkError("a DLL must export at least one symbol")
            self.export_names = names

            while len(data) % 4:
                data += b"\x00"
            func_arr = base + len(data)
            for n in names:
                data += struct.pack("<I", self.symbols[n]["addr"])
            name_arr = base + len(data)
            name_arr_pos = len(data)
            data += b"\x00" * (4 * len(names))
            ord_arr = base + len(data)
            for i in range(len(names)):
                data += struct.pack("<H", i)
            rvas = []
            for n in names:
                rvas.append(base + len(data))
                data += n.encode("ascii") + b"\x00"
            mod_rva = base + len(data)
            data += (self.module_name.encode("ascii") + b"\x00")
            for i, r in enumerate(rvas):
                struct.pack_into("<I", data, name_arr_pos + 4 * i, r)
            while len(data) % 4:
                data += b"\x00"
            self.exp_rva = base + len(data)
            data += struct.pack("<IIHHIIIIIII", 0, 0, 0, 0, mod_rva, 1,
                                len(names), len(names), func_arr, name_arr,
                                ord_arr)
            self.exp_size = 40

    # -- relocation --------------------------------------------------------
    def relocate(self, o, text, data):
        for addr, symidx, typ in o.text_relocs:
            if symidx >= len(o.symbols) or o.symbols[symidx] is None:
                raise LinkError("%s: relocation references bad symbol index %d"
                                % (o.path, symidx))
            s = o.symbols[symidx]
            S = self.resolve(o, s)
            P = self.text_rva + o.text_base + addr
            field_pos = o.text_base + addr
            if typ == IMAGE_REL_AMD64_REL32:
                A = struct.unpack_from("<i", text, field_pos)[0]
                struct.pack_into("<i", text, field_pos, S + A - (P + 4))
            elif typ == IMAGE_REL_AMD64_ADDR64:
                A = struct.unpack_from("<q", text, field_pos)[0]
                struct.pack_into("<Q", text, field_pos,
                                 self.image_base + S + A)
            else:
                raise LinkError("%s: unsupported relocation type %d"
                                % (o.path, typ))

    def resolve(self, o, s):
        name = s["name"]
        if s["sect"] > 0:
            sect = o.sect_index.get(s["sect"], "")
            if sect == ".text":
                return self.text_rva + o.text_base + s["value"]
            if sect == ".data":
                return self.data_rva + o.data_base + s["value"]
            raise LinkError("%s: symbol %r lives in unsupported section %r"
                            % (o.path, name, sect))
        if name in self.symbols:
            return self.symbols[name]["addr"]
        if name.startswith("__imp_"):
            fn = name[6:]
            if fn in self.iat:
                return self.iat[fn]
        raise LinkError("undefined reference to %r" % name,
                        "referenced from %s" % o.path)

    # -- startup stub ------------------------------------------------------
    def write_stub(self, text, stub_len):
        if self.entry_name not in self.symbols:
            raise LinkError("undefined entry symbol %r" % self.entry_name,
                            "an executable needs int %s(void)"
                            % self.entry_name)
        main_rva = self.symbols[self.entry_name]["addr"]
        stub = bytearray()
        stub += b"\x48\x81\xEC" + struct.pack("<i", 40)   # sub rsp, 40
        call_main = len(stub) + 1
        stub += b"\xE8" + b"\x00" * 4                     # call main
        stub += b"\x89\xC1"                               # mov ecx, eax
        call_exit = len(stub) + 2
        stub += b"\xFF\x15" + b"\x00" * 4                 # call [rip+exit]
        stub += b"\xC3"                                   # ret (unreachable)
        if len(stub) > stub_len:
            raise LinkError("internal: startup stub does not fit")
        struct.pack_into("<i", stub, call_main,
                         main_rva - (self.text_rva + call_main + 4))
        struct.pack_into("<i", stub, call_exit,
                         self.iat["exit"] - (self.text_rva + call_exit + 4))
        text[0:len(stub)] = stub

    # -- PE image ----------------------------------------------------------
    def write_pe(self, text, data):
        # A DLL with no relocation directory cannot be rebased, so two DLLs
        # sharing a preferred image base cannot both load into one process
        # (WinError 487). acc code is rip-relative and needs no fixups, so one
        # block of ABSOLUTE no-op entries carries the permission without the
        # work.
        reloc = b""
        if self.dll:
            reloc = struct.pack("<II", self.text_rva, 12) + struct.pack("<HH", 0, 0)

        text_raw = align_up(len(text), FILE_ALIGN)
        data_raw = align_up(len(data), FILE_ALIGN)
        reloc_raw = align_up(len(reloc), FILE_ALIGN) if reloc else 0
        headers = FILE_ALIGN
        text_off = headers
        data_off = text_off + text_raw
        reloc_off = data_off + data_raw if reloc else 0
        reloc_rva = (self.data_rva + align_up(len(data), SECT_ALIGN)
                     if reloc else 0)
        nsections = 3 if reloc else 2
        size_of_image = (reloc_rva + align_up(len(reloc), SECT_ALIGN) if reloc
                         else self.data_rva + align_up(len(data), SECT_ALIGN))

        dos = bytearray(0x80)
        dos[0:2] = b"MZ"
        struct.pack_into("<H", dos, 0x02, 0x90)
        struct.pack_into("<H", dos, 0x04, 0x03)
        struct.pack_into("<H", dos, 0x18, 0x40)
        struct.pack_into("<I", dos, 0x3C, 0x80)
        dos[0x4E:0x4E + 42] = b"This program cannot be run in DOS mode.\r\n$"

        chars = 0x2022 if self.dll else (0x0022 | 0x0001)
        coff = struct.pack("<HHIIIHH", 0x8664, nsections, 0, 0, 0, 240, chars)

        entry = 0 if self.dll else self.text_rva

        opt = b""
        opt += struct.pack("<H", 0x20B)
        opt += struct.pack("<BB", 0, 1)
        opt += struct.pack("<I", text_raw)
        opt += struct.pack("<I", data_raw)
        opt += struct.pack("<I", 0)
        opt += struct.pack("<I", entry)
        opt += struct.pack("<I", self.text_rva)
        opt += struct.pack("<Q", self.image_base)
        opt += struct.pack("<I", SECT_ALIGN)
        opt += struct.pack("<I", FILE_ALIGN)
        opt += struct.pack("<HH", 6, 0)
        opt += struct.pack("<HH", 0, 0)
        opt += struct.pack("<HH", 6, 0)
        opt += struct.pack("<I", 0)
        opt += struct.pack("<I", size_of_image)
        opt += struct.pack("<I", headers)
        opt += struct.pack("<I", 0)
        opt += struct.pack("<H", 3)
        opt += struct.pack("<H", 0x8140 if self.dll else 0x8100)
        opt += struct.pack("<Q", 0x100000)
        opt += struct.pack("<Q", 0x1000)
        opt += struct.pack("<Q", 0x100000)
        opt += struct.pack("<Q", 0x1000)
        opt += struct.pack("<I", 0)
        opt += struct.pack("<I", 16)
        dirs = [(0, 0)] * 16
        dirs[0] = (self.exp_rva, self.exp_size)
        dirs[1] = (self.desc_rva, 40)
        dirs[5] = (reloc_rva, len(reloc))
        dirs[12] = (self.iat_rva, self.iat_size)
        for rva, size in dirs:
            opt += struct.pack("<II", rva, size)

        def sect(name, vsize, rva, rawsize, rawoff, c):
            nm = (name.encode("ascii") + b"\x00" * 8)[:8]
            return nm + struct.pack("<IIIIIIHHI", vsize, rva, rawsize, rawoff,
                                    0, 0, 0, 0, c)

        out = bytearray()
        out += dos
        out += b"PE\x00\x00"
        out += coff
        out += opt
        out += sect(".text", len(text), self.text_rva, text_raw, text_off,
                    0x60000020)
        out += sect(".data", len(data), self.data_rva, data_raw, data_off,
                    0xC0000040)
        if reloc:
            out += sect(".reloc", len(reloc), reloc_rva, reloc_raw, reloc_off,
                        0x42000040)
        out += b"\x00" * (headers - len(out))
        out += bytes(text) + b"\x00" * (text_raw - len(text))
        out += bytes(data) + b"\x00" * (data_raw - len(data))
        if reloc:
            out += reloc + b"\x00" * (reloc_raw - len(reloc))
        return bytes(out)


def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(__doc__.strip() + "\n")
        return 0 if args else 2

    objs = []
    out_path = None
    dll = False
    entry = "main"
    exports = None
    want_map = False
    as_json = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "-o":
            i += 1
            out_path = args[i] if i < len(args) else None
        elif a == "--dll":
            dll = True
        elif a == "--entry":
            i += 1
            entry = args[i]
        elif a == "--export":
            i += 1
            exports = [x for x in args[i].split(",") if x]
        elif a == "--map":
            want_map = True
        elif a == "--json":
            as_json = True
        elif a.startswith("-"):
            sys.stderr.write("accld: unknown option %r\n" % a)
            return 2
        else:
            objs.append(a)
        i += 1

    if not objs:
        sys.stderr.write("accld: no input objects\n")
        return 2
    out_path = out_path or ("a.dll" if dll else "a.exe")

    try:
        loaded = [Obj(p) for p in objs]
        ln = Linker(loaded, dll=dll, entry=entry, exports=exports)
        ln.module_name = os.path.basename(out_path)
        image = ln.link()
    except LinkError as e:
        if as_json:
            json.dump({"ok": False, "error": e.msg, "detail": e.detail},
                      sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stderr.write("accld: error: %s\n" % e.msg)
            if e.detail:
                sys.stderr.write("       %s\n" % e.detail)
        return 1
    except FileNotFoundError as e:
        sys.stderr.write("accld: %s\n" % e)
        return 2

    with open(out_path, "wb") as fh:
        fh.write(image)

    info = {
        "ok": True,
        "output": out_path,
        "kind": "dll" if dll else "exe",
        "bytes": len(image),
        "objects": objs,
        "imports": ln.imports,
        "exports": getattr(ln, "export_names", []),
        "symbols": len(ln.symbols),
    }
    if want_map:
        info["map"] = sorted(
            [{"symbol": n, "rva": "0x%X" % s["addr"], "section": s["sect"],
              "object": s["from"]} for n, s in ln.symbols.items()],
            key=lambda e: e["rva"])

    if as_json:
        json.dump(info, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write("accld: wrote %s [%s] (%d bytes) from %d object(s), "
                         "%d symbol(s)\n"
                         % (out_path, info["kind"], len(image), len(objs),
                            len(ln.symbols)))
        if ln.imports:
            sys.stdout.write("accld: imports: %s\n" % ", ".join(ln.imports))
        if info["exports"]:
            sys.stdout.write("accld: exports: %s\n" % ", ".join(info["exports"]))
        if want_map:
            sys.stdout.write("\n  RVA         SECTION  SYMBOL\n")
            for e in info["map"]:
                sys.stdout.write("  %-10s  %-7s  %s  (%s)\n"
                                 % (e["rva"], e["section"], e["symbol"],
                                    e["object"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
