"""
equipment_comparison.py
-----------------------
Cayman vs AMD 產品機台清單比對模組 —
比較兩產品使用的機台差異，找出可能影響良率的設備差異，
並產出限機策略建議。

可獨立執行：
    python equipment_comparison.py
"""

import re
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════════════════
# 機台清單原始資料
# ═══════════════════════════════════════════════════════════════════════════

CAYMAN_EQUIPMENT = [
    ("E0163677", "B5_AGST_M5"),
    ("E0171415", "B1_AGST_02"),
    ("E0171416", "B1_AGST_01"),
    ("E0172053", "B1_SHTZ_01"),
    ("E0172055", "B1_THKZ_02"),
    ("E0172056", "B1_SPUT_01"),
    ("E0172065", "B1_BPHI_01"),
    ("E0172296", "B1_SCOP_02"),
    ("E0172297", "B1_SCOP_01"),
    ("E0172414", "B1_RESZ_01"),
    ("E0172415", "B1_OVEN_01"),
    ("E0173275", "B1_WFCL_01"),
    ("E0173276", "B1_ETCH_02"),
    ("E0173277", "B1_DEVP_02"),
    ("E0173278", "B1_COAT_02"),
    ("E0173279", "B1_COAT_03"),
    ("E0173290", "B1_DSCM_01"),
    ("E0173295", "B1_PRRM_01"),
    ("E0173299", "B1_SORT_01"),
    ("E0173453", "B1_AGST_04"),
    ("E0173454", "B1_BPHI_02"),
    ("E0173456", "B1_SPUT_02"),
    ("E0173457", "B1_PLAT_01"),
    ("E0173809", "B1_SPUT_03"),
    ("E0173956", "B1_SPUT_04"),
    ("E0174008", "B1_OVEN_02"),
    ("E0174009", "B1_OVEN_03"),
    ("E0174076", "B1_STEP_01"),
    ("E0174239", "B1_PLAT_02"),
    ("E0174521", "B1_DEVP_01"),
    ("E0174522", "B1_STEP_02"),
    ("E0174535", "B1_DSCM_02"),
    ("E0174663", "B1_AGST_14"),
    ("E0174687", "B1_BPHI_03"),
    ("E0174732", "B1_DSCM_03"),
    ("E0174815", "B1_XRFZ_01"),
    ("E0174937", "B1_SCOP_03"),
    ("E0175103", "B1_DEVP_03"),
    ("E0175226", "B1_SCOP_05"),
    ("E0175227", "B1_SCOP_04"),
    ("E0175228", "B1_COAT_04"),
    ("E0175302", "B1_AGST_05"),
    ("E0175303", "B1_AGST_06"),
    ("E0175530", "B1_PRRM_03"),
    ("E0175533", "B1_STEP_03"),
    ("E0176345", "B1_COAT_05"),
    ("E0176358", "B1_WFCL_02"),
    ("E0176362", "B1_AGST_03"),
    ("E0176864", "B1_AGST_16"),
    ("E0176895", "B1_ETCH_04"),
    ("E0177212", "B1_STEP_04"),
    ("E0177362", "B1_AGST_17"),
    ("E0177363", "B1_PLAT_03"),
    ("E0177662", "B1_COAT_06"),
    ("E0178172", "B1_ETCH_05"),
    ("E0178177", "B1_PRRM_05"),
]

