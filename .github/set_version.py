"""Write a release version into xresume.py: python .github/set_version.py 2026.11.19"""
import io
import re
import sys

version = sys.argv[1]
if not re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}(\.\d+)?", version):
    sys.exit("not a date version: %r" % version)
path = "xresume.py"
text = io.open(path, encoding="utf-8", newline="").read()
new, n = re.subn(r'^__version__ = ".*?"$', '__version__ = "%s"' % version, text, count=1, flags=re.M)
if n != 1:
    sys.exit("__version__ not found in xresume.py")
io.open(path, "w", encoding="utf-8", newline="").write(new)
print("xresume.py version:", version)
