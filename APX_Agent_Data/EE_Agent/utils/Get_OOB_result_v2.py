from datetime import datetime, timedelta
import glob
import logging
import os
import pandas as pd
import duckdb
import streamlit as st

def get_date_input(prompt, default_date):
    """取得使用者輸入的日期，如果輸入為空則使用預設值"""
    while True:
        user_input = input(f"{prompt} (格式: YYYY-MM-DD，直接按Enter使用預設值 {default_date.strftime('%Y-%m-%d')}): ").strip()
        
        if not user_input:
            return default_date
        
        try:
            return datetime.strptime(user_input, '%Y-%m-%d')
        except ValueError:
            print("日期格式錯誤，請使用 YYYY-MM-DD 格式")

def process_psn_data(start_date, end_date, folder_path=None):
    """
    後端處理函數：合併指定日期範圍內的PSN監控數據
    
    Args:
        start_date (str or datetime): 開始日期，格式 'YYYY-MM-DD' 或 datetime 物件
        end_date (str or datetime): 結束日期，格式 'YYYY-MM-DD' 或 datetime 物件  
        folder_path (str, optional): 資料夾路徑，預設為固定路徑
    
    Returns:
        dict: 包含處理結果的字典
            - success (bool): 是否成功
            - message (str): 處理訊息
            - output_file (str): 輸出檔案路徑
            - total_files (int): 合併檔案數量
            - total_rows (int): 總行數
    """
    try:
        # 設定預設資料夾路徑
        if folder_path is None:
            folder_path = r"D:\Paticle_OOB_system\BP_PSN_OOB_Record"

        # 轉換日期格式
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, '%Y%m%d')
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, '%Y%m%d')
        
        # 驗證日期範圍
        if start_date > end_date:
            return {
                'success': False,
                'message': '起始日期不能晚於結束日期',
                'output_file': None,
                'total_files': 0,
                'total_rows': 0
            }

        logging.info(f"開始合併指定期間的數據: {start_date.strftime('%Y%m%d')} 到 {end_date.strftime('%Y%m%d')}")
        
        # 使用 glob 找到符合模式的所有 CSV 檔案'success': False, 'message': '未找到符合條件的 CSV 檔案', 'output_file': None, 'total_files': 0, 'total_rows': 0}
        pattern = os.path.join(folder_path, "PSN_monitoring_*.csv")
        csv_files = glob.glob(pattern)
        
         # 2. 過濾出指定日期範圍的檔案
        filtered_files = []
        for file in csv_files:
            filename = os.path.basename(file)
            try:
                # 解析檔名: PSN_monitoring_20251230.csv
                # split('_')[2] -> "20251230.csv"
                # [:8] -> "20251230"
                date_str = filename.split('_')[2][:8] 
                file_date = datetime.strptime(date_str, '%Y%m%d')
                
                if start_date <= file_date <= end_date:
                    filtered_files.append(file)
            except Exception as e:
                logging.warning(f"無法解析檔案日期: {filename}, 錯誤: {e}")
                continue
        
        # ★★★ 關鍵優化：確保檔案按檔名（即日期）排序 ★★★
        # 這樣跨年時，20251230 才會排在 20260101 前面
        filtered_files.sort()

        # # 過濾出指定日期範圍的檔案
        # filtered_files = []
        # for file in csv_files:
        #     filename = os.path.basename(file)
        #     try:
        #         date_str = filename.split('_')[2].split('.')[0][:8]
        #         print(date_str)
        #         file_date = datetime.strptime(date_str, '%Y%m%d')
        #         if start_date <= file_date <= end_date:
        #             filtered_files.append(file)
        #     except Exception as e:
        #         logging.warning(f"無法解析檔案日期: {filename}, 錯誤: {e}")
        #         continue
        
        logging.info(f"找到 {len(filtered_files)} 個符合條件的檔案")
        
        # 讀取並合併所有 CSV 檔案
        dfs = []
        for file in filtered_files:
            try:
                df = pd.read_csv(file, encoding='utf-8')
                df['source_file'] = os.path.basename(file)

                # if 'status' in df.columns:
                #     df = df.drop(columns=['status'])

                dfs.append(df)
                logging.info(f"已讀取: {os.path.basename(file)}")
            except Exception as e:
                logging.error(f"讀取檔案 {file} 時發生錯誤: {e}")
        
        # 合併所有 DataFrame
        if dfs:
            combined_df = pd.concat(dfs, ignore_index=True)
            
            # 只取B1的檔案
            combined_df['Factory'] = combined_df['device_name'].str.split('_').str[0]
            combined_df = combined_df[combined_df['Factory'] == "B1"]
            
            # 儲存合併後的檔案
            output_file = os.path.join(folder_path, f"PSN_monitoring_combined_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.csv")
            
            result = {
                'success': True,
                'message': '合併完成！',
                'output_file': output_file,
                'total_files': len(dfs),
                'total_rows': len(combined_df)
            }
            
            logging.info(f"合併完成！總共合併了 {len(dfs)} 個檔案，合併後總行數: {len(combined_df)}")
            logging.info(f"輸出檔案: {output_file}")
            return combined_df
        else:
            return {
                'success': False,
                'message': '未找到符合條件的 CSV 檔案',
                'output_file': None,
                'total_files': 0,
                'total_rows': 0
            }
            
    except Exception as e:
        logging.error(f"執行過程中發生錯誤: {e}")
        return {
            'success': False,
            'message': f'執行過程中發生錯誤: {str(e)}',
            'output_file': None,
            'total_files': 0,
            'total_rows': 0
        }


