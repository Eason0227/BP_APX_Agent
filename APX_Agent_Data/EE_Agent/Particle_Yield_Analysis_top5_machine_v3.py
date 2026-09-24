import pandas as pd
import plotly.graph_objects as go
import numpy as np
import sys
import os
from datetime import datetime, timedelta, date
import time
from pathlib import Path
# from utils.Get_OOB_result_v2 import load_data_duckdb
from utils.Get_OOB_result_v2 import st_process_psn_data, load_data_duckdb
import plotly.io as pio
import kaleido

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.header import Header
import smtplib
import requests
import configparser
import schedule
import base64
import json

# ==================== 常數定義 ====================

MACHINE_FILTER_LIST = [
    "B1_COAT_03", "B1_COAT_05", "B1_COAT_02", "B1_COAT_04",
    "B1_STEP_01", "B1_STEP_02", "B1_STEP_03",
    "B1_OVEN_01", "B1_OVEN_02", "B1_OVEN_03",
    "B1_DEVP_01", "B1_DEVP_03",
    "B1_DSCM_01", "B1_DSCM_02", "B1_DSCM_03",
    "B1_SPUT_01", "B1_SPUT_02", "B1_SPUT_03", "B1_SPUT_04",
    "B1_SCOP_01", "B1_SCOP_02", "B1_SCOP_03", "B1_SCOP_04", "B1_SCOP_05", "B1_SCOP_09",
    "B1_AGST_03", "B1_AGST_04", "B1_AGST_01", "B1_AGST_02",
    "B1_BPHI_01", "B1_BPHI_02", "B1_BPHI_03",
    "B1_REFW_01", "B1_BAKE_01"
]

TRACK_DIR = r"D:\Paticle_OOB_system\Particle_OOB_Yield_anaysis\PSN_Yield_tracking_data_v2"
OUTPUT_DIR = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\EE_Agent\particle_yield_analysis_machine_report"

# TRACK_DIR = r"C:\Users\K18069\Desktop\PSN_Yield_tracking_data"
# OUTPUT_DIR = r"\\Khfs1\8n00$\8N40\Colin\share\PSN"    

# ==================== 預設參數配置 ====================
# 預設為前兩週  
start_date = date(2026,7,29)
end_date = date(2026,9,16)

# 批次回推設定：由基準週往前每次 7 天，直到指定月份（含）
enable_backfill_to_month = True
backfill_stop_month = 7

# end_date = datetime.now().date() - timedelta(days=6)
# start_date = end_date - timedelta(days=6)

date_range = (start_date, end_date)

# 分析模式：全部 LOT
lot_selection_mode = "全部 LOT"
# top_n_lots = 6

# 不啟用機台篩選
enable_machine_filter = False
machine_filter_list = None

# Inspection Type: 全部（不篩選）
selected_inspection = 'AEI+API'

# ==================== 資料載入與處理函數 ====================
def load_and_process_data(track_dir, date_range, use_all_lots=False, machine_filter=None):
    """載入並處理資料"""
    try:
        start_date, end_date = date_range
        track_files = list(Path(track_dir).glob("*.xlsx"))

        if not track_files:
            print("資料夾內找不到 Excel 檔案")
            return None

        # 只取最新(最後修改時間)的一個檔案
        latest_file = max(track_files, key=lambda p: p.stat().st_mtime)
        track_files = [latest_file]
        print(f"使用最新檔案: {latest_file.name}")

        df_list = []
        for file in track_files:
            df = pd.read_excel(file)
            df = df[['LOT_NO','LOT_RDL_INSPECTION','INSPECTION_DATE','INSPECTION','TRACK_IN_TIME','TRACK_OUT_TIME','PARTICLE_QTY','DIE_QTY','OPER','MACHINE_NO']]
            df_list.append(df)

        # 資料載入錯誤: can only concatenate str (not "int") to str
        track_df = pd.concat(df_list, ignore_index=True)                
        track_df = track_df.dropna(subset=['MACHINE_NO'])
        track_df['SCHE_NO'] = track_df['LOT_RDL_INSPECTION'].str.split('_').str[0]
        track_df['INSPECTION_DATE'] = pd.to_datetime(track_df['INSPECTION_DATE'], format='%Y/%m/%d %H:%M:%S', errors='coerce')
        track_df = track_df[track_df['INSPECTION_DATE'].between(pd.Timestamp(start_date), pd.Timestamp(end_date))]
        track_df['TRACK_IN_TIME'] = pd.to_datetime(track_df['TRACK_IN_TIME'], format='%Y/%m/%d %H:%M:%S', errors='coerce')
        track_df['TRACK_OUT_TIME'] = pd.to_datetime(track_df['TRACK_OUT_TIME'], format='%Y/%m/%d %H:%M:%S', errors='coerce')
        track_df['Particle loss'] = track_df['PARTICLE_QTY'] / track_df['DIE_QTY']
        track_df.rename(columns={'MACHINE_NO': 'Machine ID'}, inplace=True)
        
        df = track_df.drop_duplicates(['LOT_RDL_INSPECTION']).copy()
        df.rename(columns={'LOT_NO': 'LOT_ID'}, inplace=True)

        lot_stats = df.copy()
        lot_stats.index = df['LOT_RDL_INSPECTION']
        lot_stats = lot_stats.sort_values('Particle loss', ascending=False)

        if machine_filter:
            track_df = track_df[track_df['Machine ID'].isin(machine_filter)].copy()
        
        # if use_all_lots:
            # lot_stats_filtered = lot_stats[lot_stats['Particle loss'] >= 0]
        selected_lot_ids = lot_stats.index.tolist()
        top_worst_lots = lot_stats

        
        track_info = track_df[track_df['LOT_RDL_INSPECTION'].isin(selected_lot_ids)].copy()
        track_info = track_info.drop_duplicates(['LOT_RDL_INSPECTION','OPER'],keep="first")
        
        return df, lot_stats, top_worst_lots, track_info, selected_lot_ids
        
    except Exception as e:
        print(f"資料載入錯誤: {str(e)}")
        return None, None, None, None, None

