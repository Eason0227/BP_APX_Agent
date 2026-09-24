"""
conflict_orchestrator.py
-------------------------
半導體 Multi-Agent 跨部門衝突偵測與動態調度器 (Orchestrator)

三階段閉環邏輯：
  階段一：平行異步呼叫 EE / PE Agent API，取得結構化風險報告
  階段二：衝突偵測 — PE.suspect_machines ∩ ¬EE.risk_machines → 監控盲點
  階段三：動態路由 — 衝突時觸發 QA /deep-dive 微觀追查；無衝突走一般週報

職責邊界：
  本模組只負責產出 conflict_result（衝突偵測結果）與各部門報告解析，
  不再生成任何主管決策報告。主管決策報告統一由 executive_agent_api.py 的
  _executive_agent（唯一決策引擎）產出。
"""

import asyncio
import json
import os
import re
import traceback
from datetime import datetime
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════════════════
# 一、Pydantic 結構化資料模型 (Structured Output Schemas)
# ═══════════════════════════════════════════════════════════════════════════


class EEAgentResponse(BaseModel):
    """EE 設備工程部 Agent API 回傳結構"""
    status: str = Field(default="unknown", description="回傳狀態：success / error")
    risk_machines: list[str] = Field(default_factory=list, description="EE 判定的高風險機台清單")
    details: str = Field(default="", description="EE 分析摘要文字")
    raw_report: str = Field(default="", description="EE 完整週報原文（Markdown）")


class PEAgentResponse(BaseModel):
    """PE 製程工程部 Agent API 回傳結構"""
    status: str = Field(default="unknown", description="回傳狀態：success / error")
    suspect_machines: list[str] = Field(default_factory=list, description="PE 初判嫌疑機台清單")
    details: str = Field(default="", description="PE 分析摘要文字")
    raw_report: str = Field(default="", description="PE 完整週報原文（Markdown）")


class QAAgentResponse(BaseModel):
    """QA 品質保證部 Agent API 回傳結構"""
    status: str = Field(default="unknown", description="回傳狀態：success / error")
    target_machine: str = Field(default="", description="追查目標機台")
    finding: str = Field(default="", description="QA 微觀追查發現")
    raw_report: str = Field(default="", description="QA 完整週報原文（Markdown）")


class QADeepDiveResult(BaseModel):
    """QA /deep-dive 端點回傳結構（單台機台追查）"""
    status: str = Field(default="unknown", description="追查狀態")
    target_machine: str = Field(default="", description="追查目標機台")
    finding: str = Field(default="", description="微觀追查發現與真因判定")


class ConflictRecord(BaseModel):
    """單筆跨部門衝突紀錄"""
    machine_id: str = Field(..., description="衝突機台 ID")
    pe_status: str = Field(default="suspect", description="PE 觀點：suspect")
    ee_status: str = Field(default="not_flagged", description="EE 觀點：not_flagged / low_risk")
    conflict_type: str = Field(
        default="cross_dept_blind_spot",
        description="衝突類型：cross_dept_blind_spot（跨部門監控盲點）",
    )
    qa_deep_dive: Optional[QADeepDiveResult] = Field(
        default=None, description="QA 深挖追查結果（僅衝突時填入）"
    )


class ConflictDetectionResult(BaseModel):
    """衝突偵測總結"""
    conflict_detected: bool = Field(default=False, description="是否偵測到衝突")
    conflict_count: int = Field(default=0, description="衝突機台數量")
    conflict_records: list[ConflictRecord] = Field(default_factory=list, description="衝突明細")
    ee_risk_machines: list[str] = Field(default_factory=list, description="EE 高風險機台")
    pe_suspect_machines: list[str] = Field(default_factory=list, description="PE 嫌疑機台")
    consensus_machines: list[str] = Field(
        default_factory=list, description="EE 與 PE 共識高風險機台"
    )


