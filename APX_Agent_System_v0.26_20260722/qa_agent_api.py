"""
QA Agent — 保養成效 API Server (FastAPI)
基於 OpenAI Agent SDK + 本地 Excel 資料來源，
提供 REST API 透過 Agent 自動產出結構化 QA 品質驗證

啟動方式：
    uvicorn qa_agent_api:app --host 0.0.0.0 --port 8077
"""
import os
import json
import re
import time
import uuid
import logging
import traceback
import contextvars
import httpx
import pandas as pd
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Any, Optional
from agents import (
    Agent, Runner, function_tool,
    OpenAIResponsesModel, set_tracing_disabled, AsyncOpenAI,
    RunHooks,
)

logger = logging.getLogger("qa_agent_api")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

# ── 全域設定 ──
QA_HISTORY_EXCEL = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\QA_Agent\QA_weekly_report_output\history_tables\qa_weekly_history_(Security C).xlsx"
WEEKLY_TREND_SHEET = "weekly_trend_history"
TRAJECTORY_SHEET = "Machine_trajectory_history"
MACHINE_BREAKDOWN_SHEET = "weekly_Machine_breakdown_histor"

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

os.environ.setdefault("LANGFUSE_HOST",       "http://10.10.19.36:3000")
os.environ.setdefault("LANGFUSE_PUBLIC_KEY",  "pk-lf-491f2a46-7264-4efd-ba00-39a9310d01e3")
os.environ.setdefault("LANGFUSE_SECRET_KEY",  "sk-lf-40c95f0d-1601-4fa0-9769-ab6190a9b892")

set_tracing_disabled(True)

_TRACE_CONTEXT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("trace_context", default=None)

# 炭膠帶污染源分析 Excel 檔案路徑
CONTAMINATION_EXCEL = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\QA_Agent\20260520_CarbonTape_summary_(Security C).xlsx"



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
            "ts": pd.Timestamp.now().isoformat(),
            "event": event_type,
            "data": _to_jsonable(payload),
        }
    )


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


def _snapshot_run_result(result: Any) -> dict:
    snapshot = {
        "type": type(result).__name__,
        "has_final_output": hasattr(result, "final_output"),
        "has_input": hasattr(result, "input"),
    }
    for attr in ["final_output", "output", "input", "new_items", "last_agent"]:
        if hasattr(result, attr):
            try:
                snapshot[attr] = _to_jsonable(getattr(result, attr))
            except Exception:
                snapshot[attr] = f"<unavailable:{attr}>"
    return snapshot


# ═══════════════════════════════════════════════════════════════════════════
# Excel 底層工具函式
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _read_sheet(sheet_name: str) -> pd.DataFrame:
    xls = pd.ExcelFile(QA_HISTORY_EXCEL)
    if sheet_name in xls.sheet_names:
        return pd.read_excel(xls, sheet_name=sheet_name)

    fallback = {
        MACHINE_BREAKDOWN_SHEET: ["weekly_Machine_breakdown_history", "weekly_machine_breakdown_histor", "weekly_machine_breakdown_history"],
        TRAJECTORY_SHEET: ["machine_trajectory_history"],
        WEEKLY_TREND_SHEET: ["Weekly_trend_history"],
    }
    for candidate in fallback.get(sheet_name, []):
        if candidate in xls.sheet_names:
            return pd.read_excel(xls, sheet_name=candidate)

    raise ValueError(f"在 {QA_HISTORY_EXCEL} 找不到工作表: {sheet_name}，可用工作表: {xls.sheet_names}")


def _load_weekly_trend() -> pd.DataFrame:
    df = _read_sheet(WEEKLY_TREND_SHEET)
    if "YEAR_WEEK" in df.columns:
        df["YEAR_WEEK"] = df["YEAR_WEEK"].astype(str).str.strip()
    return df


def _load_machine_trajectory() -> pd.DataFrame:
    df = _read_sheet(TRAJECTORY_SHEET)
    if "latest_year_week" in df.columns:
        df["latest_year_week"] = df["latest_year_week"].astype(str).str.strip()
    if "Machine" in df.columns:
        df["Machine"] = df["Machine"].astype(str).str.strip()
    return df


def _load_machine_breakdown() -> pd.DataFrame:
    df = _read_sheet(MACHINE_BREAKDOWN_SHEET)
    if "YEAR_WEEK" in df.columns:
        df["YEAR_WEEK"] = df["YEAR_WEEK"].astype(str).str.strip()
    if "Machine" in df.columns:
        df["Machine"] = df["Machine"].astype(str).str.strip()
    return df


def _safe_float(value, digits: int = 4):
    try:
        if pd.isna(value):
            return "N/A"
        return round(float(value), digits)
    except Exception:
        return "N/A"


def _safe_int(value):
    try:
        if pd.isna(value):
            return "N/A"
        return int(float(value))
    except Exception:
        return "N/A"


def _pick_latest_week(weeks: list[str]) -> str:
    if not weeks:
        return ""
    return sorted([w for w in weeks if isinstance(w, str) and w], reverse=True)[0]


def _format_search_results(results: list, max_items: int = 6) -> str:
    if not results:
        return "（未找到相關資料）"
    lines = []
    for r in results[:max_items]:
        lines.append(f"[相關度: {r['score']:.3f}]\n{r['content']}")
    return "\n\n---\n\n".join(lines)


def _collect_text_candidates(value) -> list[str]:
    """遞迴擷取 SDK 回傳物件中的文字內容。"""
    texts = []

    if value is None:
        return texts

    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []

    if isinstance(value, dict):
        for key in ("text", "output_text", "content", "message", "value"):
            if key in value:
                texts.extend(_collect_text_candidates(value.get(key)))
        return texts

    if isinstance(value, (list, tuple)):
        for item in value:
            texts.extend(_collect_text_candidates(item))
        return texts

    for field in ("text", "output_text", "content", "message", "value"):
        try:
            if hasattr(value, field):
                texts.extend(_collect_text_candidates(getattr(value, field)))
        except Exception:
            pass

    if hasattr(value, "to_dict"):
        try:
            texts.extend(_collect_text_candidates(value.to_dict()))
        except Exception:
            pass

    if hasattr(value, "model_dump"):
        try:
            texts.extend(_collect_text_candidates(value.model_dump()))
        except Exception:
            pass

    deduped = []
    seen = set()
    for text in texts:
        if text and text not in seen:
            seen.add(text)
            deduped.append(text)
    return deduped