# @st.cache_data(ttl=3600)
def st_process_psn_data(start_date, end_date, Building):
    """
    後端處理函數：合併指定日期範圍內的PSN監控數據
    
    Args:
        start_date (str or datetime): 開始日期，格式 'YYYY-MM-DD' 或 datetime 物件
        end_date (str or datetime): 結束日期，格式 'YYYY-MM-DD' 或 datetime 物件  
        folder_path (str, optional): 資料夾路徑，預設為固定路徑
    
    Returns:
        dict: 包含處理結果的字典
            - success (bool): 是否成功
            - message (str): 處理訊息
            - output_file (str): 輸出檔案路徑
            - total_files (int): 合併檔案數量
            - total_rows (int): 總行數
    """
    try:
        # 設定預設資料夾路徑
        folder_path = r"D:\Paticle_OOB_system\BP_PSN_OOB_Record_test"

        # 轉換日期格式
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, '%Y%m%d')
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, '%Y%m%d')
        
        # 驗證日期範圍
        if start_date > end_date:
            return {
                'success': False,
                'message': '起始日期不能晚於結束日期',
                'output_file': None,
                'total_files': 0,
                'total_rows': 0
            }

        logging.info(f"開始合併指定期間的數據: {start_date.strftime('%Y%m%d')} 到 {end_date.strftime('%Y%m%d')}")
        
        # 使用 glob 找到符合模式的所有 CSV 檔案'success': False, 'message': '未找到符合條件的 CSV 檔案', 'output_file': None, 'total_files': 0, 'total_rows': 0}
        pattern = os.path.join(folder_path, "PSN_monitoring_*.csv")
        csv_files = glob.glob(pattern)
        
         # 2. 過濾出指定日期範圍的檔案
        filtered_files = []
        for file in csv_files:
            filename = os.path.basename(file)
            try:
                date_str = filename.split('_')[2][:8] 
                file_date = datetime.strptime(date_str, '%Y%m%d')
                
                if start_date <= file_date <= end_date:
                    filtered_files.append(file)
            except Exception as e:
                logging.warning(f"無法解析檔案日期: {filename}, 錯誤: {e}")
                continue
        
        # ★★★ 關鍵優化：確保檔案按檔名（即日期）排序 ★★★
        # 這樣跨年時，20251230 才會排在 20260101 前面
        filtered_files.sort()
        logging.info(f"找到 {len(filtered_files)} 個符合條件的檔案")
        
        # 讀取並合併所有 CSV 檔案
        dfs = []
        for file in filtered_files:
            try:
                df = pd.read_csv(file, encoding='utf-8',low_memory=False)
                # print(df)
                df['source_file'] = os.path.basename(file)
                dfs.append(df)
                print(f"已讀取: {os.path.basename(file)}")
                logging.info(f"已讀取: {os.path.basename(file)}")
            except Exception as e:
                logging.error(f"讀取檔案 {file} 時發生錯誤: {e}")
        
        # 合併所有 DataFrame
        if dfs:
            combined_df = pd.concat(dfs, ignore_index=True)
            
            # 只取B1的檔案
            combined_df['Factory'] = combined_df['device_name'].str.split('_').str[0]
            combined_df = combined_df[combined_df['Factory'] == "B1"]
            print(combined_df)
            # combined_df = combined_df[combined_df['Building'] == Building]
            # 支援 Building 傳入單一字串 或 list/tuple/set
            if isinstance(Building, (list, tuple, set)):
                combined_df = combined_df[combined_df['Building'].isin(Building)]
            else:
                combined_df = combined_df[combined_df['Building'] == Building]
                
            # 儲存合併後的檔案
            output_file = os.path.join(folder_path, f"PSN_monitoring_combined_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.csv")
            
            result = {  
                'success': True,
                'message': '合併完成！',
                'output_file': output_file,
                'total_files': len(dfs),
                'total_rows': len(combined_df)
            }
            
            logging.info(f"合併完成！總共合併了 {len(dfs)} 個檔案，合併後總行數: {len(combined_df)}")
            logging.info(f"輸出檔案: {output_file}")
            return combined_df
        else:
            return {
                'success': False,
                'message': '未找到符合條件的 CSV 檔案',
                'output_file': None,
                'total_files': 0,
                'total_rows': 0
            }
            
    except Exception as e:
        logging.error(f"執行過程中發生錯誤: {e}")
        return {
            'success': False,
            'message': f'執行過程中發生錯誤: {str(e)}',
            'output_file': None,
            'total_files': 0,
            'total_rows': 0
        }

