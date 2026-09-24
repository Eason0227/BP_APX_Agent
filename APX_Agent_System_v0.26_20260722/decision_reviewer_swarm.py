"""
decision_reviewer_swarm.py
---------------------------
AI Decision Reviewer — 半導體設備粒子控制決策審查（Multi-Agent / Swarm 架構）。

職責：
  接收 Dir Agent 產出報告中的「## 四、決策指令與資源調度」表格，
  依「機台設備類型」分組批次審查每一筆維修 Action 是否符合設備結構知識，
  駁回不合理 Action（無設備依據 / 套用他型 action / 假設不存在 component），
  並依該設備正確的粒子源與設備領域知識產出修正建議（原 Action 若已引用歷史案例則優先保留），
  最終回填為加上「設備類型 / 審查判定」兩欄的 Final Action Plan 表格。

架構（同類型分組批次 handoff）：
  Router（確定性 Python 分流）
      → 依機台 ID 判斷設備類型並分組
  設備專家群（OpenAI Agents SDK Agent，平行批次）
      → SCOP / PRRM / COAT / Unknown 各只裝自己設備知識
  Aggregator（Agents SDK Agent）
      → 合併各專家審查結果，輸出最終 ## 四 表格 + 審查摘要
      → 若 LLM 輸出異常，退回 Python 確定性組裝
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

import httpx
from agents import (
    Agent,
    Runner,
    OpenAIResponsesModel,
    AsyncOpenAI,
    set_tracing_disabled,
)

set_tracing_disabled(True)

# ── 模型設定（與 executive_agent_api 對齊，可用環境變數覆寫）──
import os

API_BASE_URL = os.getenv("REVIEWER_API_BASE_URL", "http://10.11.32.155:4000/v1")
API_MODEL = os.getenv("REVIEWER_API_MODEL", "Qwen3.6-35B-A3B")
# API_MODEL = os.getenv("REVIEWER_API_MODEL", "Gemma4-31B")
API_KEY = os.getenv("REVIEWER_API_KEY", "sk-vVPq-hpFctglwngOCf7bOA")


# ═══════════════════════════════════════════════════════════════════════════
# 一、設備知識庫（各專家 Agent 的 System Prompt 素材）
# ═══════════════════════════════════════════════════════════════════════════

# 機台 ID → 設備類型 推斷用關鍵詞、設備類型中文標籤
# 統一由本檔下方的 `_EQUIPMENT_REGISTRY` 衍生（見「設備知識庫」區塊末端），
# 新增設備類型只需在 registry 增列一筆，hints / label / 專家 Agent 會自動生成。


_REVIEWER_ROLE_HEADER = """\
你是一個 Semiconductor Equipment Particle Control Decision Reviewer
（半導體設備粒子控制決策審查官）。

你的任務：
1. Review 傳入的每一筆維修 Action（決策指令）。
2. 依「本設備專屬知識」與你對此設備結構的領域知識，判斷 Action 是否有設備結構依據。
3. Reject 不合理 Action：無設備結構依據、套用其他機型的 Action、假設不存在的 component。
4. 對被 Reject 的 Action，先看原 Action／決策原因是否已引用歷史參考案例（例：W19 案例、同機台或同家族 CIP 案例）：
   - 有案例且 action 與案例一致 → 優先保留，視為有實證依據。
   - 無案例 → 再依本設備的粒子源與設備領域知識，自行判斷最合適的措施產出修正建議。
5. 合理的 Action 原樣保留。

【判斷原則】
- 你可以依對此設備的領域知識自由選用合適的維修措施，不受任何固定清單限制；
  下方「常見改善措施參考」僅為範例，非窮舉清單，也非唯一合法選項。
- 但仍須遵守下列硬性禁止。

【硬性禁止】
- 產出無設備結構依據的維修建議。
- 套用其他機型的 Action。
- 假設不存在的 component。
"""

_REVIEWER_OUTPUT_SPEC = """\
【保留規範】
- 保留原本的 P1/P2 優先序、三段式決策原因、KPI 數值目標、主責部門（EE）與時限、預期效果。
- 只修改「決策指令(Action)」本身到符合設備領域知識的合理措施，不重排優先序、不重寫決策邏輯。
- 決策指令保留子步驟編號（1️⃣2️⃣3️⃣）格式。

【參考案例優先原則】
- 若原 Action 或決策原因中已引用具體歷史參考案例（例：W19 案例、同機台／同家族 CIP 案例），
  且該 action 與案例做法一致，視為有實證依據，應優先保留該 action 並判 ✅符合，
  不得僅因該 action 未出現在「常見改善措施參考」清單而替換。
- 僅在【無參考案例】或 action 與案例明顯不符、或命中禁止清單時，
  才由你依設備領域知識判斷是否修正。