def _extract_answer_and_diag(result) -> tuple[str, dict]:
    """盡可能從 Runner 結果提取可讀答案，並回傳診斷資訊。"""
    diag = {
        "result_type": type(result).__name__,
        "has_final_output": False,
        "final_output_type": "NoneType",
        "final_output_len": 0,
        "output_type": "NoneType",
        "output_len": 0,
        "new_items_len": 0,
        "available_attrs": [],
        "final_output_preview": "",
        "output_preview": "",
    }

    if result is None:
        return "", diag

    try:
        attrs = [a for a in dir(result) if not a.startswith("_")]
        diag["available_attrs"] = attrs[:25]
    except Exception:
        pass

    final_output = getattr(result, "final_output", None)
    diag["has_final_output"] = bool(final_output)
    diag["final_output_type"] = type(final_output).__name__
    try:
        diag["final_output_len"] = len(final_output) if isinstance(final_output, str) else 0
    except Exception:
        pass
    if isinstance(final_output, str) and final_output.strip():
        return final_output, diag

    final_output_texts = _collect_text_candidates(final_output)
    if final_output_texts:
        diag["final_output_preview"] = final_output_texts[0][:200]
        return "\n\n".join(final_output_texts), diag

    output = getattr(result, "output", None)
    diag["output_type"] = type(output).__name__
    try:
        diag["output_len"] = len(output) if output is not None else 0
    except Exception:
        pass

    if isinstance(output, str) and output.strip():
        return output, diag

    output_texts = _collect_text_candidates(output)
    if output_texts:
        diag["output_preview"] = output_texts[0][:200]
        return "\n\n".join(output_texts), diag

    new_items = getattr(result, "new_items", None)
    try:
        diag["new_items_len"] = len(new_items) if new_items is not None else 0
    except Exception:
        pass

    new_item_texts = _collect_text_candidates(new_items)
    if new_item_texts:
        return "\n\n".join(new_item_texts), diag

    return "", diag


# ═══════════════════════════════════════════════════════════════════════════
# Agent 工具（function_tool）
# ═══════════════════════════════════════════════════════════════════════════

@function_tool
def search_weekly_report(year_week: str) -> str:
    """
    以 ISO 週次精確查詢該週的 QA 保養成效摘要，包含 KPI 總覽、各機台保養明細、Critical Risk PSN。

    參數：
    - year_week: ISO 週次，例如 "2026-W13"
    """
    try:
        year_week = year_week.strip()
        if not year_week:
            return "請提供有效的週次（格式: YYYY-WNN，例如 2026-W13）。"

        weekly_df = _load_weekly_trend()
        machine_df = _load_machine_breakdown()

        weekly = weekly_df[weekly_df["YEAR_WEEK"] == year_week]
        if weekly.empty:
            return f"未找到第 {year_week} 週的 QA 週報資料。"

        row = weekly.iloc[0]
        machine_rows = machine_df[machine_df["YEAR_WEEK"] == year_week].copy()

        lines = [f"### QA 週報摘要（{year_week}）"]
        lines.append("#### KPI 總覽")
        lines.append("| 指標 | 數值 |")
        lines.append("|------|------|")
        lines.append(f"| 保養次數 | {_safe_int(row.get('total'))} |")
        lines.append(f"| 成功次數 | {_safe_int(row.get('success'))} |")
        lines.append(f"| 成功率 | {_safe_float(row.get('success_rate'), 1)}% |")
        lines.append(f"| 惡化次數 | {_safe_int(row.get('worsened'))} |")
        lines.append(f"| 惡化率 | {_safe_float(row.get('worsened_rate'), 1)}% |")
        lines.append(f"| 平均 DIFF | {_safe_float(row.get('avg_diff'), 4)} |")
        lines.append(f"| 成功案例平均 DIFF | {_safe_float(row.get('avg_diff_success'), 4)} |")
        lines.append(f"| 平均 PI_before | {_safe_float(row.get('avg_pi_before'), 4)} |")
        lines.append(f"| 平均 PI_after | {_safe_float(row.get('avg_pi_after'), 4)} |")
        lines.append(f"| 平均回應時間 | {_safe_float(row.get('avg_response_hours'), 1)} 小時 |")
        lines.append(f"| 機台數 | {_safe_int(row.get('unique_Machines'))} |")
        lines.append(f"| PSN 數 | {_safe_int(row.get('unique_psn'))} |")
        lines.append(f"| 最佳改善 | {row.get('best_psn', 'N/A')} (DIFF={_safe_float(row.get('best_improvement'), 3)}) |")
        lines.append("")

        if not machine_rows.empty:
            machine_rows = machine_rows.sort_values(by=["success_rate", "avg_diff"], ascending=[True, False])
            lines.append("#### 各機台保養明細")
            lines.append("| 機台 | PSN數 | 保養次數 | 成功次數 | 成功率 | 平均DIFF | 平均PI_after |")
            lines.append("|------|------|---------|--------|--------|----------|-------------|")
            for _, m in machine_rows.iterrows():
                lines.append(
                    f"| {m.get('Machine', 'N/A')} | {_safe_int(m.get('psn_count'))} | {_safe_int(m.get('total'))} | "
                    f"{_safe_int(m.get('success'))} | {_safe_float(m.get('success_rate'), 1)}% | "
                    f"{_safe_float(m.get('avg_diff'), 4)} | {_safe_float(m.get('avg_pi_after'), 4)} |"
                )

        lines.append("")
        lines.append("註解：PSN = Particle Sensor；PI = 汙染因子（數值越大代表汙染程度越嚴重）。")
        return "\n".join(lines)
    except Exception as e:
        return f"查詢週報時發生錯誤：{str(e)}"


