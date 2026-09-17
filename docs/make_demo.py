#!/usr/bin/env python3
"""
Build docs/demo.gif.

This runs each demo command for real, captures its actual stdout, and renders
the resulting transcript to an animated GIF with ffmpeg. Nothing is typed by
hand into the frames: if a command's output changes, the GIF changes.

    python docs/make_demo.py [--ffmpeg PATH]

Requires ffmpeg built with libfreetype (the drawtext filter).

Note on provenance: this is a rendering of a genuine transcript, not a screen
capture of a live terminal. docs/demo.tape drives charmbracelet/vhs for a true
terminal recording on machines where VHS can run.
"""

import os
import sys
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

FONT = None
for cand in (r"C:\Windows\Fonts\consola.ttf",
             r"C:\Windows\Fonts\lucon.ttf",
             r"C:\Windows\Fonts\cour.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
    if os.path.exists(cand):
        FONT = cand
        break

# colours picked for contrast on the dark background
C_PROMPT = "0x7AA2F7"
C_CMD = "0xC0CAF5"
C_OUT = "0x9ECE6A"
C_DIM = "0x565F89"
BG = "0x1A1B26"

FONTSIZE = 16
LINEH = 23
MARGIN_X = 24
MARGIN_Y = 18
WIDTH = 1000

# (command, how many output lines to keep, label)
DEMO = [
    ("cat examples/hello.c", None),
    ("python acc.py examples/hello.c -o hello.exe", None),
    ("./hello.exe; echo exit code $?", None),
    ("python accrun.py hello.exe", None),
    ("python acc.py examples/mathlib.c --dll -o mathlib.dll", None),
    ('python -c "import ctypes;print(ctypes.CDLL(\'./mathlib.dll\').factorial(10))"', None),
    ("python tests/run_tests.py", 2),
]


def capture():
    """Run every demo command and collect what it really printed."""
    lines = []
    for cmd, tail in DEMO:
        lines.append(("prompt", "$ " + cmd))
        # The demo commands are POSIX shell (./prog, $?), so run them under
        # bash when one exists. cmd.exe would reject "./hello.exe" and the
        # recording would show a shell error instead of the program output.
        bash = shutil.which("bash")
        if bash:
            proc = subprocess.run([bash, "-c", cmd], cwd=ROOT,
                                  capture_output=True, text=True)
        else:
            proc = subprocess.run(cmd, shell=True, cwd=ROOT,
                                  capture_output=True, text=True)
        out = (proc.stdout or "") + (proc.stderr or "")
        got = [l.rstrip() for l in out.splitlines() if l.strip()]
        if tail:
            got = got[-tail:]
        for l in got:
            lines.append(("out", l))
        lines.append(("gap", ""))
    while lines and lines[-1][0] == "gap":
        lines.pop()
    return lines


def build(lines, ffmpeg, out_path):
    height = MARGIN_Y * 2 + LINEH * len(lines)
    height = max(height, 200)
    if height % 2:
        height += 1

    tmp = tempfile.mkdtemp(prefix="accgif_")
    filters = []
    t = 0.35
    for i, (kind, text) in enumerate(lines):
        if kind == "gap" or not text:
            t += 0.25
            continue
        path = os.path.join(tmp, "l%03d.txt" % i)
        with open(path, "w", encoding="utf-8") as fh:
            # drawtext still interprets escape sequences inside a textfile,
            # so a literal backslash has to be doubled or "\n" in C source
            # renders as "n".
            fh.write(text.replace("\\", "\\\\"))
        colour = {"prompt": C_CMD, "out": C_OUT}[kind]
        if kind == "out" and (text.startswith("acc:") or text.startswith("accld:")
                              or text.startswith("[accrun]")):
            colour = C_DIM
        if kind == "out" and "PASS" in text:
            colour = C_OUT
        esc = path.replace("\\", "/").replace(":", "\\:")
        y = MARGIN_Y + i * LINEH
        filters.append(
            "drawtext=fontfile='%s':textfile='%s':x=%d:y=%d:"
            "fontsize=%d:fontcolor=%s:enable='gte(t,%.2f)'"
            % (FONT.replace("\\", "/").replace(":", "\\:"), esc,
               MARGIN_X, y, FONTSIZE, colour, t))
        t += 0.30 if kind == "prompt" else 0.14

    duration = t + 2.2
    graph = ",".join(filters)

    palette = os.path.join(tmp, "pal.png")
    src = "color=c=%s:s=%dx%d:d=%.2f:r=12" % (BG, WIDTH, height, duration)

    r = subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", src,
                        "-vf", graph + ",palettegen=max_colors=64",
                        palette], capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-1500:])
        return False

    r = subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", src,
                        "-i", palette,
                        "-lavfi", graph + "[x];[x][1:v]paletteuse",
                        "-loop", "0", out_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-1500:])
        return False

    shutil.rmtree(tmp, ignore_errors=True)
    return True


def main(argv):
    ffmpeg = "ffmpeg"
    if "--ffmpeg" in argv:
        ffmpeg = argv[argv.index("--ffmpeg") + 1]
    if not shutil.which(ffmpeg) and not os.path.exists(ffmpeg):
        sys.stderr.write("ffmpeg not found; pass --ffmpeg PATH\n")
        return 2
    if not FONT:
        sys.stderr.write("no monospace font found\n")
        return 2

    lines = capture()
    sys.stdout.write("captured %d transcript lines\n" % len(lines))
    out_path = os.path.join(HERE, "demo.gif")
    if not build(lines, ffmpeg, out_path):
        sys.stderr.write("ffmpeg failed\n")
        return 1
    sys.stdout.write("wrote %s (%d bytes)\n"
                     % (out_path, os.path.getsize(out_path)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