def load_sensor_data(track_df):
    """載入 Sensor 資料"""
    try:
        # print(track_df)
        min_time = track_df['TRACK_IN_TIME'].min()
        max_time = track_df['TRACK_OUT_TIME'].max() 
        start_date_str = min_time.strftime('%Y%m%d')
        end_date_str = max_time.strftime('%Y%m%d')
        print("tracking min time", min_time)
        sensor_df = load_data_duckdb(min_time, max_time, "K18-6F")
        # sensor_df = st_process_psn_data(start_date_str, end_date_str, "K18-6F")


        sensor_df['timestamp'] = pd.to_datetime(sensor_df['timestamp'])
        relevant_machines = track_df['Machine ID'].unique()
        sensor_df = sensor_df[sensor_df['Machine_ID'].isin(relevant_machines)].copy()
        sensor_df = sensor_df.sort_values(['Machine_ID', 'timestamp'])
        
        return sensor_df
    except Exception as e:
        print(f"警告: Sensor 資料載入失敗: {str(e)}")
        return None

def analyze_sensor_correlation(top_lot_ids, track_info, sensor_df, lot_stats):
    """分析 Sensor 與 Yield Loss 的關聯性"""
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
                'DIE_QTY': lot_data['DIE_QTY'].values[0]
            }
    
    results = []
    sensor_grouped = {name: group for name, group in sensor_df.groupby('Machine_ID')}
    
    for _, track_row in relevant_track.iterrows():
        machine_id = track_row['Machine ID']
        
        if machine_id not in sensor_grouped:
            continue
            
        machine_sensor = sensor_grouped[machine_id]
        
        mask = (machine_sensor['timestamp'] >= track_row['TRACK_IN_TIME']) & \
               (machine_sensor['timestamp'] <= track_row['TRACK_OUT_TIME'])
        
        filtered_sensor = machine_sensor.loc[mask]

        if len(filtered_sensor) == 0:
            continue
        
        total_records = len(filtered_sensor)
        ooc_count = filtered_sensor['out_of_pi_spec'].sum()
        
        # if ooc_count > 0:
        lot_id = track_row['LOT_RDL_INSPECTION']
        lot_info = lot_stats_dict.get(lot_id, {})
        
        results.append({
            'LOT_ID': lot_id,
            'OPER': track_row['OPER'],
            'Machine_ID': machine_id,
            'PI_value': filtered_sensor['PI_value'].mean(),
            'OOC_Count': ooc_count,
            'OOC_Rate_%': round((ooc_count / total_records * 100), 2),
            'OOS_Status': filtered_sensor['out_of_OOC_count_spec'].sum(),
            'Total_Records': total_records,
            'TRACK_IN_TIME': str(track_row['TRACK_IN_TIME']),
            'TRACK_OUT_TIME': str(track_row['TRACK_OUT_TIME']),
            'DIE_QTY': lot_info.get('DIE_QTY', 0),
            'Particle_yield_loss': lot_info.get('Particle_yield_loss', 0),
            'Particle_loss_Qty': lot_info.get('Particle_loss_Qty', 0)
        })
    
    lot_sensor_analysis = {}
    for result in results:
        lot_id = result['LOT_ID']   
        if lot_id not in lot_sensor_analysis:
            lot_sensor_analysis[lot_id] = []
        lot_sensor_analysis[lot_id].append(result)
    
    return lot_sensor_analysis

# ==================== 郵件通知相關程式碼 ====================
class alarm_information():
    def __init__(self):
        # 目前用手動設定
        self.alarm_mail = ["Easonch_Tsai@aseglobal.com"]
        self.cc = ["Easonch_Tsai@aseglobal.com"]
        # self.alarm_mail = ["ASEK_Bumping_Eng3_EEB1@aseglobal.com"]
        # self.cc = ["VincentYR_Lee@aseglobal.com","Easonch_Tsai@aseglobal.com","EnRui_Chang@aseglobal.com"]