@function_tool
def list_weekly_reports(start_week: str = None, end_week: str = None, limit: int = 15) -> str:
    """
    列出指定週次範圍內的所有 QA 週報 KPI 摘要，用於跨週趨勢比較或產生月報。

    參數：
    - start_week: 起始週次（含），例如 "2026-W06"，可留空
    - end_week:   結束週次（含），例如 "2026-W13"，可留空
    - limit:      最多回傳筆數，預設 15
    """
    try:
        weekly_df = _load_weekly_trend().copy()
        if start_week:
            weekly_df = weekly_df[weekly_df["YEAR_WEEK"] >= start_week]
        if end_week:
            weekly_df = weekly_df[weekly_df["YEAR_WEEK"] <= end_week]

        weekly_df = weekly_df.sort_values("YEAR_WEEK").head(limit)
        if weekly_df.empty:
            range_msg = ""
            if start_week and end_week:
                range_msg = f" {start_week} ~ {end_week} 之間"
            return f"未找到{range_msg}的 QA 週報資料。"

        lines = [f"共找到 {len(weekly_df)} 週 QA 週報：\n"]
        lines.append("| 週次 | 保養次數 | 成功率 | 惡化率 | 平均DIFF | 回應時間(hr) |")
        lines.append("|------|---------|--------|--------|----------|-------------|")
        for _, p in weekly_df.iterrows():
            lines.append(
                f"| {p.get('YEAR_WEEK', 'N/A')} "
                f"| {_safe_int(p.get('total'))} "
                f"| {_safe_float(p.get('success_rate'), 1)}% "
                f"| {_safe_float(p.get('worsened_rate'), 1)}% "
                f"| {_safe_float(p.get('avg_diff'), 4)} "
                f"| {_safe_float(p.get('avg_response_hours'), 1)} |"
            )

        lines.append("\n---\n")
        lines.append("以下為各週 KPI 重點：\n")
        for _, p in weekly_df.iterrows():
            lines.append(f"\n{'=' * 60}")
            lines.append(
                f"{p.get('YEAR_WEEK', 'N/A')}: 成功率 {_safe_float(p.get('success_rate'), 1)}%, "
                f"惡化率 {_safe_float(p.get('worsened_rate'), 1)}%, "
                f"平均DIFF {_safe_float(p.get('avg_diff'), 4)}, "
                f"最佳改善 {p.get('best_psn', 'N/A')} (DIFF={_safe_float(p.get('best_improvement'), 3)})"
            )

        return "\n".join(lines)
    except Exception as e:
        return f"列出週報時發生錯誤：{str(e)}"


@function_tool
def search_tool_trajectory(year_week: str = None, tool_name: str = None) -> str:
    """
    查詢機台保養軌跡分析，包含趨勢改善中/惡化中/長期無效/邊際遞減的機台清單。

    參數：
    - year_week: 特定週次，例如 "2026-W13"。若不指定則回傳最新一筆
    - tool_name: 特定機台名稱，例如 "B1_DEVP_01"
    """
    try:
        traj_df = _load_machine_trajectory().copy()
        if year_week:
            selected = traj_df[traj_df["latest_year_week"] == year_week]
        else:
            latest_week = _pick_latest_week(traj_df["latest_year_week"].dropna().astype(str).tolist())
            selected = traj_df[traj_df["latest_year_week"] == latest_week]

        if tool_name:
            tool_name = tool_name.strip()
            selected = selected[selected["Machine"].str.contains(tool_name, case=False, na=False)]

        if selected.empty:
            parts = []
            if year_week:
                parts.append(f"第 {year_week} 週")
            if tool_name:
                parts.append(f"機台 {tool_name}")
            return f"未找到{'、'.join(parts) if parts else ''}的機台軌跡資料。"

        selected = selected.sort_values(["latest_year_week", "Machine"])

        improving = selected[selected["is_improving"].apply(_normalize_bool)]["Machine"].tolist()
        worsening = selected[selected["is_worsening"].apply(_normalize_bool)]["Machine"].tolist()
        chronic = selected[selected["is_chronic_fail"].apply(_normalize_bool)]["Machine"].tolist()

        lines = []
        lines.append(
            f"[Metadata] {json.dumps({'year_week': selected.iloc[0].get('latest_year_week', 'N/A'), 'improving_count': len(improving), 'worsening_count': len(worsening), 'chronic_fail_count': len(chronic), 'improving_tools': improving, 'worsening_tools': worsening, 'chronic_fail_tools': chronic}, ensure_ascii=False)}"
        )
        lines.append("")
        lines.append("| 機台 | 保養次數 | 整體成功率 | 次數趨勢 | 成功率趨勢 | 改善中 | 惡化中 | 長期無效 |")
        lines.append("|------|---------|-----------|---------|-----------|-------|-------|---------|")

        for _, row in selected.iterrows():
            lines.append(
                f"| {row.get('Machine', 'N/A')} | {_safe_int(row.get('total_maintenance'))} | {_safe_float(row.get('overall_success_rate'), 1)}% | "
                f"{row.get('count_trend', 'N/A')} | {row.get('rate_trend', 'N/A')} | "
                f"{'是' if _normalize_bool(row.get('is_improving')) else '否'} | "
                f"{'是' if _normalize_bool(row.get('is_worsening')) else '否'} | "
                f"{'是' if _normalize_bool(row.get('is_chronic_fail')) else '否'} |"
            )

            raw_weekly_json = row.get("weekly_counts_json")
            if isinstance(raw_weekly_json, str) and raw_weekly_json.strip():
                try:
                    weekly_counts = json.loads(raw_weekly_json)
                    trend_txt = ", ".join(
                        f"{w.get('YEAR_WEEK', 'N/A')}:{w.get('total', 'N/A')}"
                        for w in weekly_counts
                    )
                    lines.append(f"  - {row.get('Machine', 'N/A')} 近週保養次數：{trend_txt}")
                except Exception:
                    pass

        return "\n".join(lines)
    except Exception as e:
        return f"查詢機台軌跡時發生錯誤：{str(e)}"