【監測／閾值動作通則（所有設備一致）】
- 「監測、趨勢追蹤、OOB/PI 閾值調整」等屬「監控管理」動作，非粒子落塵的根治措施。
- 此類子步驟可原樣保留，且不得僅因含此類動作就單獨判 ⛔違規已替換。
- 但監測動作不可作為根因改善的唯一依據；若一筆 Action 只剩監測/閾值調整而缺少實體設備改善，
  須依本設備的粒子源與設備領域知識補上對應的落塵改善措施，並判 ⚠️已修正。

【輸出格式】— 嚴格遵守，只輸出「資料列」，每台機台一列，不要輸出表頭、不要程式碼框、不要多餘說明。
每列固定 10 欄，欄位順序如下（以半形 | 分隔）：
| 優先序 | 機台 | 設備類型 | 決策指令(審查後) | 審查判定 | 決策原因 | 主責部門 | 協作部門 | 時限 | 預期效果 |

「設備類型」欄請填：{equipment_label}
「審查判定」欄只能填下列其一：
  ✅符合          （原 Action 完全合理或已有歷史案例佐證，未修改）
  ⚠️已修正        （原 Action 部分不當，已依本設備粒子源與設備領域知識修正）
  ⛔違規已替換     （原 Action 命中禁止清單或屬他型措施，已整筆替換）

輸出所有資料列後，另起一行輸出：
---SUMMARY---
接著逐筆條列「被修正或駁回」的機台（✅符合者免列），格式：
- {{機台}}｜原 Action：xxx｜修正後：xxx｜依據：xxx
一律繁體中文。
"""

_SCOP_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：SCOP 光學檢查／顯微量測（代表機台 V5300 SCOP）━━━━━━
【優先檢查粒子源】
主查 Facility airflow、FFU／HEPA 完整性與局部氣流、光學罩與機台外殼積塵、
FOUP／Load Port、Robot／End-Effector、Wafer Stage／Vacuum Stage（若設備配置）與接觸面。

【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - FFU/HEPA Integrity Check
  - Optical Enclosure / Window Cleaning
  - Stage Cleaning
  - FOUP Cleaning
  - Super Clean
  
【禁止（命中即 ⛔違規已替換）】
  - 將光學校正、相機參數調整、一般鏡頭擦拭視為落塵根治措施。
  - 感測器校正與粒子趨勢分析當作落塵改善（此兩者應與落塵管理分開）。
  - Chuck Seal Ring 僅在實機具 Vacuum Chuck／O-Ring 時才可列為根因；
    若無法確認機台具此配置，須於決策原因標註「待確認機台是否具 Vacuum Chuck/O-Ring 配置」，不得直接列為根因。
"""

_PRRM_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：PRRM 光阻去除／濕製程（代表機台 UFO-300 PRRM）━━━━━━
【優先檢查粒子源】
  剝膜化學液品質、Chemical Filtration／循環過濾、噴嘴、Spinner、Chuck、
  排液與排風、乾燥殘留，以及 FOUP／Robot Transfer。
  需以相同 Recipe、同化學槽及前後段 Lot Route 交叉確認污染來源。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Chemical Filter Replacement
  - Nozzle Cleaning
  - Spinner / Chuck Cleaning
  - Drain Cleaning
  - Exhaust Cleaning
  - Chemical System PM
  - FOUP / Transfer Cleaning
  - OEM Approved Solvent Cleaning
【禁止（命中即 ⛔違規已替換）】
  - 光路封閉、防閃罩、光源校正等（屬光學/微影設備措施，非 PR Stripper 落塵改善）。
  - CIP 未明確定義化學品、清洗模組及驗收條件。
"""

_COAT_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：COAT PR/PI 旋塗（代表機台 LITHIUS ProAP COAT）━━━━━━
【優先檢查粒子源】
  Cup／Bowl、Dispense Nozzle、Edge Bead Removal (EBR)、Hotplate 表面、
  Exhaust Balance、旋塗腔體及高黏度 PR／PI 殘膜。
  溶劑、烘烤及清潔頻率應符合 TEL 與廠內 BKM。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Cup/Bowl Cleaning
  - EBR 殘膜清除
  - Spin Chamber Cleaning
  - Hotplate Cleaning
  - Dispense Nozzle Cleaning
  - Exhaust Cleaning / Balance Check
  - OEM Approved Solvent Cleaning
【禁止（命中即 ⛔違規已替換）】
  - 固定風量倍率、Hotplate 拋光、固定 kWhr 更換濾網、未資格化之高頻 CIP。
"""

_UNKNOWN_KNOWLEDGE = """\
━━━━━━ 未定義設備處理原則 ━━━━━━
本組機台無法從機台 ID 對應到已知設備知識庫。
【處理原則】
  - 一律不得臆測套用其他機型的 Action。
  - 審查判定固定填：❓未定義設備
  - 設備類型欄填：未定義設備
  - 決策指令(審查後) 欄保留原 Action 原文，並在其後加註「（未定義設備，建議轉人工設備工程師確認）」。
  - 決策原因、優先序、主責部門、時限、預期效果原樣保留。
"""


