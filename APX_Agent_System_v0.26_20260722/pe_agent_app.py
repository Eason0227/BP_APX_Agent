import os
import json
import asyncio
from datetime import datetime, timedelta
from functools import lru_cache
import httpx
import pandas as pd
import requests
import streamlit as st
import nest_asyncio
from agents import Agent, Runner, function_tool, OpenAIResponsesModel, set_tracing_disabled, AsyncOpenAI
import requests
session = requests.Session()
session.trust_env = False  # 忽略環境變數（包含 proxy）

# Langfuse 可觀測性（Langfuse 3.x）
LANGFUSE_AVAILABLE = False
_lf_observe        = None
try:
    from langfuse import Langfuse, observe as _lf_observe
    from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI
    LANGFUSE_AVAILABLE = True
except ImportError:
    pass

# 解決 Streamlit 與 asyncio 的嵌套迴圈問題
try:
    nest_asyncio.apply()
except Exception:
    pass

# ── 全域設定 ──
API_BASE_URL  = "http://10.11.33.5:9120/v1"
API_MODEL     = "/model/gpt-oss-120b"

LOT_DATA_XLSX = (
    r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\PE_Agent"
    r"\lot_documents_structured_(Security C).xlsx"
)
WEEKLY_YIELD_XLSX = (
    r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\PE_Agent"
    r"\weekly_yield_report_(Security C).xlsx"
)

os.environ.setdefault("LANGFUSE_HOST",       "http://10.10.19.36:3000")
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "pk-lf-491f2a46-7264-4efd-ba00-39a9310d01e3")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "sk-lf-40c95f0d-1601-4fa0-9769-ab6190a9b892")

set_tracing_disabled(True)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PAGE_ICON_PATH = os.path.join(_BASE_DIR, "assets", "APX_Agent_icon.png")

# ── 本地 Excel 資料載入 ──

def _percent_to_decimal(value) -> float:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        has_percent = text.endswith("%")
        text = text[:-1].strip() if has_percent else text
        try:
            num = float(text)
        except ValueError:
            return 0.0
        return num / 100.0 if has_percent else (num / 100.0 if num > 1 else num)
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return num / 100.0 if num > 1 else num