@function_tool
def search_qa_semantic(query: str, top_k: int = 5) -> str:
    """
    語意搜尋 QA 保養成效資料（週報 + 機台軌跡），用於模糊查詢或自然語言查找。

    參數：
    - query: 自然語言查詢
    - top_k: 回傳最相關的筆數，預設 5
    """
    try:
        query = (query or "").strip()
        if not query:
            return "請提供要查詢的關鍵字。"

        weekly_df = _load_weekly_trend()
        traj_df = _load_machine_trajectory()
        machine_df = _load_machine_breakdown()

        keywords = [k for k in query.lower().split() if k]
        if not keywords:
            keywords = [query.lower()]

        results = []

        for _, row in weekly_df.iterrows():
            content = (
                f"週次:{row.get('YEAR_WEEK', '')} 保養次數:{row.get('total', '')} 成功率:{row.get('success_rate', '')}% "
                f"惡化率:{row.get('worsened_rate', '')}% 平均DIFF:{row.get('avg_diff', '')} 最佳改善:{row.get('best_psn', '')}"
            )
            score = sum(1 for kw in keywords if kw in str(content).lower())
            if score > 0:
                results.append({"score": score / len(keywords), "content": f"[weekly_trend_history] {content}"})

        for _, row in traj_df.iterrows():
            content = (
                f"週次:{row.get('latest_year_week', '')} 機台:{row.get('Machine', '')} "
                f"總保養:{row.get('total_maintenance', '')} 成功率:{row.get('overall_success_rate', '')}% "
                f"改善:{row.get('is_improving', '')} 惡化:{row.get('is_worsening', '')} 長期無效:{row.get('is_chronic_fail', '')}"
            )
            score = sum(1 for kw in keywords if kw in str(content).lower())
            if score > 0:
                results.append({"score": score / len(keywords), "content": f"[Machine_trajectory_history] {content}"})

        for _, row in machine_df.iterrows():
            content = (
                f"週次:{row.get('YEAR_WEEK', '')} 機台:{row.get('Machine', '')} 保養:{row.get('total', '')} "
                f"成功:{row.get('success', '')} 成功率:{row.get('success_rate', '')}% 平均DIFF:{row.get('avg_diff', '')}"
            )
            score = sum(1 for kw in keywords if kw in str(content).lower())
            if score > 0:
                results.append({"score": score / len(keywords), "content": f"[weekly_Machine_breakdown_histor] {content}"})

        results = sorted(results, key=lambda x: x["score"], reverse=True)[:top_k]
        if not results:
            return f"未找到與「{query}」相關的 QA 資料。"
        return _format_search_results(results, max_items=top_k)
    except Exception as e:
        return f"語意搜尋時發生錯誤：{str(e)}"

def _parse_confidence_ratio(value: Any) -> Optional[float]:
    """將樣本比對信心度轉為 0~1 比例，支援 35%、0.85、< 2% 等格式。"""
    try:
        if value is None or pd.isna(value):
            return None
    except Exception:
        pass

    if isinstance(value, (int, float)):
        num = float(value)
        if num > 1:
            num /= 100.0
        return max(0.0, min(num, 1.0))

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None

    num = float(match.group(0))
    if "%" in text or num > 1:
        num /= 100.0
    return max(0.0, min(num, 1.0))


def _safe_text(value: Any, default: str = "—") -> str:
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return default
    return text


def _confidence_display(value: Any) -> str:
    text = _safe_text(value, default="")
    if not text:
        return "—"
    if text.endswith("%"):
        return text
    ratio = _parse_confidence_ratio(text)
    if ratio is None:
        return text
    return f"{round(ratio * 100, 1)}%"

