"""
QA Agent — 保養成效 AI Assistant (Streamlit App)
基於 OpenAI Agent SDK + 本地 Excel 資料來源，
查詢 QA 週報摘要與機台保養軌跡，
產出結構化 QA 品質驗證週報。
"""
import os
import re
import json
import asyncio
import httpx
import pandas as pd
import streamlit as st
import nest_asyncio
from agents import (
    Agent, Runner, function_tool,
    OpenAIResponsesModel, set_tracing_disabled, AsyncOpenAI,
)

# Langfuse 可觀測性
LANGFUSE_AVAILABLE = False
_lf_observe = None
try:
    from langfuse import Langfuse, observe as _lf_observe
    from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI
    LANGFUSE_AVAILABLE = True
except ImportError:
    pass

try:
    nest_asyncio.apply()
except Exception:
    pass

# ── 全域設定 ──
QA_HISTORY_EXCEL = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\QA_Agent\QA_weekly_report_output\history_tables\qa_weekly_history_(Security C).xlsx"
WEEKLY_TREND_SHEET = "weekly_trend_history"
TRAJECTORY_SHEET = "Machine_trajectory_history"
MACHINE_BREAKDOWN_SHEET = "weekly_Machine_breakdown_histor"

API_BASE_URL  = "http://10.11.33.5:9120/v1"
API_MODEL     = "/model/gpt-oss-120b"

os.environ.setdefault("LANGFUSE_HOST",       "http://10.10.19.36:3000")
os.environ.setdefault("LANGFUSE_PUBLIC_KEY",  "pk-lf-491f2a46-7264-4efd-ba00-39a9310d01e3")
os.environ.setdefault("LANGFUSE_SECRET_KEY",  "sk-lf-40c95f0d-1601-4fa0-9769-ab6190a9b892")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PAGE_ICON_PATH = os.path.join(_BASE_DIR, "assets", "APX_Agent_icon.png")

set_tracing_disabled(True)

# 炭膠帶污染源分析 Excel 檔案路徑
CONTAMINATION_EXCEL = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\QA_Agent\20260520_CarbonTape_summary_(Security C).xlsx"


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


def _pick_latest_week(weeks: list) -> str:
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


def _parse_confidence_ratio(value) -> "float | None":
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


def _safe_text(value, default: str = "—") -> str:
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return default
    return text


def _confidence_display(value) -> str:
    text = _safe_text(value, default="")
    if not text:
        return "—"
    if text.endswith("%"):
        return text
    ratio = _parse_confidence_ratio(text)
    if ratio is None:
        return text
    return f"{round(ratio * 100, 1)}%"


# ═══════════════════════════════════════════════════════════════════════════
# QA Agent 專用工具
# ═══════════════════════════════════════════════════════════════════════════