@lru_cache(maxsize=1)
def _load_lot_dataframe() -> pd.DataFrame:
    df = pd.read_excel(LOT_DATA_XLSX, engine="openpyxl")
    df = df.rename(
        columns={
            "Lot ID": "lot_id",
            "檢驗日期": "inspection_date",
            "良率損耗": "particle_yield_loss",
            "Die 損失數量(Particle)": "particle_loss_qty",
            "Die 總生產量": "die_total_qty",
            "PE 經驗初步判定嫌疑區": "pe_suspect_area",
            "製程詳細軌跡與異常評估": "page_content",
        }
    )
    required_cols = [
        "lot_id", "inspection_date", "particle_yield_loss",
        "particle_loss_qty", "pe_suspect_area", "page_content",
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Lot Excel 欄位缺失: {', '.join(missing)}")
    df["inspection_date"] = pd.to_datetime(df["inspection_date"], errors="coerce")
    df["particle_yield_loss"] = df["particle_yield_loss"].apply(_percent_to_decimal)
    df["particle_loss_qty"] = pd.to_numeric(df["particle_loss_qty"], errors="coerce").fillna(0)
    df["lot_id"] = df["lot_id"].fillna("N/A").astype(str)
    df["pe_suspect_area"] = df["pe_suspect_area"].fillna("").astype(str)
    df["page_content"] = df["page_content"].fillna("").astype(str)
    return df


def _excel_scroll_yield_loss(
    min_yield_loss: float,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> list:
    df = _load_lot_dataframe().copy()
    if start_date:
        sdt = pd.to_datetime(start_date, errors="coerce")
        if not pd.isna(sdt):
            df = df[df["inspection_date"] >= sdt]
    if end_date:
        edt = pd.to_datetime(end_date, errors="coerce")
        if not pd.isna(edt):
            df = df[df["inspection_date"] <= edt + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)]
    df = df[df["particle_yield_loss"] >= min_yield_loss]
    df = df.sort_values("particle_yield_loss", ascending=False).head(limit)
    rows = []
    for _, r in df.iterrows():
        insp = "N/A"
        if not pd.isna(r["inspection_date"]):
            insp = r["inspection_date"].strftime("%Y-%m-%d %H:%M:%S")
        rows.append({
            "lot_id": r["lot_id"],
            "particle_yield_loss": float(r["particle_yield_loss"]),
            "particle_loss_qty": int(r["particle_loss_qty"]),
            "inspection_date": insp,
            "pe_suspect_area": r["pe_suspect_area"],
            "page_content": r["page_content"],
        })
    return rows


@lru_cache(maxsize=1)
def _load_weekly_yield_dataframe() -> pd.DataFrame:
    df = pd.read_excel(WEEKLY_YIELD_XLSX, dtype=str, engine="openpyxl").fillna("")
    required_cols = [
        "年碼", "週次", "總產量(Die)", "總Lot數",
        "平均總良率", "平均Particle Yield", "Particle損失總棵數",
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"週報 Excel 欄位缺失: {', '.join(missing)}")
    df["_year"] = pd.to_numeric(df["年碼"], errors="coerce")
    df["_week_num"] = pd.to_numeric(
        df["週次"].str.upper().str.replace("W", "", regex=False), errors="coerce"
    )
    return df


def _get_amd_yield_summary_by_week(start_date: str, end_date: str) -> str | None:
    df = _load_weekly_yield_dataframe()
    end_ts = pd.to_datetime(end_date, errors="coerce")
    start_ts = pd.to_datetime(start_date, errors="coerce")
    if pd.isna(end_ts) or pd.isna(start_ts):
        return None
    end_dt = end_ts.date()
    iso = end_dt.isocalendar()
    target_year, target_week = iso[0], iso[1]
    week_code = f"W{target_week:02d}"
    exact = df[(df["_year"] == target_year) & (df["週次"].str.upper() == week_code)]
    if exact.empty:
        fallback = df[df["_year"] == target_year]
        if fallback.empty:
            fallback = df
        fallback = fallback.sort_values(["_year", "_week_num"]).tail(1)
        if fallback.empty:
            return None
        row = fallback.iloc[0]
    else:
        row = exact.iloc[0]
    start_dt = start_ts.date()
    return (
        f"查詢區間: {start_dt.isoformat()} ~ {end_dt.isoformat()}\n"
        f"對應 AMD 週報: {row['年碼']} {row['週次']}\n"
        f"總產量(Die): {row['總產量(Die)']}\n"
        f"總Lot數: {row['總Lot數']}\n"
        f"平均總良率: {row['平均總良率']}\n"
        f"平均Particle Yield: {row['平均Particle Yield']}\n"
        f"Particle損失總棵數: {row['Particle損失總棵數']}"
    )


# ── PE Agent 專用工具 ──

@function_tool
def search_lot_by_id(lot_id: str) -> str:
    """
    以 Lot ID 精確查詢該批貨的完整生產軌跡與異常摘要。
    適用場景：使用者指定了明確的批號，例如 "幫我查 51BW3KB001_RDL2_AEI 這批貨"。

    參數：
    - lot_id : 批號，例如 "51BW3KB001_RDL2_AEI"
    """
    try:
        lot_id = lot_id.strip()
        if not lot_id:
            return "請提供有效的 Lot ID。"
        df = _load_lot_dataframe()
        matched = df[df["lot_id"].str.upper() == lot_id.upper()]
        if matched.empty:
            matched = df[df["lot_id"].str.contains(lot_id, case=False, na=False)]
        if matched.empty:
            return f"未找到 Lot ID 為「{lot_id}」的資料，請確認批號是否正確。"
        lines = []
        for _, r in matched.head(5).iterrows():
            insp = "N/A"
            if not pd.isna(r["inspection_date"]):
                insp = r["inspection_date"].strftime("%Y-%m-%d %H:%M:%S")
            lines.append(
                f"Lot ID: {r['lot_id']}\n"
                f"檢驗日期: {insp}\n"
                f"Particle Yield Loss: {r['particle_yield_loss']:.4%}\n"
                f"Particle Loss Qty: {int(r['particle_loss_qty'])}\n"
                f"PE 嫌疑區: {r['pe_suspect_area']}\n"
                f"製程詳細軌跡與異常評估:\n{r['page_content']}"
            )
        return "\n\n---\n\n".join(lines)
    except Exception as e:
        return f"查詢 Lot 時發生錯誤：{str(e)}"


@function_tool
def search_lots_by_yield_loss(
    min_yield_loss: float = 0.01,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 10,
) -> str:
    """
    列出 particle_yield_loss >= 指定門檻的 Lot 清單，如果使用者有給定期限（例如本週），可帶入 start_date / end_date 過濾。
    用於找出高良率損耗批次或產生週報的基礎資料。

    參數：
    - min_yield_loss : 良率損耗門檻（小數），預設 0.01（即 1%）
    - start_date     : 起始日期，例如 "2026-02-13" (YYYY-MM-DD)，可留空
    - end_date       : 結束日期，例如 "2026-02-26" (YYYY-MM-DD)，可留空
    - limit          : 最多回傳筆數，預設 10
    """
    try:
        points = _excel_scroll_yield_loss(
            min_yield_loss=min_yield_loss,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
        if not points:
            date_msg = f" 介於 {start_date}~{end_date}" if start_date or end_date else ""
            return f"未找到 particle_yield_loss >= {min_yield_loss:.4f}{date_msg} 的 Lot。"

        points.sort(key=lambda p: p.get("particle_yield_loss", 0), reverse=True)

        lines = []
        for p in points:
            lid    = p.get("lot_id", "N/A")
            yloss  = p.get("particle_yield_loss", 0)
            ploss  = p.get("particle_loss_qty", "N/A")
            insp   = p.get("inspection_date", "N/A")
            content    = p.get("page_content", "")
            pe_suspect = p.get("pe_suspect_area", "")
            lines.append(
                f"- **{lid}** | Yield Loss: {yloss:.4%} | Particle Loss Qty: {ploss} | 檢驗日期: {insp}\n"
                f"  PE 嫌疑區: {pe_suspect}\n"
                f"  [軌跡節錄]:\n{content[:500]}...\n"
            )

        header = f"共找到 {len(points)} 批 particle_yield_loss >= {min_yield_loss:.4%} 的 Lot：\n\n"
        return header + "\n---\n".join(lines)
    except Exception as e:
        return f"查詢高損耗 Lot 時發生錯誤：{str(e)}"


@function_tool
def search_lot_semantic(query: str, top_k: int = 5) -> str:
    """
    關鍵字搜尋 Lot 生產軌跡與異常資訊，用於查找相關歷史案例。
    適用場景：使用者問 "最近有沒有因為 Sputter 機台異常導致的 Lot" 或 "哪些批次經過 B1_ETCH_04"。

    參數：
    - query : 搜尋關鍵字，例如 "Sputter 機台異常" 或 "B1_WFCL_02 OOB 異常"
    - top_k : 回傳最多筆數，預設 5
    """
    try:
        df = _load_lot_dataframe().copy()
        keywords = [kw.strip() for kw in query.split() if kw.strip()]
        if not keywords:
            return "請提供有效的搜尋關鍵字。"

        def _match(row):
            text = f"{row['page_content']} {row['pe_suspect_area']} {row['lot_id']}"
            return any(kw.lower() in text.lower() for kw in keywords)

        matched = df[df.apply(_match, axis=1)].head(top_k)
        if matched.empty:
            return f"未找到與「{query}」相關的 Lot 資料。"

        lines = []
        for _, r in matched.iterrows():
            insp = "N/A"
            if not pd.isna(r["inspection_date"]):
                insp = r["inspection_date"].strftime("%Y-%m-%d")
            lines.append(
                f"**{r['lot_id']}** | Yield Loss: {r['particle_yield_loss']:.4%} | "
                f"Particle Loss Qty: {int(r['particle_loss_qty'])} | 檢驗日期: {insp}\n"
                f"PE 嫌疑區: {r['pe_suspect_area']}\n"
                f"[軌跡節錄]:\n{r['page_content'][:500]}..."
            )
        return f"共找到 {len(lines)} 筆相關 Lot：\n\n" + "\n\n---\n\n".join(lines)
    except Exception as e:
        return f"搜尋 Lot 時發生錯誤：{str(e)}"


@function_tool
def get_amd_yield_summary_by_week(start_date: str, end_date: str) -> str:
    """依據查詢區間回傳對應 AMD 週報摘要（來源: weekly_yield_report Excel）。"""
    try:
        summary = _get_amd_yield_summary_by_week(start_date=start_date, end_date=end_date)
        if not summary:
            return f"查無 {start_date} ~ {end_date} 對應的 AMD 週報資料。"
        return summary
    except Exception as e:
        return f"取得 AMD 週報摘要時發生錯誤：{str(e)}"


# ── 初始化 PE Agent ──

PE_SYSTEM_PROMPT = """\
# Role
你是一位資深的半導體製程工程師（Senior Process Engineer Agent）。
你的專長是透過分析 Lot 的生產軌跡、機台狀態（特別是 Particle sensor OOB Rate），找出導致 Particle Yield Loss 的根本原因（Root Cause），並驗證 PE 初步懷疑的嫌疑站區。
你同時負責單一異常批次的 Root Cause 分析，以及【每週製程與機台健康度總結】。

# Input Data Context
你將接收到來自本地 Excel 檔案的 Lot 摘要資料，包含：
1. **Metadata**: 包含批號 (lot_id)、良率損耗 (particle_yield_loss)、Die 損失數量 (particle_loss_qty)、檢驗日期 (inspection_date)、PE 初判嫌疑區 (pe_suspect_area) 等。
2. **Page Content**: 包含製程詳細軌跡與異常評估內容，記錄經過的機台及相關異常資訊。

# Workflow (你的思考邏輯)
**【情境 A：單一 Lot 分析】**
1. 檢視影像程度：確認該 Lot 的良率損耗比率與 Die 損失數量。
2. 軌跡盤點：列出所有經過的站點，特別標註「狀態為異常」且「OOB Rate 高於 0%」的機台。
3. 關聯性分析：比較高 OOB 機台與「PE 經驗初步判定嫌疑區」。如果高度吻合，請支持該論點；發現其他高風險機台請提獨立見解。
4. 時間序排查：注意機台進出時間，判斷是單一事件還是連續污染。
格式：【Lot 摘要】、【異常站點列表(按 OOB 排序)】、【Agent 分析結論】、【Action Items for EE】。
其中【Action Items for EE】必須使用表格，欄位固定為：
| 優先順序 | 機台名稱 | 處置項目（立即保養） |
優先順序請用 P1 / P2 / P3（P1 最高），且每筆處置項目需具體可執行。
每列「處置項目（立即保養）」都必須以「立即保養：」開頭，並以單行文字描述可直接開工單的保養作業。
禁止使用編號清單（例如 、1.）、換行拆點或 HTML `<br>`。
【嚴格限制】Action Items 僅能包含「機台保養」類作業，不可包含監控、抽樣、校正、參數門檻調整。
禁止出現以下類型（含同義詞）：
- Sensor 校正、感測器自檢與校正、影像參數重新校正、Thickness Sensor 校正
- 增設即時 OOB 監控點
- 每批前取樣 OOB 檢測、每 N 片抽樣 OOB 檢測報告
- 設定或調降 OOB 警示門檻
建議優先使用保養作業：腔體清潔、管路/排氣清潔、濾網/濾芯/耗材更換、密封件檢查與更換、治具清潔保養、保養週期加嚴、點檢項目加開。

**【情境 B：週報產生 (Weekly Report)】**
使用者要求產出週報時，結合多批次數據與 PE 工程經驗（例如圖形特徵）：
1. 本週災情盤點：統整本週高 Yield Loss 的批次數量、整體損失情形。
2. 觀點交叉比對 (Crucial)：
   - EE 視角 (Data)：列出本週 OOB Rate > 0% 機台中發報最高、損耗最大的。
   - PE 視角 (Domain)：依據使用者提供的圖形特徵或經驗區，找出的嫌疑站區。
3. 差異分析與對焦：
   - 【雙方共識 (高風險)】：OOB 高，且與 PE 特徵經驗高度吻合的機台。
   - 【PE 經驗懷疑 (隱患)】：OOB 沒叫或偏低，但 Particle 特徵極度吻合該站點的機台。
   - 【EE 預警 (觀察期)】：單純 OOB 發報高，但尚未造成重大影響。
4. 輸出格式必須依照：

報告的第一行必須使用 ### 標題：
### {year}-W{week} 最新一周整體良率狀態彙報
（以實際週次取代 {year}-W{week}）

各章節標題必須使用 #### 層級，格式為 #### N) 章節名稱：
#### 1) 本週 Particle 良率總覽
(簡述)
#### 2) 異常機台關注清單 (EE vs PE 觀點)
(條列：雙方共識、PE經驗懷疑、EE預警)
#### 3) Action Items for EE (下週派工重點)
(必須用表格列出：優先順序、機台名稱、處置項目（立即保養）；處置項目以機台保養為主)

Action Items 表格格式固定如下：
| 優先順序 | 機台名稱 | 處置項目（立即保養） |
|---|---|---|
| P1 | <機台名稱> | 立即保養：<可直接執行的保養作業與目的> |

嚴禁使用 # 或 ## 層級標題，所有標題只能用 ### 或 ####。

Action Items 請再次自我檢查：若含有「校正、監控、抽樣、門檻設定」字眼，必須改寫為保養處置後再輸出。

# 重要：無異常 Lot 時的處理規則

當 search_lots_by_yield_loss 工具回傳「未找到」或查無任何 Lot 時：
- 第 1) 節必須如實報告「本週查無 particle yield loss 超過門檻的 Lot」。
- 若 User 提供了「AMD 實際良率摘要」，則引用該摘要中的真實數值（Particle Yield、1P1M Yield、Die Qty）。
- 嚴禁自行編造良率數字、產線名稱（如 FE-12、PE-07 等）或機台名稱。
- 第 2) 節直接寫「本週無異常機台需關注」。
- 第 3) 節 Action Items 表格直接寫「本週無需額外派工，建議維持例行保養排程」，不得編造機台名稱。

# 工具使用指引
1. 使用者提供明確 Lot ID → 使用 `search_lot_by_id`。
2. 問週報、給定日期區間門檻篩選 → 使用 `search_lots_by_yield_loss` (帶入 start_date, end_date 以取得該週摘要)。
3. 需要 AMD 整週良率摘要 → 使用 `get_amd_yield_summary_by_week(start_date, end_date)` 取得真實良率數值。
4. 描述模糊情境或機台/站點關鍵字（例如「Sputter 異常」、「B1_ETCH_04」）→ 使用 `search_lot_semantic` 關鍵字搜尋歷史紀錄。

一律以繁體中文回答，需包含具體數字（Yield Loss、Particle Loss Qty 等）。
"""


def create_pe_agent(use_langfuse: bool = False) -> Agent:
    """建立並回傳 PE Agent 實例。"""
    http_client = httpx.AsyncClient(proxy=None, trust_env=False)
    if use_langfuse and LANGFUSE_AVAILABLE:
        openai_client = LangfuseAsyncOpenAI(
            base_url=API_BASE_URL,
            api_key="EMPTY",
            http_client=http_client,
        )
    else:
        openai_client = AsyncOpenAI(
            base_url=API_BASE_URL,
            api_key="EMPTY",
            http_client=http_client,
        )
    model_conf = OpenAIResponsesModel(
        model=API_MODEL,
        openai_client=openai_client,
    )

    pe_agent = Agent(
        name="PE_Agent",
        instructions=PE_SYSTEM_PROMPT,
        model=model_conf,
        tools=[
            search_lot_by_id,
            search_lots_by_yield_loss,
            search_lot_semantic,
            get_amd_yield_summary_by_week,
        ],
    )
    return pe_agent


# ── 週報查詢建構（與 API 版一致） ──

def _default_week_range() -> tuple[str, str]:
    """回傳近一週的起迄日期 (start, end)。"""
    today = datetime.today().date()
    start = today - timedelta(days=7)
    return start.isoformat(), today.isoformat()


def _build_weekly_query(
    start_date: str | None = None,
    end_date: str | None = None,
    min_yield_loss: float = 0.01,
    limit: int = 30,
    pe_note: str | None = None,
    yield_summary: str | None = None,
) -> str:
    """組裝與 API 版 _build_weekly_query 完全一致的結構化 prompt。"""
    if not start_date or not end_date:
        d_start, d_end = _default_week_range()
        start_date = start_date or d_start
        end_date = end_date or d_end

    note = pe_note or "PE 未提供額外備註"
    yield_info = ""
    if yield_summary:
        yield_info = (
            f"\n\n以下是 AMD 實際良率摘要（來自產線真實數據，必須優先引用這些數值，嚴禁自行編造）：\n"
            f"{yield_summary}\n"
        )
    return (
        f"請產出 {start_date} 到 {end_date} 的 Particle 良率週報。"
        f"請先用 search_lots_by_yield_loss(min_yield_loss={min_yield_loss}, "
        f"start_date='{start_date}', end_date='{end_date}', limit={limit}) 取得資料。"
        f"PE 補充觀察：{note}。"
        f"{yield_info}"        "請一律使用 get_amd_yield_summary_by_week(start_date, end_date) 取得 AMD 實際良率摘要。"        "請依照固定章節輸出，並在 Action Items 僅提供可直接派工的機台保養作業。"
        "若 search_lots_by_yield_loss 查無任何 Lot，請嚴格遵守『無異常 Lot 時的處理規則』，禁止編造數字或機台名稱。"
        "報告的第一行必須使用 ### 標題：### {year}-W{week} 最新一周整體良率狀態彙報（以實際週次取代 {year}-W{week}），"
        "各章節標題必須使用 #### 層級。"
    )


# ── Streamlit UI ──

st.set_page_config(
    page_title="APX PE Agent",
    layout="wide",
    page_icon=_PAGE_ICON_PATH if os.path.exists(_PAGE_ICON_PATH) else None,
)

st.markdown("""
<style>
    .main-header {
        font-size: 2rem; font-weight: bold; color: #1a5276;
        text-align: center; margin-bottom: 1.2rem;
    }
    .info-chip {
        display: inline-block; border-radius: 12px;
        padding: 2px 10px; font-size: 0.82rem; margin: 2px;
    }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">APX PE Agent</div>', unsafe_allow_html=True)
st.caption("代理製程工程師的 AI Agent Assistant — 負責Particle Root Cause 分析")

# 初始化 Session State
if "pe_agent" not in st.session_state:
    with st.spinner("初始化 PE Agent ..."):
        st.session_state.pe_agent = create_pe_agent(use_langfuse=LANGFUSE_AVAILABLE)

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

if "preset_query" not in st.session_state:
    st.session_state.preset_query = ""

# 側邊欄
with st.sidebar:
    st.header("快速查詢")
    if st.button("產生最新一週良率會診報告 (EE vs PE)", use_container_width=True, type="primary"):
        st.session_state.preset_query = _build_weekly_query()
    preset_map = {
        "查詢指定 Lot":            "幫我查 51BW3KB001_RDL2_AEI 這批貨的 Particle 異常分析",
        "高 Yield Loss 批次":      "列出 particle_yield_loss 大於 0.04 的所有 Lot，依損耗排序",
        "Sputter 異常相關 Lot":    "最近有沒有哪些 Lot 是因為 Sputter 機台異常導致 Particle 問題的？",
        "B1_ETCH_04 相關批次":     "哪些批次經過 B1_ETCH_04 且該機台 OOB Rate 異常？",
    }
    for label, query in preset_map.items():
        if st.button(label, use_container_width=True):
            st.session_state.preset_query = query

    st.divider()

    with st.expander("可用工具清單"):
        tools_info = [
            ("search_lot_by_id",           "以 Lot ID 精確查詢生產軌跡與異常摘要"),
            ("search_lots_by_yield_loss",  "依 Yield Loss 門檻列出高損耗批次"),
            ("search_lot_semantic",        "關鍵字搜尋相關歷史 Lot 案例"),
            ("get_amd_yield_summary_by_week", "取得對應週次的 AMD 良率摘要"),
        ]
        for name, desc in tools_info:
            st.markdown(f"- `{name}` — {desc}")

    st.divider()
    if st.button("清空對話記錄", use_container_width=True):
        st.session_state.chat_history = []
        st.rerun()

# 聊天歷史顯示
for msg in st.session_state.chat_history:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.write(msg["content"])
    else:
        with st.chat_message("assistant", avatar="🔬"):
            st.markdown(msg["content"])

# 輸入處理
user_input = st.chat_input(
    "詢問 PE Agent（例如：幫我查 51BW3KB001_RDL2_AEI 的 Particle 異常原因）"
)

if st.session_state.preset_query:
    user_input = st.session_state.preset_query
    st.session_state.preset_query = ""

# Agent 執行
if user_input:
    st.session_state.chat_history.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.write(user_input)

    with st.chat_message("assistant", avatar="🔬"):
        response_placeholder = st.empty()
        with st.spinner("PE Agent 分析中 ..."):
            try:
                _lf_client = None
                if st.session_state.get("langfuse_on") and LANGFUSE_AVAILABLE:
                    _lf_client = Langfuse()

                async def _run_agent(query: str) -> str:
                    result = await Runner.run(st.session_state.pe_agent, query)
                    return result.final_output or ""

                if st.session_state.get("langfuse_on") and LANGFUSE_AVAILABLE:
                    @_lf_observe(name="PE_Agent_Query")
                    async def _run_agent_traced(query: str) -> str:
                        return await _run_agent(query)

                    loop = asyncio.new_event_loop()
                    nest_asyncio.apply(loop)
                    asyncio.set_event_loop(loop)
                    response = loop.run_until_complete(_run_agent_traced(user_input))
                else:
                    loop = asyncio.new_event_loop()
                    nest_asyncio.apply(loop)
                    asyncio.set_event_loop(loop)
                    response = loop.run_until_complete(_run_agent(user_input))

                if not response:
                    response = "（Agent 未回傳任何內容，請確認 LLM 連線與本地資料是否正常）"

                if _lf_client:
                    _lf_client.flush()

            except Exception as e:
                import traceback
                traceback.print_exc()
                response = f"**執行錯誤：** {str(e)}\n\n```\n{traceback.format_exc()}\n```"

        response_placeholder.markdown(response)
        st.session_state.chat_history.append(
            {"role": "assistant", "content": response}
        )
