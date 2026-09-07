"""1.6.5 台版 RO 資料層。

本套件不匯入 Tkinter，也不讀取 RRF。即時監控只接收已驗證的不可變
資料快照；大型 RO 資料工作由獨立子程序執行。
"""

from catalog.repository import CatalogRepository, RuntimeCatalogSnapshot
from catalog.unknown_journal import UnknownJournal, UnknownRecord

__all__ = [
    "CatalogRepository",
    "RuntimeCatalogSnapshot",
    "UnknownJournal",
    "UnknownRecord",
]