class OrchestrationTrace(BaseModel):
    """Orchestrator 執行追蹤紀錄"""
    started_at: str = Field(default="", description="開始時間")
    finished_at: str = Field(default="", description="結束時間")
    total_elapsed_ms: float = Field(default=0.0, description="總耗時（毫秒）")
    stages: list[dict[str, Any]] = Field(default_factory=list, description="各階段追蹤事件")


class OrchestrationResult(BaseModel):
    """Orchestrator 完整輸出"""
    ee_response: EEAgentResponse = Field(default_factory=EEAgentResponse)
    pe_response: PEAgentResponse = Field(default_factory=PEAgentResponse)
    qa_response: QAAgentResponse = Field(default_factory=QAAgentResponse)
    conflict_result: ConflictDetectionResult = Field(default_factory=ConflictDetectionResult)
    trace: OrchestrationTrace = Field(default_factory=OrchestrationTrace)


# ═══════════════════════════════════════════════════════════════════════════
# 二、設定常數
# ═══════════════════════════════════════════════════════════════════════════

# 各 Agent API 端點（可透過環境變數覆寫）
EE_API_BASE = os.getenv("EE_API_BASE", "http://127.0.0.1:8099")
PE_API_BASE = os.getenv("PE_API_BASE", "http://127.0.0.1:8088")
QA_API_BASE = os.getenv("QA_API_BASE", "http://127.0.0.1:8077")

# 機台 ID 正規表達式（支援 B1_ETCH_04 與 EQ01 兩種格式）
_MACHINE_ID_RE = re.compile(r"(?:[A-Z]\d_[A-Z0-9]+_\d{2}|EQ\d{2,4})", re.IGNORECASE)

# 從報告文字中擷取機台的關鍵詞提示
_EE_RISK_HINTS = ("高風險", "critical", "warning", "異常", "熱點", "oob", "風險")
_PE_SUSPECT_HINTS = ("嫌疑", "可疑", "懷疑", "關注", "異常", "損耗", "root cause")


# ═══════════════════════════════════════════════════════════════════════════
# 三、工具函式
# ═══════════════════════════════════════════════════════════════════════════


def _extract_machine_ids(text: str) -> list[str]:
    """從任意文字中擷取所有機台 ID，去重並排序。"""
    if not text:
        return []
    ids = {m.group(0).upper() for m in _MACHINE_ID_RE.finditer(text)}
    return sorted(ids)


def _extract_tagged_machines(text: str, hints: tuple[str, ...]) -> list[str]:
    """從報告逐行抽取同時命中機台 ID 與關鍵詞的機台。"""
    tagged: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower_line = line.lower()
        if not any(h.lower() in lower_line for h in hints):
            continue
        for machine_id in _extract_machine_ids(line):
            tagged.add(machine_id)
    return sorted(tagged)


def _append_stage(trace: OrchestrationTrace, stage_name: str, **data: Any) -> None:
    """向 trace 追加一個階段事件。"""
    trace.stages.append({
        "ts": datetime.now().isoformat(),
        "stage": stage_name,
        **{k: v for k, v in data.items()},
    })


# ═══════════════════════════════════════════════════════════════════════════
# 四、階段一：平行異步呼叫 EE / PE Agent API
# ═══════════════════════════════════════════════════════════════════════════