AMD_EQUIPMENT = [
    ("E0099101", "B6_6333_01"),
    ("E0171142", "B1_2395_01"),
    ("E0172053", "B1_SHTZ_01"),
    ("E0172055", "B1_THKZ_02"),
    ("E0172064", "B1_2490_01"),
    ("E0172073", "B1_6354_01"),
    ("E0172296", "B1_SCOP_02"),
    ("E0172297", "B1_SCOP_01"),
    ("E0172301", "B1_2400_03"),
    ("E0172415", "B1_OVEN_01"),
    ("E0173275", "B1_WFCL_01"),
    ("E0173276", "B1_ETCH_02"),
    ("E0173277", "B1_DEVP_02"),
    ("E0173278", "B1_COAT_02"),
    ("E0173279", "B1_COAT_03"),
    ("E0173283", "B1_2190_01"),
    ("E0173290", "B1_DSCM_01"),
    ("E0173295", "B1_PRRM_01"),
    ("E0173299", "B1_SORT_01"),
    ("E0173301", "B1_ETCH_03"),
    ("E0173302", "B1_6347_01"),
    ("E0173456", "B1_SPUT_02"),
    ("E0173457", "B1_PLAT_01"),
    ("E0173809", "B1_SPUT_03"),
    ("E0173891", "B1_2195_02"),
    ("E0173999", "B1_2490_001"),
    ("E0174008", "B1_OVEN_02"),
    ("E0174239", "B1_PLAT_02"),
    ("E0174521", "B1_DEVP_01"),
    ("E0174523", "B1_2451_01"),
    ("E0174535", "B1_DSCM_02"),
    ("E0174536", "B1_6270_01"),
    ("E0174732", "B1_DSCM_03"),
    ("E0174777", "B1_THKZ_03"),
    ("E0174815", "B1_XRFZ_01"),
    ("E0174937", "B1_SCOP_03"),
    ("E0174938", "B1_SCOP_09"),
    ("E0175103", "B1_DEVP_03"),
    ("E0175226", "B1_SCOP_05"),
    ("E0175227", "B1_SCOP_04"),
    ("E0175228", "B1_COAT_04"),
    ("E0175530", "B1_PRRM_03"),
    ("E0176345", "B1_COAT_05"),
    ("E0176347", "B1_AGST_10"),
    ("E0176349", "B1_AGST_09"),
    ("E0176350", "B1_AGST_08"),
    ("E0176358", "B1_WFCL_02"),
    ("E0176895", "B1_ETCH_04"),
    ("E0176897", "B1_AGST_11"),
    ("E0176905", "B1_6334_01"),
    ("E0177212", "B1_STEP_04"),
    ("E0177363", "B1_PLAT_03"),
    ("E0177665", "B1_COAT_07"),
    ("E0178172", "B1_ETCH_05"),
    ("E0178177", "B1_PRRM_05"),
    ("E0178542", "B1_SCOP_06"),
    ("E0178543", "B1_SCOP_07"),
    ("E0178544", "B1_SCOP_08"),
]


# ═══════════════════════════════════════════════════════════════════════════
# 分析函式
# ═══════════════════════════════════════════════════════════════════════════

def _extract_eqp_type(name: str) -> str:
    """從機台名稱提取設備類型。數值命名（如 B1_2490_01）歸類為 'NUMERIC'。"""
    m = re.match(r"B\d+_([A-Za-z]+)", name)
    if m:
        return m.group(1).upper()
    # 數值命名機台
    if re.match(r"B\d+_\d+", name):
        return "NUMERIC"
    return "OTHER"


