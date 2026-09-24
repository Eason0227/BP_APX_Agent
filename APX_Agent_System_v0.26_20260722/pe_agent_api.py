"""
PE Weekly Report Agent API
--------------------------
提供 PE 週報與自然語言查詢服務。
"""

import os
import json
import time
import traceback
import contextvars
from functools import lru_cache
from datetime import datetime, timedelta
from typing import Any, Optional
import httpx
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from agents import (
    Agent,
    Runner,
    function_tool,
    OpenAIResponsesModel,
    AsyncOpenAI,
    RunHooks,
)

session = requests.Session()
session.trust_env = False  # 忽略環境變數（包含 proxy）

_TRACE_CONTEXT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("trace_context", default=None)

# ── 全域設定 ──
API_BASE_URL = "http://10.11.32.155:4000/v1"
API_KEY = "sk-vVPq-hpFctglwngOCf7bOA"
API_MODEL = "Qwen3.6-35B-A3B"
# API_MODEL = "Gemma4-31B"

# API_BASE_URL  = "http://10.11.33.5:9120/v1"
# API_MODEL     = "/model/gpt-oss-120b"
# API_KEY = "EMPTY"

# API_BASE_URL = "http://10.11.33.5:9988/v1"
# API_MODEL = "/model/gpt-oss-20b"

LOT_DATA_XLSX = (
    r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\PE_Agent"
    r"\lot_documents_structured_(Security C).xlsx"
)
WEEKLY_YIELD_XLSX = (
    r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\PE_Agent"
    r"\weekly_yield_report_(Security C).xlsx"
)


def _to_jsonable(value: Any, max_depth: int = 4) -> Any:
    if max_depth <= 0:
        return str(value)

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {str(k): _to_jsonable(v, max_depth=max_depth - 1) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v, max_depth=max_depth - 1) for v in value]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _to_jsonable(model_dump(), max_depth=max_depth - 1)
        except Exception:
            pass

    dict_obj = getattr(value, "__dict__", None)
    if isinstance(dict_obj, dict):
        return {k: _to_jsonable(v, max_depth=max_depth - 1) for k, v in dict_obj.items() if not str(k).startswith("_")}

    return str(value)


def _append_trace(event_type: str, **payload: Any) -> None:
    trace_ctx = _TRACE_CONTEXT.get()
    if trace_ctx is None:
        return
    trace_ctx.setdefault("events", []).append(
        {
            "ts": datetime.now().isoformat(),
            "event": event_type,
            "data": _to_jsonable(payload),
        }
    )


def _snapshot_run_result(result: Any) -> dict:
    snapshot = {
        "type": type(result).__name__,
        "has_final_output": hasattr(result, "final_output"),
        "has_input": hasattr(result, "input"),
    }
    for attr in ["final_output", "input", "new_items", "last_agent"]:
        if hasattr(result, attr):
            try:
                snapshot[attr] = _to_jsonable(getattr(result, attr))
            except Exception:
                snapshot[attr] = f"<unavailable:{attr}>"
    return snapshot


class _TraceHooks(RunHooks):
    """以 Runner 生命週期 hook 自動記錄每個工具的 tool_start / tool_done 事件，
    供前端時序圖（Session Replay）繪製 Tool / LLM 軌道。"""

    def __init__(self) -> None:
        self._starts: dict[str, list[float]] = {}

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        name = getattr(tool, "name", str(tool))
        self._starts.setdefault(name, []).append(time.perf_counter())
        _append_trace("tool_start", tool=name)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        name = getattr(tool, "name", str(tool))
        stack = self._starts.get(name) or []
        started = stack.pop() if stack else None
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2) if started else None
        text = "" if result is None else str(result)
        _append_trace(
            "tool_done",
            tool=name,
            elapsed_ms=elapsed_ms,
            output_preview=text[:2000],
            output_length=len(text),
        )


