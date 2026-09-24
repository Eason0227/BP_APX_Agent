# QA 品質驗證週報 — 逐週趨勢分析
**產出日期**: 2026-09-16  
**資料範圍**: 2026-08-27 ~ 2026-09-02  
**涵蓋週數**: 2 週 (2026-W35 ~ 2026-W36)  
**總保養筆數**: 145

---

## 一、跨週 KPI 趨勢總覽

| 週次 | 保養次數 | WoW | 成功次數 | 成功率 | WoW | 惡化率 | 平均 DIFF | 回應時間(hr) | 狀態 |
|------|----------|-----|----------|--------|-----|--------|-----------|-------------|------|
| 2026-W35 | 92 |  | 55 | 59.8% |  | 26.1% | -0.04 | 11.9 | 
| 2026-W36 | 53 | ↓-39 | 31 | 58.5% | ↓-1.3 | 32.1% | -0.0137 | 11.6 | 

## 二、各週詳細分析
各週詳細內容已拆分為獨立 Markdown 檔案輸出。

## 三、機台保養軌跡分析（近 3 週趨勢）

###   趨勢改善中的機台
保養次數下降或成功率上升，機台狀況趨穩。

| 機台 | 總保養次數 | 整體成功率 | 次數趨勢 | 成功率趨勢 |
|------|-----------|-----------|---------|-----------|
| B1_COAT_03 | 5 | 100.0% | decreasing | stable |
| B1_COAT_04 | 10 | 60.0% | decreasing | increasing |
| B1_COAT_06 | 4 | 75.0% | decreasing | decreasing |
| B1_COAT_07 | 9 | 55.6% | decreasing | decreasing |
| B1_DECM_03 | 4 | 50.0% | decreasing | decreasing |
| B1_DEVP_01 | 6 | 66.7% | increasing | increasing |
| B1_DEVP_02 | 9 | 66.7% | decreasing | increasing |
| B1_DEVP_03 | 13 | 76.9% | decreasing | increasing |
| B1_OVEN_01 | 3 | 66.7% | increasing | increasing |
| B1_SCOP_03 | 3 | 66.7% | decreasing | increasing |
| B1_SCOP_06 | 5 | 40.0% | decreasing | decreasing |
| B1_STEP_02 | 3 | 100.0% | decreasing | stable |
| B1_STEP_03 | 4 | 50.0% | decreasing | decreasing |
| B1_THKZ_05 | 4 | 0.0% | decreasing | stable |

### 趨勢惡化中的機台
保養次數上升或成功率下降，需特別關注。

| 機台 | 總保養次數 | 整體成功率 | 次數趨勢 | 成功率趨勢 |
|------|-----------|-----------|---------|-----------|
| B1_AGST_01 | 3 | 0.0% | increasing | stable |
| B1_COAT_06 | 4 | 75.0% | decreasing | decreasing |
| B1_COAT_07 | 9 | 55.6% | decreasing | decreasing |
| B1_DECM_02 | 2 | 50.0% | stable | decreasing |
| B1_DECM_03 | 4 | 50.0% | decreasing | decreasing |
| B1_DEVP_01 | 6 | 66.7% | increasing | increasing |
| B1_OVEN_01 | 3 | 66.7% | increasing | increasing |
| B1_OVEN_03 | 4 | 50.0% | increasing | decreasing |
| B1_SCOP_02 | 4 | 75.0% | stable | decreasing |
| B1_SCOP_06 | 5 | 40.0% | decreasing | decreasing |
| B1_STEP_03 | 4 | 50.0% | decreasing | decreasing |

### 長期保養無效機台 (成功率 < 30%, ≥ 3 次保養)

| 機台 | 總保養次數 | 整體成功率 |
|------|-----------|-----------|
| B1_AGST_01 | 3 | 0.0% |
| B1_THKZ_05 | 4 | 0.0% |

## 四、  Critical Quality Risk

| PSN | 機台 | 總保養次數 | 連續失敗 | 連續惡化 | 最後事件 |
|-----|------|-----------|---------|---------|---------|
| B1_AGST_01#PA | B1_AGST_01 | 3 | 3 | 3 | 2026-09-02 |
| B1_AGST_11#PA | B1_AGST_11 | 2 | 2 | 2 | 2026-08-31 |
| B1_OVEN_04#OVEN2 | B1_OVEN_04 | 2 | 2 | 2 | 2026-08-29 |
| B1_RESZ_02#LP | B1_RESZ_02 | 2 | 2 | 2 | 2026-09-01 |
| B1_SCOP_06#ST | B1_SCOP_06 | 4 | 3 | 3 | 2026-09-01 |
| B1_THKZ_05#LP | B1_THKZ_05 | 4 | 3 | 0 | 2026-09-02 |
