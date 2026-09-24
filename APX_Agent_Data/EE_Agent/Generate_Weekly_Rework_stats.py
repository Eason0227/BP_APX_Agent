from datetime import datetime
from pathlib import Path
import pandas as pd
import glob
import os

def get_latest_xlsx(folder: str) -> str:
    files = glob.glob(os.path.join(folder, "*.xlsm"))
    if not files:
        raise FileNotFoundError(f"找不到任何 xlsm 檔案：{folder}")
    return max(files, key=os.path.getmtime)


REWORK_EXCEL_PATH = get_latest_xlsx(r"\\khaiotapvp02\AMD\Rework")
# REWORK_EXCEL_PATH = r"\\khaiotapvp02\AMD\Rework\MVL AMD BRCM Customer lot RW rate 0527_(Security C).xlsx"
REWORK_SHEET_NAME = "AMD-PR Daily rework rate-B1"
REWORK_LAYERS = ["PR1", "PR2", "uPad"]

# 執行參數先固定寫死
RUN_YEAR = None
RUN_INPUT_PATH = REWORK_EXCEL_PATH
RUN_SHEET_NAME = REWORK_SHEET_NAME
RUN_OUTPUT_DIR = "./weekly_rework_output"
RUN_WINDOW = 4
RUN_OUTPUT_FILE_NAME = "weekly_rework_report_(Security C).xlsx"


# ============================================================