def compare_equipment() -> dict:
    """
    比較 Cayman 與 AMD 機台清單，回傳結構化比對結果。

    Returns:
        dict 包含：
        - summary: 總覽數據
        - common: 共用機台列表
        - cayman_only: 僅 Cayman 使用的機台列表
        - amd_only: 僅 AMD 使用的機台列表
        - by_type: 按設備類型分組的比較
        - amd_numeric_machines: AMD 使用的數值命名（非標準）機台
        - critical_findings: 關鍵發現
    """
    cayman_map = {eid: name for eid, name in CAYMAN_EQUIPMENT}
    amd_map = {eid: name for eid, name in AMD_EQUIPMENT}

    cayman_ids = set(cayman_map.keys())
    amd_ids = set(amd_map.keys())

    common_ids = cayman_ids & amd_ids
    cayman_only_ids = cayman_ids - amd_ids
    amd_only_ids = amd_ids - cayman_ids

    # 共用機台
    common = [{"eqp_id": eid, "eqp_name": cayman_map[eid]} for eid in sorted(common_ids)]

    # 僅 Cayman
    cayman_only = [{"eqp_id": eid, "eqp_name": cayman_map[eid]} for eid in sorted(cayman_only_ids)]

    # 僅 AMD
    amd_only = [{"eqp_id": eid, "eqp_name": amd_map[eid]} for eid in sorted(amd_only_ids)]

    # 數值命名機台（僅 AMD）
    amd_numeric = [
        {"eqp_id": eid, "eqp_name": amd_map[eid]}
        for eid in sorted(amd_only_ids)
        if _extract_eqp_type(amd_map[eid]) == "NUMERIC"
    ]

    # 按設備類型分組
    type_cayman = defaultdict(list)
    type_amd = defaultdict(list)
    for eid, name in CAYMAN_EQUIPMENT:
        type_cayman[_extract_eqp_type(name)].append({"eqp_id": eid, "eqp_name": name})
    for eid, name in AMD_EQUIPMENT:
        type_amd[_extract_eqp_type(name)].append({"eqp_id": eid, "eqp_name": name})

    all_types = sorted(set(list(type_cayman.keys()) + list(type_amd.keys())))
    by_type = {}
    for t in all_types:
        c_list = type_cayman.get(t, [])
        a_list = type_amd.get(t, [])
        c_ids = {e["eqp_id"] for e in c_list}
        a_ids = {e["eqp_id"] for e in a_list}
        by_type[t] = {
            "cayman_count": len(c_list),
            "amd_count": len(a_list),
            "common_count": len(c_ids & a_ids),
            "cayman_only_count": len(c_ids - a_ids),
            "amd_only_count": len(a_ids - c_ids),
            "cayman_machines": [e["eqp_name"] for e in c_list],
            "amd_machines": [e["eqp_name"] for e in a_list],
            "common_machines": [cayman_map[eid] for eid in sorted(c_ids & a_ids)],
        }

    # 關鍵發現
    critical_findings = []

    # 1. AGST 完全不同
    agst = by_type.get("AGST", {})
    if agst.get("common_count", 0) == 0 and agst.get("cayman_count", 0) > 0 and agst.get("amd_count", 0) > 0:
        critical_findings.append({
            "severity": "HIGH",
            "finding": "AGST 站點完全不重疊",
            "detail": (
                f"Cayman 使用 {agst['cayman_count']} 台 AGST（{', '.join(agst['cayman_machines'])}），"
                f"AMD 使用 {agst['amd_count']} 台 AGST（{', '.join(agst['amd_machines'])}），"
                f"兩者無任何共用 AGST 機台。不同的老化測試設備可能導致製程穩定性差異。"
            ),
        })

    # 2. AMD 缺少 BPHI
    bphi = by_type.get("BPHI", {})
    if bphi.get("cayman_count", 0) > 0 and bphi.get("amd_count", 0) == 0:
        critical_findings.append({
            "severity": "HIGH",
            "finding": "AMD 完全缺少 BPHI（背面檢查）站點",
            "detail": (
                f"Cayman 使用 {bphi['cayman_count']} 台 BPHI（{', '.join(bphi['cayman_machines'])}），"
                f"AMD 產線完全未配置 BPHI 機台。缺少背面檢查可能導致 Particle 缺陷未被及時攔截。"
            ),
        })

    # 3. AMD 缺少 RESZ
    resz = by_type.get("RESZ", {})
    if resz.get("cayman_count", 0) > 0 and resz.get("amd_count", 0) == 0:
        critical_findings.append({
            "severity": "MEDIUM",
            "finding": "AMD 缺少 RESZ（阻劑量測）站點",
            "detail": (
                f"Cayman 使用 {resz['cayman_count']} 台 RESZ（{', '.join(resz['cayman_machines'])}），"
                f"AMD 未配置 RESZ 機台，可能導致阻劑相關製程缺乏即時監控。"
            ),
        })

    # 4. SPUT 數量差異
    sput = by_type.get("SPUT", {})
    if sput.get("cayman_count", 0) > sput.get("amd_count", 0):
        critical_findings.append({
            "severity": "MEDIUM",
            "finding": "AMD Sputtering 機台不足",
            "detail": (
                f"Cayman {sput['cayman_count']} 台（{', '.join(sput['cayman_machines'])}），"
                f"AMD 僅 {sput['amd_count']} 台（{', '.join(sput['amd_machines'])}）。"
                f"Sputtering 是關鍵薄膜沉積站點，機台不足可能限制製程分配彈性。"
            ),
        })

    # 5. STEP 數量差異
    step = by_type.get("STEP", {})
    if step.get("cayman_count", 0) > step.get("amd_count", 0) * 2:
        critical_findings.append({
            "severity": "HIGH",
            "finding": "AMD STEP（階高量測）機台嚴重不足",
            "detail": (
                f"Cayman {step['cayman_count']} 台（{', '.join(step['cayman_machines'])}），"
                f"AMD 僅 {step['amd_count']} 台（{', '.join(step['amd_machines'])}）。"
                f"量測站不足可能造成排程瓶頸，導致 SPC 監控密度不足。"
            ),
        })

    # 6. AMD 數值命名機台
    if amd_numeric:
        critical_findings.append({
            "severity": "HIGH",
            "finding": f"AMD 使用 {len(amd_numeric)} 台數值命名（非標準）機台",
            "detail": (
                f"機台清單：{', '.join(e['eqp_name'] for e in amd_numeric)}。"
                f"這些機台不遵循標準命名規範（TYPE_##），可能是舊機型、非標準設備或未經完整製程驗證的機台，"
                f"為良率風險的最大嫌疑因子。建議優先排除或進行設備能力驗證。"
            ),
        })

    return {
        "summary": {
            "cayman_total": len(CAYMAN_EQUIPMENT),
            "amd_total": len(AMD_EQUIPMENT),
            "common_count": len(common_ids),
            "cayman_only_count": len(cayman_only_ids),
            "amd_only_count": len(amd_only_ids),
            "amd_numeric_count": len(amd_numeric),
        },
        "common": common,
        "cayman_only": cayman_only,
        "amd_only": amd_only,
        "amd_numeric_machines": amd_numeric,
        "by_type": by_type,
        "critical_findings": critical_findings,
    }