@function_tool
def search_weekly_report(year_week: str) -> str:
    """
    以 ISO 週次精確查詢該週的 QA 保養成效摘要，包含 KPI 總覽、各機台保養明細、Critical Risk PSN。
    適用場景：使用者指定了明確的週次，例如 "幫我查 2026-W13 的保養成效"。

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
    回傳包含 KPI 彙整表格與各週詳細內容。

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
    每週的軌跡快照是基於從第一週到該週的所有累計數據計算。

    適用場景：
    - "2026-W13 有哪些機台趨勢惡化？" → year_week="2026-W13"
    - "B1_DEVP_01 最近保養軌跡如何？" → tool_name="B1_DEVP_01"
    - "目前有哪些長期保養無效的機台？" → 不帶參數，回傳最新一筆

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

        result_lines = []
        result_lines.append(
            f"[Metadata] {json.dumps({'year_week': selected.iloc[0].get('latest_year_week', 'N/A'), 'improving_count': len(improving), 'worsening_count': len(worsening), 'chronic_fail_count': len(chronic), 'improving_tools': improving, 'worsening_tools': worsening, 'chronic_fail_tools': chronic}, ensure_ascii=False)}"
        )
        result_lines.append("")
        result_lines.append("| 機台 | 保養次數 | 整體成功率 | 次數趨勢 | 成功率趨勢 | 改善中 | 惡化中 | 長期無效 |")
        result_lines.append("|------|---------|-----------|---------|-----------|-------|-------|---------|")

        for _, row in selected.iterrows():
            result_lines.append(
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
                    result_lines.append(f"  - {row.get('Machine', 'N/A')} 近週保養次數：{trend_txt}")
                except Exception:
                    pass

        if tool_name and len(selected) > 1:
            summary = [f"\n{'=' * 60}", f"機台 {tool_name} 的狀態變化歷程："]
            for _, p in selected.iterrows():
                week = p.get("latest_year_week", "N/A")
                status = []
                if _normalize_bool(p.get("is_improving")):
                    status.append("📈 改善中")
                if _normalize_bool(p.get("is_worsening")):
                    status.append("📉 惡化中")
                if _normalize_bool(p.get("is_chronic_fail")):
                    status.append("⚫ 長期無效")
                summary.append(f"  {week}: {', '.join(status) if status else '——'}")
            result_lines.append("\n".join(summary))

        return "\n".join(result_lines)
    except Exception as e:
        return f"查詢機台軌跡時發生錯誤：{str(e)}"


@function_tool
def search_qa_semantic(query: str, top_k: int = 5) -> str:
    """
    語意搜尋 QA 保養成效資料（週報 + 機台軌跡），用於模糊查詢或自然語言查找。
    適用場景：
    - "哪幾週 B1_DEVP_01 保養成功率最低？"
    - "COAT 系列機台最近保養成效如何？"
    - "Critical Risk 連續失敗超過 3 次的 PSN"

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


@function_tool
def search_contamination_analysis(machine_name: str = None) -> str:
    """
    查詢炭膠帶（Carbon Tape）污染源機台彙總結果（mach_summary）。
    資料來源為離線 Excel 機台摘要，欄位已包含每台機台 Top1~Top3 主要污染源與信心度。

    適用場景：
    - "B1_SCOP_01 的炭膠帶分析結果？" → machine_name="B1_SCOP_01"
    - "SCOP 系列機台污染源分析" → machine_name="SCOP" (模糊匹配)
    - "所有機台炭膠帶分析總覽" → machine_name = None 不帶參數

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
        lines.append("|------|-------------|-------------|-------------|--------------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|")
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
        # 1. 取得機台軌跡資料（本地 Excel）
        traj_df = _load_machine_trajectory().copy()
        if traj_df.empty:
            return "機台軌跡資料為空，無法進行 QA 與炭膠帶交叉比對。"
        target_week = year_week or _pick_latest_week(traj_df["latest_year_week"].dropna().astype(str).tolist())
        traj_df = traj_df[traj_df["latest_year_week"] == target_week]
        if traj_df.empty:
            return f"在週次 {target_week or 'N/A'} 找不到機台軌跡資料，無法進行交叉比對。"

        # 2. 讀取炭膠帶分析資料
        contam_df = pd.read_excel(CONTAMINATION_EXCEL)
        required_columns = [
            "MachineNo",
            "主要汙染源_Top1", "主要汙染源_Top1_信心度", "主要汙染源_Top1_可疑物",
            "主要汙染源_Top2", "主要汙染源_Top2_信心度", "主要汙染源_Top2_可疑物",
            "主要汙染源_Top3", "主要汙染源_Top3_信心度", "主要汙染源_Top3_可疑物",
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
                ("Top1", _safe_text(contam_row.get("主要汙染源_Top1")), _confidence_display(contam_row.get("主要汙染源_Top1_信心度")), _safe_text(contam_row.get("主要汙染源_Top1_可疑物"))),
                ("Top2", _safe_text(contam_row.get("主要汙染源_Top2")), _confidence_display(contam_row.get("主要汙染源_Top2_信心度")), _safe_text(contam_row.get("主要汙染源_Top2_可疑物"))),
                ("Top3", _safe_text(contam_row.get("主要汙染源_Top3")), _confidence_display(contam_row.get("主要汙染源_Top3_信心度")), _safe_text(contam_row.get("主要汙染源_Top3_可疑物"))),
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
# QA Agent 初始化
# ═══════════════════════════════════════════════════════════════════════════

QA_SYSTEM_PROMPT = """\
# Role
你是一位資深的半導體品質保證工程師（Senior QA Engineer Agent）。
你的專長是分析機台保養成效（Particle Index 改善率），追蹤機台保養軌跡趨勢，並產出結構化的 QA 品質驗證週報。

