"""
Inserts flocker on to sys.path.

:var FilePath TOPLEVEL: The top-level of the flocker repository.
:var FilePath BASEPATH: The executable being run.
"""

from twisted.python.filepath import FilePath
import sys

path = BASEPATH = FilePath(sys.argv[0])
for parent in path.parents():
    if parent.descendant(['flocker', '__init__.py']).exists():
        TOPLEVEL = path
        sys.path.insert(0, parent.path)
        break
else:
    raise ImportError("Could not find top-level.")
