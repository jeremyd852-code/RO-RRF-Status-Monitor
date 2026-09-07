"""從目前用來打包的 Python 明確收錄 Tcl/Tk 執行環境。"""

from __future__ import annotations

import os
import sys


python_root = sys.base_prefix
tcl_root = os.path.join(python_root, "tcl")
dll_root = os.path.join(python_root, "DLLs")

datas = [
    (os.path.join(tcl_root, "tcl8.6"), "_tcl_data"),
    (os.path.join(tcl_root, "tk8.6"), "_tk_data"),
]

optional_tcl_modules = os.path.join(tcl_root, "tcl8")
if os.path.isdir(optional_tcl_modules):
    datas.append((optional_tcl_modules, "tcl8"))

binaries = [
    (os.path.join(dll_root, "_tkinter.pyd"), "."),
    (os.path.join(dll_root, "tcl86t.dll"), "."),
    (os.path.join(dll_root, "tk86t.dll"), "."),
]

missing = [source for source, _destination in datas + binaries if not os.path.exists(source)]
if missing:
    raise SystemExit("Tcl/Tk packaging files are missing: " + ", ".join(missing))
