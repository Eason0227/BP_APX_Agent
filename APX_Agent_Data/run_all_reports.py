"""
run_all_reports.py — 一鍵產生所有 APX Agent 數據報告

模組導入順序：
  [EE] Generate_Weekly_OOB_Fail_rate_status  → 週 OOB Fail Rate 報告 (Excel)
  [EE] Generate_Weekly_Rework_stats          → 週 Rework 統計報告 (Excel)
  [EE] Generate_Weekly_top5_analysis_status  → 週 Top5 機台分析報告 (Excel)
  [PE] Generate_weekly_yield_report          → 週 Yield 報告 (Excel)
  [PE] Generate_Particle_lot_process_info    → Particle Lot 製程資訊 (subprocess)
  [QA] Generate_effect_df                    → PM 保養成效 DataFrame (CSV)
  [QA] Generate_weekly_OOS_PM_effect_report  → 週 OOS PM 成效週報 (JSON / Markdown)

使用方式：
    python run_all_reports.py

日期等執行參數請直接修改下方「參數設定」區塊。
Generate_Particle_lot_process_info.py 無 main()，以 subprocess 方式執行。
"""

import sys
import os
from datetime import datetime
from pathlib import Path

import pandas as pd  # 統一在頂層引入，避免重複 import

# ─────────────────────────────────────────────────────────────────────────────
# 目錄設定
# ─────────────────────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent
EE_DIR   = ROOT_DIR / "EE_Agent"
PE_DIR   = ROOT_DIR / "PE_Agent"
QA_DIR   = ROOT_DIR / "QA_Agent"

# 將各 Agent 目錄加入 sys.path，使 import 可以找到各模組
for _dir in [ROOT_DIR, EE_DIR, PE_DIR, QA_DIR]:
    _p = str(_dir)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ─────────────────────────────────────────────────────────────────────────────
# 參數設定（依需求修改）
# ─────────────────────────────────────────────────────────────────────────────

# [PE] Particle Lot 查詢日期範圍
# PE_DATE_RANGE = (datetime(2026, 6, 11), datetime(2026, 6, 17))

# [QA] Generate_effect_df 查詢範圍
QA_START_DATE = "20260910"   # 起始日 YYYYMMDD
QA_END_DATE   = "20260916"   # 結束日 YYYYMMDD
QA_BUILDING   = "K18-6F"     # 樓層：K18-6F / K18-5F / K18-All

# [QA] 輸出目錄與成效 CSV 路徑（自動由日期組合，通常不須手動修改）
QA_OUTPUT_DIR = str(QA_DIR / "QA_weekly_report_output")
QA_EFFECT_CSV = str(
    QA_DIR / "PSN_PM_effect_data" / f"{QA_START_DATE}_{QA_END_DATE}_effect_df.csv"
)


# ─────────────────────────────────────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────────────────────────────────────

def _step(name: str) -> None:
    """印出帶分隔線的步驟標題。"""
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# EE Agent
# ─────────────────────────────────────────────────────────────────────────────

def run_EE_oob_report() -> None:
    """EE Agent — 週 OOB Fail Rate 報告 (Excel)。"""
    _step("[EE] 週 OOB Fail Rate 報告")
    import Generate_Weekly_OOB_Fail_rate_status as m
    m.main()


def run_EE_rework_report() -> None:
    """EE Agent — 週 Rework 統計報告 (Excel)。"""
    _step("[EE] 週 Rework 統計報告")
    import Generate_Weekly_Rework_stats as m
    m.main()


def run_EE_top5_report() -> None:
    """EE Agent — 週 Top5 機台分析報告 (Excel)。"""
    _step("[EE] 週 Top5 機台分析報告")
    import Generate_Weekly_top5_analysis_status as m
    m.main()


# ─────────────────────────────────────────────────────────────────────────────
# PE Agent
# ─────────────────────────────────────────────────────────────────────────────

