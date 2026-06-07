"""PyInstaller entry point — avoids relative-import issues."""
import sys
import os

if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
else:
    _base = os.path.join(os.path.dirname(__file__), "src")
if _base not in sys.path:
    sys.path.insert(0, _base)

from ebook_translator.cli import main

main()