class mail_content():
    def __init__(self, sender, title, msg_content=None):
        self.sender = sender
        self.sign = "----- ASE Confidentiality Notice ----- \nThe preceding message (including any attachments) contains proprietary information that may be confidential, privileged, or constitute non-public information. It is to be read and used solely by the intended recipient(s) or conveyed only to the designated recipient(s). If you are not an intended recipient of this message, please notify the author or sender immediately either by replying to this message or by telephone at 886-7-3617131 and delete this message (including any attachments hereto) immediately from your system. You should not read ,retain, disseminate, distribute, copy or use this message in whole or in part for any purpose, not disclose all or any part of its content to any other person.\n----- ASE Confidentiality Notice -----"
        self.subject = title
        
        # 使用 send_dispatch_notification 生成的內容
        self.content = f"""
        <html>
        <head>
            <meta charset="UTF-8">
        </head>
        <body style="font-family: 'Microsoft JhengHei', '微軟正黑體', sans-serif; font-size: 12pt; line-height: 1.5;">
            <p><strong>** Security C **</strong></p>
            {msg_content}
            <br><br>
            <div style="font-size: 11pt;">
                Easonch Tsai<br>
                ASE Group<br>
                Tel : 02-77517872 .Fax : +886.2.27186076<br>
                Email Address : Easonch_Tsai@aseglobal.com
            </div>
            <br>
            <div style="font-size: 11pt; border-top: 1px solid #ccc; padding-top: 10px; margin-top: 20px; white-space: pre-line;">
            ----- ASE Confidentiality Notice -----<br>
            The preceding message (including any attachments) contains proprietary information that may be confidential, privileged, or constitute non-public information. It is to be read and used solely by the intended recipient(s) or conveyed only to the designated recipient(s). If you are not an intended recipient of this message, please notify the author or sender immediately either by replying to this message or by telephone at 886-7-3617131 and delete this message (including any attachments hereto) immediately from your system. You should not read ,retain, disseminate, distribute, copy or use this message in whole or in part for any purpose, not disclose all or any part of its content to any other person.<br>
            ----- ASE Confidentiality Notice -----<br>
            <br>
            </div>
        </body>
        </html>
        """
    
