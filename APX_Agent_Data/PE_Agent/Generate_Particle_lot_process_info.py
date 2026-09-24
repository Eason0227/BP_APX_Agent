import pandas as pd
import numpy as np
import os
import traceback
import uuid
import json
import requests
from datetime import datetime
from pathlib import Path
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils.Get_OOB_result_v2 import load_data_duckdb

_session = requests.Session()
_session.trust_env = False


# ─────────────────────────────────────────────
# 函式定義
# ─────────────────────────────────────────────

def build_lot_documents(lot_df):
    """將每個 LOT 的摘要組成文件清單。"""
    documents = []
    for lot_id, group in lot_df.groupby('LOT_ID'):
        summary_text = generate_lot_summary(lot_id, group)
        if not summary_text:
            continue
        documents.append({
            "page_content": summary_text,
            "metadata": {
                "doc_type": "pe_lot_summary",
                "lot_id": str(lot_id),
                "inspection_date": str(group['INSPECTION_DATE'].iloc[0]),
                "particle_yield_loss": float(group['Particle_yield_loss'].iloc[0]),
                "particle_loss_qty": float(group['Particle_loss_Qty'].iloc[0]),
                "die_qty": float(group['DIE_QTY'].iloc[0]),
            }
        })
    return documents

def load_and_process_data(track_dir, date_range):
    """載入並處理資料的函數"""
    try:
        start_date, end_date = date_range

        track_files = sorted(Path(track_dir).glob("*.xlsx"))
        if not track_files:
            print("資料夾內找不到 Excel 檔案")
            return None, None, None, None, None

        latest_track_file = max(track_files, key=lambda p: p.stat().st_mtime)
        print(f"使用最新 Track 檔案: {latest_track_file.name}")
        track_df = pd.read_excel(latest_track_file)
        track_df = track_df.dropna(subset=['MACHINE_NO'])
        
        track_df['INSPECTION_DATE'] = pd.to_datetime(
            track_df['INSPECTION_DATE'], format='%Y/%m/%d %H:%M:%S', errors='coerce'
        )
        track_df = track_df[
            track_df['INSPECTION_DATE'].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
        ]
        track_df['LOT_RDL_INSPECTION'] = (
            track_df['LOT_RDL_INSPECTION'].astype(str) 
        )
        track_df['TRACK_IN_TIME'] = pd.to_datetime(
            track_df['TRACK_IN_TIME'], format='%Y/%m/%d %H:%M:%S', errors='coerce'
        )
        track_df['TRACK_OUT_TIME'] = pd.to_datetime(
            track_df['TRACK_OUT_TIME'], format='%Y/%m/%d %H:%M:%S', errors='coerce'
        )
        track_df['Particle loss'] = track_df['PARTICLE_QTY'] / track_df['DIE_QTY']
        track_df.rename(columns={'MACHINE_NO': 'Machine ID'}, inplace=True)
        # 若 LOT_RDL_INSPECTION、OPER、TRACK_IN_TIME 相同但 TRACK_OUT_TIME 不同，保留較早 TRACK_OUT_TIME。
        track_df = track_df.sort_values(['LOT_RDL_INSPECTION', 'OPER', 'TRACK_IN_TIME', 'TRACK_OUT_TIME'])
        track_df = track_df.drop_duplicates(['LOT_RDL_INSPECTION', 'OPER', 'TRACK_IN_TIME'], keep="first")

        df = track_df.copy()
        df = df.drop_duplicates(['LOT_RDL_INSPECTION'])
        df.rename(columns={'LOT_NO': 'LOT_ID'}, inplace=True)

        lot_stats = df.copy()
        lot_stats.index = df['LOT_RDL_INSPECTION']
        lot_stats = lot_stats.sort_values('Particle loss', ascending=False)

        # if use_all_lots:
        #     lot_stats_filtered = lot_stats[lot_stats['Particle loss'] > 0]
        #     selected_lot_ids = lot_stats_filtered.index.tolist()
        #     top_worst_lots = lot_stats_filtered
        # else:
        #     top_worst_lots = lot_stats.head(top_n)
        #     selected_lot_ids = top_worst_lots.index.tolist()

        lot_stats_filtered = lot_stats[lot_stats['Particle loss'] > 0]
        selected_lot_ids = lot_stats_filtered.index.tolist()
        top_worst_lots = lot_stats_filtered

        track_info = track_df[track_df['LOT_RDL_INSPECTION'].isin(selected_lot_ids)].copy()

        return df, lot_stats, top_worst_lots, track_info, selected_lot_ids

    except Exception as e:
        print(f"資料載入錯誤: {str(e)}")
        traceback.print_exc()
        return None, None, None, None, None