_DEVP_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：DEVP PR／PI 顯影（代表機台 LITHIUS ProAP DEVP）━━━━━━
【優先檢查粒子源】
  Developer Nozzle、Puddle 區、Wafer Edge Contact、排液／Drain、Track Transfer、
  Robot／End-Effector、模組密封及排風。
  Seal Ring 僅於設備確有 Vacuum Chuck／密封件時列入檢查。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Developer Nozzle Cleaning
  - Chemical Filter Replacement
  - Drain Cleaning
  - Developer Chamber Cleaning
  - Flow Rate Calibration
【禁止（命中即 ⛔違規已替換）】
  - Cup／Bowl、熱流校正或 HEPA 更換不可直接由其他模組移植；應先確認實際模組配置。
  - 感測器高頻檢查不等同校正；封閉測試不以固定 30 分鐘作唯一合格判定。
"""

_AGST_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：AGST AOI／自動晶圓檢查（代表機台 ARGUS X3008 / Eagle T-AP）━━━━━━
【優先檢查粒子源】
  FOUP、Load Port、Robot／End-Effector、Wafer Stage、Optical Enclosure、
  Illumination Housing 及局部氣流。
  建議搭配空片、搬運路徑分段及環境背景資料定位污染來源。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - FOUP Cleaning
  - Robot / End-Effector Cleaning
  - Stage Cleaning
  - Optical System Cleaning
  - Particle Mapping
  - OOB Trend Monitoring
  - Empty Wafer Verification
【禁止（命中即 ⛔違規已替換）】
  - 不得直接套用 SCOP 或其他設備案例；Stage 是否具 O-Ring 或 Vacuum 結構應依 BOM／設備圖面確認。
  - 機構干涉：防塵罩、光學窗或局部密封改善，絕對不可影響影像、對焦、機構行程與 OEM 校正要求。
"""

_ETCH_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：ETCH UBM 金屬濕蝕刻（代表機台 UFO-300 ETCH）━━━━━━
【優先檢查粒子源】
  酸液／化學液品質、循環過濾、噴嘴、槽體、排風、乾燥、FOUP Transfer，
  及 Consumables（Glove、Wipe、Tape）的化學相容性。
  改善後應以粒子、金屬殘留或產品缺陷共同驗證。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Process Tank / Module Cleaning
  - Chemical Replacement
  - Wet Process Filter Replacement
  - Nozzle Cleaning
  - Exhaust Cleaning
  - Consumables Management
  - Cross Contamination Control
  - FOUP / Transfer Cleaning
【禁止（命中即 ⛔違規已替換）】
  - 濕度、濾網或耗材更換頻率不宜固定設定，應依 Facility 規範、壓差、流量及化學液使用狀況管理。
  - 週期固定化：手套、濕度（如 <30%）與濾網更換不可採任意固定值，
    須依化學相容性、EHS、壓差與實際使用紀錄觸發。
"""

_WFCL_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：WFCL 單片濕式晶圓清洗（代表機台 UFO-300 WFCL）━━━━━━
【優先檢查粒子源】
  DI Water／化學液品質、濾芯、噴嘴、Spin Chuck、Final Rinse、乾燥、排液／排風及前端 Transfer。
  OOB／PI 需確認量測位置及是否受環境漂移影響。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - DI Water Filter Replacement
  - Chemical Filter Replacement
  - Brush Cleaning / Replacement
  - Nozzle Cleaning
  - Dryer Inspection
  - Drain Cleaning
  - Transfer Cleaning
【禁止（命中即 ⛔違規已替換）】
  - Helium Exposure、光學元件或濾光片清潔僅於設備具相關功能時適用，不宜作為通用粒子改善方法。
  - 泡沫使用需符合化學相容性。
"""

_OVEN_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：OVEN 熱處理／烘烤（代表機台 SO2-12L-F）━━━━━━
【優先檢查粒子源】
  循環風扇、風道、Tray／Carrier（依設備配置）、門框 Gasket、排風、
  低殘渣潤滑及人員／手套碎屑。
  溫度或風量調整後應重新完成 Recipe 與熱歷程資格化。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Furnace / Oven Chamber Cleaning
  - Door Seal Inspection
  - Circulation Fan Maintenance
  - Exhaust Cleaning
  - Airflow Verification
  - Thermal Uniformity Verification
  - Recipe Requalification
【禁止（命中即 ⛔違規已替換）】
  - 一般 Oven 不宜直接套用 Chuck Seal Ring 或未證實之化學除垢方法，
    除非設備圖面與污染分析已證實具相對應結構。
  - 參數竄改：風扇轉速、燒烤溫度與除垢對策，需先確認實際結構材質，
    並限制在熱歷程／Recipe 資格化範圍內執行。
