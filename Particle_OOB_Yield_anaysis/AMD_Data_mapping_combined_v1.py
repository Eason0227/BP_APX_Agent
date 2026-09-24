import glob
import os
from datetime import datetime

import pandas as pd
import oracledb

# ============================================================
# 共用：自動抓取來源資料夾中最新的 xlsx 檔案
# ============================================================
def get_latest_xlsx(folder: str) -> str:
    files = glob.glob(os.path.join(folder, "*.xlsx"))
    if not files:
        raise FileNotFoundError(f"找不到任何 xlsx 檔案：{folder}")
    return max(files, key=os.path.getmtime)

inspection_file = get_latest_xlsx(r"\\khaiotapvp02\AMD\Yield")  
rework_file     = get_latest_xlsx(r"\\khaiotapvp02\AMD\Rework")

print(f"[Yield 來源] {inspection_file}")
print(f"[Rework 來源] {rework_file}")

# 輸出檔名日期取自各來源檔案的修改日期
_yield_date  = datetime.fromtimestamp(os.path.getmtime(inspection_file)).strftime("%Y%m%d")
_rework_date = datetime.fromtimestamp(os.path.getmtime(rework_file)).strftime("%Y%m%d")

yield_output_file  = rf"D:\Paticle_OOB_system\Particle_OOB_Yield_anaysis\PSN_Yield_tracking_data_v2\Yield_Track_{_yield_date}_(Security C).xlsx"
rework_output_file = rf"D:\Paticle_OOB_system\Particle_OOB_Yield_anaysis\PSN_Yield_tracking_data_rework\Yield_Track_{_rework_date}_(Security C).xlsx"

# ============================================================
# 共用：Oracle 連線 & 查詢 tracking 資料（只查一次）
# ============================================================
DB_HOST      = "10.10.19.131"
DB_PORT      = 1521
SERVICE_NAME = "a3db"
DB_USER      = "aiap01"
DB_PASSWORD  = "aiap01"

dsn = oracledb.makedsn(DB_HOST, DB_PORT, service_name=SERVICE_NAME)
query_tracking = """
    SELECT *
    FROM bp_mes_edc_trackin_out_extend t
    WHERE t.IMPORT_DATE >= :import_date_from
    AND (
        t.CUST_ID LIKE 'BW%'
    )
"""

print("開始從 Oracle 讀取 tracking 資料...")
with oracledb.connect(user=DB_USER, password=DB_PASSWORD, dsn=dsn) as conn:
    with conn.cursor() as cursor:
        cursor.arraysize = 10000
        cursor.execute(query_tracking, import_date_from=pd.Timestamp('2026-02-01').to_pydatetime())
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()

df_tracking_raw = pd.DataFrame(rows, columns=columns)
print(f"Oracle tracking 筆數: {len(df_tracking_raw)}")

# ============================================================
# 共用：本地端過濾 & 去重
# ============================================================
if 'IMPORT_DATE' in df_tracking_raw.columns:
    df_tracking_raw['IMPORT_DATE'] = pd.to_datetime(df_tracking_raw['IMPORT_DATE'], errors='coerce')
    df_tracking_raw = df_tracking_raw[df_tracking_raw['IMPORT_DATE'] >= pd.Timestamp('2026-02-01')]

if 'MAH_LOCATION' in df_tracking_raw.columns:
    df_tracking_raw = df_tracking_raw[
        df_tracking_raw['MAH_LOCATION'].astype(str).str.startswith(('K18', 'K21', 'K22'), na=False)
    ]

# 同一 SCHD_NO + OPER + TRACK_IN_TIME 若有多筆，保留最舊 TRACK_OUT_TIME
df_tracking_raw = df_tracking_raw.sort_values(
    ['SCHD_NO', 'OPER', 'TRACK_IN_TIME', 'TRACK_OUT_TIME'],
    kind='stable'
)
df_tracking_raw = df_tracking_raw.drop_duplicates(
    subset=['SCHD_NO', 'OPER', 'TRACK_IN_TIME'],
    keep='first'
)