def load_sensor_data(track_df):
    """載入 Sensor 資料"""
    try:
        min_time = track_df['TRACK_IN_TIME'].min()
        max_time = track_df['TRACK_OUT_TIME'].max()

        sensor_df = load_data_duckdb(min_time, max_time, "K18-6F")
        sensor_df['Building'] = "K18-6F"
        sensor_df['timestamp'] = pd.to_datetime(sensor_df['timestamp'])

        relevant_machines = track_df['Machine ID'].unique()
        sensor_df = sensor_df[sensor_df['Machine_ID'].isin(relevant_machines)].copy()
        sensor_df = sensor_df.sort_values(['Machine_ID', 'timestamp'])

        return sensor_df
    except Exception as e:
        traceback.print_exc()
        print(f"Sensor 資料載入失敗: {str(e)}")
        return None


def analyze_sensor_correlation(top_lot_ids, track_info, sensor_df, lot_stats):
    """分析 Sensor 與 count Loss 的關聯性"""
    if sensor_df is None:
        return None

    relevant_track = track_info[track_info['LOT_RDL_INSPECTION'].isin(top_lot_ids)].copy()
    if len(relevant_track) == 0:
        return {}

    lot_stats_dict = {}
    for lot_id in top_lot_ids:
        lot_data = lot_stats[lot_stats['LOT_RDL_INSPECTION'] == lot_id]
        if len(lot_data) > 0:
            lot_stats_dict[lot_id] = {
                'Particle_yield_loss': lot_data['Particle loss'].values[0],
                'Particle_loss_Qty': lot_data['PARTICLE_QTY'].values[0],
                'DIE_QTY': lot_data['DIE_QTY'].values[0],
            }

    results = []
    sensor_grouped = {name: group for name, group in sensor_df.groupby('Machine_ID')}

    for _, track_row in relevant_track.iterrows():
        machine_id = track_row['Machine ID']
        lot_id = track_row['LOT_RDL_INSPECTION']
        lot_info = lot_stats_dict.get(lot_id, {})

        # 預設保留每一筆 Track 站點；若無 sensor 對應則以 None 表示，後續轉為 N/A。
        ooc_count = None
        ooc_rate = None
        oos_status = None
        total_records = 0
        pi_value = None

        if machine_id in sensor_grouped:
            machine_sensor = sensor_grouped[machine_id]
            mask = (
                (machine_sensor['timestamp'] >= track_row['TRACK_IN_TIME']) &
                (machine_sensor['timestamp'] <= track_row['TRACK_OUT_TIME'])
            )
            filtered_sensor = machine_sensor.loc[mask]

            if len(filtered_sensor) > 0:
                total_records = len(filtered_sensor)
                ooc_count = filtered_sensor['out_of_pi_spec'].sum()
                ooc_rate = round((ooc_count / total_records * 100), 2)
                oos_status = filtered_sensor['out_of_OOC_count_spec'].sum()
                pi_value = filtered_sensor['PI_value'].mean()

        results.append({
            'LOT_ID': lot_id,
            'OPER': track_row['OPER'],
            'Machine_ID': machine_id,
            'PI_value': pi_value,
            'OOC_Count': ooc_count,
            'OOC_Rate_%': ooc_rate,
            'OOS_Status': oos_status,
            'Total_Records': total_records,
            'TRACK_IN_TIME': str(track_row['TRACK_IN_TIME']),
            'TRACK_OUT_TIME': str(track_row['TRACK_OUT_TIME']),
            'INSPECTION_DATE': str(track_row['INSPECTION_DATE']),
            'DIE_QTY': lot_info.get('DIE_QTY', 0),
            'Particle_yield_loss': lot_info.get('Particle_yield_loss', 0),
            'Particle_loss_Qty': lot_info.get('Particle_loss_Qty', 0),
        })

    lot_sensor_analysis = {}
    for result in results:
        lot_id = result['LOT_ID']
        if lot_id not in lot_sensor_analysis:
            lot_sensor_analysis[lot_id] = []
        lot_sensor_analysis[lot_id].append(result)

    return lot_sensor_analysis