async def _call_ee_agent(
    client: httpx.AsyncClient,
    week_range: str,
    trace: OrchestrationTrace,
) -> EEAgentResponse:
    """
    呼叫 EE Agent API /latest-week 端點，取得結構化風險報告。
    回傳後從報告文字中解析 risk_machines。
    """
    _append_stage(trace, "ee_agent_start", week_range=week_range)
    try:
        resp = await client.post(
            f"{EE_API_BASE}/latest-week",
            json={"week_range": week_range, "include_trace": False},
            timeout=httpx.Timeout(120.0, connect=20.0),
        )
        resp.raise_for_status()
        body = resp.json()

        raw_report = body.get("output", "") or body.get("report", "") or ""
        # 從 EE 報告文字中擷取高風險機台
        risk_machines = _extract_tagged_machines(raw_report, _EE_RISK_HINTS)
        # 若關鍵詞未命中，退回所有辨識到的機台
        if not risk_machines:
            risk_machines = _extract_machine_ids(raw_report)

        result = EEAgentResponse(
            status="success",
            risk_machines=risk_machines,
            details=raw_report[:500],
            raw_report=raw_report,
        )
        _append_stage(
            trace, "ee_agent_done",
            risk_machines=result.risk_machines,
            report_length=len(raw_report),
        )
        return result

    except httpx.HTTPStatusError as e:
        error_msg = f"EE API HTTP {e.response.status_code}: {e.response.text[:300]}"
        _append_stage(trace, "ee_agent_error", error=error_msg)
        return EEAgentResponse(status="error", details=error_msg)
    except Exception as e:
        error_msg = f"EE Agent 呼叫失敗: {str(e)}"
        _append_stage(trace, "ee_agent_error", error=error_msg, traceback=traceback.format_exc())
        return EEAgentResponse(status="error", details=error_msg)


async def _call_pe_agent(
    client: httpx.AsyncClient,
    week_range: str,
    trace: OrchestrationTrace,
) -> PEAgentResponse:
    """
    呼叫 PE Agent API /api/weekly-report 端點，取得結構化良率報告。
    回傳後從報告文字中解析 suspect_machines。
    """
    _append_stage(trace, "pe_agent_start", week_range=week_range)
    try:
        # PE API 使用 start_date / end_date 格式
        # 從 week_range (如 2026-W21) 推算日期區間
        start_date, end_date = _week_to_date_range(week_range)

        resp = await client.post(
            f"{PE_API_BASE}/api/weekly-report",
            json={
                "start_date": start_date,
                "end_date": end_date,
                "min_yield_loss": 0.01,
                "limit": 30,
                "include_trace": False,
            },
            timeout=httpx.Timeout(120.0, connect=20.0),
        )
        resp.raise_for_status()
        body = resp.json()

        raw_report = body.get("report", "") or body.get("output", "") or ""
        # 從 PE 報告文字中擷取嫌疑機台
        suspect_machines = _extract_tagged_machines(raw_report, _PE_SUSPECT_HINTS)
        if not suspect_machines:
            suspect_machines = _extract_machine_ids(raw_report)

        result = PEAgentResponse(
            status="success",
            suspect_machines=suspect_machines,
            details=raw_report[:500],
            raw_report=raw_report,
        )
        _append_stage(
            trace, "pe_agent_done",
            suspect_machines=result.suspect_machines,
            report_length=len(raw_report),
        )
        return result

    except httpx.HTTPStatusError as e:
        error_msg = f"PE API HTTP {e.response.status_code}: {e.response.text[:300]}"
        _append_stage(trace, "pe_agent_error", error=error_msg)
        return PEAgentResponse(status="error", details=error_msg)
    except Exception as e:
        error_msg = f"PE Agent 呼叫失敗: {str(e)}"
        _append_stage(trace, "pe_agent_error", error=error_msg, traceback=traceback.format_exc())
        return PEAgentResponse(status="error", details=error_msg)


async def _call_qa_weekly_report(
    client: httpx.AsyncClient,
    week_range: str,
    trace: OrchestrationTrace,
) -> QAAgentResponse:
    """
    呼叫 QA Agent API /generate-report/{year_week} 端點，取得一般週報。
    僅在「無衝突」時使用。
    """
    _append_stage(trace, "qa_weekly_start", week_range=week_range)
    try:
        resp = await client.get(
            f"{QA_API_BASE}/generate-report/{week_range}",
            params={"include_trace": False},
            timeout=httpx.Timeout(120.0, connect=20.0),
        )
        resp.raise_for_status()
        body = resp.json()

        raw_report = body.get("answer", "") or body.get("report", "") or ""
        result = QAAgentResponse(
            status="success",
            raw_report=raw_report,
        )
        _append_stage(trace, "qa_weekly_done", report_length=len(raw_report))
        return result

    except Exception as e:
        error_msg = f"QA 週報呼叫失敗: {str(e)}"
        _append_stage(trace, "qa_weekly_error", error=error_msg)
        return QAAgentResponse(status="error", finding=error_msg)


