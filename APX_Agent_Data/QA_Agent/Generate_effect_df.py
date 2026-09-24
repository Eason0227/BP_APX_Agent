import pandas as pd
import numpy as np
import os
import json
import sys
from datetime import datetime, timedelta
from scipy import stats
import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils.Get_OOB_result_v2 import load_data_duckdb, st_process_psn_data

# ========== 常數定義 ==========
DATA_STORAGE_DIR = r"D:\Paticle_OOB_system\BP_PSN_PM_record"
EVENT_STATUS_FILE = os.path.join(DATA_STORAGE_DIR, "event_status.json")
EVENT_RECORDS_FILE = os.path.join(DATA_STORAGE_DIR, "event_records.json")

OUTPUT_DIR = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\QA_Agent\PSN_PM_effect_data"

# ========== 核心函數 ==========

def load_oos_pm_event_data():
    event_status = {}
    event_records = {}
    if os.path.exists(EVENT_STATUS_FILE):
        try:
            with open(EVENT_STATUS_FILE, 'r', encoding='utf-8') as f:
                event_status = json.load(f)
        except Exception as e:
            print(f"載入事件狀態時發生錯誤: {e}")
    if os.path.exists(EVENT_RECORDS_FILE):
        try:
            with open(EVENT_RECORDS_FILE, 'r', encoding='utf-8') as f:
                event_records = json.load(f)
        except Exception as e:
            print(f"載入事件記錄時發生錯誤: {e}")
    return event_status, event_records


def analyze_ooc_periods(device_df, time_col):
    ooc_periods = []
    ooc_values = device_df['out_of_OOC_count_spec'].values
    times = device_df[time_col].values

    state_changes = np.diff(ooc_values)
    start_indices = np.where(state_changes == 1)[0] + 1
    end_indices = np.where(state_changes == -1)[0] + 1

    if len(ooc_values) > 0 and ooc_values[0] == 1:
        start_indices = np.concatenate([[0], start_indices])
    if len(ooc_values) > 0 and ooc_values[-1] == 1:
        end_indices = np.concatenate([end_indices, [len(ooc_values) - 1]])

    for start_idx, end_idx in zip(start_indices, end_indices):
        start_time = pd.to_datetime(times[start_idx])
        end_time = pd.to_datetime(times[end_idx])
        duration = end_time - start_time
        ooc_periods.append({'start': start_time, 'end': end_time, 'duration': duration})

    return ooc_periods


