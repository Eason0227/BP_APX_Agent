import re
from datetime import date
from pathlib import Path

import pandas as pd
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils.Get_OOB_result_v2 import load_data_duckdb

# =========================
# 使用者輸入參數（以變數表示）
# =========================
TRACK_DIR = r"D:\Paticle_OOB_system\Particle_OOB_Yield_anaysis\PSN_Yield_tracking_data_rework"
OUTPUT_DIR = Path(r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\EE_Agent\weekly_rework_top5")

START_DATE = date(2026, 7, 22)
END_DATE = date(2026, 9, 16)

# PROCESS: "RDL" 或 "TP"
PROCESS = "RDL"

# Layer 預設全選: 設為 None 或 [] 代表全選
# 若 PROCESS="RDL"，可指定 ["PR1"], ["PR2"], ["uPad"] 或其組合
# 若 PROCESS="TP"，Layer 會自動固定為 ["TP"]
LAYER_SELECTION = None

# 可選：指定機台清單，不篩選請設為 None
MACHINE_FILTER = None

# 是否只保留 Rework_Particle > 0 的 LOT
REWORK_POSITIVE_ONLY = False


def get_thu_wed_week(start_date, end_date):
    # 週數直接使用該段結束日的 ISO 週數
    week_no = int(pd.Timestamp(end_date).isocalendar().week)
    return f"W{week_no:02d}"


def generate_thu_wed_windows(start_date, end_date):
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)

    # 先找到小於等於開始日的最近一個週四
    # Monday=0 ... Thursday=3 ... Sunday=6
    offset = (start_ts.weekday() - 3) % 7
    anchor_thu = start_ts - pd.Timedelta(days=offset)

    windows = []
    current_thu = anchor_thu
    while current_thu <= end_ts:
        current_wed = current_thu + pd.Timedelta(days=6)
        window_start = max(current_thu, start_ts)
        window_end = min(current_wed, end_ts)
        if window_start <= window_end:
            windows.append((window_start.date(), window_end.date()))
        current_thu = current_thu + pd.Timedelta(days=7)

    return windows


def add_run_metadata(df, start_date, end_date):
    out = df.copy()
    out["Stat_Start_Date"] = pd.Timestamp(start_date).strftime("%Y%m%d")
    out["Stat_End_Date"] = pd.Timestamp(end_date).strftime("%Y%m%d")
    out["Week"] = get_thu_wed_week(start_date, end_date)
    return out


def append_to_excel(output_path, new_data):
    if output_path.exists():
        try:
            old_data = pd.read_excel(output_path)
            combined = pd.concat([old_data, new_data], ignore_index=True)
        except Exception:
            combined = new_data.copy()
    else:
        combined = new_data.copy()

    combined.to_excel(output_path, index=False)


def resolve_layers(process_name, layer_selection):
    if process_name.upper() == "TP":
        return ["TP"]

    valid_rdl_layers = ["PR1", "PR2", "uPad"]
    if not layer_selection:
        return valid_rdl_layers

    normalized = [str(x).strip() for x in layer_selection]
    if "全選" in normalized:
        return valid_rdl_layers

    return [x for x in normalized if x in valid_rdl_layers]


def extract_layer(lot_id):
    m = re.search(r"_(PR1|PR2|uPad|TP)(?:_\d+)?$", str(lot_id))
    return m.group(1) if m else None


def filter_df_by_layers(df, lot_col, selected_layers):
    if len(df) == 0:
        return df.copy()
    layer_series = df[lot_col].astype(str).str.extract(r"_(PR1|PR2|uPad|TP)(?:_\d+)?$", expand=False)
    return df[layer_series.isin(selected_layers).values].copy()