"""

_DSCM_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：DSCM Descum／等離子去膠（代表機台 EcoLite 3000）━━━━━━
【優先檢查粒子源】
  Chamber Wall Polymer、Quartz Components（若設備配置）、ESC／Chuck、Lift Pin、
  Gas／RF、Load Lock、Robot 及排氣系統。
  石英件、Seal Ring 或材料清潔應確認設備配置及材料相容性。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Chamber PM Cleaning
  - RF Electrode Cleaning
  - Quartz Ring Replacement
  - Chamber Seasoning
  - Pump Maintenance
  - Leak Check
  - ESC / Chuck Inspection
【禁止（命中即 ⛔違規已替換）】
  - 不宜使用未核准溶劑或過度頻繁人工清潔取代 Plasma Chamber PM／Seasoning 的 OEM BKM。
"""

_THKZ_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：THKZ 3D 光學量測／Step Height（代表機台 Zeta-580）━━━━━━
【優先檢查粒子源】
  FOUP、Load Port、Robot、Sample Stage／Fixture、Optical Enclosure、
  鏡頭外部積塵及局部環境氣流。
  建議利用 Reference Wafer、Repeatability 及搬運分段資料確認污染來源。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Optical System Cleaning
  - Optical Enclosure Cleaning
  - Stage Cleaning
  - Fixture Cleaning
  - FOUP Cleaning
  - Reference Wafer Verification
  - Repeatability Check
  - Particle Trend Monitoring
【禁止（命中即 ⛔違規已替換）】
  - 屬 3D 光學量測設備，非熱處理設備；不宜直接套用爐溫均勻性、加熱區或爐門密封改善措施。
  - 不建議直接對 Stage／Chuck 使用超音波清洗。
"""

_SHTZ_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：SHTZ Ball Shear／機械測試（代表機台 Condor Sigma W12）━━━━━━
【優先檢查粒子源】
  夾具、Probe／Shear Pin、磨耗碎屑、Sample Stage、Auto Loader（若設備配置）及機台罩體。
  新機建議建立首批基線、清潔簽核及量測 Repeatability 作為預防性管制。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Fixture Cleaning
  - Probe / Shear Pin Inspection
  - Wear Debris Removal
  - Sample Stage Cleaning
  - Auto Loader Cleaning
  - Measurement Repeatability Check
【禁止（命中即 ⛔違規已替換）】
  - PI／粒子管制門檻應明確定義量測儀器、粒徑、量測位置及統計窗口，不宜僅引用單一數值。
"""

_STEP_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：STEP i-line 光刻曝光（代表機台 FPA-5520iV LF）━━━━━━
【優先檢查粒子源】
  Reticle／Reticle Pod、Wafer Stage、Robot／Belt、Load Port、Optical Enclosure、
  Purge 系統及 Set-Up 清潔。
  所有清潔應依 Canon 與廠內核准方法執行，並以空片、曝光品質及粒子結果共同驗證。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Reticle Cleaning
  - Reticle Pod Cleaning
  - Optical Enclosure Cleaning
  - Stage Cleaning
  - Robot / Transfer Cleaning
  - Set-Up Super Clean
  - Empty Wafer Particle Verification
  - Exposure Quality Verification
【禁止（命中即 ⛔違規已替換）】
  - 不宜於曝光機內使用超音波清洗、一般 HEPA 抽塵或爐溫均勻性測試。
  - 粒子規格亦不應使用未定義之 ppb 單位。
"""

_SPUT_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：SPUT PVD Sputter（代表機台 SIGMA FXP）━━━━━━
【優先檢查粒子源】
  Target、Shield、Liner Flakes、Chamber PM、Load Lock、Slit Valve、Chamber Seal、
  Lift Pin、Robot 及 FOUP 磨耗碎屑。
  Target Clean、密封件更換及 Chamber 清潔後應完成 Vacuum／Leak Test、Seasoning 及薄膜資格化。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Target Clean
  - Shield Replacement
  - Chamber Wall Cleaning
  - Al Pasting
  - Target Replacement
  - Chamber PM
  - Pump Maintenance
  - Vacuum Leak Check
  - Seasoning Process
【禁止（命中即 ⛔違規已替換）】
  - 不宜將超音波清洗直接用於真空腔體內部。
  - Al Pasting、N₂ Purge、密封件材質及 PM 週期應依 OEM BKM 執行。
"""

_PLAT_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：PLAT 電鍍／Plating（代表機台 Sabre 3D）━━━━━━
【優先檢查粒子源】
  Plating Cell／Cup、Contact Ring、Cathode Contact、Anode Bag、Bath Recirculation Filter、
  化學液、Rinse／Dry、Wafer Carrier 及 FOUP。
  改善後應以槽液粒子、金屬殘留、接觸面品質、產品缺陷及 OOB 結果共同驗證。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Tank Cleaning
  - Plating Cell Cleaning
  - Chemical Filter Replacement
  - Anode Maintenance
  - Pump Maintenance
  - Recirculation Line Cleaning
  - Contact Ring Cleaning / Replacement
  - Bath Particle Monitoring