# ============================================================
# Rework mapping 用的共用函式
# ============================================================

def build_tracking_with_layer(df_tracking_src, process_map):
    df_tracking_layer = df_tracking_src.copy()

    df_tracking_layer['OPER'] = df_tracking_layer['OPER'].astype(int)
    df_tracking_layer['Layer'] = None

    for station_key, processes in process_map.items():
        mask = df_tracking_layer['OPER'].isin(processes)
        df_tracking_layer.loc[mask, 'Layer'] = station_key

    df_tracking_layer['LOT_RDL_INSPECTION'] = (
        df_tracking_layer['SCHD_NO'] + "_" + df_tracking_layer['Layer'].astype(str)
    )

    df_tracking_layer['TRACK_IN_TIME']  = pd.to_datetime(df_tracking_layer['TRACK_IN_TIME'],  errors='coerce')
    df_tracking_layer['TRACK_OUT_TIME'] = pd.to_datetime(df_tracking_layer['TRACK_OUT_TIME'], errors='coerce')
    df_tracking_layer = df_tracking_layer.sort_values(
        ['LOT_RDL_INSPECTION', 'OPER', 'TRACK_IN_TIME'], kind='stable'
    )
    df_tracking_layer['_dup_seq'] = (
        df_tracking_layer.groupby(['LOT_RDL_INSPECTION', 'OPER']).cumcount() + 1
    )
    df_tracking_layer['LOT_RDL_INSPECTION'] = (
        df_tracking_layer['LOT_RDL_INSPECTION']
        + df_tracking_layer['_dup_seq'].map(lambda n: '' if n == 1 else f'_{n}')
    )
    df_tracking_layer = df_tracking_layer.drop(columns=['_dup_seq'])

    return df_tracking_layer


def build_rework_layer_df(rework_file_path, sheet_name, target_layers):
    df_rework = pd.read_excel(rework_file_path, sheet_name=sheet_name)
    df_rework = df_rework[df_rework['Layer'].isin(target_layers)]
    df_rework = df_rework.rename(columns={'particle': 'Rework_Particle'})
    df_rework['Rework_Particle'] = df_rework['Rework_Particle'] / (df_rework["Wafer Q'ty"] * 21)
    df_rework['LOT_RDL_INSPECTION'] = df_rework['Schedule'] + '_' + df_rework['Layer']

    if 'ADI date' in df_rework.columns:
        df_rework['ADI date'] = pd.to_datetime(df_rework['ADI date'], errors='coerce')

    df_rework = df_rework.sort_values(['LOT_RDL_INSPECTION', 'ADI date'], kind='stable')
    df_rework['_dup_seq'] = df_rework.groupby('LOT_RDL_INSPECTION').cumcount() + 1
    df_rework['LOT_RDL_INSPECTION'] = (
        df_rework['LOT_RDL_INSPECTION']
        + df_rework['_dup_seq'].map(lambda n: '' if n == 1 else f'_{n}')
    )
    df_rework = df_rework.drop(columns=['_dup_seq'])

    return df_rework