def run_PE_yield_report() -> None:
    """PE Agent — 週 Yield 報告 (Excel)。"""
    _step("[PE] 週 Yield 報告")
    import Generate_weekly_yield_report as m

    file_path   = m.get_latest_xlsx(r"\\khaiotapvp02\AMD\Yield")
    export_path = str(PE_DIR / "weekly_yield_report_(Security C).xlsx")
    result_df   = m.build_weekly_yield_report(
        file_path=file_path,
        export_path=export_path,
        # sheet_name="lot Single yield analysis",
        sheet_name="lot merge AEI yield analysis ",
    )
    print(f"[PE] Yield 報告產出: {export_path}")
    # print(result_df.to_string(index=False))


def run_PE_particle_lot() -> None:
    """PE Agent — Particle Lot 製程資訊。"""
    _step("[PE] Particle Lot Process Info")
    import Generate_Particle_lot_process_info as m
    m.main(run_date_range=PE_DATE_RANGE)


# ─────────────────────────────────────────────────────────────────────────────
# QA Agent
# ─────────────────────────────────────────────────────────────────────────────

def run_QA_effect_df() -> None:
    """QA Agent — 計算 PM 保養成效 DataFrame，輸出 CSV。"""
    _step("[QA] 計算 PM 保養成效 DataFrame")
    import Generate_effect_df as m

    effect_df = m.main(QA_START_DATE, QA_END_DATE, QA_BUILDING)
    print(f"[QA] 成效資料輸出完成，共 {len(effect_df)} 筆")


def run_QA_weekly_report() -> None:
    """QA Agent — OOS PM 成效週報（需先執行 run_QA_effect_df）。"""
    _step("[QA] 週 OOS PM 成效週報")

    csv_path = Path(QA_EFFECT_CSV)
    if not csv_path.exists():
        raise FileNotFoundError(
            f"找不到成效 CSV，請確認 run_QA_effect_df() 已成功執行：{QA_EFFECT_CSV}"
        )

    import Generate_weekly_OOS_PM_effect_report as m

    result, trend = m.run_weekly_analysis(str(csv_path), QA_OUTPUT_DIR)
    print(f"[QA] 週報完成，涵蓋 {len(result['weeks_covered'])} 週")

    for _, r in trend.iterrows():
        wow = (
            f" (WoW {'+' if r['success_rate_wow'] > 0 else ''}{r['success_rate_wow']}%)"
            if pd.notna(r["success_rate_wow"])
            else ""
        )
        print(f"  {r['YEAR_WEEK']}: {r['total']:>3} 次 | 成功率 {r['success_rate']:>5.1f}%{wow}")


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    start_time = datetime.now()

    print(f"\n{'#' * 60}")
    print(f"  APX Agent 一鍵報告產生器")
    print(f"  執行時間：{start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  QA 查詢範圍：{QA_START_DATE} ~ {QA_END_DATE} / {QA_BUILDING}")
    print(f"{'#' * 60}")

    # 依序執行，注意 QA 週報依賴 QA effect_df 先輸出
    tasks: list[tuple[str, object]] = [
        ("EE - OOB Fail Rate",       run_EE_oob_report),
        ("EE - Rework Stats",        run_EE_rework_report),
        ("EE - Top5 Analysis",       run_EE_top5_report),
        ("PE - Yield Report",        run_PE_yield_report),
        # ("PE - Particle Lot Info",   run_PE_particle_lot),
        ("QA - Effect DataFrame",    run_QA_effect_df), 
        ("QA - Weekly OOS Report",   run_QA_weekly_report),
    ]

    errors: list[tuple[str, Exception]] = []

    for name, task_fn in tasks:
        try:
            task_fn()
        except Exception as exc:
            errors.append((name, exc))
            print(f"\n[ERROR] {name} 執行失敗：{exc}")

    elapsed = (datetime.now() - start_time).total_seconds()

    print(f"\n{'#' * 60}")
    print(f"  執行完畢（耗時 {elapsed:.1f} 秒）")
    if errors:
        print(f"  以下 {len(errors)} 項執行失敗：")
        for task_name, err in errors:
            print(f"    ✗ {task_name}：{err}")
    else:
        print("  所有任務執行成功！")
    print(f"{'#' * 60}\n")


if __name__ == "__main__":
    main()
