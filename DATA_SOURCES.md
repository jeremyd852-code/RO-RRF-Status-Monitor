# 內建資料來源與文件狀態

本表記錄隨附資料檔現有的 `source`／`description` 欄位，不把資料來源與程式碼授權混為一談。

| 資料檔 | 資料檔記錄的來源 |
|---|---|
| `client_catalog.json` | RO 主程式資料抽取基線（不含個人設定） |
| `pet_catalog.json` | 台版 RO 客戶端 petinfo.lub 的 PetStringTable／PetFoodTable；食物名稱以 iteminfo_new.lub identifiedDisplayName 核對。 |
| `status_catalog.json` | 台版 RO data.grf 狀態圖示、技能樹及物品同名分析 |
| `job_skill_catalog.json` | RO 主程式 data.grf 官方 job/skill Lua 資料 |
| `job_status_index.json` | 台版 RO 職業系譜與狀態連結精簡索引 |
| `status_classification.json` | 台版 RO 主程式 EFST、StateIcon 與同名物品資料 |
| `可擴充資料庫.json` | 可攜式擴充資料；只載入 review_status 為 confirmed 的資料。 |

`EFSTIDs.lua` 與 `stateiconinfo.lua` 是台版 RO 客戶端狀態 ID／圖示表的既有抽取資料，檔頭保留原反編譯工具標記；`runtime_status_index.json` 是供啟動使用的精簡索引；`catalog_manifest.json` 記錄本次內建檔案的校驗值。

`status_reviews.json` 保存按 ID 與 EFST 代碼核對的名稱、用途及效果性質修訂，逐筆附依據；`status_catalog.json` 保留原始值及採納結果。相關職業用於查找，不代表施法者身份。官方其他地區資料僅在效果方向與現有台版描述一致時作為佐證，不套用其他地區數值或宣稱台版逐技能實測。

1.6.7 於 2026-09-27 重新讀取 9 月 22 日更新的台版資料，並核對 ROItemSearchApp、ROCalculator、rAthena、Hercules 的固定版本。後兩者為模擬器實作，用來交叉確認同一 EFST 符號的關聯與效果方向，並非台服官方伺服器原始碼。每筆採納記錄保留來源、版本及限制；名稱可信與效果可信分開處理。此批共採納 140 筆修訂，增加 80 筆可讀名稱、補齊 72 筆效果分類。

重建狀態資料時，先執行 `build_tools/build_reviewed_status_catalog.py`，再產生 runtime index 與 manifest，避免新讀取的原始字色或不完整名稱覆蓋已覆核結果。正式打包已依此順序執行。

物品名稱包含 2026-09-07 以台版客戶端 `System/iteminfo_new.lub` 的 `identifiedDisplayName` 欄位核對的更正及補充。RRF 僅用於離線比對 ID 與行為，原始錄影、人物名稱與聊天內容不隨附。名稱存在不表示已證明物品或技能一定觸發某個狀態。

`pet_catalog.json` 另從客戶端 `petinfo.lub` 的中文種類表及逐隻食物表建立關聯，食物中文名稱核對 `iteminfo_new.lub`。未提供名稱或關聯的項目保留缺值；不以名稱猜測食物，也不包含寵物能力或進化資料。

此份來源不包含 RO 主程式、GRF、完整物品說明表或開發參考專案。現有狀態 Lua 是必要資料的一部分，應與程式碼分別處理散布聲明。本次沒有選定或新增程式碼 LICENSE，也沒有宣稱遊戲資料具有 MIT 或其他開源授權；正式授權文件由專案維護者確定。
