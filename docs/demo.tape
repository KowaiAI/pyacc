# acc demo -- recorded with VHS (https://github.com/charmbracelet/vhs)
#
#   vhs docs/demo.tape
#
# Every command below really runs. The GIF is a recording of a live terminal,
# not a mock-up, so regenerating it re-proves the output.

Output docs/demo.gif

Set Shell bash
Set FontSize 17
Set Width 1080
Set Height 680
Set Padding 22
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

Sleep 700ms

Type "cat hello.c"
Enter
Sleep 1600ms

Type "python acc.py hello.c"
Enter
Sleep 2600ms

Type "./hello.exe"
Enter
Sleep 2200ms

Type "python acc.py mathlib.c --dll"
Enter
Sleep 2600ms

Type `python -c "import ctypes;print(ctypes.CDLL('./mathlib.dll').factorial(10))"`
Enter
Sleep 2800ms

Type "python tests/run_tests.py | tail -12"
Enter
Sleep 6000ms