def _format_suspect_machines(raw) -> str:
    """將「PE 嫌疑區對應機台」JSON 欄位格式化為可讀文字，供 Agent 直接引用。"""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return text

    if not isinstance(data, dict):
        return text

    lines: list[str] = []
    for area, machines in data.items():
        lines.append(f"[{area}]")
        if not isinstance(machines, list):
            lines.append(f"  - {machines}")
            continue
        for m in machines:
            if not isinstance(m, dict):
                lines.append(f"  - {m}")
                continue
            parts = [f"機台: {m.get('machine', 'N/A')}"]
            for key, label in (
                ("stage", "stage"),
                ("站點", "站點"),
                ("process", "process"),
                ("狀態", "狀態"),
                ("OOB Rate", "OOB Rate"),
                ("進出時間", "進出時間"),
            ):
                val = m.get(key)
                if val not in (None, ""):
                    parts.append(f"{label}: {val}")
            lines.append("  - " + " | ".join(parts))
    return "\n".join(lines)


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
        if has_percent:
            return num / 100.0
        return num / 100.0 if num > 1 else num

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
            "PE 嫌疑區對應機台": "suspect_machines",
        }
    )

    required_cols = [
        "lot_id",
        "inspection_date",
        "particle_yield_loss",
        "particle_loss_qty",
        "pe_suspect_area",
        "suspect_machines",
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Lot Excel 欄位缺失: {', '.join(missing)}")

    df["inspection_date"] = pd.to_datetime(df["inspection_date"], errors="coerce")
    df["particle_yield_loss"] = df["particle_yield_loss"].apply(_percent_to_decimal)
    df["particle_loss_qty"] = pd.to_numeric(df["particle_loss_qty"], errors="coerce").fillna(0)
    df["lot_id"] = df["lot_id"].fillna("N/A").astype(str)
    df["pe_suspect_area"] = df["pe_suspect_area"].fillna("").astype(str)
    df["suspect_machines"] = df["suspect_machines"].fillna("").astype(str)
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
        rows.append(
            {
                "lot_id": r["lot_id"],
                "particle_yield_loss": float(r["particle_yield_loss"]),
                "particle_loss_qty": int(r["particle_loss_qty"]),
                "inspection_date": insp,
                "pe_suspect_area": r["pe_suspect_area"],
                "suspect_machines": _format_suspect_machines(r["suspect_machines"]),
            }
        )
    return rows


@lru_cache(maxsize=1)
def _load_weekly_yield_dataframe() -> pd.DataFrame:
    df = pd.read_excel(WEEKLY_YIELD_XLSX, dtype=str, engine="openpyxl").fillna("")

    required_cols = [
        "年碼",
        "週次",
        "總產量(Die)",
        "總Lot數",
        "平均總良率",
        "平均Particle Yield",
        "Particle損失總棵數",
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"週報 Excel 欄位缺失: {', '.join(missing)}")

    df["_year"] = pd.to_numeric(df["年碼"], errors="coerce")
    df["_week_num"] = pd.to_numeric(df["週次"].str.upper().str.replace("W", "", regex=False), errors="coerce")
    return df


def _get_amd_yield_summary_by_week(start_date: str, end_date: str) -> str | None:
    df = _load_weekly_yield_dataframe()
    end_ts = pd.to_datetime(end_date, errors="coerce") 
    start_ts = pd.to_datetime(start_date, errors="coerce")
    if pd.isna(end_ts) or pd.isna(start_ts):
        return None

    end_dt = end_ts.date()
    iso = end_dt.isocalendar()
    target_year = iso[0]
    target_week = iso[1]
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


@function_tool
def get_amd_yield_summary_by_week(start_date: str, end_date: str) -> str:
    """依據查詢區間回傳對應 AMD 週報摘要（來源: weekly_yield_report Excel）。"""
    try:
        summary = _get_amd_yield_summary_by_week(start_date=start_date, end_date=end_date)
        if not summary:
            return "查無可用的 AMD 週報摘要（請確認日期格式與 Excel 內容）。"
        return summary
    except Exception as e:
        return f"取得 AMD 週報摘要時發生錯誤：{str(e)}"


@function_tool
def search_lots_by_yield_loss(
    min_yield_loss: float = 0.01,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 10,
) -> str:
    try:
        points = _excel_scroll_yield_loss(
            min_yield_loss=min_yield_loss,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
        start_date = pd.to_datetime(start_date, errors="coerce") - pd.Timedelta(days = 4)
        end_date = pd.to_datetime(end_date, errors="coerce") - pd.Timedelta(days = 4)

        if not points:
            date_msg = f" 介於 {start_date}~{end_date}" if start_date or end_date else ""
            return f"未找到 particle_yield_loss >= {min_yield_loss:.4f}{date_msg} 的 Lot。"

        points.sort(key=lambda p: p.get("particle_yield_loss", 0), reverse=True)

        lines = []
        for p in points:
            lid = p.get("lot_id", "N/A")
            yloss = p.get("particle_yield_loss", 0)
            ploss = p.get("particle_loss_qty", "N/A")
            insp = p.get("inspection_date", "N/A")
            pe_area = p.get("pe_suspect_area", "")

            machines = p.get("suspect_machines", "")
            pe_exp_text = f"PE 經驗初步判定嫌疑區: {pe_area}" if pe_area else ""

            lines.append(
                f"- **{lid}** | Yield Loss: {yloss:.4%} | Particle Loss Qty: {ploss} | 檢驗日期: {insp}\n"
                f"  {pe_exp_text}"
            )
            lines.append(f"  [PE 嫌疑區對應機台]:\n{machines}\n")

        header = f"共找到 {len(points)} 批 particle_yield_loss >= {min_yield_loss:.4%} 的 Lot：\n\n"
        return header + "\n---\n".join(lines)
    except Exception as e:
        return f"查詢高損耗 Lot 時發生錯誤：{str(e)}"


PE_SYSTEM_PROMPT = \
"""
# Role
你是一位資深的半導體製程工程師（Senior Process Engineer Agent），負責產出每週製程與機台健康度總結（Weekly Report）。
- 核心職責是「症狀偵測 + 嫌疑假設」：從良率損失反推可疑站點/機台，提出待確認的因果假設。
- 你不具備設備處置深度（無 EE 的 CIP 案例、無 QA 汙染源分析），產出的機台與處置方向是「交由 EE 以設備資料確認」的派工線索，非最終命令。

# 工具使用規則
- 先用 search_lots_by_yield_loss 取得該週異常 Lot 與 PE 判讀線索；一律用 get_amd_yield_summary_by_week(start_date, end_date) 取得 AMD 實際良率摘要。
- 回傳的每筆 Lot 已含「PE 嫌疑區對應機台」結構化欄位（machine、stage、站點、process、狀態、OOB Rate、進出時間），不需自行從軌跡文字推斷機台。

# 通用規則（全節適用，必須遵守）
1. 禁止編造：任何良率、Lot、機台、產線、站點、OOB Rate 數據一律取自工具回傳，查無時照實標示，不得杜撰（如 FE-12、PE-07 等）。
2. 機台名稱：一律使用「PE 嫌疑區對應機台」欄位的完整名稱（如 B1_OVEN_02），禁止只寫類型前綴（如 OVEN）；同一嫌疑區對應多台須逐台列出；該欄位查無對應機台時才以「{類型前綴}_待確認」標示並於備註說明。
3. 判讀以 PE 初判為主：以「PE 經驗初步判定嫌疑區」為主要依據；欄位中的 stage/process/狀態/OOB Rate 僅供輔助，不得用來否定 PE 初判。即使某機台「狀態=正常」，只要落在 PE 初判嫌疑區，仍須列為重點關注並要求 EE 確認。
4. 機制引用：每台嫌疑機台的「可能生成機制」「EE 應查」「保養方向」一律引用下方「粒子根因知識庫」，須點名具體工法（高溫燒爐、拆解清潔自動門內側、Al Pasting、Target Clean 等），禁止泛講「清腔/換件」、禁止自創庫外機制與設備名詞，無對應時寫「機制待確認」。
5. 待確認定位：所有處置方向均須經 EE 以設備資料（CIP/rework/hotspot）確認後始得執行，僅用「保養」類方向，禁止「立即保養」「即刻執行」等命令式語氣。
6. 標題層級：全文僅能用 ### 或 ####，嚴禁 # 或 ##；一律繁體中文並帶具體數字。

# 粒子根因知識庫（依公司內部 Bumping 製程分析）

## Coat（COAT，PR Coat / PI Coat 旋塗）
- 主要生成機制：光阻 Spin 旋塗過程產生液體噴濺，冷凝後形成微小 particle。
  （PI Coat 同屬旋塗噴濺機制，PI 黏度高、噴濺殘膜更易堆積；判讀時 PR/PI 機台編號不可混淆。）
- EE 應查：cup/bowl 與杯壁噴濺殘膜堆積、dispense 噴濺範圍、旋塗腔清潔 PM 週期。
- 保養方向：清潔 cup/bowl 與旋塗腔噴濺殘膜，確認無乾涸回濺。

## Oven（OVEN，PI Cure / 光阻加熱）
- 主要生成機制：光阻加熱使部分分子脫離材料表面，冷凝後形成微小 particle，於製程中被帶至 wafer 表面。
- 已知清潔死角（重要 PE 線索）：爐腔自動門內側為清潔死角，光阻揮發物主要堆積於門板內側上方；
  關門後曾量測到 0.5um 揚塵，須密閉循環後始被清除。此區需從機台後方拆解才能清潔，並以丙酮輔助清除殘留。
- 清潔工法：高溫燒爐 — 缺氧環境下以熱裂解原理使殘留有機物化學鍵斷裂、碳化，再以抽風管路帶走揮發物。
- EE 應查：上次高溫燒爐紀錄、抽風管路狀態、自動門內側上方殘渣堆積、關門後密閉循環揚塵量測。
- 保養方向：執行高溫燒爐；拆解清潔自動門內側上方死角並以丙酮輔助清除。

## Sput（SPUT，PVD seed 濺鍍）
- 主要生成機制：濺鍍過程的金屬原子在腔體內殘留，形成金屬外物。
- 清潔工法（依腔體類型擇用）：
  - ICP chamber → Al Pasting：以鋁控片電漿蝕刻方式將鋁噴濺覆蓋控片/腔體表面，提高槽體潔淨度。
  - PVD(Ti) chamber → Target Clean：以濺鍍方式將靶材金屬覆蓋 dummy 表面，刷新槽體潔淨度。
- EE 應查：上次 Al Pasting / Target Clean 執行紀錄、腔體累積生成物、particle monitor 控片/dummy 趨勢。
- 保養方向：依腔體類型執行 Al Pasting（ICP）或 Target Clean（PVD Ti）以提高槽體潔淨度。

## PR Exp（STEP，曝光）
- 主要生成機制：
  - 曝光機光罩（Mask/Reticle）於傳送或存放過程沾附微粒，或光罩保護膜（Pellicle）破損／髒污，直接遮蔽或散射曝光光源，機台傳輸機構摩擦產生粉塵。
- 清潔工法（依異常區域擇用）：
  - Reticle Clean：清潔光罩（Reticle）及 Pellicle，去除表面灰塵與殘留污染物。
  - Stage / Robot Clean：清潔 Wafer Stage、Aligner、Robot Arm 等搬送機構，降低磨耗粉塵。
  - PM Particle Qualification：保養後執行 Particle Monitor Wafer 驗證設備潔淨度。
  - Wafer Chuck Clean：清潔真空吸附平台（Chuck），避免顆粒累積。
- EE 應查：
  - 上次 PM、Reticle Clean、Chuck Clean 執行紀錄、Robot、Aligner、Stage 磨耗狀況。
  - 是否存在固定座標重複性 Defect（光罩污染特徵）。
- 保養方向：
  - 定期執行 Reticle 與 Chuck 清潔，加強 Robot、Aligner、Stage 機構保養與磨耗檢查。
  - 若 AOI Particle 呈現固定位置重複發生，優先檢查 Reticle 污染；若呈現隨機散佈，則優先檢查設備潔淨度、搬送機構及周邊環境落塵。

## Final（SCOP / AGST，最終檢驗）
- 主要生成機制：
    - Final 為「檢出站」（SCOP 顯微鏡或 AGST AOI 光學檢機台）；所見粒子可能來自檢測機台本身不潔淨，或由上游製程轉移帶入並於此被檢出。
- EE 應查：
    - 優先回溯上游製程機台，再評估 Final 自身 wafer handling / robot transfer 帶入，並一併檢查檢測機台本身的保養問題。
- 保養方向：
    - 評估 Final 自身 wafer handling / robot transfer 帶入，並一併檢查檢測機台本身的保養問題。
    - 不得對 Final 直接開立上游清潔動作（高溫燒爐、Al Pasting、Target Clean 、Reticle 等）。

# 輸出格式

第一行用 ### 標題：### {year}-W{week} 整體良率狀態彙報（以實際週次取代）。各章節用 #### N) 章節名稱：
#### 1) 本週 Particle 良率總覽
#### 2) 異常機台關注清單 (EE vs PE 觀點)
#### 3) 異常機台製程軌跡與 OOB Fail 摘要
#### 4) Action Items for EE (下週派工確認重點)

## #### 1) 本週 Particle 良率總覽

依序輸出兩個子區塊：

**子區塊 A：AMD 良率摘要** — 引用 get_amd_yield_summary_by_week 真實數值（總產量、Lot 數、平均良率、Particle Yield 等）。

**子區塊 B：本週異常 Lot 概況**（粗體標題），接兩行：
- 「發現 {N} 件 Particle Yield Loss ≥ {門檻}% 的 Lot，累計 {M} 棵 Particle 損失。」（N、M 由工具回傳資料計算）
- 「主要 PE 初判嫌疑區域：{所有 Lot 的 pe_suspect_area 去重後以頓號連接}。」

再輸出異常 Lot 明細表（每筆回傳 Lot 都須列入、不得增減；檢驗日期只到日 YYYY-MM-DD）：
| Lot 編號 | 檢驗日期 | Particle Loss Qty | PE 初判嫌疑區域 |
|---|---|---|---|
| {lot_id} | {YYYY-MM-DD} | {particle_loss_qty} | {pe_suspect_area} |

## #### 2) 異常機台關注清單 (EE vs PE 觀點)

逐台列出第 1) 節嫌疑區對應機台（SCOP/AGST 的 Final 可直接對機台開立清潔動作）：

| 順序 | 機台名稱 | 挑戰機台 (PE 初判嫌疑區) | 可能生成機制（PE 領域知識） | EE 觀點說明（應查重點） |
|---|---|---|---|---|
| 1 | <完整機台名稱> | <pe_suspect_area> | <引用粒子根因知識庫之生成機制／清潔死角> | <對應該機制之 EE 應查指標> |

## #### 3) 異常機台製程軌跡與 OOB Fail 摘要

逐筆列出「PE 嫌疑區對應機台」欄位的原始內容（同一機台多筆 process/時間須逐筆列、不可合併；OOB Fail 直接填原值如 12.5%、0.0%、N/A）：

| 順序 | 機台名稱 | 來源 Lot | 挑戰機台 (PE 初判嫌疑區) | stage | 站點 | process | 進出時間 | OOB Fail (OOB Rate) |
|---|---|---|---|---|---|---|---|---|
| 1 | <完整機台名稱> | <lot_id> | <pe_suspect_area> | <stage> | <站點> | <process> | <進出時間> | <OOB Rate 或 N/A> |

## #### 4) Action Items for EE（派工確認單）

把 PE 嫌疑機台交給 EE 確認（每筆須能回溯第 1)/2) 節的 Lot 或嫌疑區，禁止新增機台）：

| 優先順序 | 機台名稱 | 嫌疑依據（PE 初判） | 建議 EE 確認項目 | 初步處置方向（待確認後執行） |
|---|---|---|---|---|
| P1 | <完整機台名稱> | <pe_suspect_area 與良率損失之關聯> | <EE 應以 CIP/rework/hotspot 確認的具體點> | 保養方向：<建議保養作業與目的> |

### 無異常 Lot 時（search_lots_by_yield_loss 回傳「未找到」或查無 Lot）
- 第 1) 節子區塊 A 仍引用 AMD 良率摘要真實數值；子區塊 B 輸出：**本週異常 Lot 概況**\\n本週查無 Particle Yield Loss 超過門檻的 Lot。
- 第 2) 節寫「本週無異常機台需關注」；第 3) 節寫「本週無異常機台製程軌跡與 OOB Fail 可列示」；第 4) 節寫「本週無需額外派工確認，建議維持例行保養排程」。
"""


def create_weekly_report_agent() -> Agent:
    http_client = httpx.AsyncClient(proxy=None, trust_env=False)
    openai_client = AsyncOpenAI(
        base_url=API_BASE_URL,
        api_key=API_KEY,
        http_client=http_client,
    )
    model_conf = OpenAIResponsesModel(
        model=API_MODEL,
        openai_client=openai_client,
    )

    return Agent(
        name="PE_Weekly_Report_Agent",
        instructions=PE_SYSTEM_PROMPT,
        model=model_conf,
        tools=[search_lots_by_yield_loss, get_amd_yield_summary_by_week],
    )


class WeeklyReportRequest(BaseModel):
    start_date: str | None = Field(default=None, description="YYYY-MM-DD")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD")
    min_yield_loss: float = Field(default=0.01, ge=0, description="良率損耗門檻，小數")
    limit: int = Field(default=30, ge=1, le=200)
    pe_note: str | None = Field(default=None, description="PE 補充觀察，例如疑似站點或圖形特徵")
    yield_summary: str | None = Field(default=None, description="保留欄位；系統會統一改由 get_amd_yield_summary_by_week 工具取得 AMD 實際良率摘要")
    use_langfuse: bool = Field(default=False, description="保留欄位，與其他 Agent API 介面對齊")
    include_trace: bool = Field(default=False, description="是否回傳執行 trace")


class WeeklyReportResponse(BaseModel):
    report: str
    query_used: str
    generated_at: str
    trace: Optional[dict] = None


app = FastAPI(title="APX PE Weekly Report API", version="1.0.0")
weekly_agent = create_weekly_report_agent()


def _default_week_range() -> tuple[str, str]:
    today = datetime.today().date()
    start = today - timedelta(days=7)
    return start.isoformat(), today.isoformat()


def _build_weekly_query(req: WeeklyReportRequest) -> str:
    start_date = req.start_date
    end_date = req.end_date
    if not start_date or not end_date:
        d_start, d_end = _default_week_range()
        start_date = start_date or d_start
        end_date = end_date or d_end

    start_date = pd.to_datetime(start_date, errors="coerce") - pd.Timedelta(days = 4)
    end_date = pd.to_datetime(end_date, errors="coerce") - pd.Timedelta(days = 4)

    note = req.pe_note or "PE 未提供額外備註"

    return (
        f"請產出 {start_date} 到 {end_date} 的 Particle 良率週報。"
        f"請先用 search_lots_by_yield_loss(min_yield_loss={req.min_yield_loss}, "
        f"start_date='{start_date}', end_date='{end_date}', limit={req.limit}) 取得資料。"
        f"PE 補充觀察：{note}。"
        "請一律使用 get_amd_yield_summary_by_week(start_date, end_date) 取得 AMD 實際良率摘要。"
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "PE_Weekly_Report_API"}


@app.post("/api/weekly-report", response_model=WeeklyReportResponse)
async def generate_weekly_report(req: WeeklyReportRequest) -> WeeklyReportResponse:
    trace_ctx = {
        "request": {
            "endpoint": "/api/weekly-report",
            "start_date": req.start_date,
            "end_date": req.end_date,
            "min_yield_loss": req.min_yield_loss,
            "limit": req.limit,
            "include_trace": req.include_trace,
        },
        "events": [],
    }
    token = _TRACE_CONTEXT.set(trace_ctx)
    started = datetime.now()
    try:
        query = _build_weekly_query(req)
        _append_trace("request_received", query_type="weekly_report")
        _append_trace("runner_start", prompt=query)
        result = await Runner.run(weekly_agent, query, hooks=_TraceHooks())
        elapsed_ms = round((datetime.now() - started).total_seconds() * 1000, 2)
        _append_trace("runner_done", elapsed_ms=elapsed_ms)
        _append_trace("run_result_snapshot", snapshot=_snapshot_run_result(result))
        report = result.final_output or ""
        if not report:
            raise HTTPException(status_code=502, detail="Agent 未回傳內容，請檢查模型服務或 Excel 資料來源")

        return WeeklyReportResponse(
            report=report,
            query_used=query,
            generated_at=datetime.now().isoformat(timespec="seconds"),
            trace=trace_ctx if req.include_trace else None,
        )
    except HTTPException:
        raise
    except Exception as e:
        elapsed_ms = round((datetime.now() - started).total_seconds() * 1000, 2)
        _append_trace(
            "runner_error",
            elapsed_ms=elapsed_ms,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        raise HTTPException(status_code=500, detail=f"產生週報失敗: {str(e)}")
    finally:
        _TRACE_CONTEXT.reset(token)


class ChatRequest(BaseModel):
    query: str = Field(..., description="自然語言查詢")
    include_trace: bool = Field(default=False, description="是否回傳執行 trace")


class ChatResponse(BaseModel):
    answer: str = Field(..., description="Agent 回覆內容")
    trace: Optional[dict] = None


def _to_jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _to_jsonable(model_dump())
        except Exception:
            pass

    dict_obj = getattr(value, "__dict__", None)
    if isinstance(dict_obj, dict):
        return {k: _to_jsonable(v) for k, v in dict_obj.items() if not str(k).startswith("_")}

    return str(value)


def _extract_stream_event(event) -> dict:
    data = _to_jsonable(event)
    event_type = getattr(event, "type", None)
    if isinstance(data, dict):
        if event_type and "type" not in data:
            data["type"] = event_type
        return data

    return {
        "type": event_type or type(event).__name__,
        "data": data,
    }


def _sse_line(payload: dict, event_name: str = "message") -> str:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {body}\n\n"


async def _stream_agent_events(query: str):
    yield _sse_line({"type": "meta", "query": query}, event_name="meta")

    try:
        streamed_result = Runner.run_streamed(weekly_agent, query)
        async for event in streamed_result.stream_events():
            yield _sse_line(_extract_stream_event(event), event_name="agent_event")

        final_output = getattr(streamed_result, "final_output", "") or ""

        # 新版 event 型別下，streamed_result.final_output 可能為空字串。
        # 發生時以同 query 補跑一次非串流，確保前端可取得最終文字內容。
        if not str(final_output).strip():
            try:
                fallback_result = await Runner.run(weekly_agent, query)
                final_output = fallback_result.final_output or ""
            except Exception:
                final_output = ""

        yield _sse_line({"type": "final_output", "content": final_output}, event_name="final")
    except Exception as e:
        yield _sse_line({"type": "error", "message": str(e)}, event_name="error")


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """以自然語言向 PE Agent 發問，取得製程工程觀點的分析。"""
    trace_ctx = {
        "request": {
            "endpoint": "/chat",
            "query": req.query,
            "include_trace": req.include_trace,
        },
        "events": [],
    }
    token = _TRACE_CONTEXT.set(trace_ctx)
    started = datetime.now()
    try:
        _append_trace("request_received", query_type="chat")
        _append_trace("runner_start", prompt=req.query)
        result = await Runner.run(weekly_agent, req.query, hooks=_TraceHooks())
        elapsed_ms = round((datetime.now() - started).total_seconds() * 1000, 2)
        _append_trace("runner_done", elapsed_ms=elapsed_ms)
        _append_trace("run_result_snapshot", snapshot=_snapshot_run_result(result))
        answer = result.final_output or "（Agent 未回傳任何內容）"
        return ChatResponse(answer=answer, trace=trace_ctx if req.include_trace else None)
    except Exception as e:
        elapsed_ms = round((datetime.now() - started).total_seconds() * 1000, 2)
        _append_trace(
            "runner_error",
            elapsed_ms=elapsed_ms,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        raise HTTPException(status_code=500, detail=f"PE Agent 查詢失敗: {str(e)}")
    finally:
        _TRACE_CONTEXT.reset(token)


@app.post("/api/weekly-report/stream")
async def generate_weekly_report_stream(req: WeeklyReportRequest):
    query = _build_weekly_query(req)
    return StreamingResponse(
        _stream_agent_events(query),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    return StreamingResponse(
        _stream_agent_events(req.query),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("PE_API_HOST", "0.0.0.0")
    port = int(os.getenv("PE_API_PORT", "8088"))
    uvicorn.run(app, host=host, port=port)