# 直接查詢資料夾下所有的 CSV，並按時間過濾
# @st.cache_data(ttl=3600)
# def load_data_duckdb(start_date, end_date):
#     sql = f"""
#     SELECT *
#     FROM read_csv(
#     'D:/Paticle_OOB_system/BP_PSN_OOB_Record/*.csv',
#     delim=',',
#     header=true,
#     strict_mode=false,
#     null_padding=true,
#     ignore_errors=true,
#     union_by_name=true
#     )
#     WHERE TRY_CAST(timestamp AS TIMESTAMP)
#         BETWEEN TIMESTAMP '{start_date}'
#             AND TIMESTAMP '{end_date}'
#     """
#     return duckdb.query(sql).df()

@st.cache_data(ttl=3600)
def load_data_duckdb(start_date, end_date, buildings=None):
    """
    從分區 Parquet 檔案讀取數據
    :param start_date: 起始時間字串 (e.g., '2025-11-21 00:00:00')
    :param end_date: 結束時間字串
    :param buildings: 樓層清單或字串 (e.g., ['K18-6F', 'K18-5F'] 或 'K22')
    """
    end_date = pd.to_datetime(end_date) + timedelta(days=1)
    # 路徑指向根目錄，DuckDB 會自動往下挖所有 Building=... 的資料夾
    base_path = 'D:/Paticle_OOB_system/BP_PSN_OOB_Record/Record_Parquet/*/*.parquet'
    
    # 基礎 SQL 語句 (使用 read_parquet 並啟動 hive_partitioning)
    # 我們在這裡選取需要的欄位，或者用 * 取代
    sql_base = f"""
                SELECT * 
                FROM read_parquet(
                '{base_path}',
                hive_partitioning=True,
                union_by_name = True)
                """
    
    # 建立過濾條件
    conditions = []
    
    # 時間過濾 (假設 timestamp 在 Parquet 裡是字串或 TIMESTAMP)
    conditions.append(f"CAST(timestamp AS TIMESTAMP) BETWEEN '{start_date}' AND '{end_date}'")
    
    # 樓層過濾 (參數化處理)
    if buildings:
        if isinstance(buildings, str):
            conditions.append(f"Building = '{buildings}'")
        elif isinstance(buildings, (list, tuple)) and len(buildings) > 0:
            # 轉換成 SQL 格式的清單，例如 ('K18-6F', 'K18-5F')
            b_tuple = str(tuple(buildings)) if len(buildings) > 1 else f"('{buildings[0]}')"
            conditions.append(f"Building IN {b_tuple}")
            
    # 組合完整的 SQL
    where_clause = " WHERE " + " AND ".join(conditions)
    final_sql = sql_base + where_clause
    
    # 執行查詢並回傳 DataFrame
    result_df = duckdb.query(final_sql).df()
    result_df['out_of_OOC_count_spec'] = result_df['out_of_OOC_count_spec'].astype(int)
    return result_df

# # --- 組合 SQL ---
# sql = f"""
# SELECT *
# FROM read_parquet('{parquet_base_path}', hive_partitioning=True)

# parquet_path = 'D:/Paticle_OOB_system/BP_PSN_OOB_Record/Parquet/*.parquet'
# sql = f"""
# SELECT *
# FROM read_parquet('{parquet_path}', union_by_name=true)
# WHERE Building IN ('K18-6F','K18-5F')
#     AND timestamp BETWEEN '{start_date}' AND  '{end_date}'
# """

def main():
    """原有的互動式主函數，保留向後相容性"""
    try:
        # 設定資料夾路徑
        folder_path = r"D:\Paticle_OOB_system\BP_PSN_OOB_Record_test"
        
        # 計算預設日期範圍
        # today = datetime.now()
        # default_start = datetime(2025, 7, 30)
        # default_end = datetime.now() - timedelta(days=1)
        
        # 讓使用者自訂日期
        # print("請輸入要合併的日期範圍：")
        # last_monday = get_date_input("起始日期", default_start)
        # last_sunday = get_date_input("結束日期", default_end)
        
        # 呼叫後端處理函數
        # result = process_psn_data(last_monday, last_sunday, folder_path)

        # default_start = datetime.now() - timedelta(days=1)
        # default_end = datetime.now() 
        default_start = "20260224"
        default_end = "20260226" 

        print(default_start)
        print(default_end)

        # result = load_data_duckdb( default_start, default_end, ["K18-6F"])
        result = st_process_psn_data( default_start, default_end, "K18-6F")

        print(result)
        # print(result[result['out_of_OOC_count_spec']==1])

        # if result['success']:
        #     print(f"✅ {result['message']}")
        #     print(f"📁 輸出檔案: {result['output_file']}")
        #     print(f"📊 合併檔案數: {result['total_files']}")
        #     print(f"📈 總行數: {result['total_rows']}")
        #     return True
        # else:
        #     print(f"❌ {result['message']}")
        #     return False
            
    except Exception as e:
        logging.error(f"執行過程中發生錯誤: {e}")
        return False

# 使用範例
if __name__ == "__main__":
    # 方式1: 互動式執行
    main()

    # 方式2:  
    # result = process_psn_data('20260101', '20260102')
    # print(result)