你同時負責：
- 單週保養成效分析
- 跨週趨勢比較（WoW 變化）
- 機台保養軌跡追蹤（改善中 / 惡化中 / 長期無效）
- Critical Quality Risk 偵測
- Multi-Agent 協作（產出建議給 EE Agent）

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

# Workflow

## 【情境 A：單週查詢】
使用者指定明確週次 → 使用 `search_weekly_report` 取得完整報告。

## 【情境 B：週報產生 (Weekly Report)】
1. 使用 `search_weekly_report` 取得目標週的 KPI 與機台明細。
2. 使用 `search_tool_trajectory` 取得該週的機台軌跡分析。
3. 若需要跨週比較，使用 `list_weekly_reports` 取得多週趨勢。
4. 使用 `search_contamination_analysis` 查詢炭膠帶機台摘要，取得各機台 Top1~Top3 汙染源、信心度與可疑物。
5. 使用 `cross_reference_qa_contamination` 將 QA 保養機台對應到炭膠帶 Top1~Top3 汙染資訊。
6. 綜合以上數據，依照以下格式產出結構化週報：

```
# QA 品質驗證週報 — {year_week} ({date_range})

## 一、本週 KPI 總覽
| 指標 | 數值 | 狀態 |
|------|------|------|
| 保養次數 | XX 次 | — |
| 成功率 | XX.X% (成功/總數) | 🟢(≥60%) / 🟡(40~60%) / 🔴(<40%) |
| 惡化率 | XX.X% (惡化次數) | — |
| 平均 DIFF | -X.XXXX | — |
| 平均回應時間 | X.X 小時 | — |
| 最佳改善 | PSN (DIFF = -X.XXX) | — |

## 二、機台保養明細（按風險排序）

### 🔴 高風險機台（成功率 < 40% 或 DIFF > 0）
| 機台 | 保養次數 | 成功率 | 平均DIFF | 平均PI_AFTER |
|------|---------|--------|----------|-------------|

### 🟡 需關注機台（成功率 40%~60%）

### 🟢 狀態良好機台（成功率 > 60%）

## 三、Critical Risk PSN
| PSN | 連續失敗次數 | 連續惡化次數 | 風險等級 |
|-----|------------|------------|---------|

## 四、機台保養軌跡趨勢（近 3 週）

### 📈 趨勢改善中
### 📉 趨勢惡化中 ⚠️
### ⚫ 長期保養無效 🚨（成功率 < 30%, ≥ 3 次保養）

## 五、建議事項與 Multi-Agent 協作

### 給 EE Agent 的協作請求：
1. 請驗證「有效保養」機台的良率是否同步改善（PI-Yield 交叉驗證）。
2. 以下 Critical Risk PSN 建議優先排查：...
3. 以下長期保養無效機台建議評估是否需停機檢修：...

## 六、炭膠帶污染源分析摘要
（依 mach_summary 摘要重點說明各機台 Top1~Top3 主要汙染源與可疑物）

## 七、QA 保養成效 vs 炭膠帶分析交叉比對

### 對照表
| 機台 | QA 保養狀態 | TopN | 炭膠帶汙染源 | 信心度 | 可疑物 |
|------|-----------|------|-------------|--------|--------|

### 交叉比對結論
- 針對 QA 惡化/長期無效機台，優先引用 Top1 與 Top2 可疑物作為排查起點。
- 若同一機台 Top1~Top3 指向相近來源，請標註為「污染來源一致性高」。
- 若 QA 有保養異常但無炭膠帶資料，請標示為資料缺口。
```

