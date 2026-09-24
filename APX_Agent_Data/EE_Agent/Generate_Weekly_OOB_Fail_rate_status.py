import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd
import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils.Get_OOB_result_v2 import load_data_duckdb


def parse_date(date_text: str):
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date format: {date_text}. Use YYYYMMDD or YYYY-MM-DD.")


def normalize_building(building: str):
    if building == "K18-All":
        return ["K18-6F", "K18-5F", "K18-4F"]
    return building


def get_time_column(df: pd.DataFrame) -> str:
    if "time" in df.columns:
        return "time"
    if "timestamp" in df.columns:
        return "timestamp"
    raise ValueError("No time column found. Expected 'time' or 'timestamp'.")


def calculate_weekly_oob_fail_rate(df: pd.DataFrame) -> pd.DataFrame:
    required_cols = {"device_name", "out_of_pi_spec"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        raise ValueError(f"Missing required columns: {sorted(missing_cols)}")

    time_col = get_time_column(df)
    work_df = df.copy()
    work_df[time_col] = pd.to_datetime(work_df[time_col], errors="coerce")
    work_df = work_df.dropna(subset=[time_col])

    work_df["machine_name"] = work_df["device_name"].astype(str).str.split("#").str[0]

    iso = work_df[time_col].dt.isocalendar()
    work_df["iso_year"] = iso.year.astype(int)
    work_df["iso_week"] = iso.week.astype(int)
    work_df["year_week"] = (
        work_df["iso_year"].astype(str)
        + "-W"
        + work_df["iso_week"].astype(str).str.zfill(2)
    )

    weekly = (
        work_df.groupby(["iso_year", "iso_week", "year_week", "machine_name"], as_index=False)
        .agg(
            sample_count=("out_of_pi_spec", "size"),
            oob_count=("out_of_pi_spec", "sum"),
            week_start=(time_col, "min"),
            week_end=(time_col, "max"),
        )
    )

    weekly["oob_fail_rate_pct"] = (weekly["oob_count"] / weekly["sample_count"] * 100).round(4)
    weekly["week_start"] = pd.to_datetime(weekly["week_start"]).dt.strftime("%Y-%m-%d")
    weekly["week_end"] = pd.to_datetime(weekly["week_end"]).dt.strftime("%Y-%m-%d")

    return weekly.sort_values(["iso_year", "iso_week", "machine_name"]).reset_index(drop=True)


def export_weekly_oob_excel(weekly_df: pd.DataFrame, output_path: Path) -> None:
    df_to_write = weekly_df.copy()

    if output_path.exists():
        try:
            existing_df = pd.read_excel(output_path, sheet_name="weekly_machine_oob")
            df_to_write = pd.concat([existing_df, weekly_df], ignore_index=True)
            df_to_write = (
                df_to_write.drop_duplicates(subset=["year_week", "machine_name"], keep="last")
                .sort_values(["iso_year", "iso_week", "machine_name"])
                .reset_index(drop=True)
            )
        except Exception:
            # Fallback: write current run data when existing file cannot be read.
            df_to_write = weekly_df.copy()

    pivot_df = df_to_write.pivot(
        index="machine_name", columns="year_week", values="oob_fail_rate_pct"
    ).sort_index(axis=0)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df_to_write.to_excel(writer, index=False, sheet_name="weekly_machine_oob")
        pivot_df.to_excel(writer, sheet_name="pivot_fail_rate")


def main():
    start_date = datetime(2026, 4, 1)
    end_date = datetime(2026, 5, 1)
    if start_date > end_date:
        raise ValueError("start-date cannot be later than end-date")

    building_filter = normalize_building(["K18-6F"])
    raw_df = load_data_duckdb(start_date, end_date, building_filter)
    if raw_df is None or raw_df.empty:
        raise ValueError("No data loaded in the selected date range/building.")

    weekly_df = calculate_weekly_oob_fail_rate(raw_df)
    if weekly_df.empty:
        raise ValueError("No valid weekly data after preprocessing.")

    output_path = Path(__file__).resolve().parent / (
        f"weekly_oob_fail_rate_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}_(Security C).xlsx"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    export_weekly_oob_excel(weekly_df, output_path)
    print(f"Excel generated: {output_path}")
    print(f"Rows exported: {len(weekly_df)}")


if __name__ == "__main__":
    main()