@function_tool
def search_contamination_analysis(machine_name: str = None) -> str:
    """
    查詢炭膠帶（Carbon Tape）污染源機台彙總結果（mach_summary）。
    資料來源為離線 Excel 機台摘要，欄位已包含每台機台 Top1~Top3 主要污染源與信心度。

    適用場景：
    - "B1_SCOP_01 的炭膠帶分析結果？" → machine_name="B1_SCOP_01"
    - "SCOP 系列機台污染源分析" → machine_name="SCOP" (模糊匹配)
    - "所有機台炭膠帶分析總覽" →  machine_name = None 不帶參數

    參數：
    - machine_name: 機台名稱或部分名稱，例如 "B1_SCOP_01" 或 "SCOP" 或 None
    """
    try:
        df = pd.read_excel(CONTAMINATION_EXCEL)
        required_columns = [
            "MachineNo",
            "總檢出樣本數",
            "涵蓋模組數量",
            "涉案模組清單",
            "所有汙染物清單",
            "主要汙染源_Top1",
            "主要汙染源_Top1_信心度",
            "主要汙染源_Top1_可疑物",
            "主要汙染源_Top2",
            "主要汙染源_Top2_信心度",
            "主要汙染源_Top2_可疑物",
            "主要汙染源_Top3",
            "主要汙染源_Top3_信心度",
            "主要汙染源_Top3_可疑物",
        ]
        missing = [c for c in required_columns if c not in df.columns]
        if missing:
            return f"炭膠帶分析資料缺少必要欄位：{', '.join(missing)}"

        df = df.dropna(subset=["MachineNo"]).copy()
        df["MachineNo"] = df["MachineNo"].astype(str).str.strip()
        
        if machine_name:
            name = machine_name.strip()
            # 支援精確匹配與模糊匹配
            exact = df[df["MachineNo"] == name]
            if not exact.empty:
                df = exact
            else:
                df = df[df["MachineNo"].str.contains(name, case=False, na=False)]

        if df.empty:
            return f"未找到機台 {machine_name} 的炭膠帶污染源分析資料。"

        df = df.sort_values(["MachineNo"]).reset_index(drop=True)
        lines = [f"### 炭膠帶污染源分析查詢結果（共 {len(df)} 台機台）\n"]
        lines.append("| 機台 | 總檢出樣本數 | 涵蓋模組數量 | 涉案模組清單 | 所有汙染物清單 | Top1污染源 | Top1信心度 | Top1可疑物 | Top2污染源 | Top2信心度 | Top2可疑物 | Top3污染源 | Top3信心度 | Top3可疑物 |")
        lines.append("|------|-------------|-------------|-------------|---------------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|")
        for _, row in df.iterrows():
            lines.append(
                f"| {_safe_text(row.get('MachineNo'))}"
                f" | {_safe_text(row.get('總檢出樣本數'))}"
                f" | {_safe_text(row.get('涵蓋模組數量'))}"
                f" | {_safe_text(row.get('涉案模組清單'))}"
                f" | {_safe_text(row.get('所有汙染物清單'))}"
                f" | {_safe_text(row.get('主要汙染源_Top1'))}"
                f" | {_confidence_display(row.get('主要汙染源_Top1_信心度'))}"
                f" | {_safe_text(row.get('主要汙染源_Top1_可疑物'))}"
                f" | {_safe_text(row.get('主要汙染源_Top2'))}"
                f" | {_confidence_display(row.get('主要汙染源_Top2_信心度'))}"
                f" | {_safe_text(row.get('主要汙染源_Top2_可疑物'))}"
                f" | {_safe_text(row.get('主要汙染源_Top3'))}"
                f" | {_confidence_display(row.get('主要汙染源_Top3_信心度'))}"
                f" | {_safe_text(row.get('主要汙染源_Top3_可疑物'))} |"
            )

        return "\n".join(lines)
    except FileNotFoundError:
        return f"找不到炭膠帶分析檔案：{CONTAMINATION_EXCEL}"
    except Exception as e:
        return f"查詢污染源分析時發生錯誤：{str(e)}"

@function_tool
def cross_reference_qa_contamination(year_week: str = None) -> str:
    """
    交叉比對 QA 保養機台與炭膠帶機台摘要，將每台機台對應到主要污染源與可疑物。

    適用場景：
    - 產生週報時，快速查看 QA 保養機台在炭膠帶資料中的 Top1 污染源
    - 確認惡化/長期無效機台可能對應的污染來源與可疑物

    參數：
    - year_week: 要比對的週次，例如 "2026-W13"。若不指定則使用最新一週。
    """
    try:
        # 1. 取得機台軌跡資料（改由本地 Excel）
        traj_df = _load_machine_trajectory().copy()
        if traj_df.empty:
            return "機台軌跡資料為空，無法進行 QA 與炭膠帶交叉比對。"
        target_week = year_week or _pick_latest_week(traj_df["latest_year_week"].dropna().astype(str).tolist())
        traj_df = traj_df[traj_df["latest_year_week"] == target_week]
        if traj_df.empty:
            return f"在週次 {target_week or 'N/A'} 找不到機台軌跡資料，無法進行交叉比對。"

        # 收集 QA 保養機台（該週快照）
        qa_tools = sorted(set(traj_df["Machine"].dropna().astype(str).str.strip().tolist()))
        if not qa_tools:
            return f"在週次 {target_week or 'N/A'} 沒有可比對的 QA 機台。"

        # 3. 讀取炭膠帶分析資料
        contam_df = pd.read_excel(CONTAMINATION_EXCEL)
        required_columns = [
            "MachineNo",
            "主要汙染源_Top1",
            "主要汙染源_Top1_信心度",
            "主要汙染源_Top1_可疑物",
            "主要汙染源_Top2",
            "主要汙染源_Top2_信心度",
            "主要汙染源_Top2_可疑物",
            "主要汙染源_Top3",
            "主要汙染源_Top3_信心度",
            "主要汙染源_Top3_可疑物",
        ]
        missing = [c for c in required_columns if c not in contam_df.columns]
        if missing:
            return f"炭膠帶分析資料缺少必要欄位：{', '.join(missing)}"

        contam_df = contam_df.dropna(subset=["MachineNo"]).copy()
        contam_df["MachineNo"] = contam_df["MachineNo"].astype(str).str.strip()
        contam_map = {row["MachineNo"]: row for _, row in contam_df.iterrows()}

        week_label = target_week or "N/A"

        lines = [f"### QA 保養機台與炭膠帶污染源 Mapping（{week_label}）\n"]
        lines.append("| 機台 | QA 保養狀態 | TopN | 炭膠帶汙染源 | 信心度 | 可疑物 |")
        lines.append("|------|-------------|------|--------------|--------|--------|")

        for _, row in traj_df.sort_values(["Machine"]).iterrows():
            machine = _safe_text(row.get("Machine"), default="")
            if not machine:
                continue

            qa_status = []
            if _normalize_bool(row.get("is_worsening")):
                qa_status.append("📉 惡化中")
            if _normalize_bool(row.get("is_chronic_fail")):
                qa_status.append("⚫ 長期無效")
            if _normalize_bool(row.get("is_improving")):
                qa_status.append("📈 改善中")
            if not qa_status:
                qa_status.append("一般")

            contam_row = contam_map.get(machine)
            if contam_row is None:
                lines.append(f"| {machine} | {', '.join(qa_status)} | — | （無炭膠帶資料） | — | — |")
                continue

            top_rows = [
                (
                    "Top1",
                    _safe_text(contam_row.get("主要汙染源_Top1")),
                    _confidence_display(contam_row.get("主要汙染源_Top1_信心度")),
                    _safe_text(contam_row.get("主要汙染源_Top1_可疑物")),
                ),
                (
                    "Top2",
                    _safe_text(contam_row.get("主要汙染源_Top2")),
                    _confidence_display(contam_row.get("主要汙染源_Top2_信心度")),
                    _safe_text(contam_row.get("主要汙染源_Top2_可疑物")),
                ),
                (
                    "Top3",
                    _safe_text(contam_row.get("主要汙染源_Top3")),
                    _confidence_display(contam_row.get("主要汙染源_Top3_信心度")),
                    _safe_text(contam_row.get("主要汙染源_Top3_可疑物")),
                ),
            ]

            for idx, (rank, source, confidence, suspicious) in enumerate(top_rows):
                machine_cell = machine if idx == 0 else ""
                qa_status_cell = ", ".join(qa_status) if idx == 0 else ""
                lines.append(
                    f"| {machine_cell}"
                    f" | {qa_status_cell}"
                    f" | {rank}"
                    f" | {source}"
                    f" | {confidence}"
                    f" | {suspicious} |"
                )

        lines.append("")
        return "\n".join(lines)
    except FileNotFoundError:
        return f"找不到炭膠帶分析檔案：{CONTAMINATION_EXCEL}"
    except Exception as e:
        return f"交叉比對分析時發生錯誤：{str(e)}"