## 【情境 C：特定機台追蹤】
使用 `search_tool_trajectory` 帶入 tool_name 查詢機台的跨週狀態變化歷程。

## 【情境 D：模糊查詢】
使用 `search_qa_semantic` 語意搜尋。

## 【情境 E：炭膠帶污染源分析】
使用 `search_contamination_analysis` 查詢機台的炭膠帶污染源分析結果（mach_summary 格式）。
- 可查詢單一機台（如 "B1_SCOP_01"）或整個系列（如 "SCOP"）。
- 資料包含各機台 Top1~Top3 主要污染源、信心度與可疑物。
- 產出週報時，**必須**在週報尾部新增「## 六、炭膠帶污染源分析摘要」章節。

## 【情境 F：QA 與炭膠帶交叉比對】
使用 `cross_reference_qa_contamination` 交叉比對保養成效與炭膠帶污染源分析結果。
- 自動從機台軌跡擷取惡化中/長期無效機台，並對照炭膠帶 Top1~Top3 汙染資訊。
- 產出週報時，**必須**在週報新增「## 七、QA 保養成效 vs 炭膠帶分析交叉比對」章節。

# 工具使用指引
1. 使用者指定明確週次 → 使用 `search_weekly_report`
2. 要求產出週報或查看多週趨勢 → 先 `search_weekly_report` 取得當週，再 `search_tool_trajectory` 取得軌跡，再 `search_contamination_analysis` 取得炭膠帶分析，再 `cross_reference_qa_contamination` 做交叉比對，視需要 `list_weekly_reports`
3. 詢問特定機台保養趨勢 → `search_tool_trajectory`（帶 tool_name）
4. 模糊查詢或關鍵字查詢 → `search_qa_semantic`
5. 詢問炭膠帶/污染源分析 → `search_contamination_analysis`
6. 要求比較保養成效與炭膠帶分析的關聯 → `cross_reference_qa_contamination`