def smtp_send(mail_content, receiver, Cc, chart_path=None, excel_path=None):
    try: 
        smtp = smtplib.SMTP('10.12.10.31')
        
        # 建立多部分郵件
        message = MIMEMultipart('mixed')
        message['From'] = Header(f"{mail_content.sender} <Easonch_Tsai@aseglobal.com>", 'utf-8')
        message['To'] = Header(', '.join(receiver), 'utf-8')
        message['Cc'] = Header(', '.join(Cc), 'utf-8')
        message['Subject'] = Header(mail_content.subject, 'utf-8')
        
        # 建立 related 子部分（HTML + 內嵌圖片）
        related_part = MIMEMultipart('related')
        
        # 添加HTML內容
        html_part = MIMEText(mail_content.content, 'html', 'utf-8')
        related_part.attach(html_part)
        
        
        # 如果有圖片路徑，將圖片作為內嵌附件
        if chart_path and os.path.exists(chart_path):
            try:
                with open(chart_path, 'rb') as f:
                    img_data = f.read()
                    img = MIMEImage(img_data, _subtype='jpeg')
                    img.add_header('Content-ID', '<chart_image>')
                    img.add_header('Content-Disposition', 'inline', filename='chart.jpg')
                    related_part.attach(img)
                    print(f"✓ 圖片已附加為內嵌圖片 (CID)")
            except Exception as e:
                print(f"   ✗ 附加圖片時發生錯誤: {e}")
 
        message.attach(related_part)

        # 附加 Excel 檔案
        if excel_path and os.path.exists(excel_path):
            try:
                with open(excel_path, 'rb') as f:
                    excel_data = f.read()
                excel_attachment = MIMEApplication(excel_data, _subtype='vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                excel_filename = os.path.basename(excel_path)
                excel_attachment.add_header('Content-Disposition', 'attachment', filename=excel_filename)
                message.attach(excel_attachment)
                print(f"✓ Excel 檔案已附加: {excel_filename}")
            except Exception as e:
                print(f"   ✗ 附加 Excel 時發生錯誤: {e}")

        # 合併收件人列表
        all_recipients = receiver + Cc
        # print(f"發送郵件至: {', '.join(all_recipients)}")
        
        smtp.sendmail(mail_content.sender, all_recipients, message.as_string())
        smtp.quit()
        # print("郵件發送成功")
        return True
    except Exception as ex:
        print(f"smtp_send 發生錯誤: {ex}")
        return False


def generate_report_html(top_lot_ids, machine_stats, top5_max, top5_max_all_normal, top5_max_particle_loss, date_range, use_cid=True):
    """生成 HTML 郵件內容"""
    start_date, end_date = date_range
    date_str = f"{start_date.strftime('%Y/%m/%d')} ~ {end_date.strftime('%Y/%m/%d')}"
    
    # 將 top5_max 轉換為 HTML 表格
    top5_html = top5_max.to_html(index=False, 
                                classes='table',
                                border=1, 
                                float_format=lambda x: f'{x:.2f}' if isinstance(x, float) else x,
                                table_id = 'top5_table'
                                )

    # 將 top5_max_all_normal 轉換為 HTML 表格
    top5_max_all_html = top5_max_all_normal.to_html(index=False, 
                                                    classes='table',
                                                    border=1, 
                                                    float_format=lambda x: f'{x:.2f}' if isinstance(x, float) else x,
                                                    table_id = 'top5_table'
                                                    )

    # 將 top5_max_particle_loss 轉換為 HTML 表格
    top5_particle_loss_html = top5_max_particle_loss.to_html(index=False, 
                                classes='table',
                                border=0, 
                                float_format=lambda x: f'{x:.2f}' if isinstance(x, float) else x,
                                table_id = 'top5_particle_loss_table'
                                )
    
    # 為第二個表格添加內聯樣式，確保在郵件中正確顯示
    top5_particle_loss_html = top5_particle_loss_html.replace(
        '<table',
        '<table style="width: auto; max-width: 600px; border-collapse: collapse; margin: 20px 0; font-size: 11pt;"'
    ).replace(
        '<th>',
        '<th style="background-color: #2ca02c; color: white; padding: 10px 15px; text-align: left; font-weight: bold; border: 1px solid #ddd;">'
    ).replace(
        '<td>',
        '<td style="padding: 8px 15px; border: 1px solid #ddd; text-align: left;">'
    )
    
    if use_cid:
        chart_img_html = "<img src='cid:chart_image'"

    msg = f"""
    <div style="font-family: 'Microsoft JhengHei', '微軟正黑體', sans-serif;">
        <p><strong>Dear sir:</strong></p>

        <h2 style="color: #1f77b4;">Particle Yield OOB Analysis - TOP 5 Machine Report</h2>
        <p><strong>分析期間:</strong> {date_str}</p>
        <p><strong>分析範圍:</strong> 共 {len(top_lot_ids)} LOT </p>
        <p><strong>分析Lot:</strong> { top_lot_ids } </p>

        <p><strong>Inspection Type:</strong> AEI+API (全部)</p>
        
        <hr style="border: 1px solid #ccc; margin: 20px 0;">
        
        <h3 style="color: #ff7f0e;">機台 OOB Fail Rate 分析圖表</h3>
        <p>以下圖表顯示各機台的 <strong>平均 OOB Fail Rate</strong>、<strong>DIE QTY</strong> 及 <strong>Particle Loss Rate</strong> 統計：</p>
        
        {chart_img_html}

        <hr style="border: 1px solid #ccc; margin: 20px 0;">
        
        <h3 style="color: #ff7f0e;">前 5 名關鍵機台</h3>
        <p>根據 <strong> Weighted Risk index </strong> 最高的5部機台 (Weighted Risk index需 > 0) ，以下需要優先關注：</p>
        <style>
            #top5_table {{
                width: auto;
                max-width: 600px;
                border-collapse: collapse;
                margin: 20px 0;
                font-size: 11pt;
            }}
            #top5_table th {{
                background-color: #1f77b4;
                color: white;
                padding: 10px 15px;
                text-align: left;
                font-weight: bold;
                border: 1px solid #ddd;
            }}
            #top5_table td {{
                padding: 8px 15px;
                border: 1px solid #ddd;
                text-align: left;
            }}
            #top5_table tr:nth-child(even) {{
                background-color: #f9f9f9;
            }}
            #top5_table tr:hover {{
                background-color: #f0f0f0;
            }}
        </style>
        
        <p> 原始數據 </p>
        <div style="margin: 20px 0;">
            {top5_html}
        </div>

        <p><strong> Move Count </strong> :分析期間所有lot在某個機台進出的總次數 <br>
        <strong> Total Die Qty </strong> :分析期間某個機台處理過的總DIE數 </p>

        <p> 正規化後數據 </p>
        <div style="margin: 20px 0;">
            {top5_max_all_html}
        </div>

        <p> <strong> Weighted Risk index </strong> = (w1 × OOB Fail Rate Score) + ( w2 × Particle Yield Loss Rate Score) + ( w3 ×  DIE QTY Score ) <br>
        其中每項指標皆經過正規化(Normalization)轉換，指標權重 w1 (OOB Rate) = 40%，w2 (Loss Rate) = 40%，w3 ( DIE QTY ) = 20% <br>
        <strong> OOB Fail Rate Score </strong> = Normalize( OOB Fail Rate ) <br>
        <strong> DIE QTY Score </strong> = Normalize( DIE QTY ) <br>
        <strong> Particle Yield Loss Rate Score </strong> = Normalize( Particle Yield Loss Rate ) <br>
        </p>

        <div style="background-color: #f0f2f6; padding: 15px; border-radius: 5px; border-left: 5px solid #1f77b4; margin: 20px 0;">
            <h4 style="color: #333; margin-top: 0;"><br>機台集中性洞察</h4>
            <p>在問題 LOT 的生產過程中，機台 <strong>{', '.join(top5_max['Machine_ID'].values)}</strong> 
            作業時的<strong> Particle OOB Fail rate 觸發頻率過高</strong> 且涉及 <strong>Particle Qty loss 數量多</strong>，
            這可能表明這些機台存在潛在的環境問題，建議針對機台進行檢查。</p>
        </div>
            
        <div style="margin-top: 15px; padding-top: 15px; border-top: 1px solid #d0d0d0;">
            <strong>請相關人員至以下系統填寫機台處理表單:</strong></p>
            <a href="https://khpsn.kh.asegroup.com:8443/BP_AI_Particle_Expert_System/[%E6%B8%AC%E8%A9%A6]_%E6%A9%9F%E5%8F%B0%E9%9B%86%E4%B8%AD%E6%80%A7_feedback" 
            style="color: #1f77b4; 
                  text-decoration: underline;
                  font-weight: blod;"
            target = "_blank">
            前往填寫處理表單
            </a>
        </div>

        <hr style="border: 1px solid #ccc; margin: 20px 0;">
        
        <h3 style="color: #2ca02c;">其他需關注機台 (依Particle Yield Loss Rate 平均)</h3>
        <p>排除前述 Top5 關鍵機台後，根據 <strong>Particle Yield Loss Rate 平均</strong> 排序，以下機台雖未出現在綜合評分前5名，但Yield loss同樣偏高</p>

        <div style="margin: 20px 0;">
            {top5_particle_loss_html}
        </div>
        
        <div style="background-color: #f0f9f4; padding: 15px; border-radius: 5px; border-left: 5px solid #2ca02c; margin: 20px 0;">
            <h4 style="color: #333; margin-top: 0;">Particle Loss 洞察</h4>
            <p>機台 <strong>{', '.join( top5_max_particle_loss['Machine_ID'].values )}</strong> 
            雖未出現在綜合評分前5名，但在分析期間累積了最高的 Particle Yield Loss Rate，
            建議針對這些機台進行 <strong>Particle 污染源調查</strong> 及 <strong>機台清潔保養檢查</strong>。</p>
        </div>

        <hr style="border: 1px solid #ccc; margin: 20px 0;">
        
        <p style="font-size: 11pt; color: #666;">APX System</p>
    </div>
    """

    title = f"【Particle Yield Analysis】機台集中性分析報告 ({date_str}) (Security C)"
    return title, msg

def send_report_email(top_lot_ids, machine_stats, top5_max, top5_max_all_normal, top5_max_particle_loss, date_range, chart_path, excel_path=None):
    """
    發送 Particle Yield Analysis 報告郵件
    
    Returns:
        bool: 郵件發送成功回傳 True，失敗回傳 False
    """
    try:
        sender = "Particle OOB System"
        alarm_info = alarm_information()

        use_cid = os.path.exists(chart_path) if chart_path else False

        # 產生郵件標題和內容
        title, msg_content = generate_report_html(top_lot_ids, machine_stats, top5_max, top5_max_all_normal,top5_max_particle_loss, date_range, use_cid)
        
        # 建立郵件內容
        content = mail_content(sender, title, msg_content)
        
        # 發送郵件
        success = smtp_send(content, alarm_info.alarm_mail, alarm_info.cc, chart_path if use_cid else None, excel_path)
        
        if success:
            print(f"已發送郵件通知...")
            return True
        else:
            print("郵件發送失敗")
            return False
            
    except Exception as e:
        print(f"發送通知時發生錯誤: {e}")
        return False

def main(target_date_range=None, send_email=True):
    """
    主要執行函數
    """
    def normalize(series):
        """將數值正規化到 0-100。"""
        if series.min() != series.max():
            return (series - series.min()) / (series.max() - series.min()) * 100
        return 0

    def build_sensor_analysis_df(top_lot_ids, sensor_analysis):
        """將 lot 分析結果彙整為 DataFrame。"""
        frames = [
            pd.DataFrame(sensor_analysis[lot_id])
            for lot_id in top_lot_ids
            if lot_id in sensor_analysis and sensor_analysis[lot_id]
        ]
        if not frames:
            return pd.DataFrame()

        result_df = pd.concat(frames, ignore_index=True)
        result_df['Insepection'] = result_df['LOT_ID'].str.split('_').str[2]
        return result_df.sort_values(['TRACK_IN_TIME'])

    run_date_range = target_date_range if target_date_range else date_range

    print("="*80)
    print("Particle Yield OOB Analysis - TOP 5 Machine Report")
    print(f"執行時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"分析期間: {run_date_range[0]} ~ {run_date_range[1]}")
    print("="*80)
    
    total_start_time = time.time()
    
    # 載入並處理資料
    print("\n[1/4] 載入並處理資料...")
    step_start = time.time()
    use_all_lots = True

    df, lot_stats, top_worst_lots, track_info, top_lot_ids = load_and_process_data(
        TRACK_DIR, run_date_range, use_all_lots, machine_filter_list
    )
    print(f"   完成 (耗時: {time.time() - step_start:.2f} 秒)")

    if df is None:
        print("\n❌ 資料載入失敗，程式結束")
        return False

    print(f"   - 總 LOT 數: {len(top_lot_ids)}")
    print(f"   - 追蹤記錄數: {len(track_info)}")

    # 載入 Sensor 資料
    print("\n[2/4] 載入 Sensor 資料...")
    step_start = time.time()
    sensor_df = load_sensor_data(track_info) 
    # print( sensor_df['Machine_ID'].unique() )   
    # print(sensor_df[sensor_df['Machine ID']== "B1_COAT_03"])
    print(f"   完成 (耗時: {time.time() - step_start:.2f} 秒)")
    
    if sensor_df is None:
        print("\n❌ Sensor 資料載入失敗，程式結束")
        return False
    
    # Sensor 關聯分析
    print("\n[3/4] 進行 Sensor 關聯分析...")
    step_start = time.time()
    sensor_analysis = analyze_sensor_correlation(top_lot_ids, track_info, sensor_df, lot_stats)
    # print("sensor_analysis",sensor_analysis)
    print(f"   完成 (耗時: {time.time() - step_start:.2f} 秒)")
    
    if sensor_analysis is None or len(sensor_analysis) == 0:
        print("\n❌ Sensor 關聯分析失敗，程式結束")
        return False
    
    # 生成報告
    print("\n[4/4] 生成報告並發送郵件...")
    step_start = time.time()

    # 整理分析資料
    sensor_analysis_df = build_sensor_analysis_df(top_lot_ids, sensor_analysis)

    # 機台做的總die數
    machine_die_total = (
        track_info.groupby("Machine ID",as_index =False)['DIE_QTY']
        .sum()
        .sort_values("DIE_QTY",ascending=False)
    )


    # 機台出現過幾次
    machine_move_count_total = (
        track_info.groupby("Machine ID",as_index =False)['DIE_QTY']
        .count()
        .sort_values("DIE_QTY",ascending=False)
    )

    machine_stats = sensor_analysis_df.groupby('Machine_ID',as_index =False).agg({
        'OOC_Rate_%': ['mean', 'max', 'count'],
        "PI_value": ['mean'],
        'Total_Records': 'sum',
        'Particle_yield_loss': ['mean', 'sum'],
        'Particle_loss_Qty': ['sum'],
        # 'DIE_QTY': "sum",
    }).round(4)

    machine_stats.columns = ["Machine_ID",'Avg_OOC_Rate', 'Max_OOC_Rate', 'LOT_Count', 'avg_PI_value', 
                            'Total_Records', 'Particle_yield_loss_Mean', 'Particle_yield_loss_sum',
                            'Particle_loss_Qty']

    machine_stats['Total_Die_Qty'] = machine_stats["Machine_ID"].map(machine_die_total.set_index('Machine ID')['DIE_QTY'])
    machine_stats['Move_Count'] = machine_stats["Machine_ID"].map(machine_move_count_total.set_index('Machine ID')['DIE_QTY'])

    machine_stats = machine_stats.sort_values('Particle_yield_loss_Mean', ascending=False)

    # 計算平均值用於參考線
    avg_ooc_rate = machine_stats['Avg_OOC_Rate'].mean()
    avg_particle_loss = machine_stats['Particle_yield_loss_Mean'].mean()

    machine_order = machine_stats['Machine_ID'].tolist()
        
    # 生成圖表
    try:
        fig = go.Figure()
            
        fig.add_trace(go.Bar(
            name='Total Die Qty',
            x=machine_stats['Machine_ID'],
            y=machine_stats['Total_Die_Qty'],
            marker_color='lightblue',
            text=[f"{rate:.0f}" for rate in machine_stats['Total_Die_Qty']],
            textposition='auto',
            hovertemplate='<b>Machine:</b> %{x}<br>' +
                            '<b>Avg OOB Fail Rate:</b> %{customdata[0]}%<br>' +
                            '<b>Total Die Qty:</b> %{customdata[1]}<extra></extra>',
            customdata=list(zip(machine_stats['Avg_OOC_Rate'], 
                                machine_stats['Total_Die_Qty']))
        ))
        
        fig.add_trace(go.Scatter(
            name='Avg OOB Fail Rate',
            x=machine_stats['Machine_ID'],
            y=machine_stats['Avg_OOC_Rate']*50,
            mode='markers+lines+text',
            text=[f"OOB <br> {rate:.1f}%" for rate in machine_stats['Avg_OOC_Rate']],
            textposition='top center',
            marker=dict(
                size=13,
                color='red',
                line=dict(width=2, color='white'),
                opacity=0.8
            ),
            line=dict(
                color='red',
                width=2,
                dash='dot'
            ),
            hovertemplate='<b>Machine:</b> %{x}<br>' +
                            '<b>Avg OOB Fail Rate:</b> %{customdata[0]}%<br>',
            customdata=list(zip(machine_stats['Avg_OOC_Rate'])),
        ))
        
        fig.add_trace(go.Scatter(
            name='Particle_yield_loss_rate',
            x=machine_stats['Machine_ID'],
            y=machine_stats['Particle_yield_loss_Mean']*100,

            mode='markers+text',
            text=[f"yield <br> {rate:.1f}%" for rate in machine_stats['Particle_yield_loss_Mean']*100],
            textposition='top center',
            marker=dict(
                size=17,
                symbol='triangle-up',
                color='green',
                line=dict(width=2, color='white'),
                opacity=0.9
            ),
            hovertemplate='<b>Machine:</b> %{x}<br>' +
                            '<b>Particle yield loss rate:</b> %{customdata[1]:.2f}<br>',
            customdata=list(zip(machine_stats['LOT_Count'], 
                                machine_stats['Particle_yield_loss_Mean'])),
            yaxis='y2'
        ))

        # 加入參考線
        # 水平線：Avg OOC Rate 平均值
        fig.add_hline(
            y=avg_ooc_rate * 150,  # 需要乘以相同的縮放因子
            line_dash="solid",
            line_color="red",
            line_width=2,
            annotation_text=f"Avg OOC Rate 平均: {avg_ooc_rate:.2f}%",
            annotation_position="right",
            annotation_font_size=11,
            annotation_font_color="red"
        )
        
         # 垂直線：找出最接近 Particle Loss Qty 平均值的機台
        machine_stats['diff_from_avg'] = abs(machine_stats['Particle_yield_loss_Mean'] - avg_particle_loss)
        closest_machine_idx = machine_stats['diff_from_avg'].idxmin()
        closest_machine = machine_stats.loc[closest_machine_idx, 'Machine_ID']
        closest_machine_position = machine_order.index(closest_machine)
        
        fig.add_vline(
            x=closest_machine_position,
            line_dash="solid",
            line_color="darkgreen",
            line_width=2,
            annotation_text=f"Particle Loss rate 平均: {avg_particle_loss*100:.2f} %",
            annotation_position="top",
            annotation_font_size=10,
            annotation_font_color="darkgreen"
        )

        fig.update_layout(
            title={
                'text': 'Machine OOB Fail Rate / Yield Loss rate - Statistics<br><sub>柱狀圖： Die Qty | 散佈圖：平均 OOB Fail Rate vs. Particle Loss Rate</sub>',
                'x': 0.5,
                'font': {'size': 16}
            },
            xaxis_title='Machine ID',
            yaxis_title='DIE QTY (Bar)',
            yaxis=dict(side='left'),
            yaxis2=dict(
                title='Particle Yield Loss Rate',
                overlaying='y',
                side='right',
                showgrid=False,
                rangemode = 'tozero'
            ),
            height=600,
            showlegend=True,
            xaxis=dict(
                type='category',
                categoryorder='array',
                categoryarray=machine_order
            )
        )
        
        # 儲存圖表為圖片 - 加入錯誤處理
        chart_path = os.path.join(OUTPUT_DIR, f"machine_analysis_chart/machine_analysis_chart_{run_date_range[0]}_{run_date_range[1]}.png")
        
        print(f"   - 正在生成圖表...")
        
        # 儲存圖表為圖片
        pio.write_image(fig, chart_path, format = "jpg", width=1400, height=600)

    except Exception as e:
        print(f"   ✗ 圖表生成過程發生錯誤: {e}")
        chart_path = None

    # 依指標計算加權風險分數
    # 如果該項數值越高代表越嚴重，就直接歸一化
    machine_stats['Lot_count'] = len(top_lot_ids)
    machine_stats['Total_Die_Qty'] = np.array( machine_stats['Total_Die_Qty']).astype(int)
    machine_stats['Move_Count'] = np.array( machine_stats['Move_Count']).astype(int)

    machine_stats['OOB_Score'] = normalize(machine_stats['Avg_OOC_Rate'])
    machine_stats['QTY_Score'] = normalize(machine_stats['Total_Die_Qty'])
    machine_stats['Loss_Score'] = normalize(machine_stats['Particle_yield_loss_Mean'])

    # 4. 設定權重 (可根據需求調整，加總需為 1.0)
    # 這裡設定 OOB(40%), Loss(40%), QTY(20%)
    w_oob, w_loss, w_qty = 0.4, 0.4, 0.2

    # 5. 計算加權總分 (Total_Score)
    machine_stats['Weighted_Risk_index'] = (
        machine_stats['OOB_Score'] * w_oob +
        machine_stats['Loss_Score'] * w_loss +
        machine_stats['QTY_Score'] * w_qty
    )

    # 6. 依照總分排序，取得 Top 5
    machine_stats_raw = machine_stats.sort_values(by='Weighted_Risk_index', ascending=False).reset_index(drop=True)

    # 儲存統計資料
    output_csv = os.path.join(OUTPUT_DIR, f"machine_stats/machine_stats_{run_date_range[0]}_{run_date_range[1]}.csv")

    # 處理要寄信的數據
    machine_stats = machine_stats_raw.drop(['Max_OOC_Rate','avg_PI_value','Total_Records','Particle_yield_loss_sum','diff_from_avg'], axis=1, errors='ignore')
    machine_stats.to_csv(output_csv, index=False)

    machine_stats = machine_stats.rename( columns={
        "Avg_OOC_Rate":"Avg_OOB_Fail_Rate",
        "Particle_yield_loss_Mean":"Particle_yield_Avg_Loss",
    })
    print(f"   - 統計資料已儲存: {output_csv}")
    machine_stats = machine_stats[ machine_stats["Weighted_Risk_index"] > 0]

    # 計算前 5 名機台，排序依 Weighted_Risk_index，用原始的數據
    top5_max_all = machine_stats.nlargest(5, "Weighted_Risk_index")[[
        "Machine_ID", "Lot_count", "Move_Count", "Total_Die_Qty","Avg_OOB_Fail_Rate","Particle_yield_Avg_Loss","Weighted_Risk_index"
    ]]

    # 計算前 5 名機台，排序依 Weighted_Risk_index，欄位顯示 Normalize 後的分數
    top5_max_all_normal = machine_stats.nlargest(5, "Weighted_Risk_index")[[
        "Machine_ID", "OOB_Score","Loss_Score","QTY_Score","Weighted_Risk_index"
    ]]
    
    print(f"   - 前 5 名關鍵機台: {', '.join( top5_max_all['Machine_ID'].values )}")
    
    # 計算前 5 名機台，排序依 Particle_yield_loss_sum
    top5_max_particle_loss = machine_stats.nlargest(5, "Particle_yield_Avg_Loss")[[
        "Machine_ID", "Avg_OOB_Fail_Rate","Total_Die_Qty", "Particle_yield_Avg_Loss","Weighted_Risk_index"
    ]]
    
    top_5_machine = top5_max_all['Machine_ID'].values
    top5_max_particle_loss = top5_max_particle_loss[~top5_max_particle_loss['Machine_ID'].isin(top_5_machine)]

    # 儲存篩選後的機台統計資料（top5_max_all 及 top5_max_particle_loss 機台），合併兩組機台清單
    all_selected_machines = list(top5_max_all['Machine_ID'].values) + list(top5_max_particle_loss['Machine_ID'].values)
    selected_machine_stats = machine_stats[machine_stats['Machine_ID'].isin(all_selected_machines)].copy()

    # 儲存原始的機台統計資料，取final top5
    machine_stats_raw_top5 = machine_stats_raw[machine_stats_raw['Machine_ID'].isin(all_selected_machines)].copy()

    weighted_top5_set = set(top5_max_all['Machine_ID'].values)
    particle_loss_top5_set = set(top5_max_particle_loss['Machine_ID'].values)

    machine_stats_raw_top5['Source'] = machine_stats_raw_top5['Machine_ID'].apply(
        lambda m: 'top_5_weighted_score'
        if m in weighted_top5_set
        else ('top5_particle_loss' if m in particle_loss_top5_set else 'NA')
    )
    
    # 計算周數（ISO 8601 標準）
    start_week = run_date_range[0].isocalendar()
    end_week = run_date_range[1].isocalendar()
    start_week_str = f"{start_week[0]}-W{start_week[1]:02d}"
    end_week_str = f"{end_week[0]}-W{end_week[1]:02d}"
    
    # 添加周數欄位
    selected_machine_stats['Week_Range'] = f"{start_week_str} ~ {end_week_str}"
    selected_machine_stats['Start_Week'] = start_week_str
    selected_machine_stats['End_Week'] = end_week_str

    # 添加周數欄位，原始資料的top 5
    machine_stats_raw_top5['Week_Range'] = f"{start_week_str} ~ {end_week_str}"
    machine_stats_raw_top5['Start_Week'] = start_week_str
    machine_stats_raw_top5['End_Week'] = end_week_str
    print(machine_stats_raw_top5)

    machine_stats_raw_top5_output_csv = os.path.join(OUTPUT_DIR, rf"top5_machine_stats\top5_machines_{end_week_str}.csv")
    print(machine_stats_raw_top5_output_csv)
    machine_stats_raw_top5.to_csv(machine_stats_raw_top5_output_csv, index=False)

    # output_csv = os.path.join(OUTPUT_DIR, f"machine_stats/machine_stats_{run_date_range[0]}_{run_date_range[1]}.csv")

    # 儲存檔案，檔名使用周數格式
    output_filename = f"top_5_machines_{end_week_str}_(Security C).xlsx"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    selected_machine_stats.to_excel(output_path, index=False, engine='openpyxl')
    # print(f"   - 已儲存 TOP 機台統計資料: {output_filename} (共 {len(selected_machine_stats)} 台機台)")

    # 發送郵件（附上 Excel 檔案）
    success = True
    if send_email:
        success = send_report_email(
            top_lot_ids,
            machine_stats,
            top5_max_all,
            top5_max_all_normal,
            top5_max_particle_loss,
            run_date_range,
            chart_path,
            output_path
        )
    else:
        print("   - 批次模式：略過郵件發送")

    print(f"   完成 (耗時: {time.time() - step_start:.2f} 秒)")
    
    total_time = time.time() - total_start_time
    print(f"\n{'='*80}")
    print(f"總執行時間: {total_time:.2f} 秒")
    print(f"{'='*80}\n")
    
    return success

def run_backfill_to_month(base_start_date, base_end_date, stop_month=3):
    """以 7 天為單位回推區間，直到指定月份（含）為止。"""
    print("\n" + "="*80)
    print("Particle Yield Analysis - Backfill Mode")
    print(f"基準區間: {base_start_date} ~ {base_end_date}")
    print(f"回推至月份: {stop_month} 月 (含)")
    print("="*80 + "\n")

    current_start = base_start_date
    current_end = base_end_date
    run_results = []

    while current_end.month >= stop_month:
        print(f"\n>>> Backfill 分析區間: {current_start} ~ {current_end}")
        ok = main(target_date_range=(current_start, current_end), send_email=True)
        run_results.append((current_start, current_end, ok))

        current_start = current_start - timedelta(days=7)
        current_end = current_end - timedelta(days=7)

    success_count = sum(1 for _, _, ok in run_results if ok)
    total_count = len(run_results)
    print("\n" + "="*80)
    print(f"Backfill 完成: {success_count}/{total_count} 成功")
    print("="*80 + "\n")
    return success_count == total_count

def run_scheduler():
    """
    排程執行函數 - 每週一 09:00 執行
    """
    print("\n" + "="*80)
    print("Particle Yield Analysis - TOP 5 Machine Report Scheduler")
    print("排程設定：每週一 09:00 執行")
    print("="*80 + "\n")
    
    # 設定排程：每週一 09:00 執行
    schedule.every().monday.at("10:10").do(main)
    
    print(f" 排程已啟動，等待執行...")
    print(f" 下次執行時間: {schedule.next_run()}")
    print(f" 按 Ctrl+C 可停止排程\n")
    
    # 持續運行排程
    try:
        while True:
            schedule.run_pending()
            time.sleep(600)  # 每 60 秒檢查一次
    except KeyboardInterrupt:
        print("\n\n⚠ 排程已手動停止")
        sys.exit(0)

if __name__ == "__main__":
    # 排程模式
    # run_scheduler()

    # 立即執行模式（預設）
    if enable_backfill_to_month:
        success = run_backfill_to_month(start_date, end_date, backfill_stop_month)
    else:
        success = main()
    # if success:
    #     print("\n✅ 報告已成功生成並發送")
    # else:
    #     print("\n❌ 報告生成或發送失敗")