def preprocess_data(df):
    # 去除完全重複的 row
    df = df.drop_duplicates()
    
    # 嘗試轉換時間欄位以確保路徑順序正確
    try:
        df['TRACK_IN_TIME'] = pd.to_datetime(df['TRACK_IN_TIME'])
        df['TRACK_OUT_TIME'] = pd.to_datetime(df['TRACK_OUT_TIME'])
        df = df.sort_values(['LOT_ID', 'TRACK_IN_TIME'])
    except Exception:
        pass # 若轉換失敗則維持原順序

    summary_list = []
    # 針對每個 LOT 進行分組處理
    for lot_id, group in df.groupby('LOT_ID'):
        # 假設同一 LOT 的 Yield Loss 是一樣的，取第一筆即可
        yield_loss = group['Particle_yield_loss'].iloc[0]
        
        # 建立路徑字串，包含機台與 OOB 數值
        # 格式範例: B1_WFCL_02(OOB:50%, Time:16m)
        path_steps = []
        for _, row in group.iterrows():
            # 計算作業時間 (分鐘)
            try:
                duration = (row['TRACK_OUT_TIME'] - row['TRACK_IN_TIME']).total_seconds() / 60
                time_str = f"{int(duration)}m"
            except Exception:
                time_str = f"{row['Total_Records']}m"

            step = f"{row['Machine_ID']}(OOB:{row['OOB_Fail_Rate']}%, Time:{time_str})"
            path_steps.append(step)
        
        path_str = " -> ".join(path_steps)
        
        summary_list.append({
            "LOT_ID": lot_id,
            "Yield_Loss": yield_loss,
            "Process_Path": path_str
        })
    
    summary_df = pd.DataFrame(summary_list)
    # 依據 Yield Loss 由大到小排序 (Loss 越高代表問題越大)
    summary_df = summary_df.sort_values('Yield_Loss', ascending=False)
    return summary_df


def generate_lot_summary(lot_id, group):
    # 提取 PE 經驗標籤 (值為 1 的欄位)
    experience_cols = ['PI Coat', 'PI Cure', 'PR Coat', 'PR Exp', 'Sput', 'Etch', 'Final', 'Recon', 'FOUP']
    suspect_areas = [col for col in experience_cols if pd.to_numeric(group[col].iloc[0], errors='coerce') > 0]

    # 未命中任何 PE 經驗嫌疑區時，直接略過該 LOT 摘要輸出。
    if not suspect_areas:
        return None
    
    summary = f"### Lot ID: {lot_id}\n"
    summary += f"- **檢驗日期**: {group['INSPECTION_DATE'].iloc[0]}\n"
    summary += f"- **良率損耗**: {group['Particle_yield_loss'].iloc[0]:.4%}\n"
    summary += f"- **Die 損失數量(Particle)**: {group['Particle_loss_Qty'].iloc[0]}\n"
    summary += f"- **Die 總生產量**: {group['DIE_QTY'].iloc[0]}\n"
    summary += f"- **PE 經驗初步判定嫌疑區**: {', '.join(suspect_areas)}\n\n"
    summary += "#### 製程詳細軌跡與異常評估:\n"
    
    for _, row in group.iterrows():
        oob_rate_numeric = pd.to_numeric(row['OOB_Fail_Rate'], errors='coerce')
        if pd.isna(oob_rate_numeric):
            status = "無資料"
            oob_rate_display = "N/A"
        else:
            status = "異常" if oob_rate_numeric > 30 else "正常"
            oob_rate_display = f"{oob_rate_numeric}%"
        summary += f"- **[{row['STAGE']}]** 在站點{row['OPER']}，由機台 {row['Machine_ID']}進行作業({row['PROCESS_CLASSIFICATION']})：\n"
        summary += f"  - 狀態: {status} (OOB Rate: {oob_rate_display})\n"
        summary += f"  - 進出時間: {row['TRACK_IN_TIME']} -> {row['TRACK_OUT_TIME']}\n"
    
    summary += "\n---\n"
    return summary


def export_documents_to_local(documents, documents_path, summaries_path=None):
    """將原本要匯入 Vector DB 的文件輸出到地端。"""
    os.makedirs(os.path.dirname(documents_path), exist_ok=True)

    with open(documents_path, "a", encoding="utf-8") as f:
        for doc in documents:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    # print(f"已輸出 Vector DB 文件到地端: {documents_path}")

    if summaries_path:
        with open(summaries_path, "a", encoding="utf-8") as f:
            for doc in documents:
                f.write(doc["page_content"])
                if not str(doc["page_content"]).endswith("\n"):
                    f.write("\n")
        print(f"已輸出摘要文本到地端: {summaries_path}")


