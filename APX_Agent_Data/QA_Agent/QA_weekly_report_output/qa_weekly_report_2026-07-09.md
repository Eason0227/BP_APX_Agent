# QA 品質驗證週報 — 逐週趨勢分析
**產出日期**: 2026-07-09  
**資料範圍**: 2026-06-24 ~ 2026-07-09  
**涵蓋週數**: 3 週 (2026-W26 ~ 2026-W28)  
**總保養筆數**: 198

---

## 一、跨週 KPI 趨勢總覽

| 週次 | 保養次數 | WoW | 成功次數 | 成功率 | WoW | 惡化率 | 平均 DIFF | 回應時間(hr) | 狀態 |
|------|----------|-----|----------|--------|-----|--------|-----------|-------------|------|
| 2026-W26 | 53 |  | 36 | 67.9% |  | 24.5% | -0.0825 | 13.7 | 
| 2026-W27 | 93 | ↑40 | 64 | 68.8% | ↑0.9 | 24.7% | -0.0903 | 10.3 | 
| 2026-W28 | 52 | ↓-41 | 32 | 61.5% | ↓-7.3 | 28.8% | -0.0643 | 9.5 | 

## 二、各週詳細分析
各週詳細內容已拆分為獨立 Markdown 檔案輸出。

## 三、機台保養軌跡分析（近 3 週趨勢）

###   趨勢改善中的機台
保養次數下降或成功率上升，機台狀況趨穩。

| 機台 | 總保養次數 | 整體成功率 | 次數趨勢 | 成功率趨勢 |
|------|-----------|-----------|---------|-----------|
| B1_COAT_02 | 11 | 45.5% | decreasing | increasing |
| B1_COAT_04 | 16 | 87.5% | decreasing | decreasing |
| B1_COAT_07 | 6 | 66.7% | decreasing | increasing |
| B1_DECM_02 | 3 | 66.7% | decreasing | increasing |
| B1_DEVP_02 | 11 | 54.5% | increasing | increasing |
| B1_OVEN_02 | 7 | 85.7% | decreasing | increasing |
| B1_SCOP_02 | 4 | 100.0% | decreasing | stable |
| B1_SCOP_03 | 5 | 40.0% | decreasing | decreasing |
| B1_SCOP_06 | 2 | 50.0% | stable | increasing |
| B1_SCOP_09 | 3 | 0.0% | decreasing | stable |

### 趨勢惡化中的機台
保養次數上升或成功率下降，需特別關注。

| 機台 | 總保養次數 | 整體成功率 | 次數趨勢 | 成功率趨勢 |
|------|-----------|-----------|---------|-----------|
| B1_AGST_03 | 6 | 33.3% | increasing | decreasing |
| B1_COAT_04 | 16 | 87.5% | decreasing | decreasing |
| B1_COAT_05 | 7 | 85.7% | increasing | stable |
| B1_COAT_06 | 9 | 88.9% | increasing | decreasing |
| B1_DEVP_01 | 12 | 41.7% | increasing | decreasing |
| B1_DEVP_02 | 11 | 54.5% | increasing | increasing |
| B1_DEVP_03 | 12 | 66.7% | increasing | decreasing |
| B1_OVEN_03 | 8 | 100.0% | increasing | stable |
| B1_OVEN_04 | 3 | 100.0% | increasing | stable |
| B1_REFW_01 | 2 | 50.0% | stable | decreasing |
| B1_SCOP_03 | 5 | 40.0% | decreasing | decreasing |
| B1_STEP_01 | 2 | 50.0% | stable | decreasing |
| B1_STEP_04 | 8 | 62.5% | increasing | decreasing |
| B1_WFCL_01 | 5 | 60.0% | stable | decreasing |

### 長期保養無效機台 (成功率 < 30%, ≥ 3 次保養)

| 機台 | 總保養次數 | 整體成功率 |
|------|-----------|-----------|
| B1_SCOP_09 | 3 | 0.0% |

## 四、  Critical Quality Risk

| PSN | 機台 | 總保養次數 | 連續失敗 | 連續惡化 | 最後事件 |
|-----|------|-----------|---------|---------|---------|
| B1_AGST_03#PA | B1_AGST_03 | 4 | 4 | 2 | 2026-07-07 |
| B1_AGST_08#PA | B1_AGST_08 | 2 | 2 | 2 | 2026-07-08 |
| B1_COAT_02#1-0 | B1_COAT_02 | 4 | 4 | 4 | 2026-07-07 |
| B1_DEVP_01#11-2 | B1_DEVP_01 | 2 | 2 | 2 | 2026-07-06 |
| B1_DEVP_02#11-1 | B1_DEVP_02 | 3 | 3 | 3 | 2026-07-07 |
| B1_SCOP_07#ST | B1_SCOP_07 | 2 | 2 | 2 | 2026-06-25 |
| B1_SCOP_09#ST | B1_SCOP_09 | 2 | 2 | 2 | 2026-07-04 |