一律以繁體中文回答，需包含具體數字（成功率、DIFF、保養次數等）。
使用 🟢🟡🔴⚫ 等符號標示機台健康狀態。
"""


def create_qa_agent(use_langfuse: bool = False) -> Agent:
    """建立並回傳 QA Excel Agent 實例。"""
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

    qa_agent = Agent(
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
    return qa_agent

# ═══════════════════════════════════════════════════════════════════════════
# Streamlit UI
# ═══════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="APX QA Agent",
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

st.markdown('<div class="main-header">APX QA Agent</div>', unsafe_allow_html=True)
st.caption(
    "代理品質保證工程師的 AI Agent Assistant— 負責機台保養成效分析、週報產出、汙染源分析"
)

# 初始化 Session State
if "qa_agent" not in st.session_state:
    with st.spinner("初始化 QA Agent ..."):
        st.session_state.qa_agent = create_qa_agent(use_langfuse=LANGFUSE_AVAILABLE)

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

if "preset_query" not in st.session_state:
    st.session_state.preset_query = ""

# 側邊欄
with st.sidebar:
    st.header("快速查詢")
    preset_map = {
        "產生本週 QA 保養週報": (
            "請幫我產生 2026-W13 的 QA 品質驗證週報。"
            "請查詢該週的保養成效 KPI、各機台明細、Critical Risk PSN，"
            "以及機台保養軌跡趨勢（改善中 / 惡化中 / 長期無效），"
            "並產出完整的結構化週報，包含給 EE Agent 的協作建議。"
        ),
        "查看跨週 KPI 趨勢 (W06~W13)": (
            "列出 2026-W06 到 2026-W13 之間所有週次的 QA 保養成效 KPI 趨勢，"
            "比較各週的成功率、惡化率、平均 DIFF 變化。"
        ),
        "查詢 B1_DEVP_01 保養軌跡": (
            "幫我查 B1_DEVP_01 這台機台的歷史保養軌跡，"
            "它在各週的趨勢狀態（改善/惡化/長期無效）變化如何？"
        ),
        "SCOP 機台炭膠帶污染源分析": (
            "請幫我分析 SCOP 系列機台（B1_SCOP_01~09）的炭膠帶 SEM-EDS 污染源分析結果，"
            "包含各機台各模組的污染物鑑定、污染類型分布、跨機台共通污染警示與高風險機台清單。"
        ),
        "長期保養無效機台清單": (
            "目前有哪些機台是長期保養無效的（成功率 < 30% 且至少 3 次保養）？"
            "請列出清單並提供建議。"
        ),
        "Critical Risk PSN 查詢": (
            "幫我查詢最新一週有哪些 Critical Risk PSN（連續失敗 ≥ 3 次或連續惡化 ≥ 2 次），"
            "並分析其風險程度。"
        ),
        "SCOP 機台炭膠帶污染源分析": (
            "請幫我分析 SCOP 系列機台（B1_SCOP_01~09）的炭膠帶 SEM-EDS 污染源分析結果，"
            "包含各機台各模組的污染物鑑定、污染類型分布、跨機台共通污染警示與高風險機台清單。"
        ),
    }
    for label, query in preset_map.items():
        if st.button(label, use_container_width=True):
            st.session_state.preset_query = query

    st.divider()

    with st.expander("可用工具清單"):
        tools_info = [
            ("search_weekly_report",   "以週次精確查詢該週保養成效摘要與 KPI"),
            ("list_weekly_reports",     "列出多週保養 KPI 彙整，用於趨勢比較"),
            ("search_tool_trajectory", "查詢機台保養軌跡（改善/惡化/長期無效）"),
            ("search_qa_semantic",     "語意搜尋 QA 保養成效資料"),
            ("search_contamination_analysis", "查詢炭膠帶 SEM-EDS 污染源分析"),
            ("cross_reference_qa_contamination", "交叉比對 QA 成效與炭膠帶分析"),
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
        with st.chat_message("assistant", avatar="🔧"):
            st.markdown(msg["content"])

# 輸入處理
user_input = st.chat_input(
    "詢問 QA Agent（例如：幫我產生 2026-W13 的保養週報）"
)

if st.session_state.preset_query:
    user_input = st.session_state.preset_query
    st.session_state.preset_query = ""

# Agent 執行
if user_input:
    st.session_state.chat_history.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.write(user_input)

    with st.chat_message("assistant", avatar="🔧"):
        response_placeholder = st.empty()
        with st.spinner("QA Agent 分析中 ..."):
            try:
                _lf_client = None
                if st.session_state.get("langfuse_on") and LANGFUSE_AVAILABLE:
                    _lf_client = Langfuse()

                async def _run_agent(query: str) -> str:
                    result = await Runner.run(st.session_state.qa_agent, query)
                    return result.final_output or ""

                if st.session_state.get("langfuse_on") and LANGFUSE_AVAILABLE:
                    @_lf_observe(name="QA_Agent_Query")
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
                    response = "（Agent 未回傳任何內容，請確認 LLM 與 Qdrant 連線是否正常）"

                if _lf_client:
                    _lf_client.flush()

            except Exception as e:
                import traceback
                traceback.print_exc()
                response = (
                    f"**執行錯誤：** {str(e)}\n\n"
                    f"```\n{traceback.format_exc()}\n```"
                )

        response_placeholder.markdown(response)
        st.session_state.chat_history.append(
            {"role": "assistant", "content": response}
        )