# ═══════════════════════════════════════════════════════════════════════════
# Agent 定義
# ═══════════════════════════════════════════════════════════════════════════

QA_SYSTEM_PROMPT = """\
# Role
你是一位資深的半導體品質保證工程師（Senior QA Engineer Agent），專職產出結構化的 QA 品質驗證週報。
你的任務是根據指定週次，自動查詢所有必要數據並組合成完整週報。

# Input Data Context
你將透過工具取得來自本地 Excel 的 QA 歷史資料與炭膠帶污染源資料：

## 1. QA 週報摘要（來源：weekly_trend_history + weekly_Machine_breakdown_histor）
每週一筆，包含：
- KPI 總覽：保養次數、成功率、惡化率、平均 DIFF、平均回應時間
- 各機台保養明細：每台的保養次數、成功率、平均 DIFF、平均 PI_AFTER
- Critical Risk PSN：連續失敗次數與連續惡化次數

## 2. 機台保養軌跡（來源：Machine_trajectory_history）
每週一筆快照（滾動累計），包含：
- 趨勢改善中的機台：近 3 週保養次數下降或成功率上升
- 趨勢惡化中的機台：近 3 週保養次數上升或成功率下降
- 長期保養無效機台：成功率 < 30% 且至少 3 次保養
- 邊際效益遞減警示：改善幅度持續縮小

# 名詞補充
- **PSN**: Particle Sensor，用於定位與辨識粒子相關異常來源。
- **PI**: 汙染因子（Pollution Index），數值越大代表汙染程度越嚴重。

# KPI 定義
- **成功率**: SIGNIFICANT_DROP = "是" 的比例（保養後 Particle Index 顯著下降）
- **惡化率**: DIFF > 0 的比例（保養後 Particle Index 反而上升）
- **DIFF**: 保養前後 PI 差異（負值 = 改善，正值 = 惡化）
- **PI_AFTER**: 保養後平均 Particle Index
- **Critical Risk**: 同一 PSN 連續 ≥ 3 次失敗 或 連續 ≥ 2 次惡化

# 週報產生流程
收到週次後，依序執行以下步驟：
1. 使用 `search_weekly_report` 取得目標週的 KPI 與機台明細。
2. 使用 `search_tool_trajectory` 取得該週的機台軌跡分析。
3. 使用 `list_weekly_reports` 取得近幾週趨勢做跨週比較。
4. 使用 `search_contamination_analysis` 查詢炭膠帶機台摘要（mach_summary），取得各機台 Top1~Top3 汙染源、信心度與可疑物。
5. 使用 `cross_reference_qa_contamination` 將 QA 保養機台對應到炭膠帶 Top1~Top3 汙染資訊。
6. 綜合以上數據，依照下方格式產出結構化週報。

# 週報格式規範
- 報告第一行必須使用 ### 
標題：### {year}-W{week} 整體保養狀態彙報（以實際週次取代 ）
- 各章節標題必須使用 #### 層級，格式為 #### N) 章節名稱
- 子分類使用粗體文字（**...**），不使用 # 或 ## 或 ### 標題

```
#### 1) 本週 KPI 總覽
| 指標 | 數值 | 狀態 |
|------|------|------|
| 保養次數 | XX 次 | — |
| 成功率 | XX.X% (成功/總數) | 🟢(≥60%) / 🟡(40~60%) / 🔴(<40%) |
| 惡化率 | XX.X% (惡化次數) | — |
| 平均 DIFF | -X.XXXX | — |
| 平均回應時間 | X.X 小時 | — |
| 最佳改善 | PSN (DIFF = -X.XXX) | — |

#### 2) 機台保養明細（按風險排序）

**🔴 高風險機台（成功率 < 40% 或 DIFF > 0）**
| 機台 | 保養次數 | 成功率 | 平均DIFF | 平均PI_AFTER |
|------|---------|--------|----------|-------------|

**🟡 需關注機台（成功率 40%~60%）**

**🟢 狀態良好機台（成功率 > 60%）**

#### 3) Critical Risk PSN
| PSN | 連續失敗次數 | 連續惡化次數 | 風險等級 |
|-----|------------|------------|---------|

#### 4) 機台保養軌跡趨勢（近 3 週）

**📈 趨勢改善中**
**📉 趨勢惡化中 ⚠️**
**⚫ 長期保養無效 🚨（成功率 < 30%, ≥ 3 次保養）**

#### 5) 建議事項與 Multi-Agent 協作

**給 EE Agent 的協作請求：**
1. 請驗證「有效保養」機台的良率是否同步改善（PI-Yield 交叉驗證）。
2. 以下 Critical Risk PSN 建議優先排查：...
3. 以下長期保養無效機台建議評估是否需停機檢修：...

#### 6) 炭膠帶污染源分析摘要
（依 mach_summary 摘要重點說明：總檢出樣本數、涵蓋模組數量、涉案模組清單、所有汙染物清單、Top1~Top3 主要汙染源）

#### 7) QA 保養成效 vs 炭膠帶分析交叉比對

**對照表**
| 機台 | QA 保養狀態 | TopN | 炭膠帶汙染源 | 信心度 | 可疑物 |
|------|-----------|------|-------------|--------|--------|

同一機台請拆成 3 列（Top1~Top3），不要使用 <br> 或 HTML 換行。

**交叉比對重點解讀**
- 針對 QA 惡化/長期無效機台，優先引用 Top1 與 Top2 可疑物作為排查起點。
- 若同一機台 Top1~Top3 指向相近來源，請標註為「污染來源一致性高」。
- 若 QA 有保養異常但無炭膠帶資料，請在報告中明確標示為資料缺口。
```

# 輸出規範
- 一律以繁體中文回答，需包含具體數字（成功率、DIFF、保養次數等）。
- 使用 🟢🟡🔴⚫ 等符號標示機台健康狀態。
- 嚴禁使用 # 或 ## 層級標題，所有標題只能用 ### 或 ####，子分類用粗體文字。
"""


