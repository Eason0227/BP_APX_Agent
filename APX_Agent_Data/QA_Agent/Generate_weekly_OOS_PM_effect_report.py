"""
QA Agent — 週報分析腳本 (逐週分析 + 跨週趨勢版)
功能：讀取 2/1~4/14 的保養成效 CSV，按週拆分分析，產出：
  1. 每週獨立分析
  2. 跨週趨勢比較（WoW 變化）
  3. 機台保養軌跡追蹤（改善中 / 惡化中 / 長期無效）
  4. 特殊發現（有效方法、邊際遞減、Critical Risk）

時間來源：從 EVENT_ID 尾端 14 碼萃取 OOS 事件發生時間。
未來用途：結果供 OpenAI SDK Agent 調用。
"""

import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from typing import Optional


HISTORY_FILE_PREFIX = "qa_weekly_history_(Security C)"
# ═══════════════════════════════════════════════════════════════════════════
# 1. 資料載入與前處理
# ═══════════════════════════════════════════════════════════════════════════

def load_data(csv_path: str) -> pd.DataFrame:
    """載入 CSV，從 EVENT_ID 萃取事件時間，建立週次欄位。"""
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df["MAINTENANCE_START"] = pd.to_datetime(df["MAINTENANCE_START"])
    df["MAINTENANCE_END"] = pd.to_datetime(df["MAINTENANCE_END"])

    # 從 EVENT_ID 尾端 14 碼萃取 OOS 事件發生時間
    df["EVENT_TIME"] = pd.to_datetime(
        df["EVENT_ID"].str.extract(r"(\d{14})$")[0], format="%Y%m%d%H%M%S"
    )
    # ISO 週次（以事件時間為準）
    iso = df["EVENT_TIME"].dt.isocalendar()
    df["WEEK"] = iso.week.astype(int)
    df["YEAR"] = iso.year.astype(int)
    df["YEAR_WEEK"] = df["YEAR"].astype(str) + "-W" + df["WEEK"].astype(str).str.zfill(2)

    # 從 PSN 萃取機台名稱（# 前面的部分）
    df["Machine"] = df["PSN"].str.split("#").str[0]

    # 保養回應時間（小時）= 保養開始 - 事件發生
    response = (df["MAINTENANCE_START"] - df["EVENT_TIME"]).dt.total_seconds() / 3600
    df["RESPONSE_HOURS"] = response.where(response >= 0).round(1)  # 負值（追溯登記）排除

    return df


# ═══════════════════════════════════════════════════════════════════════════
# 2. 單週 KPI 計算
# ═══════════════════════════════════════════════════════════════════════════

def calc_week_kpi(week_df: pd.DataFrame) -> dict:
    """計算單一週的所有 KPI。"""
    total = len(week_df)
    if total == 0:
        return {"total": 0}

    success = int((week_df["SIGNIFICANT_DROP"] == "是").sum())
    worsened = int((week_df["DIFF"] > 0).sum())
    success_df = week_df[week_df["SIGNIFICANT_DROP"] == "是"]

    kpi = {
        "total_maintenance": total,
        "success_count": success,
        "success_rate": round(success / total * 100, 1),
        "worsened_count": worsened,
        "worsened_rate": round(worsened / total * 100, 1),
        "avg_diff_all": round(week_df["DIFF"].mean(), 4),
        "avg_response_hours": round(week_df["RESPONSE_HOURS"].mean(), 1),
    }

    if not success_df.empty:
        best_idx = success_df["DIFF"].idxmin()
        kpi["avg_diff_success"] = round(success_df["DIFF"].mean(), 4)
        kpi["best_improvement"] = round(success_df.loc[best_idx, "DIFF"], 4)
        kpi["best_psn"] = success_df.loc[best_idx, "PSN"]
    else:
        kpi["avg_diff_success"] = None
        kpi["best_improvement"] = None
        kpi["best_psn"] = None

    return kpi


def calc_week_user_performance(week_df: pd.DataFrame) -> list[dict]:
    """單週人員績效。"""
    grouped = week_df.groupby("MAINTENANCE_USER").agg(
        total=("EVENT_ID", "count"),
        success=("SIGNIFICANT_DROP", lambda x: (x == "是").sum()),
        avg_diff=("DIFF", "mean"),
    ).reset_index()
    grouped["success_rate"] = round(grouped["success"] / grouped["total"] * 100, 1)
    grouped["avg_diff"] = grouped["avg_diff"].round(4)
    return grouped.sort_values("success_rate", ascending=False).to_dict(orient="records")