def _resolve_input_file(input_path: str) -> Path:
    """
    如果輸入是資料夾，抓最新修改時間的 xlsx；如果是檔案則直接使用。
    """
    p = Path(input_path)
    if p.is_dir():
        excel_files = sorted(
            [f for f in p.glob("*.xlsx") if not f.name.startswith("~$")],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        if not excel_files:
            raise FileNotFoundError(f"找不到任何 .xlsx 檔案: {p}")
        return excel_files[0]

    if not p.exists():
        raise FileNotFoundError(f"找不到輸入檔案: {p}")

    return p


def _parse_adi_date(value, fallback_year: int):
    """
    支援 datetime、含年份日期字串、以及像 11/14 的無年份日期字串。
    """
    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text or text.lower() == "nan":
        return pd.NaT

    dt = pd.to_datetime(text, errors="coerce")
    if pd.notna(dt):
        return dt.to_pydatetime()

    dt_no_year = pd.to_datetime(
        f"{fallback_year}/{text}",
        format="%Y/%m/%d",
        errors="coerce",
    )
    if pd.notna(dt_no_year):
        return dt_no_year.to_pydatetime()

    return pd.NaT


def _load_rework_weekly_summary(
    year: int = None,
    input_path: str = REWORK_EXCEL_PATH,
    sheet_name: str = REWORK_SHEET_NAME,
) -> pd.DataFrame:
    """
    讀取 Rework Excel，篩選 Layer∈{PR1, PR2, uPad}，
    以 ISO 週為單位彙整 Wafer Q'ty、particle rework 數量與 rework rate。
    回傳 DataFrame 欄位：
    iso_year, iso_week, year_week, Layer, total_wafers, particle_rework, rework_rate_pct
    """
    if year is None:
        year = datetime.now().year

    excel_file = _resolve_input_file(input_path)

    df = pd.read_excel(
        excel_file,
        sheet_name=sheet_name,
        engine="openpyxl",
    )

    # 欄位名標準化（去空白）
    df.columns = [str(c).strip() for c in df.columns]

    required_cols = ["ADI date", "Layer", "Wafer Q'ty", "particle"]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise ValueError(f"Excel 缺少必要欄位: {missing}")

    # 篩選 Layer
    df = df[df["Layer"].isin(REWORK_LAYERS)].copy()

    # 解析日期
    df["date"] = df["ADI date"].apply(lambda x: _parse_adi_date(x, year))
    df = df.dropna(subset=["date"])

    # ISO 週次
    iso = df["date"].dt.isocalendar()
    df["iso_week"] = iso.week.astype(int)
    df["iso_year"] = iso.year.astype(int)
    df["year_week"] = df["iso_year"].astype(str) + "-W" + df["iso_week"].astype(str).str.zfill(2)

    # 確保數值欄位
    df["Wafer Q'ty"] = pd.to_numeric(df["Wafer Q'ty"], errors="coerce").fillna(0)
    df["particle"] = pd.to_numeric(df["particle"], errors="coerce").fillna(0)

    # 分 Layer 彙整
    grouped = (
        df.groupby(["iso_year", "iso_week", "year_week", "Layer"], as_index=False)
        .agg(total_wafers=("Wafer Q'ty", "sum"), particle_rework=("particle", "sum"))
    )
    grouped["total_wafers"] =  grouped["total_wafers"].replace(0, pd.NA)
    grouped["total_die"] = grouped["total_wafers"] * 21
    grouped["rework_rate_pct"] = (
        grouped["particle_rework"] / grouped["total_die"]
    ).fillna(0).round(2)

    # 依週次排序
    grouped = grouped.sort_values(["iso_year", "iso_week", "Layer"]).reset_index(drop=True)
    return grouped


def _build_reference_metrics(weekly_df: pd.DataFrame, window: int = 4) -> pd.DataFrame:
    """
    每個 Layer 逐週滾動計算你需要的指標：
    - latest_week_rate_pct
    - previous_week_rate_pct
    - wow_diff_pct（本週與上週差異）
    - latest_4w_avg_rate_pct
    - this_week_vs_latest_4w_avg_diff_pct（本週與最近4週平均差異）
    """
    rows = []

    for layer, g in weekly_df.groupby("Layer"):
        g = g.sort_values(["iso_year", "iso_week"]).reset_index(drop=True).copy()
        rates = g["rework_rate_pct"].astype(float)

        g["latest_week_rate_pct"] = rates.round(2)
        g["previous_week_rate_pct"] = rates.shift(1).round(2)
        g["wow_diff_pct"] = (rates - rates.shift(1)).round(2)

        g["latest_4w_avg_rate_pct"] = rates.rolling(window=window, min_periods=1).mean().round(2)
        g["this_week_vs_latest_4w_avg_diff_pct"] = (rates - g["latest_4w_avg_rate_pct"]).round(2)

        rows.append(
            g[
                [
                    "iso_year",
                    "iso_week",
                    "Layer",
                    "latest_week_rate_pct",
                    "previous_week_rate_pct",
                    "wow_diff_pct",
                    "latest_4w_avg_rate_pct",
                    "this_week_vs_latest_4w_avg_diff_pct",
                ]
            ]
        )

    return pd.concat(rows, ignore_index=True)



def main():
    
    year = RUN_YEAR
    input_path = RUN_INPUT_PATH
    sheet_name = RUN_SHEET_NAME
    output_dir = RUN_OUTPUT_DIR
    window = RUN_WINDOW
    output_file_name = RUN_OUTPUT_FILE_NAME

    out_dir = Path(output_dir)
    if not out_dir.is_absolute():
        out_dir = Path(__file__).resolve().parent / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    weekly = _load_rework_weekly_summary(
        year=year,
        input_path=input_path,
        sheet_name=sheet_name,
    )
    report_xlsx = out_dir / output_file_name

    base_cols = [
        "iso_year",
        "iso_week",
        "year_week",
        "Layer",
        "total_die",
        "particle_rework",
        "rework_rate_pct",
    ]

    if report_xlsx.exists():
        try:
            existing_weekly = pd.read_excel(report_xlsx, sheet_name="weekly_summary", engine="openpyxl")
            if set(base_cols).issubset(existing_weekly.columns):
                existing_base = existing_weekly[base_cols].copy()
                weekly = pd.concat([existing_base, weekly], ignore_index=True)
        except Exception:
            # Fallback: if old file is unreadable, use current run data only.
            pass

    weekly = (
        weekly.drop_duplicates(subset=["iso_year", "iso_week", "Layer"], keep="last")
        .sort_values(["iso_year", "iso_week", "Layer"])
        .reset_index(drop=True)
    )

    metrics = _build_reference_metrics(weekly_df=weekly, window=window)
    weekly = weekly.merge(metrics, on=["iso_year", "iso_week", "Layer"], how="left")

    with pd.ExcelWriter(report_xlsx, engine="openpyxl") as writer:
        weekly.to_excel(writer, index=False, sheet_name="weekly_summary")

    print(f"[OK] excel report: {report_xlsx}")


if __name__ == "__main__":
    main()