def generate_comparison_report() -> str:
    """產出 Cayman vs AMD 機台比對分析報告（Markdown 格式）。"""
    data = compare_equipment()
    s = data["summary"]
    lines = []

    lines.append("# Cayman vs AMD 產品機台比對分析報告\n")

    # ── 一、總覽 ──
    lines.append("## 一、總覽\n")
    lines.append(f"| 項目 | 數量 |")
    lines.append(f"|------|------|")
    lines.append(f"| Cayman 機台總數 | {s['cayman_total']} |")
    lines.append(f"| AMD 機台總數 | {s['amd_total']} |")
    lines.append(f"| 共用機台 | {s['common_count']} |")
    lines.append(f"| 僅 Cayman 使用 | {s['cayman_only_count']} |")
    lines.append(f"| 僅 AMD 使用 | {s['amd_only_count']} |")
    lines.append(f"| AMD 數值命名（非標準）機台 | {s['amd_numeric_count']} |")
    lines.append("")

    # ── 二、按設備類型比較 ──
    lines.append("## 二、按設備類型比較\n")
    lines.append("| 設備類型 | Cayman 數量 | AMD 數量 | 共用 | 僅Cayman | 僅AMD | 風險 |")
    lines.append("|----------|------------|---------|------|---------|-------|------|")
    for t, info in sorted(data["by_type"].items()):
        risk = "—"
        if info["cayman_count"] > 0 and info["amd_count"] == 0:
            risk = "🔴 AMD 缺失"
        elif info["common_count"] == 0 and info["cayman_count"] > 0 and info["amd_count"] > 0:
            risk = "🔴 無重疊"
        elif info["cayman_count"] > info["amd_count"] * 2:
            risk = "🟡 AMD 不足"
        elif t == "NUMERIC":
            risk = "🔴 非標準"
        lines.append(
            f"| {t} | {info['cayman_count']} | {info['amd_count']} | "
            f"{info['common_count']} | {info['cayman_only_count']} | "
            f"{info['amd_only_count']} | {risk} |"
        )
    lines.append("")

    # ── 三、關鍵發現 ──
    lines.append("## 三、關鍵發現（按風險排序）\n")
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    for i, f in enumerate(
        sorted(data["critical_findings"], key=lambda x: severity_order.get(x["severity"], 9)),
        1,
    ):
        icon = "🔴" if f["severity"] == "HIGH" else ("🟡" if f["severity"] == "MEDIUM" else "🟢")
        lines.append(f"### {icon} 發現 {i}：{f['finding']}\n")
        lines.append(f"{f['detail']}\n")

    # ── 四、AMD 數值命名機台明細 ──
    lines.append("## 四、AMD 數值命名（非標準）機台明細\n")
    lines.append("以下機台不遵循標準命名規範，為良率風險最大嫌疑因子：\n")
    lines.append("| EQP_ID | EQP_NAME | 備註 |")
    lines.append("|--------|----------|------|")
    for m in data["amd_numeric_machines"]:
        fab = m["eqp_name"].split("_")[0]
        note = "跨 Fab（B6）" if fab != "B1" else "B1 非標準命名"
        lines.append(f"| {m['eqp_id']} | {m['eqp_name']} | {note} |")
    lines.append("")

    # ── 五、僅 Cayman 使用的機台 ──
    lines.append("## 五、僅 Cayman 使用的機台（AMD 未使用）\n")
    lines.append("| EQP_ID | EQP_NAME | 設備類型 |")
    lines.append("|--------|----------|----------|")
    for m in data["cayman_only"]:
        lines.append(f"| {m['eqp_id']} | {m['eqp_name']} | {_extract_eqp_type(m['eqp_name'])} |")
    lines.append("")

    # ── 六、僅 AMD 使用的機台 ──
    lines.append("## 六、僅 AMD 使用的機台（Cayman 未使用）\n")
    lines.append("| EQP_ID | EQP_NAME | 設備類型 | 標準命名 |")
    lines.append("|--------|----------|----------|----------|")
    for m in data["amd_only"]:
        t = _extract_eqp_type(m["eqp_name"])
        std = "✅" if t != "NUMERIC" else "❌ 非標準"
        lines.append(f"| {m['eqp_id']} | {m['eqp_name']} | {t} | {std} |")
    lines.append("")

    # ── 七、限機策略建議 ──
    lines.append("## 七、限機策略建議\n")
    lines.append("### 策略 A：收斂至共用機台（最保守）\n")
    lines.append(
        f"將 AMD 生產限制在與 Cayman 共用的 **{s['common_count']}** 台機台上，"
        f"排除所有 AMD 獨有機台（{s['amd_only_count']} 台）。\n"
    )
    lines.append("**優點**：最大程度消除機台差異變數，快速驗證機台是否為良率差異主因\n")
    lines.append("**缺點**：產能將受限，需評估交期影響\n")

    lines.append("### 策略 B：排除非標準機台（建議優先執行）\n")
    lines.append(
        f"優先排除 AMD 的 **{s['amd_numeric_count']}** 台數值命名非標準機台，"
        f"保留其他標準命名機台。\n"
    )
    lines.append("排除清單：\n")
    for m in data["amd_numeric_machines"]:
        lines.append(f"- {m['eqp_id']} {m['eqp_name']}")
    lines.append("")
    lines.append("**優點**：對產能影響較小，優先排除最高風險機台\n")
    lines.append("**缺點**：未涵蓋 AGST 無重疊等其他差異因素\n")

    lines.append("### 策略 C：補齊關鍵缺失站點\n")
    lines.append("將 Cayman 使用但 AMD 缺少的關鍵站點機台納入 AMD 產線：\n")
    lines.append("1. **BPHI**：加入 B1_BPHI_01/02/03 至 AMD 產線，強化背面檢查能力\n")
    lines.append("2. **SPUT**：加入 B1_SPUT_01、B1_SPUT_04 至 AMD 產線，增加 Sputtering 分配彈性\n")
    lines.append("3. **STEP**：加入 B1_STEP_01/02/03 至 AMD 產線，提升量測站覆蓋率\n")
    lines.append("4. **RESZ**：加入 B1_RESZ_01 至 AMD 產線\n")
    lines.append("5. **AGST**：評估是否將 Cayman 的 AGST 機台（01~06 系列）也用於 AMD 產線\n")
    lines.append("")
    lines.append("**優點**：同時提升 AMD 產線能力與良率\n")
    lines.append("**缺點**：需要設備調度與產能重新規劃\n")

    lines.append("### 建議執行順序\n")
    lines.append("1. **立即執行**：策略 B — 排除 12 台非標準機台，觀察良率變化\n")
    lines.append("2. **短期驗證**：策略 A — 選擇部分 Lot 限機至共用機台，進行 A/B 比較\n")
    lines.append("3. **中期改善**：策略 C — 根據驗證結果，補齊關鍵缺失站點\n")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 獨立執行
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(generate_comparison_report())