def calc_week_method_effectiveness(week_df: pd.DataFrame) -> list[dict]:
    """單週保養方法有效性。"""
    grouped = week_df.groupby("MAINTENANCE_CONTENT").agg(
        total=("EVENT_ID", "count"),
        success=("SIGNIFICANT_DROP", lambda x: (x == "是").sum()),
        avg_diff=("DIFF", "mean"),
    ).reset_index()
    grouped["success_rate"] = round(grouped["success"] / grouped["total"] * 100, 1)
    grouped["avg_diff"] = grouped["avg_diff"].round(4)
    return grouped.sort_values("success_rate", ascending=False).to_dict(orient="records")


def calc_week_Machine_breakdown(week_df: pd.DataFrame) -> list[dict]:
    """單週各機台保養統計。"""
    grouped = week_df.groupby("Machine").agg(
        psn_count=("PSN", "nunique"),
        total=("EVENT_ID", "count"),
        success=("SIGNIFICANT_DROP", lambda x: (x == "是").sum()),
        avg_diff=("DIFF", "mean"),
        avg_pi_after=("AVG_PI_AFTER", "mean"),
    ).reset_index()
    grouped["success_rate"] = round(grouped["success"] / grouped["total"] * 100, 1)
    grouped["avg_diff"] = grouped["avg_diff"].round(4)
    grouped["avg_pi_after"] = grouped["avg_pi_after"].round(4)
    return grouped.sort_values("total", ascending=False).to_dict(orient="records")