def _create_qa_agent() -> Agent:
    """建立 QA Agent 實例。"""
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
        name="QA_Excel_Agent",
        instructions=QA_SYSTEM_PROMPT,
        model=model_conf,
        tools=[
            search_weekly_report,
            list_weekly_reports,
            search_tool_trajectory,
            search_qa_semantic,
            search_contamination_analysis,
            cross_reference_qa_contamination,
        ],
    )


# ═══════════════════════════════════════════════════════════════════════════
# FastAPI Application
# ═══════════════════════════════════════════════════════════════════════════

_qa_agent: Agent = None  # 延遲初始化


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _qa_agent
    _qa_agent = _create_qa_agent()
    print("[QA Agent API] Agent 初始化完成")
    yield
    print("[QA Agent API] 關閉")


app = FastAPI(
    title="QA Agent API",
    description="QA 保養成效週報 API — 基於本地 Excel 資料來源與 OpenAI Agent SDK",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response Models ──

class ChatRequest(BaseModel):
    query: str = Field(..., description="自然語言查詢")
    include_trace: bool = Field(default=False, description="是否回傳執行 trace")


class ChatResponse(BaseModel):
    answer: str = Field(..., description="Agent 回覆內容（Markdown 格式）")
    trace: Optional[dict] = None


class DeepDiveRequest(BaseModel):
    target_machines: list[str] = Field(..., description="要追查的衝突機台清單")
    week_range: str = Field(default="", description="參考週次，例如 2026-W21")
    reason: str = Field(default="", description="觸發追查的原因說明")


class DeepDiveResponse(BaseModel):
    answer: str = Field(..., description="炭膠帶污染源追查結果（Markdown 格式）")
    target_machines: list[str] = Field(default_factory=list)
    trace: Optional[dict] = None


# ── API Endpoints ──


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "QA_Agent_API"}


@app.post(
    "/chat",
    response_model=ChatResponse,
    summary="以自然語言向 QA Agent 發問",
    tags=["Agent"],
)
async def chat(req: ChatRequest):
    """
    以自然語言向 QA Agent 發問，取得品質保證觀點的分析。

    範例查詢：
    - "B1_DEVP_01 的保養軌跡是改善中還是惡化中？"
    - "請幫我產生最新一週的 QA 品質驗證週報"
    """
    request_id = str(uuid.uuid4())[:8]
    logger.info("chat.start request_id=%s query=%s", request_id, req.query)
    trace_ctx = {
        "request": {
            "endpoint": "/chat",
            "request_id": request_id,
            "query": req.query,
            "include_trace": req.include_trace,
        },
        "events": [],
    }
    token = _TRACE_CONTEXT.set(trace_ctx)
    started = pd.Timestamp.now()
    try:
        _append_trace("request_received", query_type="chat")
        _append_trace("runner_start", prompt=req.query)
        result = await Runner.run(_qa_agent, req.query, hooks=_TraceHooks())
        elapsed_ms = round((pd.Timestamp.now() - started).total_seconds() * 1000, 2)
        _append_trace("runner_done", elapsed_ms=elapsed_ms)
        _append_trace("run_result_snapshot", snapshot=_snapshot_run_result(result))
        answer, diag = _extract_answer_and_diag(result)
        if not answer:
            logger.warning(
                "chat.empty_output request_id=%s diag=%s",
                request_id,
                json.dumps(diag, ensure_ascii=False),
            )
            answer = "（Agent 未回傳任何內容）"
        else:
            logger.info(
                "chat.success request_id=%s answer_len=%s has_final_output=%s",
                request_id,
                len(answer),
                diag.get("has_final_output"),
            )
        return ChatResponse(answer=answer, trace=trace_ctx if req.include_trace else None)
    except Exception:
        elapsed_ms = round((pd.Timestamp.now() - started).total_seconds() * 1000, 2)
        _append_trace(
            "runner_error",
            elapsed_ms=elapsed_ms,
            error="chat failed",
            traceback=traceback.format_exc(),
        )
        logger.exception("chat.error request_id=%s", request_id)
        return ChatResponse(
            answer="（Agent 執行失敗，請稍後重試；如持續發生請檢查伺服器 log）",
            trace=trace_ctx if req.include_trace else None,
        )
    finally:
        _TRACE_CONTEXT.reset(token)