【禁止（命中即 ⛔違規已替換）】
  - 不宜以泛稱 Chamber PM 或 Chuck Seal Ring 取代 Plating Cell 的 Cup Seal、Contact Ring 或 Filter 管理。
  - HEPA 應經 Airflow 與粒子量測確認後再評估是否為根因。
"""

_SORT_KNOWLEDGE = """\
━━━━━━ 本設備專屬知識：SORT Wafer Sorter／搬運（代表機台 1TRSC152-SB4）━━━━━━
【優先檢查粒子源】
  Mechanical Transmission（Belt／Rail／Gear）、End-Effector、FOUP／Cassette、Load Port、
  磨耗碎屑及罩體氣流。
  建議先完成磨耗檢查與粒子 Mapping，再決定維修或更換範圍。
【常見改善措施參考（範例，非窮舉；可依設備領域知識判斷更適合的措施）】
  - Robot Arm Cleaning
  - End-Effector Cleaning
  - Conveyor / Rail Maintenance
  - Mechanical Wear Inspection
  - FOUP Cleaning
  - Transfer Path Cleaning
  - Particle Mapping
  - Particle Monitor Trend
【禁止（命中即 ⛔違規已替換）】
  - 局部 HEPA 加裝可能改變既有氣流分布，應完成 Airflow Design Review 與驗證後再導入。
"""


# ── 設備知識庫 registry（新增設備類型只需在此增列一筆）──
# 機台 ID 格式固定為 B1_{類型代碼}_NN（如 B1_SCOP_03），類型代碼即為推斷關鍵詞，
# 無需另外維護 hints。每筆值為 (設備類型中文標籤, 該設備專家 Agent 專屬知識 prompt)。
_EQUIPMENT_REGISTRY: dict[str, tuple[str, str]] = {
    "SCOP": ("光學檢查／顯微量測 (V5300 SCOP)", _SCOP_KNOWLEDGE),
    "PRRM": ("光阻去除／濕製程 (UFO-300 PRRM)", _PRRM_KNOWLEDGE),
    "COAT": ("PR/PI 旋塗 (LITHIUS ProAP COAT)", _COAT_KNOWLEDGE),
    "DEVP": ("PR／PI 顯影 (LITHIUS ProAP DEVP)", _DEVP_KNOWLEDGE),
    "AGST": ("AOI／自動晶圓檢查 (ARGUS X3008 / Eagle T-AP)", _AGST_KNOWLEDGE),
    "ETCH": ("UBM 金屬濕蝕刻 (UFO-300 ETCH)", _ETCH_KNOWLEDGE),
    "WFCL": ("單片濕式晶圓清洗 (UFO-300 WFCL)", _WFCL_KNOWLEDGE),
    "OVEN": ("熱處理／烘烤 (SO2-12L-F)", _OVEN_KNOWLEDGE),
    "DSCM": ("Descum／等離子去膠 (EcoLite 3000)", _DSCM_KNOWLEDGE),
    "THKZ": ("3D 光學量測／Step Height (Zeta-580)", _THKZ_KNOWLEDGE),
    "SHTZ": ("Ball Shear／機械測試 (Condor Sigma W12)", _SHTZ_KNOWLEDGE),
    "STEP": ("i-line 光刻曝光 (FPA-5520iV LF)", _STEP_KNOWLEDGE),
    "SPUT": ("PVD Sputter (SIGMA FXP)", _SPUT_KNOWLEDGE),
    "PLAT": ("電鍍／Plating (Sabre 3D)", _PLAT_KNOWLEDGE),
    "SORT": ("Wafer Sorter／搬運 (1TRSC152-SB4)", _SORT_KNOWLEDGE),
}

# 由 registry 衍生：設備類型中文標籤
_EQUIPMENT_LABEL: dict[str, str] = {k: v[0] for k, v in _EQUIPMENT_REGISTRY.items()}
_EQUIPMENT_LABEL["UNKNOWN"] = "未定義設備"


_AGGREGATOR_PROMPT = """\
你是 Decision Review Aggregator（決策審查彙整官）。
你會收到多個設備專家 Reviewer 對各自設備分組的審查結果（皆為 10 欄資料列，
以及各自的 ---SUMMARY--- 區塊）。

你的任務：
1. 將所有專家的「資料列」合併為單一 Markdown 表格。
2. 依「優先序」由高到低排序（P1 在最上，其次 P2、P3...；無編號者排最後）。
3. 不得竄改任何資料列的內容，只做合併與排序。
4. 將各專家的 ---SUMMARY--- 條列合併為一段「審查摘要」。