def get_BP_oos_events_with_clean_records(start_date_str, end_date_str, df=None):
    event_status, event_records = load_oos_pm_event_data()

    if df is None:
        df = process_psn_data(start_date_str, end_date_str)

    if 'device_name' not in df.columns:
        return pd.DataFrame()

    filtered_df = df.copy()
    filtered_df['Building'] = "K18-6F"

    if 'out_of_OOC_count_spec' not in df.columns:
        return pd.DataFrame()

    if 'time' in filtered_df.columns:
        time_col = 'time'
    elif 'timestamp' in filtered_df.columns:
        time_col = 'timestamp'
    else:
        return pd.DataFrame()

    filtered_df[time_col] = pd.to_datetime(filtered_df[time_col])

    periods_data_list = []
    for device, device_df in filtered_df.groupby('device_name'):
        device_df = device_df.sort_values(time_col).reset_index(drop=True)
        if len(device_df) > 0:
            ooc_periods = analyze_ooc_periods(device_df, time_col)
            machine = device.split('#')[0]
            if ooc_periods:
                for i, period in enumerate(ooc_periods, 1):
                    duration_hours = period['duration'].total_seconds() / 3600
                    period_mask = (device_df[time_col] >= period['start']) & (device_df[time_col] <= period['end'])
                    period_data = device_df[period_mask]
                    avg_pi_value = period_data['PI_value'].mean() if len(period_data) > 0 and 'PI_value' in period_data.columns else 0
                    pi_spec = period_data['PI_spec'].iloc[0] if len(period_data) > 0 and 'PI_spec' in period_data.columns else 0
                    location = period_data['Building'].iloc[0]
                    event_id = f"{machine}_{device}_{period['start'].strftime('%Y%m%d%H%M%S')}"
                    periods_data_list.append({
                        '觸發次數': f"第 {i} 次",
                        '開始時間': period['start'].strftime('%Y-%m-%d %H:%M:%S'),
                        '結束時間': period['end'].strftime('%Y-%m-%d %H:%M:%S'),
                        '持續時間(小時)': f"{duration_hours:.2f}",
                        'PSN': device,
                        '機台': machine,
                        'location': location,
                        '汙染因子平均值': f"{avg_pi_value:.3f}" if avg_pi_value > 0 else "N/A",
                        'OOB': f"{pi_spec:.3f}" if pi_spec > 0 else "N/A",
                        'event_id': event_id
                    })

    if not periods_data_list:
        return pd.DataFrame()

    periods_df = pd.DataFrame(periods_data_list)
    periods_df = periods_df.sort_values(['開始時間'], ascending=False).reset_index(drop=True)

    periods_df['處理狀態'] = periods_df['event_id'].apply(
        lambda x: '已完成' if event_status.get(x, False) else '未完成'
    )
    periods_df['清潔開始時間'] = periods_df['event_id'].apply(
        lambda x: event_records.get(x, {}).get('clean_start_time', '')
    )
    periods_df['清潔結束時間'] = periods_df['event_id'].apply(
        lambda x: event_records.get(x, {}).get('clean_end_time', '')
    )
    periods_df['清潔人員'] = periods_df['event_id'].apply(
        lambda x: event_records.get(x, {}).get('cleaner', '')
    )
    periods_df['清潔內容'] = periods_df['event_id'].apply(
        lambda x: event_records.get(x, {}).get('clean_content', '')
    )

    display_df = periods_df[[
        '處理狀態', '機台', 'PSN', 'location', '開始時間', '結束時間',
        '持續時間(小時)', '汙染因子平均值', 'OOB',
        '清潔開始時間', '清潔結束時間', '清潔人員', '清潔內容', 'event_id'
    ]].rename(columns={
        '處理狀態': 'STATUS', 'location': 'LOCATION', '機台': 'MACHINE_NAME',
        '開始時間': 'START_TIME', '結束時間': 'END_TIME', '持續時間(小時)': 'DURATION',
        '汙染因子平均值': 'PI', 'OOB': 'OOB',
        '清潔開始時間': 'MAINTENANCE_START_TIME', '清潔結束時間': 'MAINTENANCE_END_TIME',
        '清潔人員': 'MAINTENANCE_USER', '清潔內容': 'MAINTENANCE_CONTENT', 'event_id': 'EVENT_ID'
    })
    return display_df