def flatten_weekly_details(weekly_details: dict, report_date: str) -> dict[str, pd.DataFrame]:
    """將各週分析結果展平成可累積的歷史表。"""
    user_rows = []
    method_rows = []
    Machine_rows = []

    for year_week, detail in weekly_details.items():
        for row in detail["user_performance"]:
            user_rows.append({"report_date": report_date, "YEAR_WEEK": year_week, **row})

        for row in detail["method_effectiveness"]:
            method_rows.append({"report_date": report_date, "YEAR_WEEK": year_week, **row})

        for row in detail["Machine_breakdown"]:
            Machine_rows.append({"report_date": report_date, "YEAR_WEEK": year_week, **row})

    return {
        "weekly_user_performance_history": pd.DataFrame(user_rows),
        "weekly_method_effectiveness_history": pd.DataFrame(method_rows),
        "weekly_Machine_breakdown_history": pd.DataFrame(Machine_rows),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 3. 跨週趨勢分析
# ═══════════════════════════════════════════════════════════════════════════

def calc_weekly_trend(df: pd.DataFrame) -> pd.DataFrame:
    """按週彙總 KPI，產出唯一的週級主表。"""
    weeks = sorted(df["YEAR_WEEK"].unique())
    rows = []
    for w in weeks:
        wdf = df[df["YEAR_WEEK"] == w]
        total = len(wdf)
        success = (wdf["SIGNIFICANT_DROP"] == "是").sum()
        worsened = (wdf["DIFF"] > 0).sum()
        success_df = wdf[wdf["SIGNIFICANT_DROP"] == "是"]
        best_idx = success_df["DIFF"].idxmin() if not success_df.empty else None
        rows.append({
            "YEAR_WEEK": w,
            "total": total,
            "success": int(success),
            "success_rate": round(success / total * 100, 1) if total else 0,
            "worsened": int(worsened),
            "worsened_rate": round(worsened / total * 100, 1) if total else 0,
            "avg_diff": round(wdf["DIFF"].mean(), 4),
            "avg_diff_success": round(success_df["DIFF"].mean(), 4) if not success_df.empty else None,
            "avg_pi_before": round(wdf["AVG_PI_BEFORE"].mean(), 4),
            "avg_pi_after": round(wdf["AVG_PI_AFTER"].mean(), 4),
            "avg_response_hours": round(wdf["RESPONSE_HOURS"].mean(), 1),
            "unique_Machines": wdf["Machine"].nunique(),
            "unique_psn": wdf["PSN"].nunique(),
            "best_improvement": round(success_df.loc[best_idx, "DIFF"], 4) if best_idx is not None else None,
            "best_psn": success_df.loc[best_idx, "PSN"] if best_idx is not None else None,
        })
    trend_df = pd.DataFrame(rows)
    # WoW 變化
    trend_df["success_rate_wow"] = trend_df["success_rate"].diff().round(1)
    trend_df["total_wow"] = trend_df["total"].diff()
    return trend_df


def calc_Machine_weekly_trend(df: pd.DataFrame) -> pd.DataFrame:
    """各機台的逐週趨勢（保養次數、成功率）。"""
    grouped = df.groupby(["Machine", "YEAR_WEEK"]).agg(
        total=("EVENT_ID", "count"),
        success=("SIGNIFICANT_DROP", lambda x: (x == "是").sum()),
        avg_diff=("DIFF", "mean"),
    ).reset_index()
    grouped["success_rate"] = round(grouped["success"] / grouped["total"] * 100, 1)
    grouped["avg_diff"] = grouped["avg_diff"].round(4)
    return grouped.sort_values(["Machine", "YEAR_WEEK"])


def analyze_Machine_trajectory(df: pd.DataFrame) -> dict:
    """
    分析各機台的保養軌跡：
    - improving: 近 3 週保養次數下降 or 成功率上升
    - worsening: 近 3 週保養次數上升 or 成功率下降
    - chronic_fail: 累計成功率 < 30% 且至少 3 次保養
    """
    Machine_weekly = calc_Machine_weekly_trend(df)
    weeks = sorted(df["YEAR_WEEK"].unique())

    improving = []
    worsening = []
    chronic_fail = []

    for Machine, group in Machine_weekly.groupby("Machine"):
        if len(group) < 2:
            continue

        Machine_total = group["total"].sum()
        Machine_success = group["success"].sum()
        overall_rate = round(Machine_success / Machine_total * 100, 1) if Machine_total else 0

        # 最近 3 週內有出現的資料
        recent = group[group["YEAR_WEEK"].isin(weeks[-3:])] if len(weeks) >= 3 else group
        counts = recent["total"].tolist()
        rates = recent["success_rate"].tolist()

        count_trend = "decreasing" if len(counts) >= 2 and counts[-1] < counts[0] else (
            "increasing" if len(counts) >= 2 and counts[-1] > counts[0] else "stable"
        )
        rate_trend = "increasing" if len(rates) >= 2 and rates[-1] > rates[0] else (
            "decreasing" if len(rates) >= 2 and rates[-1] < rates[0] else "stable"
        )

        record = {
            "Machine": Machine,
            "total_maintenance": int(Machine_total),
            "overall_success_rate": overall_rate,
            "weekly_counts": group[["YEAR_WEEK", "total"]].to_dict(orient="records"),
            "weekly_rates": group[["YEAR_WEEK", "success_rate"]].to_dict(orient="records"),
            "count_trend": count_trend,
            "rate_trend": rate_trend,
        }

        if count_trend == "decreasing" or rate_trend == "increasing":
            improving.append(record)
        if count_trend == "increasing" or rate_trend == "decreasing":
            worsening.append(record)
        if overall_rate < 30 and Machine_total >= 3:
            chronic_fail.append(record)

    return {
        "improving_Machines": improving,
        "worsening_Machines": worsening,
        "chronic_fail_Machines": chronic_fail,
    }


def flatten_Machine_trajectory(Machine_trajectory: dict, report_date: str, latest_year_week: str) -> pd.DataFrame:
    """將機台軌跡轉成一列一機台的歷史快照，方便 Excel / Agent 持續追蹤。"""
    Machine_records = {}

    for status, key in (
        ("improving", "improving_Machines"),
        ("worsening", "worsening_Machines"),
        ("chronic_fail", "chronic_fail_Machines"),
    ):
        for row in Machine_trajectory.get(key, []):
            Machine = row["Machine"]
            if Machine not in Machine_records:
                Machine_records[Machine] = {
                    "report_date": report_date,
                    "latest_year_week": latest_year_week,
                    "Machine": Machine,
                    "total_maintenance": row["total_maintenance"],
                    "overall_success_rate": row["overall_success_rate"],
                    "count_trend": row["count_trend"],
                    "rate_trend": row["rate_trend"],
                    "is_improving": False,
                    "is_worsening": False,
                    "is_chronic_fail": False,
                    "weekly_counts_json": json.dumps(row["weekly_counts"], ensure_ascii=False),
                    "weekly_rates_json": json.dumps(row["weekly_rates"], ensure_ascii=False),
                }
            Machine_records[Machine][f"is_{status}"] = True

    records = []
    for record in Machine_records.values():
        status_labels = []
        if record["is_improving"]:
            status_labels.append("improving")
        if record["is_worsening"]:
            status_labels.append("worsening")
        if record["is_chronic_fail"]:
            status_labels.append("chronic_fail")
        record["status_label"] = ",".join(status_labels) if status_labels else "stable"
        records.append(record)

    return pd.DataFrame(records).sort_values(["status_label", "Machine"]) if records else pd.DataFrame()


def detect_marginal_decline(df: pd.DataFrame, min_weeks: int = 3) -> list[dict]:
    """偵測保養邊際效益遞減：同一機台成功保養的改善幅度持續縮小。"""
    success = df[df["SIGNIFICANT_DROP"] == "是"].copy()
    if success.empty:
        return []

    alerts = []
    for Machine, group in success.groupby("Machine"):
        weekly = group.groupby("YEAR_WEEK")["DIFF"].mean().sort_index()
        if len(weekly) < min_weeks:
            continue
        recent = weekly.tail(min_weeks).tolist()
        abs_recent = [abs(v) for v in recent]
        if all(abs_recent[i] > abs_recent[i + 1] for i in range(len(abs_recent) - 1)):
            alerts.append({
                "Machine": Machine,
                "weeks": weekly.tail(min_weeks).index.tolist(),
                "recent_diffs": [round(v, 4) for v in recent],
                "signal": "MARGINAL_BENEFIT_DECLINING",
                "recommendation": "建議 EE Agent 檢查該機台是否有深層硬體老化現象",
            })
    return alerts


def detect_critical_risk(df: pd.DataFrame) -> list[dict]:
    """
    Critical Quality Risk 偵測（以事件時間排序）：
    - 同一 PSN 連續 ≥ 2 次 DIFF > 0（保養後惡化）
    - 同一 PSN 連續 ≥ 3 次 SIGNIFICANT_DROP = "否"
    """
    risks = []
    for psn, group in df.sort_values("EVENT_TIME").groupby("PSN"):
        sig_list = group["SIGNIFICANT_DROP"].tolist()
        diff_list = group["DIFF"].tolist()

        max_consec_fail = _max_consecutive(sig_list, lambda s: s == "否")
        max_consec_worse = _max_consecutive(diff_list, lambda d: d > 0)

        if max_consec_fail >= 3 or max_consec_worse >= 2:
            risks.append({
                "PSN": psn,
                "Machine": group["Machine"].iloc[0],
                "total_maintenance": len(group),
                "consecutive_fail": max_consec_fail,
                "consecutive_worsen": max_consec_worse,
                "last_event": group["EVENT_TIME"].max().strftime("%Y-%m-%d"),
                "risk_level": "CRITICAL",
            })
    return risks


def _max_consecutive(lst, condition_fn) -> int:
    """計算清單中滿足條件的最大連續次數。"""
    max_c = cur = 0
    for item in lst:
        cur = cur + 1 if condition_fn(item) else 0
        max_c = max(max_c, cur)
    return max_c


def find_effective_methods(df: pd.DataFrame) -> list[dict]:
    """找出特別有效的保養方法（成功率 ≥ 70% 且至少 3 次）。"""
    grouped = df.groupby("MAINTENANCE_CONTENT").agg(
        total=("EVENT_ID", "count"),
        success=("SIGNIFICANT_DROP", lambda x: (x == "是").sum()),
        avg_diff=("DIFF", "mean"),
    ).reset_index()
    grouped["success_rate"] = round(grouped["success"] / grouped["total"] * 100, 1)
    effective = grouped[(grouped["success_rate"] >= 70) & (grouped["total"] >= 3)]
    effective = effective.sort_values("success_rate", ascending=False)
    return effective.to_dict(orient="records")


def detect_repeat_maintenance(df: pd.DataFrame, window_days: int = 7) -> list[dict]:
    """偵測同一 PSN 在 window_days 天內被重複保養。"""
    df_sorted = df.sort_values(["PSN", "EVENT_TIME"])
    repeats = []
    for psn, group in df_sorted.groupby("PSN"):
        if len(group) < 2:
            continue
        times = group["EVENT_TIME"].tolist()
        for i in range(1, len(times)):
            gap = (times[i] - times[i - 1]).total_seconds() / 86400
            if gap <= window_days:
                repeats.append({
                    "PSN": psn,
                    "Machine": group["Machine"].iloc[0],
                    "gap_days": round(gap, 1),
                    "total_in_period": len(group),
                })
                break
    return repeats


def _upsert_history(existing_df: Optional[pd.DataFrame], new_df: pd.DataFrame, key_columns: list[str], sort_columns: list[str]) -> pd.DataFrame:
    """將新資料併入歷史表，並用 key 去重保留最新版本。"""
    if existing_df is not None and not existing_df.empty:
        combined = pd.concat([existing_df, new_df], ignore_index=True)
    else:
        combined = new_df.copy()

    if combined.empty:
        return combined

    combined = combined.drop_duplicates(subset=key_columns, keep="last")
    return combined.sort_values(sort_columns).reset_index(drop=True)


def _load_excel_history_sheet(workbook_path: Path, sheet_name: str) -> Optional[pd.DataFrame]:
    """從既有 Excel 工作簿讀取指定工作表，若不存在則回傳 None。"""
    if not workbook_path.exists():
        return None

    try:
        return pd.read_excel(workbook_path, sheet_name=sheet_name)
    except ValueError:
        return None


def _recompute_weekly_wow(history_df: pd.DataFrame) -> pd.DataFrame:
    """依完整歷史週序重算 WoW 欄位，避免跨批次輸出出現空值。"""
    if history_df.empty:
        return history_df

    df = history_df.copy()
    df = df.sort_values(["YEAR_WEEK"]).reset_index(drop=True)
    df["success_rate_wow"] = df["success_rate"].diff().round(1)
    df["total_wow"] = df["total"].diff()
    return df


def export_history_tables(
    output_dir: str,
    weekly_trend: pd.DataFrame,
    weekly_detail_tables: dict[str, pd.DataFrame],
    Machine_trajectory_df: pd.DataFrame,
    critical_risks: list[dict],
    report_date: str,
):
    """輸出單一 Excel 歷史工作簿，並在各工作表中持續累積更新。"""
    history_dir = Path(output_dir) / "history_tables"
    history_dir.mkdir(parents=True, exist_ok=True)
    workbook_path = history_dir / f"{HISTORY_FILE_PREFIX}.xlsx"

    history_tables = {
        "weekly_trend_history": {
            "df": weekly_trend.assign(report_date=report_date),
            "keys": ["YEAR_WEEK"],
            "sort": ["YEAR_WEEK"],
        },
        "Machine_trajectory_history": {
            "df": Machine_trajectory_df,
            "keys": ["latest_year_week", "Machine"],
            "sort": ["latest_year_week", "status_label", "Machine"],
        },
        # "critical_risk_history": {
        #     "df": pd.DataFrame(critical_risks).assign(report_date=report_date) if critical_risks else pd.DataFrame(),
        #     "keys": ["report_date", "PSN"],
        #     "sort": ["report_date", "Machine", "PSN"],
        # },
    }
    history_tables.update({
        name: {
            "df": df,
            "keys": ["report_date", "YEAR_WEEK", *(col for col in df.columns if col in {"MAINTENANCE_USER", "MAINTENANCE_CONTENT", "Machine"})],
            "sort": ["YEAR_WEEK", *(col for col in ("Machine", "MAINTENANCE_USER", "MAINTENANCE_CONTENT") if col in df.columns)],
        }
        for name, df in weekly_detail_tables.items()
    })

    exported = {}
    for table_name, config in history_tables.items():
        table_df = config["df"]
        if table_df.empty:
            exported[table_name] = table_df
            continue

        existing_df = _load_excel_history_sheet(workbook_path, table_name)
        merged_df = _upsert_history(existing_df, table_df, config["keys"], config["sort"])
        if table_name == "weekly_trend_history":
            merged_df = _recompute_weekly_wow(merged_df)
        exported[table_name] = merged_df

    try:
        with pd.ExcelWriter(workbook_path) as writer:
            for table_name, table_df in exported.items():
                if table_df.empty:
                    continue
                table_df.to_excel(writer, sheet_name=table_name[:31], index=False)
        print(f"[QA Agent] 歷史 Excel 已輸出: {workbook_path}")
    except (ImportError, OSError, ValueError, PermissionError) as exc:
        print(f"[QA Agent] 歷史 Excel 輸出失敗: {exc}")

    print(f"[QA Agent] 歷史資料表已輸出至: {history_dir}")
    return exported


# ═══════════════════════════════════════════════════════════════════════════
# 4. Markdown 週報產生器
# ═══════════════════════════════════════════════════════════════════════════

def _trend_arrow(val):
    if val is None or pd.isna(val):
        return ""
    return "↑" if val > 0 else ("↓" if val < 0 else "→")



def generate_weekly_detail_report(year_week: str, week_df: pd.DataFrame) -> str:
    """產出單一週的詳細 Markdown 報告。"""
    kpi = calc_week_kpi(week_df)
    users = calc_week_user_performance(week_df)
    methods = calc_week_method_effectiveness(week_df)
    Machines = calc_week_Machine_breakdown(week_df)

    report_date = datetime.now().strftime("%Y-%m-%d")
    date_range = f"{week_df['EVENT_TIME'].min().strftime('%Y-%m-%d')} ~ {week_df['EVENT_TIME'].max().strftime('%Y-%m-%d')}"

    L = []
    L.append(f"# QA 品質驗證週報 — {year_week} 詳細分析")
    L.append(f"**產出日期**: {report_date}  ")
    L.append(f"**資料範圍**: {date_range}  ")
    L.append(f"**總保養筆數**: {kpi['total_maintenance']}")
    L.append("")
    L.append("---")
    L.append("")

    L.append("## KPI 摘要")
    L.append("")
    L.append(f"- 保養次數: **{kpi['total_maintenance']}** 次")
    L.append(f"- 成功率: **{kpi['success_rate']}%** ({kpi['success_count']}/{kpi['total_maintenance']})")
    L.append(f"- 惡化率: {kpi['worsened_rate']}% ({kpi['worsened_count']} 次)")
    L.append(f"- 平均 DIFF: {kpi['avg_diff_all']}")
    L.append(f"- 平均回應時間: {kpi['avg_response_hours']} 小時")
    if kpi.get("best_psn"):
        L.append(f"- 最佳改善: `{kpi['best_psn']}` (DIFF = {kpi['best_improvement']})")
    L.append("")

    if Machines:
        L.append(f"## 機台明細 ({len(Machines)} 台)")
        L.append("")
        L.append("| 機台 | PSN 數 | 保養次數 | 成功率 | 平均 DIFF | 平均 PI_AFTER |")
        L.append("|------|--------|----------|--------|-----------|---------------|")
        for t in Machines:
            L.append(f"| {t['Machine']} | {t['psn_count']} | {t['total']} | {t['success_rate']}% | {t['avg_diff']} | {t['avg_pi_after']} |")
        L.append("")

    if users:
        L.append(f"## 人員績效 ({len(users)} 人)")
        L.append("")
        L.append("| 人員 | 次數 | 成功率 | 平均 DIFF |")
        L.append("|------|------|--------|-----------|")
        for u in users:
            L.append(f"| {u['MAINTENANCE_USER']} | {u['total']} | {u['success_rate']}% | {u['avg_diff']} |")
        L.append("")

    if methods:
        L.append(f"## 保養方法 ({len(methods)} 種)")
        L.append("")
        L.append("| 方法 | 次數 | 成功率 | 平均 DIFF |")
        L.append("|------|------|--------|-----------|")
        for m in methods:
            L.append(f"| {m['MAINTENANCE_CONTENT']} | {m['total']} | {m['success_rate']}% | {m['avg_diff']} |")
        L.append("")

    L.append("---")
    L.append(f"*本報告由 QA Supervisor Agent 自動產出 | {report_date}*")
    return "\n".join(L)


def generate_markdown_report(
    df: pd.DataFrame,
    weekly_trend: pd.DataFrame,
    Machine_trajectory: dict,
    critical_risks: list,
) -> str:
    """產出以『逐週分析 + 跨週趨勢』為核心的 Markdown 週報。"""
    weeks = sorted(df["YEAR_WEEK"].unique())
    report_date = datetime.now().strftime("%Y-%m-%d")
    data_range = f"{df['EVENT_TIME'].min().strftime('%Y-%m-%d')} ~ {df['EVENT_TIME'].max().strftime('%Y-%m-%d')}"

    L = []

    # ── 標題 ──
    L.append("# QA 品質驗證週報 — 逐週趨勢分析")
    L.append(f"**產出日期**: {report_date}  ")
    L.append(f"**資料範圍**: {data_range}  ")
    L.append(f"**涵蓋週數**: {len(weeks)} 週 ({weeks[0]} ~ {weeks[-1]})  ")
    L.append(f"**總保養筆數**: {len(df)}")
    L.append("")
    L.append("---")
    L.append("")

    # ══════════════════════════════════════════════════════
    # 第一部分：跨週趨勢總覽
    # ══════════════════════════════════════════════════════
    L.append("## 一、跨週 KPI 趨勢總覽")
    L.append("")
    L.append("| 週次 | 保養次數 | WoW | 成功次數 | 成功率 | WoW | 惡化率 | 平均 DIFF | 回應時間(hr) | 狀態 |")
    L.append("|------|----------|-----|----------|--------|-----|--------|-----------|-------------|------|")
    for _, r in weekly_trend.iterrows():
        wow_total = f"{_trend_arrow(r['total_wow'])}{int(r['total_wow']) if pd.notna(r['total_wow']) else ''}"
        wow_rate = f"{_trend_arrow(r['success_rate_wow'])}{r['success_rate_wow'] if pd.notna(r['success_rate_wow']) else ''}"
        L.append(
            f"| {r['YEAR_WEEK']} | {r['total']} | {wow_total} "
            f"| {r['success']} | {r['success_rate']}% | {wow_rate} "
            f"| {r['worsened_rate']}% | {r['avg_diff']} | {r['avg_response_hours']} | "
        )
    L.append("")

    # ══════════════════════════════════════════════════════
    # 第二部分：各週詳細分析（獨立檔案）
    # ══════════════════════════════════════════════════════
    L.append("## 二、各週詳細分析")
    L.append("各週詳細內容已拆分為獨立 Markdown 檔案輸出。")
    L.append("")

    # ══════════════════════════════════════════════════════
    # 第三部分：機台軌跡分析
    # ══════════════════════════════════════════════════════
    L.append("## 三、機台保養軌跡分析（近 3 週趨勢）")
    L.append("")

    if Machine_trajectory["improving_Machines"]:
        L.append("###   趨勢改善中的機台")
        L.append("保養次數下降或成功率上升，機台狀況趨穩。")
        L.append("")
        L.append("| 機台 | 總保養次數 | 整體成功率 | 次數趨勢 | 成功率趨勢 |")
        L.append("|------|-----------|-----------|---------|-----------|")
        for t in Machine_trajectory["improving_Machines"]:
            L.append(f"| {t['Machine']} | {t['total_maintenance']} | {t['overall_success_rate']}% | {t['count_trend']} | {t['rate_trend']} |")
        L.append("")

    if Machine_trajectory["worsening_Machines"]:
        L.append("### 趨勢惡化中的機台")
        L.append("保養次數上升或成功率下降，需特別關注。")
        L.append("")
        L.append("| 機台 | 總保養次數 | 整體成功率 | 次數趨勢 | 成功率趨勢 |")
        L.append("|------|-----------|-----------|---------|-----------|")
        for t in Machine_trajectory["worsening_Machines"]:
            L.append(f"| {t['Machine']} | {t['total_maintenance']} | {t['overall_success_rate']}% | {t['count_trend']} | {t['rate_trend']} |")
        L.append("")

    if Machine_trajectory["chronic_fail_Machines"]:
        L.append("### 長期保養無效機台 (成功率 < 30%, ≥ 3 次保養)")
        L.append("")
        L.append("| 機台 | 總保養次數 | 整體成功率 |")
        L.append("|------|-----------|-----------|")
        for t in Machine_trajectory["chronic_fail_Machines"]:
            L.append(f"| {t['Machine']} | {t['total_maintenance']} | {t['overall_success_rate']}% |")
        L.append("")

    # ══════════════════════════════════════════════════════
    # 第四部分：Critical Risk
    # ══════════════════════════════════════════════════════
    if critical_risks:
        L.append("## 四、  Critical Quality Risk")
        L.append("")
        L.append("| PSN | 機台 | 總保養次數 | 連續失敗 | 連續惡化 | 最後事件 |")
        L.append("|-----|------|-----------|---------|---------|---------|")
        for c in critical_risks:
            L.append(f"| {c['PSN']} | {c['Machine']} | {c['total_maintenance']} | {c['consecutive_fail']} | {c['consecutive_worsen']} | {c['last_event']} |")
        L.append("")
    else:
        L.append("## 四、Critical Quality Risk")
        L.append("本期無 Critical Risk。")
        L.append("")

    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════
# 5. 主流程
# ═══════════════════════════════════════════════════════════════════════════

def run_weekly_analysis(csv_path: str, output_dir: str = None):
    """執行完整逐週分析並輸出結果。"""
    df = load_data(csv_path)
    report_date = datetime.now().strftime("%Y-%m-%d")

    if output_dir is None:
        output_dir = str(Path(csv_path).parent)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    weeks = sorted(df["YEAR_WEEK"].unique())

    # ── 跨週趨勢 ──
    weekly_trend = calc_weekly_trend(df)

    # ── 各週詳細 ──
    weekly_details = {}
    for w in weeks:
        wdf = df[df["YEAR_WEEK"] == w]
        weekly_details[w] = {
            "kpi": calc_week_kpi(wdf),
            "user_performance": calc_week_user_performance(wdf),
            "method_effectiveness": calc_week_method_effectiveness(wdf),
            "Machine_breakdown": calc_week_Machine_breakdown(wdf),
        }
    weekly_detail_tables = flatten_weekly_details(weekly_details, report_date)

    # ── 跨週進階分析 ──
    Machine_trajectory = analyze_Machine_trajectory(df)
    Machine_trajectory_df = flatten_Machine_trajectory(Machine_trajectory, report_date, weeks[-1])
    critical_risks = detect_critical_risk(df)

    # ── 組合分析結果 ──
    analysis_result = {
        "report_date": report_date,
        "data_range": f"{df['EVENT_TIME'].min().strftime('%Y-%m-%d')} ~ "
                      f"{df['EVENT_TIME'].max().strftime('%Y-%m-%d')}",
        "total_records": len(df),
        "weeks_covered": weeks,
        "weekly_trend": weekly_trend.to_dict(orient="records"),
        "weekly_details": weekly_details,
        "history_tables": {
            "excel_workbook": f"history_tables/{HISTORY_FILE_PREFIX}.xlsx",
            "sheets": {
                "weekly_trend": "weekly_trend_history",
                "weekly_user_performance": "weekly_user_performance_history",
                "weekly_method_effectiveness": "weekly_method_effectiveness_history",
                "weekly_Machine_breakdown": "weekly_Machine_breakdown_history",
                "Machine_trajectory": "Machine_trajectory_history",
                "critical_risk": "critical_risk_history",
            },
        },
        "Machine_trajectory": {
            "improving": [t["Machine"] for t in Machine_trajectory["improving_Machines"]],
            "worsening": [t["Machine"] for t in Machine_trajectory["worsening_Machines"]],
            "chronic_fail": [t["Machine"] for t in Machine_trajectory["chronic_fail_Machines"]],
        },
        "critical_quality_risks": critical_risks,
    }

    # ── 輸出 JSON（供 Agent 使用）──
    json_path = Path(output_dir) / f"qa_weekly_report_{report_date}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(analysis_result, f, ensure_ascii=False, indent=2, default=str)
    print(f"[QA Agent] 分析結果已輸出: {json_path}")

    # ── Markdown 週報 ──
    md_report = generate_markdown_report(
        df, weekly_trend, Machine_trajectory,
        critical_risks,
    )
    md_path = Path(output_dir) / f"qa_weekly_report_{report_date}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    print(f"[QA Agent] Markdown 週報已輸出: {md_path}")

    # ── 各週詳細報告（獨立檔案） ──
    weekly_dir = Path(output_dir) / "weekly_details"
    weekly_dir.mkdir(parents=True, exist_ok=True)
    for w in weeks:
        wdf = df[df["YEAR_WEEK"] == w]
        weekly_md = generate_weekly_detail_report(w, wdf)
        weekly_file = weekly_dir / f"qa_weekly_detail_{w}_{report_date}.md"
        with open(weekly_file, "w", encoding="utf-8") as f:
            f.write(weekly_md)
    print(f"[QA Agent] 各週詳細分析已輸出至: {weekly_dir}")

    export_history_tables(
        output_dir=output_dir,
        weekly_trend=weekly_trend,
        weekly_detail_tables=weekly_detail_tables,
        Machine_trajectory_df=Machine_trajectory_df,
        critical_risks=critical_risks,
        report_date=report_date,
    )

    return analysis_result, weekly_trend


# ═══════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys

    csv_file = sys.argv[1] if len(sys.argv) > 1 else r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\QA_Agent\PSN_PM_effect_data\20260520_20260527_effect_df.csv"
    output = sys.argv[2] if len(sys.argv) > 2 else r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\QA_Agent\QA_weekly_report_output"

    result, trend = run_weekly_analysis(csv_file, output)

    print("\n" + "=" * 60)
    print("  QA 品質驗證週報摘要 (逐週趨勢)")
    print("=" * 60)
    print(f"資料範圍: {result['data_range']}")
    print(f"總保養次數: {result['total_records']}")
    print(f"涵蓋週數: {len(result['weeks_covered'])}")
    print()

    print("── 跨週趨勢 ──")
    for _, r in trend.iterrows():
        wow = f" (WoW {'+' if r['success_rate_wow'] > 0 else ''}{r['success_rate_wow']}%)" if pd.notna(r["success_rate_wow"]) else ""
        print(f"  {r['YEAR_WEEK']}: {r['total']:>3} 次 | 成功率 {r['success_rate']:>5.1f}%{wow}")
    print()

    if result["Machine_trajectory"]["chronic_fail"]:
        print(f" 長期無效機台: {result['Machine_trajectory']['chronic_fail']}")
    if result["critical_quality_risks"]:
        print(f"  Critical Risk: {len(result['critical_quality_risks'])} 個 PSN")
