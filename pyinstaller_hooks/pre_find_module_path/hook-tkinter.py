"""不要因打包環境無法開啟桌面視窗而排除 tkinter。

PyInstaller 的預設檢查會實際建立 Tcl/Tk；受限工作階段可能因此誤判
安裝損壞。真正需要的腳本與 DLL 由同層 hook-_tkinter.py 明確收錄。
"""


def pre_find_module_path(_hook_api):
    return