def build_rework_output_df(df_tracking_layer, df_rework_layer, layer_oper_allow_map, layer_regex):
    df_merged = pd.merge(
        df_tracking_layer,
        df_rework_layer[['Schedule', 'ADI date', 'LOT_RDL_INSPECTION', 'Rework_Particle', "Wafer Q'ty"]],
        on='LOT_RDL_INSPECTION',
        how='left'
    )

    df_merged['MACHINE_NO'] = df_merged['EQP_NAME'].astype(str).str.split('-').str[-1]

    output_columns = [
        'YEAR', 'LOT_NO', 'SCHD_NO', 'LOT_RDL_INSPECTION', 'Layer',
        'ADI date', 'Rework_Particle', "Wafer Q'ty", 'OPER', 'TRACK_IN_TIME', 'TRACK_OUT_TIME', 'MACHINE_NO'
    ]
    available_columns = [col for col in output_columns if col in df_merged.columns]
    df_output = df_merged[available_columns]
    df_output = df_output.dropna(subset=["ADI date"])
    df_output = df_output[df_output['ADI date'] != "-"]

    if 'LOT_RDL_INSPECTION' in df_output.columns and 'OPER' in df_output.columns:
        lot_layer  = df_output['LOT_RDL_INSPECTION'].astype(str).str.extract(layer_regex)[0]
        oper_num   = pd.to_numeric(df_output['OPER'], errors='coerce')

        apply_mask = lot_layer.notna()
        valid_mask = pd.Series([
            (layer in layer_oper_allow_map) and pd.notna(op) and (op in layer_oper_allow_map[layer])
            for layer, op in zip(lot_layer, oper_num)
        ], index=df_output.index)
        keep_mask = (~apply_mask) | (apply_mask & valid_mask)
        df_output = df_output[keep_mask].copy()

    return df_output


# ============================================================
# Part 1：Yield mapping（AMD_Data_mapping_v4.py）
# ============================================================
print("\n===== Part 1: Yield Mapping =====")

layer_process_map = {
    "RDL1_API": [6517, 6409, 6380, 6401, 6403, 6405, 6411, 6446],
    "RDL1_AEI": [
        6416, 6425, 6440, 6443, 6453, 6450, 6454, 6456,
        6500, 6520, 6530, 6540, 6802, 6535, 6815, 6570,
        6662, 6580, 6885, 6841, 6675, 6654, 6857, 6845,
        6683, 6581,
    ],
    "RDL2_API": [6574, 6519, 6369, 6681, 7462, 6691, 6699, 6700, 6711, 6601],
    "RDL2_AEI": [
        6716, 6720, 6730, 6449, 6603, 6585, 6589, 6586,
        6590, 6610, 6620, 6630, 6806, 6790, 6816, 6660,
        6663, 6670, 6886, 6852, 6840, 6658, 6877, 6856,
        6837, 6688,
    ],
    "RDL3_API": [6661, 6523, 6680, 6801, 7463, 6692, 6404, 6707, 6713, 6494],
    "RDL3_AEI": [
        6718, 6792, 6733, 6753, 6604, 6461, 6485, 6765,
        6501, 6521, 6528, 6531, 6541, 6996, 6532, 6533,
        6553, 6825, 6817, 6573, 6551, 6666, 6887, 6848,
        6721, 6697, 6876,
    ],
    "RDL3_6905": [6920, 6910, 6950, 6903, 6905],
}

df_tracking_yield = df_tracking_raw.copy()
df_tracking_yield['OPER'] = df_tracking_yield['OPER'].astype(int)
df_tracking_yield['Layer'] = None

for station_key, processes in layer_process_map.items():
    mask = df_tracking_yield['OPER'].isin(processes)
    df_tracking_yield.loc[mask, 'Layer'] = station_key

df_tracking_yield['LOT_RDL_INSPECTION'] = (
    df_tracking_yield['SCHD_NO'] + "_" + df_tracking_yield['Layer'].astype(str)
)

df_inspection = pd.read_excel(inspection_file, sheet_name="lot Single yield analysis")

for col in ['Lot', 'Layer', 'Team']:
    if col in df_inspection.columns:
        df_inspection[col] = df_inspection[col].astype(str).str.replace(r'\s+', '', regex=True)

df_inspection['LOT_RDL_INSPECTION'] = (
    df_inspection['Lot'] + '_' + df_inspection['Layer'] + "_" + df_inspection['Team'].astype(str)
)

df_merged_yield = pd.merge(
    df_tracking_yield,
    df_inspection,
    on='LOT_RDL_INSPECTION',
    how='left'
)

df_merged_yield['MACHINE_NO'] = df_merged_yield['EQP_NAME'].astype(str).str.split('-').str[-1]