def _week_to_date_range(week_range: str) -> tuple[str, str]:
    """
    將 ISO 週次（如 2026-W21）轉換為 start_date / end_date 字串。
    回傳該週的週一與週日。
    """
    from datetime import date, timedelta

    m = re.search(r"(\d{4})-?W(\d{1,2})", week_range or "")
    if m:
        year = int(m.group(1))
        week = int(m.group(2))
        monday = date.fromisocalendar(year, week, 1)
        sunday = date.fromisocalendar(year, week, 7)
        return monday.isoformat(), sunday.isoformat()

    # Fallback：最近 7 天
    today = date.today()
    start = today - timedelta(days=7)
    return start.isoformat(), today.isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# 五、階段二：衝突偵測演算法
# ═══════════════════════════════════════════════════════════════════════════


def detect_conflicts(
    ee_resp: EEAgentResponse,
    pe_resp: PEAgentResponse,
) -> ConflictDetectionResult:
    """
    衝突判定演算法：
    遍歷 PE.suspect_machines，若某機台存在於 PE 嫌疑清單
    但不存在於 EE.risk_machines，即觸發「跨部門監控盲點衝突」。

    同時計算 EE ∩ PE 共識高風險機台。
    """
    ee_set = set(m.upper() for m in ee_resp.risk_machines)
    pe_set = set(m.upper() for m in pe_resp.suspect_machines)

    # 衝突機台：PE 懷疑但 EE 未示警
    conflict_machines = sorted(pe_set - ee_set)
    # 共識機台：兩部門都標記
    consensus_machines = sorted(pe_set & ee_set)

    records = []
    for mid in conflict_machines:
        records.append(ConflictRecord(
            machine_id=mid,
            pe_status="suspect",
            ee_status="not_flagged",
            conflict_type="cross_dept_blind_spot",
        ))

    return ConflictDetectionResult(
        conflict_detected=len(records) > 0,
        conflict_count=len(records),
        conflict_records=records,
        ee_risk_machines=sorted(ee_set),
        pe_suspect_machines=sorted(pe_set),
        consensus_machines=consensus_machines,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 六、階段三：QA 深挖追查（衝突時動態路由）
# ═══════════════════════════════════════════════════════════════════════════


async def _call_qa_deep_dive(
    client: httpx.AsyncClient,
    target_machines: list[str],
    week_range: str,
    trace: OrchestrationTrace,
) -> list[QADeepDiveResult]:
    """
    對衝突機台逐台呼叫 QA /deep-dive 端點進行微觀追查。
    若 /deep-dive 失敗，fallback 到 /chat 端點。
    """
    results: list[QADeepDiveResult] = []

    for machine_id in target_machines:
        _append_stage(trace, "qa_deep_dive_start", target_machine=machine_id)
        try:
            # 優先嘗試 /deep-dive 端點
            resp = await client.post(
                f"{QA_API_BASE}/deep-dive",
                json={
                    "target_machines": [machine_id],
                    "week_range": week_range,
                    "reason": f"PE 懷疑 {machine_id} 但 EE 未示警，觸發跨部門監控盲點追查",
                },
                timeout=httpx.Timeout(60.0, connect=20.0),
            )
            resp.raise_for_status()
            body = resp.json()

            dive_result = QADeepDiveResult(
                status=body.get("status", "success"),
                target_machine=body.get("target_machine", machine_id),
                finding=body.get("finding", json.dumps(body, ensure_ascii=False)),
            )
            _append_stage(trace, "qa_deep_dive_done", target_machine=machine_id, finding=dive_result.finding[:300])
            results.append(dive_result)

        except Exception as deep_err:
            # Fallback：透過 /chat 端點進行追查
            _append_stage(
                trace, "qa_deep_dive_fallback",
                target_machine=machine_id,
                deep_dive_error=str(deep_err),
            )
            try:
                fallback_query = (
                    f"請針對機台 {machine_id} 進行微觀追查分析。"
                    f"此機台被 PE 列為嫌疑機台，但 EE 未將其標記為高風險。"
                    f"請查詢該機台的保養軌跡與炭膠帶污染源分析，"
                    f"判斷是否存在 EE 感測器未偵測到的微觀污染問題。"
                    f"請回傳 JSON 格式，包含 status、target_machine、finding 欄位。"
                )
                resp = await client.post(
                    f"{QA_API_BASE}/chat",
                    json={"query": fallback_query, "include_trace": False},
                    timeout=httpx.Timeout(60.0, connect=20.0),
                )
                resp.raise_for_status()
                body = resp.json()

                finding_text = body.get("answer", "") or body.get("finding", "")
                dive_result = QADeepDiveResult(
                    status="success_via_fallback",
                    target_machine=machine_id,
                    finding=finding_text,
                )
                _append_stage(trace, "qa_deep_dive_fallback_done", target_machine=machine_id)
                results.append(dive_result)

            except Exception as fallback_err:
                error_msg = f"QA deep-dive 與 fallback 均失敗: {str(fallback_err)}"
                _append_stage(trace, "qa_deep_dive_all_failed", target_machine=machine_id, error=error_msg)
                results.append(QADeepDiveResult(
                    status="error",
                    target_machine=machine_id,
                    finding=error_msg,
                ))

    return results


# ═══════════════════════════════════════════════════════════════════════════
# 七、主調度函式 — 完整三階段 Pipeline
# ═══════════════════════════════════════════════════════════════════════════


async def orchestrate_full_pipeline(
    week_range: str = "",
) -> OrchestrationResult:
    """
    執行完整的三階段跨部門衝突偵測與動態調度 Pipeline。

    參數：
    - week_range: ISO 週次，例如 "2026-W21"

    回傳：
    - OrchestrationResult：包含衝突偵測結果與各部門報告解析（不含主管決策報告）
    """
    trace = OrchestrationTrace(started_at=datetime.now().isoformat())
    _append_stage(trace, "pipeline_start", week_range=week_range)

    async with httpx.AsyncClient(
        proxy=None,
        trust_env=False,
        timeout=httpx.Timeout(180.0, connect=30.0),
    ) as client:

        # ── 階段一：平行異步呼叫 EE / PE ──
        _append_stage(trace, "stage1_parallel_start")
        ee_task = _call_ee_agent(client, week_range, trace)
        pe_task = _call_pe_agent(client, week_range, trace)
        ee_resp, pe_resp = await asyncio.gather(ee_task, pe_task, return_exceptions=False)
        _append_stage(
            trace, "stage1_parallel_done",
            ee_status=ee_resp.status,
            pe_status=pe_resp.status,
            ee_risk_count=len(ee_resp.risk_machines),
            pe_suspect_count=len(pe_resp.suspect_machines),
        )

        # ── 階段二：衝突偵測 ──
        _append_stage(trace, "stage2_conflict_detection_start")
        conflict_result = detect_conflicts(ee_resp, pe_resp)
        _append_stage(
            trace, "stage2_conflict_detection_done",
            conflict_detected=conflict_result.conflict_detected,
            conflict_count=conflict_result.conflict_count,
            conflict_machines=[r.machine_id for r in conflict_result.conflict_records],
            consensus_machines=conflict_result.consensus_machines,
        )

        # ── 階段三：動態路由 — QA 深挖或一般週報 ──
        qa_resp = QAAgentResponse()

        if conflict_result.conflict_detected:
            _append_stage(
                trace, "stage3_dynamic_route",
                route="deep_dive",
                target_machines=[r.machine_id for r in conflict_result.conflict_records],
            )
            # 衝突時：觸發 QA deep-dive 追查
            conflict_machine_ids = [r.machine_id for r in conflict_result.conflict_records]
            deep_dive_results = await _call_qa_deep_dive(
                client, conflict_machine_ids, week_range, trace,
            )

            # 將 deep-dive 結果回填到 conflict_records
            dive_map = {d.target_machine.upper(): d for d in deep_dive_results}
            for rec in conflict_result.conflict_records:
                rec.qa_deep_dive = dive_map.get(rec.machine_id.upper())

            # 同時也取得 QA 一般週報作為補充
            qa_resp = await _call_qa_weekly_report(client, week_range, trace)

            # 將 deep-dive 發現附加到 QA 報告
            dive_findings = []
            for d in deep_dive_results:
                if d.finding:
                    dive_findings.append(
                        f"### 衝突追查：{d.target_machine}\n"
                        f"狀態：{d.status}\n"
                        f"發現：{d.finding}"
                    )
            if dive_findings:
                qa_resp.raw_report += "\n\n## QA 衝突追查補充\n" + "\n\n".join(dive_findings)
                qa_resp.finding = "; ".join(d.finding[:200] for d in deep_dive_results if d.finding)

        else:
            _append_stage(trace, "stage3_dynamic_route", route="normal_weekly")
            # 無衝突：走一般 QA 週報
            qa_resp = await _call_qa_weekly_report(client, week_range, trace)

    trace.finished_at = datetime.now().isoformat()
    if trace.started_at:
        try:
            t0 = datetime.fromisoformat(trace.started_at)
            t1 = datetime.fromisoformat(trace.finished_at)
            trace.total_elapsed_ms = round((t1 - t0).total_seconds() * 1000, 2)
        except Exception:
            pass

    _append_stage(trace, "pipeline_done", total_elapsed_ms=trace.total_elapsed_ms)

    return OrchestrationResult(
        ee_response=ee_resp,
        pe_response=pe_resp,
        qa_response=qa_resp,
        conflict_result=conflict_result,
        trace=trace,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 八、便捷函式 — 供 executive_agent_api.py 整合使用
# ═══════════════════════════════════════════════════════════════════════════


async def run_conflict_detection_from_reports(
    ee_report: str,
    pe_report: str,
    week_range: str = "",
    qa_report: str = "",
) -> OrchestrationResult:
    """
    從已有的 EE / PE 報告文字中執行衝突偵測與 QA 追查，回傳結構化衝突結果。
    適用於 /api/weekly-decision-fast 端點（UI 已預載報告的場景）。

    參數：
    - ee_report / pe_report：UI 預載的部門週報
    - week_range：ISO 週次，例如 "2026-W21"
    - qa_report：UI 預載的 QA 週報（補充到 qa_response.raw_report）

    此函式不重新呼叫 EE/PE API，而是直接從報告文字解析機台清單，
    再執行階段二（衝突偵測）、階段三（QA 動態路由）。
    不再產出主管決策報告，該職責由 executive_agent_api.py 的 _executive_agent 負責。
    """
    trace = OrchestrationTrace(started_at=datetime.now().isoformat())
    _append_stage(trace, "pipeline_from_reports_start", week_range=week_range)

    # 從報告文字解析機台
    ee_risk = _extract_tagged_machines(ee_report, _EE_RISK_HINTS)
    if not ee_risk:
        ee_risk = _extract_machine_ids(ee_report)

    pe_suspect = _extract_tagged_machines(pe_report, _PE_SUSPECT_HINTS)
    if not pe_suspect:
        pe_suspect = _extract_machine_ids(pe_report)

    ee_resp = EEAgentResponse(
        status="success",
        risk_machines=ee_risk,
        details="（從預載報告解析）",
        raw_report=ee_report,
    )
    pe_resp = PEAgentResponse(
        status="success",
        suspect_machines=pe_suspect,
        details="（從預載報告解析）",
        raw_report=pe_report,
    )

    # 階段二：衝突偵測
    conflict_result = detect_conflicts(ee_resp, pe_resp)
    _append_stage(
        trace, "conflict_detection_done",
        conflict_detected=conflict_result.conflict_detected,
        conflict_count=conflict_result.conflict_count,
        conflict_machines=[r.machine_id for r in conflict_result.conflict_records],
    )

    # 階段三：動態路由
    qa_resp = QAAgentResponse()

    if conflict_result.conflict_detected:
        async with httpx.AsyncClient(
            proxy=None, trust_env=False,
            timeout=httpx.Timeout(120.0, connect=20.0),
        ) as client:
            conflict_machine_ids = [r.machine_id for r in conflict_result.conflict_records]
            deep_dive_results = await _call_qa_deep_dive(
                client, conflict_machine_ids, week_range, trace,
            )
            dive_map = {d.target_machine.upper(): d for d in deep_dive_results}
            for rec in conflict_result.conflict_records:
                rec.qa_deep_dive = dive_map.get(rec.machine_id.upper())

            # 彙整 QA 追查發現
            findings = [d.finding for d in deep_dive_results if d.finding]
            qa_resp = QAAgentResponse(
                status="success" if findings else "no_findings",
                finding="; ".join(f[:300] for f in findings),
                raw_report="\n\n".join(
                    f"### 衝突追查：{d.target_machine}\n{d.finding}"
                    for d in deep_dive_results if d.finding
                ),
            )
    else:
        _append_stage(trace, "no_conflict_skip_deep_dive")

    # 若有預載 QA 週報，補充到 qa_resp.raw_report（不覆蓋已有的 deep-dive 結果）
    if qa_report:
        existing = qa_resp.raw_report.strip()
        qa_resp = QAAgentResponse(
            status=qa_resp.status if qa_resp.status != "unknown" else "success",
            target_machine=qa_resp.target_machine,
            finding=qa_resp.finding,
            raw_report=(existing + "\n\n---\n\n" + qa_report) if existing else qa_report,
        )

    trace.finished_at = datetime.now().isoformat()
    try:
        t0 = datetime.fromisoformat(trace.started_at)
        t1 = datetime.fromisoformat(trace.finished_at)
        trace.total_elapsed_ms = round((t1 - t0).total_seconds() * 1000, 2)
    except Exception:
        pass

    return OrchestrationResult(
        ee_response=ee_resp,
        pe_response=pe_resp,
        qa_response=qa_resp,
        conflict_result=conflict_result,
        trace=trace,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 九、獨立執行入口（測試用）
# ═══════════════════════════════════════════════════════════════════════════


async def _main():
    """獨立執行測試：完整三階段 Pipeline。"""
    print("=" * 70)
    print("半導體 Multi-Agent 跨部門衝突偵測與動態調度器")
    print("=" * 70)

    week = input("請輸入週次（例如 2026-W21，直接 Enter 使用最新）: ").strip() or ""

    result = await orchestrate_full_pipeline(week_range=week)

    print("\n" + "=" * 70)
    print("【調度結果摘要】")
    print("=" * 70)
    print(f"EE 狀態: {result.ee_response.status}, 高風險機台: {result.ee_response.risk_machines}")
    print(f"PE 狀態: {result.pe_response.status}, 嫌疑機台: {result.pe_response.suspect_machines}")
    print(f"衝突偵測: {result.conflict_result.conflict_detected}")
    print(f"衝突機台: {[r.machine_id for r in result.conflict_result.conflict_records]}")
    print(f"共識機台: {result.conflict_result.consensus_machines}")
    print(f"QA 狀態: {result.qa_response.status}")
    print(f"總耗時: {result.trace.total_elapsed_ms:.0f} ms")

    # 輸出完整 trace（JSON）
    trace_json = result.trace.model_dump_json(indent=2)
    print("\n" + "=" * 70)
    print("【執行 Trace】")
    print("=" * 70)
    print(trace_json)


if __name__ == "__main__":
    asyncio.run(_main())