【輸出格式】— 嚴格遵守，只輸出以下內容，不要程式碼框、不要多餘說明：
## 四、決策指令與資源調度
| 優先序 | 機台 | 設備類型 | 決策指令(審查後) | 審查判定 | 決策原因 | 主責部門 | 協作部門 | 時限 | 預期效果 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
（所有合併後的資料列）

### 審查摘要
（合併後的逐筆修正/駁回條列；若全部 ✅符合，寫「本次所有決策均符合設備知識，無需修正。」）
一律繁體中文。
"""


# ═══════════════════════════════════════════════════════════════════════════
# 二、Agent 建立（模組級單例）
# ═══════════════════════════════════════════════════════════════════════════

_AGENTS_CACHE: dict[str, Agent] | None = None


def _build_model_conf() -> OpenAIResponsesModel:
    http_client = httpx.AsyncClient(proxy=None, trust_env=False)
    openai_client = AsyncOpenAI(
        base_url=API_BASE_URL,
        api_key=API_KEY,
        http_client=http_client,
    )
    return OpenAIResponsesModel(model=API_MODEL, openai_client=openai_client)


def _specialist_instructions(role_knowledge: str, equipment_key: str) -> str:
    label = _EQUIPMENT_LABEL.get(equipment_key, "未定義設備")
    return (
        _REVIEWER_ROLE_HEADER
        + "\n"
        + role_knowledge
        + "\n"
        + _REVIEWER_OUTPUT_SPEC.format(equipment_label=label)
    )


def _get_agents() -> dict[str, Agent]:
    """建立並快取所有 Reviewer / Aggregator Agent（設備專家由 registry 自動生成）。"""
    global _AGENTS_CACHE
    if _AGENTS_CACHE is not None:
        return _AGENTS_CACHE

    model_conf = _build_model_conf()
    agents: dict[str, Agent] = {}
    for eq_type, (_label, knowledge) in _EQUIPMENT_REGISTRY.items():
        agents[eq_type] = Agent(
            name=f"{eq_type}_Reviewer",
            instructions=_specialist_instructions(knowledge, eq_type),
            model=model_conf,
        )
    agents["UNKNOWN"] = Agent(
        name="Unknown_Handler",
        instructions=_specialist_instructions(_UNKNOWN_KNOWLEDGE, "UNKNOWN"),
        model=model_conf,
    )
    agents["AGGREGATOR"] = Agent(
        name="Review_Aggregator",
        instructions=_AGGREGATOR_PROMPT,
        model=model_conf,
    )
    _AGENTS_CACHE = agents
    return agents


# ═══════════════════════════════════════════════════════════════════════════
# 三、## 四 區塊抽取 / 回填
# ═══════════════════════════════════════════════════════════════════════════

# 抓取「## 四...」到下一個「## 五...」之前（或文末）的整段
_SECTION_FOUR_RE = re.compile(
    r"(##\s*四[、.\s].*?)(?=\n##\s*五[、.\s]|\Z)",
    re.DOTALL,
)


def extract_section_four(report: str) -> Optional[str]:
    """從完整報告中抽出『## 四、決策指令與資源調度』整段（含標題）。找不到回傳 None。"""
    if not report:
        return None
    m = _SECTION_FOUR_RE.search(report)
    return m.group(1).strip() if m else None


def backfill_section_four(report: str, new_section_four: str) -> str:
    """以新的 ## 四 內容取代原報告的 ## 四 區塊；找不到則原樣回傳。"""
    if not report:
        return new_section_four
    if not _SECTION_FOUR_RE.search(report):
        return report

    def _sub(_m: re.Match) -> str:
        return new_section_four.strip() + "\n"

    return _SECTION_FOUR_RE.sub(_sub, report, count=1)


# ═══════════════════════════════════════════════════════════════════════════
# 四、Router：解析 ## 四 表格 + 依設備類型分組
# ═══════════════════════════════════════════════════════════════════════════

# 機台 ID（B1_SCOP_06 / EQ01 兩種格式）
_MACHINE_ID_RE = re.compile(r"(?:[A-Z]\d_[A-Z0-9]+_\d{1,3}|EQ\d{2,4})", re.IGNORECASE)


def infer_equipment_type(text: str) -> str:
    """依機台 ID 推斷設備類型（機台格式固定 B1_{類型}_NN，類型代碼即關鍵詞）。
    回傳類型代碼（SCOP/PRRM/...）或 UNKNOWN。"""
    upper = (text or "").upper()
    for eq_type in _EQUIPMENT_REGISTRY:
        if eq_type in upper:
            return eq_type
    return "UNKNOWN"


def _split_table_row(line: str) -> list[str]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def _is_separator_row(line: str) -> bool:
    stripped = line.strip().strip("|")
    return bool(stripped) and set(stripped) <= set("-: |")