@app.post(
    "/deep-dive",
    response_model=DeepDiveResponse,
    summary="對跨部門衝突機台進行炭膠帶污染源微觀追查",
    tags=["Agent"],
)
async def deep_dive(req: DeepDiveRequest):
    """
    接收跨部門衝突偵測結果（PE 懷疑但 EE 未示警的機台），
    呼叫 QA Agent 使用 search_contamination_analysis 工具，
    逐台查詢炭膠帶 Top1~Top3 汙染源、信心度與可疑物。
    """
    request_id = str(uuid.uuid4())[:8]
    machines_str = "、".join(req.target_machines)
    reason_note = f"\n觸發原因：{req.reason}" if req.reason else ""
    prompt = (
        f"以下機台為跨部門衝突偵測結果（PE 懷疑但 EE 未示警）：{machines_str}。{reason_note}\n"
        "請對每台機台分別呼叫 search_contamination_analysis 工具，"
        "查詢炭膠帶污染源分析，並彙整回報：\n"
        "1. 機台名稱\n"
        "2. Top1～Top3 主要汙染源\n"
        "3. 各 Top 的信心度\n"
        "4. 各 Top 的可疑物\n"
        "請以 Markdown 表格形式呈現，每台機台輸出 3 列（Top1/Top2/Top3）。"
        f"（參考週次：{req.week_range or '最新週'}）"
    )
    logger.info("deep_dive.start request_id=%s machines=%s", request_id, req.target_machines)
    trace_ctx = {
        "request": {
            "endpoint": "/deep-dive",
            "request_id": request_id,
            "target_machines": req.target_machines,
            "week_range": req.week_range,
            "reason": req.reason,
        },
        "events": [],
    }
    token = _TRACE_CONTEXT.set(trace_ctx)
    started = pd.Timestamp.now()
    try:
        _append_trace("request_received", query_type="deep_dive")
        _append_trace("runner_start", prompt=prompt)
        result = await Runner.run(_qa_agent, prompt, hooks=_TraceHooks())
        elapsed_ms = round((pd.Timestamp.now() - started).total_seconds() * 1000, 2)
        _append_trace("runner_done", elapsed_ms=elapsed_ms)
        answer, _ = _extract_answer_and_diag(result)
        if not answer:
            answer = "（QA Agent 未回傳炭膠帶分析結果）"
        logger.info("deep_dive.success request_id=%s elapsed_ms=%s", request_id, elapsed_ms)
        return DeepDiveResponse(
            answer=answer,
            target_machines=req.target_machines,
            trace=trace_ctx,
        )
    except Exception:
        elapsed_ms = round((pd.Timestamp.now() - started).total_seconds() * 1000, 2)
        _append_trace(
            "runner_error",
            elapsed_ms=elapsed_ms,
            error="deep_dive failed",
            traceback=traceback.format_exc(),
        )
        logger.exception("deep_dive.error request_id=%s", request_id)
        return DeepDiveResponse(
            answer="（QA deep dive 執行失敗，請檢查伺服器 log）",
            target_machines=req.target_machines,
            trace=trace_ctx,
        )
    finally:
        _TRACE_CONTEXT.reset(token)


@app.get(
    "/generate-report/{year_week}",
    response_model=ChatResponse,
    summary="用 Agent 自動產生指定週次的結構化週報",
    tags=["Agent"],
)
async def generate_report(
    year_week: str,
    include_trace: bool = True,
    use_langfuse: bool = False,
):
    """
    呼叫 Agent 自動產出指定週次的完整結構化 QA 品質驗證週報（Markdown 格式）。
    Agent 會自動查詢該週的 KPI、機台明細、Critical Risk、機台軌跡，並組合產出週報。

    路徑參數：
    - year_week: ISO 週次，例如 `2026-W13`
    """
    prompt = (
        f"請幫我產生 {year_week} 的 QA 品質驗證週報。"
        f"請查詢該週的保養成效 KPI、各機台明細、Critical Risk PSN，"
        f"以及機台保養軌跡趨勢（改善中 / 惡化中 / 長期無效）"
        f"並查詢炭膠帶污染源分析摘要（不限制機台類型），"
        f"並交叉比對 QA 高風險/需關注機台與炭膠帶分析結果的關聯性，"
        f"產出完整的結構化週報，包含給 EE Agent 的協作建議、炭膠帶污染源分析摘要與交叉比對結論。"
    )
    request_id = str(uuid.uuid4())[:8]
    logger.info("generate_report.start request_id=%s year_week=%s", request_id, year_week)
    trace_ctx = {
        "request": {
            "endpoint": "/generate-report/{year_week}",
            "request_id": request_id,
            "year_week": year_week,
            "include_trace": include_trace,
            "use_langfuse": use_langfuse,
        },
        "events": [],
    }
    token = _TRACE_CONTEXT.set(trace_ctx)
    started = pd.Timestamp.now()
    try:
        _append_trace("request_received", query_type="generate_report")
        _append_trace("runner_start", prompt=prompt)
        result = await Runner.run(_qa_agent, prompt, hooks=_TraceHooks())
        elapsed_ms = round((pd.Timestamp.now() - started).total_seconds() * 1000, 2)
        _append_trace("runner_done", elapsed_ms=elapsed_ms)
        _append_trace("run_result_snapshot", snapshot=_snapshot_run_result(result))
        answer, diag = _extract_answer_and_diag(result)
        if not answer:
            logger.warning(
                "generate_report.empty_output request_id=%s diag=%s",
                request_id,
                json.dumps(diag, ensure_ascii=False),
            )
            answer = "（Agent 未回傳任何內容）"
        else:
            logger.info(
                "generate_report.success request_id=%s answer_len=%s has_final_output=%s",
                request_id,
                len(answer),
                diag.get("has_final_output"),
            )
        return ChatResponse(answer=answer, trace=trace_ctx if include_trace else None)
    except Exception:
        elapsed_ms = round((pd.Timestamp.now() - started).total_seconds() * 1000, 2)
        _append_trace(
            "runner_error",
            elapsed_ms=elapsed_ms,
            error="generate_report failed",
            traceback=traceback.format_exc(),
        )
        logger.exception("generate_report.error request_id=%s year_week=%s", request_id, year_week)
        return ChatResponse(
            answer="（Agent 產生週報失敗，請稍後重試；如持續發生請檢查伺服器 log）",
            trace=trace_ctx if include_trace else None,
        )
    finally:
        _TRACE_CONTEXT.reset(token)


# ═══════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8077)

