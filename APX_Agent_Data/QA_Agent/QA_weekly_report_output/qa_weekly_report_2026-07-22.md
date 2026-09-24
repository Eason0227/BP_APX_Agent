# QA 品質驗證週報 — 逐週趨勢分析
**產出日期**: 2026-07-22  
**資料範圍**: 2026-07-09 ~ 2026-07-21  
**涵蓋週數**: 3 週 (2026-W28 ~ 2026-W30)  
**總保養筆數**: 269

---

## 一、跨週 KPI 趨勢總覽

| 週次 | 保養次數 | WoW | 成功次數 | 成功率 | WoW | 惡化率 | 平均 DIFF | 回應時間(hr) | 狀態 |
|------|----------|-----|----------|--------|-----|--------|-----------|-------------|------|
| 2026-W28 | 90 |  | 65 | 72.2% |  | 22.2% | -0.0541 | 16.6 | 
| 2026-W29 | 134 | ↑44 | 94 | 70.1% | ↓-2.1 | 22.4% | -0.0605 | 12.2 | 
| 2026-W30 | 45 | ↓-89 | 30 | 66.7% | ↓-3.4 | 20.0% | -0.059 | 10.5 | 

## 二、各週詳細分析
各週詳細內容已拆分為獨立 Markdown 檔案輸出。

## 三、機台保養軌跡分析（近 3 週趨勢）

###   趨勢改善中的機台
保養次數下降或成功率上升，機台狀況趨穩。

| 機台 | 總保養次數 | 整體成功率 | 次數趨勢 | 成功率趨勢 |
|------|-----------|-----------|---------|-----------|
| B1_AGST_01 | 5 | 80.0% | decreasing | increasing |
| B1_AGST_02 | 2 | 50.0% | stable | increasing |
| B1_AGST_08 | 3 | 100.0% | decreasing | stable |
| B1_AGST_09 | 3 | 100.0% | decreasing | stable |
| B1_COAT_01 | 3 | 100.0% | decreasing | stable |
| B1_COAT_02 | 9 | 55.6% | decreasing | increasing |
| B1_COAT_03 | 9 | 100.0% | decreasing | stable |
| B1_COAT_04 | 17 | 70.6% | decreasing | decreasing |
| B1_COAT_05 | 10 | 60.0% | decreasing | increasing |
| B1_COAT_06 | 10 | 80.0% | decreasing | increasing |
| B1_COAT_07 | 14 | 78.6% | decreasing | stable |
| B1_DECM_02 | 4 | 100.0% | decreasing | stable |
| B1_DECM_03 | 4 | 75.0% | stable | increasing |
| B1_DEVP_01 | 15 | 66.7% | decreasing | decreasing |
| B1_DEVP_02 | 13 | 76.9% | decreasing | increasing |
| B1_DEVP_03 | 17 | 52.9% | increasing | increasing |
| B1_DEVP_04 | 12 | 75.0% | increasing | increasing |
| B1_DSCM_01 | 4 | 25.0% | decreasing | decreasing |
| B1_ETCH_04 | 4 | 25.0% | decreasing | decreasing |
| B1_OVEN_01 | 4 | 75.0% | decreasing | increasing |
| B1_OVEN_02 | 4 | 50.0% | decreasing | increasing |
| B1_OVEN_04 | 5 | 100.0% | decreasing | stable |
| B1_PRRM_05 | 2 | 50.0% | stable | increasing |
| B1_RESZ_03 | 3 | 66.7% | decreasing | increasing |
| B1_SCOP_02 | 5 | 100.0% | decreasing | stable |
| B1_SCOP_04 | 4 | 50.0% | decreasing | decreasing |
| B1_SCOP_05 | 3 | 100.0% | decreasing | stable |
| B1_SCOP_07 | 3 | 33.3% | decreasing | decreasing |
| B1_SCOP_08 | 3 | 66.7% | decreasing | increasing |
| B1_STEP_02 | 9 | 55.6% | decreasing | increasing |
| B1_STEP_04 | 3 | 66.7% | decreasing | increasing |
| B1_WFCL_02 | 4 | 25.0% | increasing | increasing |

### 趨勢惡化中的機台
保養次數上升或成功率下降，需特別關注。

| 機台 | 總保養次數 | 整體成功率 | 次數趨勢 | 成功率趨勢 |
|------|-----------|-----------|---------|-----------|
| B1_AGST_03 | 6 | 66.7% | stable | decreasing |
| B1_AGST_11 | 4 | 100.0% | increasing | stable |
| B1_COAT_04 | 17 | 70.6% | decreasing | decreasing |
| B1_DEVP_01 | 15 | 66.7% | decreasing | decreasing |
| B1_DEVP_03 | 17 | 52.9% | increasing | increasing |
| B1_DEVP_04 | 12 | 75.0% | increasing | increasing |
| B1_DSCM_01 | 4 | 25.0% | decreasing | decreasing |
| B1_ETCH_04 | 4 | 25.0% | decreasing | decreasing |
| B1_SCOP_01 | 3 | 100.0% | increasing | stable |
| B1_SCOP_03 | 5 | 60.0% | stable | decreasing |
| B1_SCOP_04 | 4 | 50.0% | decreasing | decreasing |
| B1_SCOP_06 | 4 | 50.0% | stable | decreasing |
| B1_SCOP_07 | 3 | 33.3% | decreasing | decreasing |
| B1_SCOP_09 | 5 | 60.0% | increasing | decreasing |
| B1_WFCL_02 | 4 | 25.0% | increasing | increasing |

### 長期保養無效機台 (成功率 < 30%, ≥ 3 次保養)

| 機台 | 總保養次數 | 整體成功率 |
|------|-----------|-----------|
| B1_DSCM_01 | 4 | 25.0% |
| B1_ETCH_04 | 4 | 25.0% |
| B1_WFCL_02 | 4 | 25.0% |

## 四、  Critical Quality Risk

| PSN | 機台 | 總保養次數 | 連續失敗 | 連續惡化 | 最後事件 |
|-----|------|-----------|---------|---------|---------|
| B1_COAT_02#1-0 | B1_COAT_02 | 2 | 2 | 2 | 2026-07-18 |
| B1_COAT_05#11-0 | B1_COAT_05 | 3 | 2 | 2 | 2026-07-18 |
| B1_COAT_07#HP | B1_COAT_07 | 2 | 2 | 2 | 2026-07-17 |
| B1_DEVP_03#12-1 | B1_DEVP_03 | 2 | 2 | 2 | 2026-07-18 |
| B1_DEVP_03#14-1 | B1_DEVP_03 | 3 | 2 | 2 | 2026-07-20 |
| B1_ETCH_04#LP | B1_ETCH_04 | 4 | 3 | 2 | 2026-07-20 |
| B1_OVEN_02#OVEN2 | B1_OVEN_02 | 2 | 2 | 2 | 2026-07-12 |
| B1_SCOP_09#ST | B1_SCOP_09 | 2 | 2 | 2 | 2026-07-18 |
| B1_STEP_02#LP | B1_STEP_02 | 4 | 2 | 2 | 2026-07-20 |