def parse_section_four_rows(section_four: str) -> list[dict[str, str]]:
    """解析 ## 四 表格資料列。回傳每列 dict（含 raw_cells 與推斷的 machine/eq_type）。"""
    rows: list[dict[str, str]] = []
    for line in (section_four or "").splitlines():
        if "|" not in line:
            continue
        if _is_separator_row(line):
            continue
        cells = _split_table_row(line)
        if len(cells) < 2:
            continue
        joined = " ".join(cells)
        # 跳過表頭列
        if "優先序" in joined and "機台" in joined:
            continue
        m = _MACHINE_ID_RE.search(joined)
        machine = m.group(0) if m else ""
        if not machine:
            continue
        rows.append(
            {
                "raw_cells": cells,
                "machine": machine,
                "eq_type": infer_equipment_type(machine),
            }
        )
    return rows


def group_rows_by_type(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """依設備類型分組，維持原順序。"""
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(row["eq_type"], []).append(row)
    return groups


def _rows_to_markdown(rows: list[dict[str, str]]) -> str:
    """將一組原始資料列重建為 8 欄 Markdown 表格（含表頭），餵給專家 Agent。"""
    header = "| 優先序 | 機台 | 決策指令 | 決策原因 | 主責部門 | 協作部門 | 時限 | 預期效果 |"
    sep = "| --- | --- | --- | --- | --- | --- | --- | --- |"
    body_lines = ["| " + " | ".join(r["raw_cells"]) + " |" for r in rows]
    return "\n".join([header, sep, *body_lines])


# ═══════════════════════════════════════════════════════════════════════════
# 五、Aggregator fallback：Python 確定性組裝
# ═══════════════════════════════════════════════════════════════════════════

_SUMMARY_MARK = "---SUMMARY---"
_PRIORITY_RE = re.compile(r"P(\d+)", re.IGNORECASE)


def _priority_key(cells: list[str]) -> tuple[int, int]:
    m = _PRIORITY_RE.search(cells[0]) if cells else None
    return (0, int(m.group(1))) if m else (1, 999)


def _extract_data_and_summary(specialist_output: str) -> tuple[list[list[str]], list[str]]:
    """從專家輸出中拆出 10 欄資料列與 summary 條列。"""
    data_part, _, summary_part = specialist_output.partition(_SUMMARY_MARK)
    data_rows: list[list[str]] = []
    for line in data_part.splitlines():
        if "|" not in line or _is_separator_row(line):
            continue
        cells = _split_table_row(line)
        joined = " ".join(cells)
        if "優先序" in joined and "機台" in joined:
            continue
        if len(cells) >= 10:
            data_rows.append(cells[:10])
    summary_lines = [
        ln.strip()
        for ln in summary_part.splitlines()
        if ln.strip().startswith("-")
    ]
    return data_rows, summary_lines


def _assemble_final_section_four(
    specialist_outputs: list[str],
) -> str:
    """Python 確定性組裝：合併所有專家資料列 + 排序 + 審查摘要。"""
    all_rows: list[list[str]] = []
    all_summaries: list[str] = []
    for out in specialist_outputs:
        rows, summaries = _extract_data_and_summary(out)
        all_rows.extend(rows)
        all_summaries.extend(summaries)

    all_rows.sort(key=_priority_key)

    header = (
        "| 優先序 | 機台 | 設備類型 | 決策指令(審查後) | 審查判定 "
        "| 決策原因 | 主責部門 | 協作部門 | 時限 | 預期效果 |"
    )
    sep = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    body = ["| " + " | ".join(r) + " |" for r in all_rows]

    lines = ["## 四、決策指令與資源調度", "", header, sep, *body, "", "### 審查摘要"]
    if all_summaries:
        lines.extend(all_summaries)
    else:
        lines.append("本次所有決策均符合設備知識，無需修正。")
    return "\n".join(lines)


def _looks_like_valid_section_four(text: str) -> bool:
    """檢查 Aggregator 輸出是否為合法 10 欄 ## 四 表格。"""
    if not text or "## 四" not in text:
        return False
    for line in text.splitlines():
        if "|" in line and not _is_separator_row(line):
            cells = _split_table_row(line)
            if "設備類型" in " ".join(cells) and "審查判定" in " ".join(cells):
                return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# 六、主流程：run_review_swarm
# ═══════════════════════════════════════════════════════════════════════════

# 專家 Agent 執行順序（決定分組批次順序，僅影響 trace 呈現）
# 由 registry 順序衍生，UNKNOWN 排最後。
_SPECIALIST_ORDER = tuple(_EQUIPMENT_REGISTRY.keys()) + ("UNKNOWN",)


async def _run_specialist(
    eq_type: str,
    rows: list[dict[str, str]],
    stages: list[dict[str, Any]],
) -> str:
    agents = _get_agents()
    agent = agents[eq_type]
    sub_table = _rows_to_markdown(rows)
    prompt = (
        f"以下是 {_EQUIPMENT_LABEL.get(eq_type, eq_type)} 設備分組的決策指令，"
        f"共 {len(rows)} 台機台，請逐台審查並依規定格式輸出：\n\n{sub_table}"
    )
    started = time.perf_counter()
    stages.append(
        {
            "ts": datetime.now().isoformat(),
            "stage": "specialist_start",
            "agent": agent.name,
            "eq_type": eq_type,
            "machine_count": len(rows),
            "machines": [r["machine"] for r in rows],
        }
    )
    result = await Runner.run(agent, prompt)
    output = str(getattr(result, "final_output", "") or "")
    stages.append(
        {
            "ts": datetime.now().isoformat(),
            "stage": "specialist_done",
            "agent": agent.name,
            "eq_type": eq_type,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "output_preview": output[:1500],
        }
    )
    return output


async def run_review_swarm(
    section_four: str,
) -> tuple[str, list[dict[str, Any]]]:
    """
    對 ## 四 表格執行同類型分組批次審查。
    回傳 (final_section_four_markdown, stages)。
    stages 為結構化追蹤事件，供上層寫入 trace_ctx。
    """
    stages: list[dict[str, Any]] = []
    overall_started = time.perf_counter()

    # ── Router（確定性分流）──
    rows = parse_section_four_rows(section_four)
    groups = group_rows_by_type(rows)
    stages.append(
        {
            "ts": datetime.now().isoformat(),
            "stage": "router_done",
            "total_rows": len(rows),
            "groups": {k: [r["machine"] for r in v] for k, v in groups.items()},
        }
    )

    if not rows:
        stages.append(
            {
                "ts": datetime.now().isoformat(),
                "stage": "no_decision_rows",
                "note": "## 四 未解析到任何機台決策列，原樣回傳。",
            }
        )
        return section_four, stages

    # ── 設備專家群（平行批次）──
    ordered_types = [t for t in _SPECIALIST_ORDER if t in groups]
    specialist_coros = [
        _run_specialist(eq_type, groups[eq_type], stages) for eq_type in ordered_types
    ]
    specialist_outputs = await asyncio.gather(*specialist_coros)

    # ── Aggregator（Agent 合併，失敗則 Python fallback）──
    fallback_section_four = _assemble_final_section_four(list(specialist_outputs))
    final_section_four = fallback_section_four
    agg_started = time.perf_counter()
    stages.append(
        {
            "ts": datetime.now().isoformat(),
            "stage": "aggregator_start",
            "agent": "Review_Aggregator",
            "specialist_count": len(specialist_outputs),
        }
    )
    try:
        agents = _get_agents()
        agg_input_blocks = []
        for eq_type, out in zip(ordered_types, specialist_outputs):
            agg_input_blocks.append(f"【{eq_type} 專家審查結果】\n{out}")
        agg_prompt = (
            "請合併以下各設備專家的審查結果為最終 Final Action Plan：\n\n"
            + "\n\n".join(agg_input_blocks)
        )
        agg_result = await Runner.run(agents["AGGREGATOR"], agg_prompt)
        agg_output = str(getattr(agg_result, "final_output", "") or "").strip()
        used_fallback = not _looks_like_valid_section_four(agg_output)
        if not used_fallback:
            final_section_four = agg_output
        stages.append(
            {
                "ts": datetime.now().isoformat(),
                "stage": "aggregator_done",
                "agent": "Review_Aggregator",
                "elapsed_ms": round((time.perf_counter() - agg_started) * 1000, 2),
                "used_fallback": used_fallback,
                "output_preview": final_section_four[:1500],
            }
        )
    except Exception as e:  # noqa: BLE001
        stages.append(
            {
                "ts": datetime.now().isoformat(),
                "stage": "aggregator_error",
                "error": str(e),
                "note": "改用 Python 確定性組裝結果。",
            }
        )
        final_section_four = fallback_section_four

    stages.append(
        {
            "ts": datetime.now().isoformat(),
            "stage": "review_done",
            "total_elapsed_ms": round((time.perf_counter() - overall_started) * 1000, 2),
        }
    )
    return final_section_four, stages


async def review_report(report: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """
    對完整 Dir Agent 報告執行審查：抽出 ## 四 → 分組批次審查 → 回填。
    回傳 (reviewed_report, stages, meta)。
    meta 含是否找到 ## 四、機台數等摘要。
    """
    section_four = extract_section_four(report)
    if not section_four:
        stages = [
            {
                "ts": datetime.now().isoformat(),
                "stage": "section_four_not_found",
                "note": "報告中找不到 ## 四 區塊，原樣回傳。",
            }
        ]
        return report, stages, {"section_four_found": False, "machine_count": 0}

    final_section_four, stages = await run_review_swarm(section_four)
    reviewed_report = backfill_section_four(report, final_section_four)
    rows = parse_section_four_rows(section_four)
    meta = {
        "section_four_found": True,
        "machine_count": len(rows),
        "equipment_groups": {
            k: len(v) for k, v in group_rows_by_type(rows).items()
        },
    }
    return reviewed_report, stages, meta