def export_jsonl_to_structured_excel(documents_path, excel_path):
    """將 lot_documents_for_vector_db.jsonl 轉成結構化 Excel，並以追加模式輸出。"""
    required_columns = [
        "Lot ID",
        "檢驗日期",
        "良率損耗",
        "Die 損失數量(Particle)",
        "Die 總生產量",
        "PE 經驗初步判定嫌疑區",
        "製程詳細軌跡與異常評估",
    ]

    if not os.path.exists(documents_path):
        raise FileNotFoundError(f"找不到 JSONL 檔案: {documents_path}")

    rows = []
    with open(documents_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            obj = json.loads(line)
            text = str(obj.get("page_content", ""))

            lot_id = ""
            inspection_date = ""
            yield_loss = ""
            particle_loss_qty = ""
            die_qty = ""
            suspect_area = ""
            process_detail = ""

            for raw_line in text.splitlines():
                line_text = raw_line.strip()

                if line_text.startswith("### Lot ID:"):
                    lot_id = line_text.replace("### Lot ID:", "", 1).strip()
                elif line_text.startswith("- **檢驗日期**:"):
                    inspection_date = line_text.replace("- **檢驗日期**:", "", 1).strip()
                elif line_text.startswith("- **良率損耗**:"):
                    yield_loss = line_text.replace("- **良率損耗**:", "", 1).strip()
                elif line_text.startswith("- **Die 損失數量(Particle)**:"):
                    particle_loss_qty = line_text.replace("- **Die 損失數量(Particle)**:", "", 1).strip()
                elif line_text.startswith("- **Die 總生產量**:"):
                    die_qty = line_text.replace("- **Die 總生產量**:", "", 1).strip()
                elif line_text.startswith("- **PE 經驗初步判定嫌疑區**:"):
                    suspect_area = line_text.replace("- **PE 經驗初步判定嫌疑區**:", "", 1).strip()

            marker = "#### 製程詳細軌跡與異常評估:"
            if marker in text:
                process_detail = text.split(marker, 1)[1].strip()
                if process_detail.endswith("---"):
                    process_detail = process_detail[:-3].rstrip()

            rows.append({
                "Lot ID": lot_id,
                "檢驗日期": inspection_date,
                "良率損耗": yield_loss,
                "Die 損失數量(Particle)": particle_loss_qty,
                "Die 總生產量": die_qty,
                "PE 經驗初步判定嫌疑區": suspect_area,
                "製程詳細軌跡與異常評估": process_detail,
            })

    structured_df = pd.DataFrame(rows)
    for col in required_columns:
        if col not in structured_df.columns:
            structured_df[col] = ""
    structured_df = structured_df[required_columns]

    if os.path.exists(excel_path):
        try:
            existing_df = pd.read_excel(excel_path)
            for col in required_columns:
                if col not in existing_df.columns:
                    existing_df[col] = ""
            existing_df = existing_df[required_columns]
            structured_df = pd.concat([existing_df, structured_df], ignore_index=True)
        except Exception as e:
            print(f"既有 Excel 讀取失敗，改以新檔覆蓋寫入: {e}")

    os.makedirs(os.path.dirname(excel_path), exist_ok=True)
    # 若 Lot ID 重複，保留最後一筆（通常代表最新追加結果）。
    structured_df = structured_df.drop_duplicates(subset=['Lot ID'], keep='last')
    structured_df.to_excel(excel_path, index=False)
    print(f"已輸出結構化 Excel 到地端: {excel_path}")


# ─────────────────────────────────────────────
# 主流程：產生 lot_df
# ─────────────────────────────────────────────


# 參數設定 
date_range = (datetime(2026, 5, 19), datetime(2026, 5, 27))

track_dir = r"D:\Paticle_OOB_system\Particle_OOB_Yield_anaysis\PSN_Yield_tracking_data_v2"
# output_path = r"\\Khfs1\8n00$\8N40\Eason\PSN_OOB_共用資料夾\APX_Agent\lot_df.csv" 
output_path = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\PE_Agent\lot_df.csv"
routing_file = r"\\Khfs1\8n00$\8N40\Eason\PSN_OOB_共用資料夾\APX_Agent\AMD Routing_(Security C).xlsx"
yield_dir    = r"\\khaiotapvp02\AMD\Yield"
local_export_dir = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\PE_Agent"
local_summary_path = os.path.join(local_export_dir, "lot_summaries.md")
local_documents_path = os.path.join(local_export_dir, "lot_documents_for_vector_db.jsonl")
local_structured_excel_path = os.path.join(local_export_dir, "lot_documents_structured_(Security C).xlsx")


def main(run_date_range=None):
    """一鍵執行 Particle Lot Process Info 分析並輸出結果。

    run_date_range: (datetime, datetime)，未傳入時使用模組頂端的 date_range。
    其餘路徑參數固定使用模組頂端常數。
    """
    _date_range = run_date_range or date_range

    _local_summary_path          = local_summary_path
    _local_documents_path        = local_documents_path
    _local_structured_excel_path = local_structured_excel_path

    print("載入並處理 track 資料...")
    df, lot_stats, top_worst_lots, track_info, top_lot_ids = load_and_process_data(track_dir, _date_range)
    # print(track_info)

    if df is None:
        print("資料載入失敗，程式結束。")
        return

    print("載入 Sensor 資料...")
    sensor_df = load_sensor_data(track_info)

    if sensor_df is None:
        print("Sensor 資料載入失敗，程式結束。")
        return

    print("進行 Sensor 關聯分析...")
    sensor_analysis = analyze_sensor_correlation(top_lot_ids, track_info, sensor_df, lot_stats)

    if not sensor_analysis:
        print("無 Sensor 關聯分析結果，程式結束。")
        return

    # 建立 ooc_df → lot_df
    rows = []
    for lot, items in sensor_analysis.items():
        for item in items:
            rows.append({
                "LOT_ID": item["LOT_ID"],
                "OPER": item["OPER"],
                "Machine_ID": item["Machine_ID"],
                "OOB_Count": item["OOC_Count"] if item["OOC_Count"] is not None else "N/A",
                "OOB_Fail_Rate": item["OOC_Rate_%"] if item["OOC_Rate_%"] is not None else "N/A",
                "Total_Records": item["Total_Records"],
                "TRACK_IN_TIME": item["TRACK_IN_TIME"],
                "TRACK_OUT_TIME": item["TRACK_OUT_TIME"],
                "INSPECTION_DATE": item["INSPECTION_DATE"],
                "DIE_QTY": item["DIE_QTY"],
                "Particle_loss_Qty": item["Particle_loss_Qty"],
                "Particle_yield_loss": item["Particle_yield_loss"],
            })

    ooc_df = pd.DataFrame(rows)
    ooc_df['OPER'] = ooc_df['OPER'].astype(str)

    lot_df = ooc_df.copy()

    # ── 新功能 1：加入 PROCESS_CLASSIFICATION (OPER → 製程步驟名稱) ──────────
    print("載入 Routing 對照表...")
    try:
        routing_df = pd.read_excel(routing_file)
        routing_df.columns = routing_df.columns.str.strip()
        routing_df['STEPID'] = routing_df['STEPID'].astype(str).str.strip()
        oper_to_class = routing_df.set_index('STEPID')['PROCESS CLASSIFICATION'].to_dict()
        oper_to_stage = routing_df.set_index('STEPID')['stage'].to_dict()
        lot_df['STAGE'] = lot_df['OPER'].astype(str).map(oper_to_stage).fillna('')
        lot_df['PROCESS_CLASSIFICATION'] = lot_df['OPER'].astype(str).map(oper_to_class).fillna('')
        print(f"  PROCESS_CLASSIFICATION 對應完成，命中率: "
              f"{(lot_df['PROCESS_CLASSIFICATION'] != '').sum()}/{len(lot_df)}")
    except Exception as e:
        print(f"  Routing 對照表載入失敗: {e}")
        lot_df['PROCESS_CLASSIFICATION'] = ''

    # ── 新功能 2：合併 Yield 明細資料 (掃描資料夾內所有檔案，讀 Single yield particle analysis 工作表) ─
    print("載入 Yield 明細資料...")
    try:
        yield_files = list(Path(yield_dir).glob("AMD Weisshorn lot RDL Yield_*_(Security C).xlsx"))
        if not yield_files:
            print(f"  找不到 Yield 檔案，路徑: {yield_dir}")
        else:
            lot_df['LOT_ID'] = lot_df['LOT_ID'].astype(str).str.strip().str.upper()
            yield_list = []
            latest_yield_file = max(yield_files, key=lambda p: p.stat().st_mtime)
            print(f"  使用最新 Yield 檔案: {latest_yield_file.name}")
            try:
                try:
                    tmp = pd.read_excel(latest_yield_file, sheet_name="Single yield particle analysis")
                except Exception:
                    tmp = pd.read_excel(latest_yield_file, sheet_name="lot Single yield analysis")
                tmp.columns = tmp.columns.str.strip()
                yield_list.append(tmp)
            except Exception as ye:
                print(f"  讀取 {latest_yield_file.name} 失敗: {ye}")

            if yield_list:
                yield_raw = pd.concat(yield_list, ignore_index=True)

                # 去除 Lot/Layer/Team 欄位中的空白（含前後與中間空格）
                for col in ['Lot', 'Layer', 'Team']:
                    if col in yield_raw.columns:
                        yield_raw[col] = yield_raw[col].astype(str).str.replace(r'\s+', '', regex=True)

                # 組合 key：Lot_Layer_Team (對應 LOT_RDL_INSPECTION)
                yield_raw['LOT_ID'] = (
                    yield_raw['Lot'].astype(str).str.strip() + "_" +
                    yield_raw['Layer'].astype(str).str.strip() + "_" +
                    yield_raw['Team'].astype(str).str.strip()
                ).str.upper()
                yield_cols = ['LOT_ID', 'Particle', 'PI Coat', 'PI Cure',
                              'PR Coat', 'PR Exp', 'Sput', 'Etch', 'Final', 'Recon', 'FOUP']

                # 同一 LOT_ID 跨檔案可能重複，保留第一筆；加總用 .sum() 亦可視需求調整
                yield_agg = (
                    yield_raw[yield_cols]
                    .drop_duplicates(subset=['LOT_ID'])
                    .reset_index(drop=True)
                )
                lot_df = lot_df.merge(yield_agg, on='LOT_ID', how='left')
                print(f"  Yield 資料合併完成，讀取 1 個檔案，"
                      f"命中筆數: {lot_df['Particle'].notna().sum()}/{len(lot_df)}")
                    



                experience_cols = ['PI Coat', 'PI Cure', 'PR Coat', 'PR Exp', 'Sput', 'Etch', 'Final', 'Recon', 'FOUP']
                for col in experience_cols:
                    if col not in lot_df.columns:
                        lot_df[col] = 0
                lot_df[experience_cols] = lot_df[experience_cols].apply(pd.to_numeric, errors='coerce').fillna(0)

                exp_hit = lot_df[experience_cols].gt(0).any(axis=1).sum()
                print(f"  PE 經驗欄位命中 LOT: {exp_hit}/{lot_df['LOT_ID'].nunique()}")
            else:
                print("  Yield 檔案存在，但無可用資料列可合併。")
    except Exception as e:
        print(f"  Yield 資料載入失敗: {e}")
        traceback.print_exc()

    # 若未成功合併 Yield，仍確保下游摘要欄位存在，避免 KeyError。
    experience_cols = ['PI Coat', 'PI Cure', 'PR Coat', 'PR Exp', 'Sput', 'Etch', 'Final', 'Recon', 'FOUP']
    for col in experience_cols:
        if col not in lot_df.columns:
            lot_df[col] = 0
    lot_df[experience_cols] = lot_df[experience_cols].apply(pd.to_numeric, errors='coerce').fillna(0)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    # lot_df.to_csv(output_path, index=False)


    lot_df = lot_df.drop_duplicates().sort_values(by=['LOT_ID', 'TRACK_IN_TIME'])

    # 執行轉換為文本格式
    all_summaries = ""
    for lot_id, group in lot_df.groupby('LOT_ID'):
        summary_text = generate_lot_summary(lot_id, group)
        if summary_text:
            all_summaries += summary_text

    # print(all_summaries)

    print("開始輸出 lot 摘要文本到地端...")
    try:
        lot_documents = build_lot_documents(lot_df)
        export_documents_to_local(
            lot_documents,
            documents_path=_local_documents_path,
            summaries_path=_local_summary_path,
        )
        export_jsonl_to_structured_excel(
            documents_path=_local_documents_path,
            excel_path=_local_structured_excel_path,
        )
    except Exception as e:
        print(f"lot 摘要輸出到地端失敗: {e}")
        traceback.print_exc()

    # print(f"lot_df 已儲存至：{_output_path}")
    # print(lot_df.head())


if __name__ == "__main__":
    main()