df_merged_yield = df_merged_yield.rename(columns={
    'YEAR_CODE': 'YEAR',
    'Layer':     'RDL',
    'Team':      'INSPECTION',
    'Date':      'INSPECTION_DATE',
    'Die':       'DIE_QTY',
    'All Defect':'ALL_DEFECT_QTY',
    'Particle':  'PARTICLE_QTY',
    'SCHD_NO':   'LOT_NO',
})

output_columns_yield = [
    'YEAR', 'LOT_NO', 'LOT_RDL_INSPECTION', 'TRACK_SEQUENCE', 'RDL', 'INSPECTION',
    'INSPECTION_DATE', 'DIE_QTY', 'ALL_DEFECT_QTY', 'PARTICLE_QTY',
    'OPER', 'TRACK_IN_TIME', 'TRACK_OUT_TIME', 'MACHINE_NO',
]
available_columns_yield = [col for col in output_columns_yield if col in df_merged_yield.columns]
df_output_yield = df_merged_yield[available_columns_yield]
df_output_yield['PARTICLE_QTY'] = df_output_yield['PARTICLE_QTY'].astype("Int64").fillna(0)
df_output_yield = df_output_yield.dropna(subset=["INSPECTION_DATE"])
df_output_yield = df_output_yield[df_output_yield['INSPECTION_DATE'] != "-"]

print(df_output_yield.head())
df_output_yield.to_excel(yield_output_file, index=False)
print(f"[Yield] 檔案已輸出至: {yield_output_file}")
print(f"[Yield] 共 {len(df_output_yield)} 筆資料")

# ============================================================
# Part 2：Rework mapping（AMD_Data_mapping_rework_v5.py）
# ============================================================
print("\n===== Part 2: Rework Mapping =====")

# --- RDL (PR1 / PR2 / uPad) ---
pr_process_map = {
    "PR1":  [6570, 6662, 6425, 6440, 6443, 6453, 6450, 6454, 6456, 6500, 6520, 6530, 6540, 6802],
    "PR2":  [6660, 6663, 6449, 6603, 6585, 6589, 6586, 6590, 6610, 6620, 6630, 6806],
    "uPad": [6571, 6753, 6604, 6461, 6485, 6765, 6501, 6521, 6528, 6531, 6541, 6996, 6532],
}
layer_oper_allow_map_rdl = {
    'PR1':  {6570, 6662, 6682, 6540, 6500, 6520, 6530},
    'PR2':  {6660, 6663, 6682, 6630, 6590, 6610, 6620},
    'uPad': {6571, 6541, 6501, 6521, 6528, 6531},
}

df_tracking_RDL           = build_tracking_with_layer(df_tracking_raw, pr_process_map)
df_inspection_rework_RDL  = build_rework_layer_df(rework_file, "AMD-PR Daily rework rate-B1", ['PR1', 'PR2', 'uPad'])
df_output_RDL             = build_rework_output_df(
    df_tracking_RDL, df_inspection_rework_RDL,
    layer_oper_allow_map_rdl, r'_(PR1|PR2|uPad)_\d+$'
)

# --- TP ---
TP_process_map = {
    'TP': {6425, 6440, 6453, 6450, 6454, 6456, 6501, 6506, 6521, 6531, 6542, 6543, 6830, 7193},
}
layer_oper_allow_map_tp = {
    'TP': {6542, 6543, 6830, 7193, 6501, 6506, 6521, 6531},
}

df_tracking_TP           = build_tracking_with_layer(df_tracking_raw, TP_process_map)
df_inspection_rework_TP  = build_rework_layer_df(rework_file, "AMD-PR Daily rework rate-B1", ['TP'])
df_output_TP             = build_rework_output_df(
    df_tracking_TP, df_inspection_rework_TP,
    layer_oper_allow_map_tp, r'_(TP)_\d+$'
)

df_output_rework = pd.concat([df_output_RDL, df_output_TP], ignore_index=True, sort=False)

df_output_rework.to_excel(rework_output_file, index=False)
print(f"[Rework] 檔案已輸出至: {rework_output_file}")
print(f"[Rework] 共 {len(df_output_rework)} 筆資料")