def analyze_maintenance_effect(display_df, raw_df):
    results = []

    if 'time' in raw_df.columns:
        time_col = 'time'
    elif 'timestamp' in raw_df.columns:
        time_col = 'timestamp'
    else:
        return pd.DataFrame()

    analysis_df = raw_df.copy()
    analysis_df[time_col] = pd.to_datetime(analysis_df[time_col])
    if pd.api.types.is_datetime64tz_dtype(analysis_df[time_col]):
        analysis_df[time_col] = analysis_df[time_col].dt.tz_localize(None)

    if 'STATUS' not in display_df.columns:
        return pd.DataFrame()

    completed_events = display_df[display_df['STATUS'] == '已完成']

    for _, row in completed_events.iterrows():
        psn = row['PSN']
        event_id = row['EVENT_ID']
        user = row.get('MAINTENANCE_USER', '')
        content = row.get('MAINTENANCE_CONTENT', '')

        m_start = pd.to_datetime(row['MAINTENANCE_START_TIME'], errors='coerce')
        m_end = pd.to_datetime(row['MAINTENANCE_END_TIME'], errors='coerce')

        if pd.isna(m_start) or pd.isna(m_end):
            continue
        if m_start.tzinfo is not None:
            m_start = m_start.tz_localize(None)
        if m_end.tzinfo is not None:
            m_end = m_end.tz_localize(None)

        psn_data = analysis_df[analysis_df['device_name'] == psn]
        if psn_data.empty:
            continue

        before_data = psn_data[
            (psn_data[time_col] < m_start) & (psn_data[time_col] >= m_start - timedelta(hours=24))
        ]
        after_data = psn_data[
            (psn_data[time_col] > m_end) & (psn_data[time_col] <= m_end + timedelta(hours=24))
        ]

        vals_before = before_data['PI_value'].dropna()
        vals_after = after_data['PI_value'].dropna()

        avg_before = vals_before.mean() if not vals_before.empty else np.nan
        avg_after = vals_after.mean() if not vals_after.empty else np.nan
        diff = avg_after - avg_before if (not np.isnan(avg_before) and not np.isnan(avg_after)) else np.nan

        p_value = np.nan
        is_significant = "N/A"
        if len(vals_before) >= 2 and len(vals_after) >= 2:
            try:
                _, p_val = stats.ttest_ind(vals_before, vals_after, equal_var=False, alternative='greater')
                p_value = p_val
                is_significant = "是" if p_val < 0.05 else "否"
            except Exception as e:
                print(f"T-test error for {event_id}: {e}")

        results.append({
            'EVENT_ID': event_id,
            'PSN': psn,
            'MAINTENANCE_START': m_start,
            'MAINTENANCE_END': m_end,
            'MAINTENANCE_USER': user,
            'MAINTENANCE_CONTENT': content,
            'AVG_PI_BEFORE': avg_before,
            'AVG_PI_AFTER': avg_after,
            'DIFF': diff,
            'P_VALUE': p_value,
            'SIGNIFICANT_DROP': is_significant
        })

    if not results:
        return pd.DataFrame()

    result_df = pd.DataFrame(results)
    for col in ['AVG_PI_BEFORE', 'AVG_PI_AFTER', 'DIFF']:
        result_df[col] = result_df[col].apply(lambda x: f"{x:.3f}" if not pd.isna(x) else "N/A")
    result_df['P_VALUE'] = result_df['P_VALUE'].apply(lambda x: f"{x:.4f}" if not pd.isna(x) else "N/A")
    result_df = result_df.drop_duplicates(subset=['PSN', 'MAINTENANCE_START', 'MAINTENANCE_END'])
    result_df['DIFF'] = pd.to_numeric(result_df['DIFF'], errors='coerce')
    result_df['AVG_PI_BEFORE'] = pd.to_numeric(result_df['AVG_PI_BEFORE'], errors='coerce')
    result_df['AVG_PI_AFTER'] = pd.to_numeric(result_df['AVG_PI_AFTER'], errors='coerce')
    return result_df


# ========== 主程式 ==========

def main(start_date_str: str, end_date_str: str, building: str = "K18-All"):
    """
    start_date_str: 'YYYYMMDD'
    end_date_str:   'YYYYMMDD'
    building: 'K18-6F' | 'K18-5F' | 'K18-All'
    """
    output_csv = os.path.join(OUTPUT_DIR, f"{start_date_str}_{end_date_str}_effect_df.csv")
    print(f"查詢範圍：{start_date_str} ~ {end_date_str}，樓層：{building}")

    if building == "K18-All":
        selected_building_list = ['K18-6F', 'K18-5F']
    else:
        selected_building_list = building

    start_date = pd.to_datetime(start_date_str)
    end_date = pd.to_datetime(end_date_str)

    print("載入原始資料中...")
    PM_feedback_OOB_data = load_data_duckdb( start_date, end_date, selected_building_list)

    filtered_df = PM_feedback_OOB_data.copy()
    required_status = ["RUN", "ENG", "IDLE", "TEST"]
    oos_mask = pd.to_numeric(filtered_df.get('oos_over_one_day'), errors='coerce').eq(1)
    status_mask = (
        filtered_df.get('Machine_Status', pd.Series(index=filtered_df.index, dtype='object'))
        .astype(str).str.strip().str.upper().isin(required_status)
    )
    filtered_df = filtered_df[oos_mask & status_mask].copy()

    print("計算 OOS 事件...")
    feedback_OOS_df = get_BP_oos_events_with_clean_records(start_date_str, end_date_str, df=filtered_df)

    print("分析保養成效...")
    effect_df = analyze_maintenance_effect(feedback_OOS_df, PM_feedback_OOB_data)

    print(f"輸出 CSV：{output_csv}")
    effect_df.to_csv(output_csv, encoding="utf-8-sig", index=False)
    print(f"完成！共 {len(effect_df)} 筆記錄。")
    return effect_df


if __name__ == "__main__":
    # 直接修改此處的日期即可執行
    START_DATE = "20260520"
    END_DATE   = "20260527"
    BUILDING   = "K18-6F"  # 可選：K18-6F / K18-5F / K18-All

    effect_df = main(START_DATE, END_DATE, BUILDING)
    print(effect_df)