def load_and_process_data(track_dir, date_range, use_all_lots=True, top_n=20, machine_filter=None, rework_positive_only=False):
    start_date, end_date = date_range

    track_files = sorted(Path(track_dir).glob("*.xlsx"))
    if not track_files:
        raise FileNotFoundError("資料夾內找不到 Excel 檔案")

    df_list = [pd.read_excel(file) for file in track_files]
    track_df = pd.concat(df_list, ignore_index=True)

    track_df = track_df.dropna(subset=["MACHINE_NO"])
    track_df["ADI date"] = pd.to_datetime(track_df["ADI date"], format="%Y/%m/%d %H:%M:%S", errors="coerce")
    track_df = track_df[track_df["ADI date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))]
    track_df["TRACK_IN_TIME"] = pd.to_datetime(track_df["TRACK_IN_TIME"], format="%Y/%m/%d %H:%M:%S", errors="coerce")
    track_df["TRACK_OUT_TIME"] = pd.to_datetime(track_df["TRACK_OUT_TIME"], format="%Y/%m/%d %H:%M:%S", errors="coerce")
    track_df = track_df.rename(columns={"MACHINE_NO": "Machine ID"})

    df = track_df.drop_duplicates(["LOT_RDL_INSPECTION"]).copy()
    df = df.rename(columns={"LOT_NO": "LOT_ID"})

    lot_stats = df.copy()
    lot_stats.index = df["LOT_RDL_INSPECTION"]
    lot_stats = lot_stats.sort_values("Rework_Particle", ascending=False)

    if machine_filter:
        track_df = track_df[track_df["Machine ID"].isin(machine_filter)].copy()

    if use_all_lots:
        if rework_positive_only:
            lot_stats_filtered = lot_stats[lot_stats["Rework_Particle"] > 0]
        else:
            lot_stats_filtered = lot_stats[lot_stats["Rework_Particle"] >= 0]
        selected_lot_ids = lot_stats_filtered.index.tolist()
        selected_lots = lot_stats_filtered
    else:
        selected_lots = lot_stats.head(top_n)
        selected_lot_ids = selected_lots.index.tolist()

    track_info = track_df[track_df["LOT_RDL_INSPECTION"].isin(selected_lot_ids)].copy()

    return df, lot_stats, selected_lots, track_info, selected_lot_ids


def load_sensor_data(track_df):
    valid_track_in = track_df["TRACK_IN_TIME"].dropna()
    valid_track_out = track_df["TRACK_OUT_TIME"].dropna()

    if len(valid_track_in) == 0 or len(valid_track_out) == 0:
        return None

    min_time = valid_track_in.min()
    max_time = valid_track_out.max()

    if pd.isna(min_time) or pd.isna(max_time):
        return None

    sensor_df = load_data_duckdb(min_time, max_time, "K18-6F")
    if sensor_df is None or len(sensor_df) == 0:
        return None

    sensor_df["timestamp"] = pd.to_datetime(sensor_df["timestamp"])

    relevant_machines = track_df["Machine ID"].unique()
    sensor_df = sensor_df[sensor_df["Machine_ID"].isin(relevant_machines)].copy()

    if len(sensor_df) == 0:
        return None

    return sensor_df.sort_values(["Machine_ID", "timestamp"])


def analyze_sensor_correlation(top_lot_ids, track_info, sensor_df, lot_stats):
    if sensor_df is None:
        return {}

    relevant_track = track_info[track_info["LOT_RDL_INSPECTION"].isin(top_lot_ids)].copy()
    if len(relevant_track) == 0:
        return {}

    lot_stats_dict = {}
    for lot_id in top_lot_ids:
        lot_data = lot_stats[lot_stats["LOT_RDL_INSPECTION"] == lot_id]
        if len(lot_data) > 0:
            lot_stats_dict[lot_id] = {
                "Rework_Particle": lot_data["Rework_Particle"].values[0],
                "ADI_date": lot_data["ADI date"].values[0] if "ADI date" in lot_data.columns else None,
                "Wafer_Qty": lot_data["Wafer Q'ty"].values[0] if "Wafer Q'ty" in lot_data.columns else None,
            }

    results = []
    sensor_grouped = {name: group for name, group in sensor_df.groupby("Machine_ID")}

    for _, track_row in relevant_track.iterrows():
        machine_id = track_row["Machine ID"]
        if machine_id not in sensor_grouped:
            continue

        machine_sensor = sensor_grouped[machine_id]
        mask = (machine_sensor["timestamp"] >= track_row["TRACK_IN_TIME"]) & (
            machine_sensor["timestamp"] <= track_row["TRACK_OUT_TIME"]
        )
        filtered_sensor = machine_sensor.loc[mask]

        if len(filtered_sensor) == 0:
            continue

        total_records = len(filtered_sensor)
        ooc_count = filtered_sensor["out_of_pi_spec"].sum()
        lot_id = track_row["LOT_RDL_INSPECTION"]
        lot_info = lot_stats_dict.get(lot_id, {})

        results.append(
            {
                "LOT_ID": lot_id,
                "ADI_date": str(lot_info.get("ADI_date", "")),
                "OPER": track_row["OPER"],
                "SCHD_NO": track_row.get("SCHD_NO", None),
                "Machine_ID": machine_id,
                "PI_value": filtered_sensor["PI_value"].mean(),
                "OOC_Count": ooc_count,
                "OOC_Rate_%": round((ooc_count / total_records * 100), 2),
                "OOS_Status": filtered_sensor["out_of_OOC_count_spec"].sum(),
                "Interval_Time": total_records,
                "TRACK_IN_TIME": str(track_row["TRACK_IN_TIME"]),
                "TRACK_OUT_TIME": str(track_row["TRACK_OUT_TIME"]),
                "Rework_Particle": lot_info.get("Rework_Particle", 0),
                "Wafer_Qty": track_row.get("Wafer Q'ty", lot_info.get("Wafer_Qty", None)),
            }
        )

    if len(results) == 0:
        return {}

    result_df = pd.DataFrame(results).drop_duplicates()
    lot_sensor_analysis = {}
    for _, row in result_df.iterrows():
        lot_id = row["LOT_ID"]
        lot_sensor_analysis.setdefault(lot_id, []).append(row.to_dict())

    return lot_sensor_analysis


def build_sensor_analysis_df(sensor_analysis, selected_lot_ids, selected_layers):
    df = pd.DataFrame()
    for lot_id in selected_lot_ids:
        if lot_id in sensor_analysis and sensor_analysis[lot_id]:
            df = pd.concat([df, pd.DataFrame(sensor_analysis[lot_id])], ignore_index=True)

    if len(df) == 0:
        return df

    df["Layer"] = df["LOT_ID"].astype(str).str.extract(r"_(PR1|PR2|uPad|TP)(?:_\d+)?$")[0]
    df = df[df["Layer"].isin(selected_layers)].copy()

    for col in ["OOC_Rate_%", "PI_value", "Interval_Time", "Rework_Particle"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["OOC_Rate_%", "PI_value", "Interval_Time", "Rework_Particle"])
    return df


def build_machine_stats(lot_sensor_df):
    if len(lot_sensor_df) == 0:
        return pd.DataFrame()

    if "Wafer_Qty" in lot_sensor_df.columns and "SCHD_NO" in lot_sensor_df.columns:
        wafer_qty_by_machine = (
            lot_sensor_df[["Machine_ID", "SCHD_NO", "Wafer_Qty"]]
            .dropna(subset=["Machine_ID", "SCHD_NO"])
            .drop_duplicates(subset=["Machine_ID", "SCHD_NO"], keep="first")
            .copy()
        )
        wafer_qty_by_machine["Wafer_Qty"] = pd.to_numeric(wafer_qty_by_machine["Wafer_Qty"], errors="coerce").fillna(0)
        wafer_qty_by_machine = wafer_qty_by_machine.groupby("Machine_ID", as_index=False)["Wafer_Qty"].sum()
    else:
        wafer_qty_by_machine = pd.DataFrame(columns=["Machine_ID", "Wafer_Qty"])

    machine_stats = lot_sensor_df.groupby("Machine_ID").agg(
        {
            "OOC_Rate_%": ["mean", "max"],
            "PI_value": ["mean"],
            "Interval_Time": "sum",
            "Rework_Particle": ["mean", "max"],
        }
    ).round(4)

    machine_stats.columns = [
        "Avg_OOC_Rate",
        "Max_OOC_Rate",
        "avg_PI_value",
        "Interval_Time",
        "Avg_Rework_Ratio",
        "Max_Rework_Ratio",
    ]
    machine_stats = machine_stats.reset_index()
    machine_stats = machine_stats.merge(wafer_qty_by_machine, on="Machine_ID", how="left")
    machine_stats["Wafer_Qty"] = machine_stats["Wafer_Qty"].fillna(0)

    machine_stats["Avg_Rework_Ratio(%)"] = (machine_stats["Avg_Rework_Ratio"] * 100).round(2)
    machine_stats["Max_Rework_Ratio(%)"] = (machine_stats["Max_Rework_Ratio"] * 100).round(2)
    machine_stats = machine_stats.drop(columns=["Avg_Rework_Ratio", "Max_Rework_Ratio"])

    # 將三個指標正規化到 0~1，再計算加權風險指標
    normalize_cols = ["Avg_OOC_Rate", "Avg_Rework_Ratio(%)", "Wafer_Qty"]
    for col in normalize_cols:
        col_min = machine_stats[col].min()
        col_max = machine_stats[col].max()
        if pd.isna(col_min) or pd.isna(col_max) or col_max == col_min:
            machine_stats[f"{col}_Norm"] = 0.0
        else:
            machine_stats[f"{col}_Norm"] = (machine_stats[col] - col_min) / (col_max - col_min)

    machine_stats["RW_Weighted_Risk_Index"] = (
        machine_stats["Avg_OOC_Rate_Norm"] * 0.4
        + machine_stats["Avg_Rework_Ratio(%)_Norm"] * 0.4
        + machine_stats["Wafer_Qty_Norm"] * 0.2
    ).round(4)

    machine_stats = machine_stats.drop(columns=[
        "Avg_OOC_Rate_Norm",
        "Avg_Rework_Ratio(%)_Norm",
        "Wafer_Qty_Norm",
    ])

    return machine_stats.sort_values("Avg_Rework_Ratio(%)", ascending=False)


def build_oper_stats(lot_oper_df):
    if len(lot_oper_df) == 0:
        return pd.DataFrame()

    if "Wafer_Qty" in lot_oper_df.columns and "SCHD_NO" in lot_oper_df.columns and "OPER" in lot_oper_df.columns:
        wafer_qty_by_oper = (
            lot_oper_df[["OPER", "SCHD_NO", "Wafer_Qty"]]
            .dropna(subset=["OPER", "SCHD_NO"])
            .drop_duplicates(subset=["OPER", "SCHD_NO"], keep="first")
            .copy()
        )
        wafer_qty_by_oper["Wafer_Qty"] = pd.to_numeric(wafer_qty_by_oper["Wafer_Qty"], errors="coerce").fillna(0)
        wafer_qty_by_oper = wafer_qty_by_oper.groupby("OPER", as_index=False)["Wafer_Qty"].sum()
    else:
        wafer_qty_by_oper = pd.DataFrame(columns=["OPER", "Wafer_Qty"])

    oper_stats = lot_oper_df.groupby("OPER").agg(
        {
            "OOC_Rate_%": ["mean", "max"],
            "PI_value": ["mean"],
            "Interval_Time": "sum",
            "Rework_Particle": ["mean", "max"],
        }
    ).round(4)

    oper_stats.columns = [
        "Avg_OOC_Rate",
        "Max_OOC_Rate",
        "avg_PI_value",
        "Interval_Time",
        "Avg_Rework_Ratio",
        "Max_Rework_Ratio",
    ]
    oper_stats = oper_stats.reset_index()
    oper_stats = oper_stats.merge(wafer_qty_by_oper, on="OPER", how="left")
    oper_stats["Wafer_Qty"] = oper_stats["Wafer_Qty"].fillna(0)

    oper_stats["Avg_Rework_Ratio(%)"] = (oper_stats["Avg_Rework_Ratio"] * 100).round(2)
    oper_stats["Max_Rework_Ratio(%)"] = (oper_stats["Max_Rework_Ratio"] * 100).round(2)
    oper_stats = oper_stats.drop(columns=["Avg_Rework_Ratio", "Max_Rework_Ratio"])

    return oper_stats.sort_values("Avg_Rework_Ratio(%)", ascending=False)


def split_lot_type(df):
    lot_mask = df["LOT_ID"].astype(str).str.contains(r"_\d+$", na=False)
    fresh_df = df[~lot_mask].copy()
    rework_df = df[lot_mask].copy()

    # 沿用原本邏輯：Fresh lot 的機台統計排除 OPER 6682
    if "OPER" in fresh_df.columns:
        fresh_df = fresh_df[fresh_df["OPER"] != 6682].copy()

    return fresh_df, rework_df


def save_outputs(output_dir, fresh_machine, rework_machine, fresh_oper, rework_oper, merged_machine):
    output_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "Fresh lot - 機台統計詳細資料_(Security C).xlsx": fresh_machine,
        "Rework lot - 機台統計詳細資料_(Security C).xlsx": rework_machine,
        "Merged - 機台統計詳細資料_(Security C).xlsx": merged_machine,
        "Fresh lot - 站點統計詳細資料_(Security C).xlsx": fresh_oper,
        "Rework lot - 站點統計詳細資料_(Security C).xlsx": rework_oper,
    }

    for file_name, data in files.items():
        output_path = output_dir / file_name
        append_to_excel(output_path, data)
        print(f"[輸出完成] {output_path}")


def main():
    selected_layers = resolve_layers(PROCESS, LAYER_SELECTION)
    if not selected_layers:
        raise ValueError("Layer 設定無效，請檢查 LAYER_SELECTION")

    print("=== 參數設定 ===")
    print(f"日期範圍: {START_DATE} ~ {END_DATE}")
    print(f"Process: {PROCESS}")
    print(f"Layer: {selected_layers}")

    weekly_windows = generate_thu_wed_windows(START_DATE, END_DATE)
    print(f"週期切分數量: {len(weekly_windows)}")

    success_count = 0
    for run_start, run_end in weekly_windows:
        print(f"\n--- 週期分析: {run_start} ~ {run_end} ---")

        date_range = (run_start, run_end)
        _, lot_stats, _, track_info, selected_lot_ids = load_and_process_data(
            TRACK_DIR,
            date_range,
            use_all_lots=True,
            machine_filter=MACHINE_FILTER,
            rework_positive_only=REWORK_POSITIVE_ONLY,
        )

        lot_stats = filter_df_by_layers(lot_stats, "LOT_RDL_INSPECTION", selected_layers)
        track_info = filter_df_by_layers(track_info, "LOT_RDL_INSPECTION", selected_layers)
        selected_lot_ids = [lot_id for lot_id in selected_lot_ids if extract_layer(lot_id) in selected_layers]

        if len(track_info) == 0 or len(selected_lot_ids) == 0:
            print("此週無符合條件的 track 資料，跳過。")
            continue

        sensor_df = load_sensor_data(track_info)
        if sensor_df is None or len(sensor_df) == 0:
            print("此週找不到對應 Sensor 資料，跳過。")
            continue

        sensor_analysis = analyze_sensor_correlation(selected_lot_ids, track_info, sensor_df, lot_stats)
        if not sensor_analysis:
            print("此週沒有可用的 Sensor 關聯結果，跳過。")
            continue

        analysis_df = build_sensor_analysis_df(sensor_analysis, selected_lot_ids, selected_layers)
        if len(analysis_df) == 0:
            print("此週分析結果在指定 Layer 下為空，跳過。")
            continue

        fresh_df, rework_df = split_lot_type(analysis_df)

        fresh_machine_stats = build_machine_stats(fresh_df)
        rework_machine_stats = build_machine_stats(rework_df)
        fresh_oper_stats = build_oper_stats(fresh_df)
        rework_oper_stats = build_oper_stats(rework_df)

        fresh_machine_stats = fresh_machine_stats.copy()
        rework_machine_stats = rework_machine_stats.copy()
        fresh_machine_stats["LOT_Type"] = "Fresh lot"
        rework_machine_stats["LOT_Type"] = "Rework lot"
        merged_machine_stats = pd.concat([fresh_machine_stats, rework_machine_stats], ignore_index=True)

        fresh_machine_stats = add_run_metadata(fresh_machine_stats, run_start, run_end)
        rework_machine_stats = add_run_metadata(rework_machine_stats, run_start, run_end)
        merged_machine_stats = add_run_metadata(merged_machine_stats, run_start, run_end)
        fresh_oper_stats = add_run_metadata(fresh_oper_stats, run_start, run_end)
        rework_oper_stats = add_run_metadata(rework_oper_stats, run_start, run_end)

        save_outputs(
            OUTPUT_DIR,
            fresh_machine_stats,
            rework_machine_stats,
            fresh_oper_stats,
            rework_oper_stats,
            merged_machine_stats,
        )
        success_count += 1

    if success_count == 0:
        print("=== 完成（沒有可輸出的週期資料）===")
        return

    print("=== 完成 ===")

if __name__ == "__main__":
    main()