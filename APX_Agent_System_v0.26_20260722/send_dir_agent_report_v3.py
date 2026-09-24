import html
import json
import os
import re
import smtplib
import requests
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── 全域設定 ──
LLM_API_URL = "http://10.11.32.155:4000/v1/chat/completions"
API_KEY = "sk-vVPq-hpFctglwngOCf7bOA"
# LLM_MODEL = "Qwen3.6-35B-A3B"
LLM_MODEL = "Gemma4-31B"


# LLM_API_URL = "http://10.11.33.5:9120/v1/chat/completions"
# LLM_MODEL = "/model/gpt-oss-120b"

FIXED_FACTORY = "B1"
FIXED_SENDER = "APX Agent"
FIXED_SUBJECT = "[ BP-APX Agent ] Weekly Particle Control Decision Report"
FIXED_WEEK = "W30"
SYSTEM_URL = "http://10.11.33.250:8503"  # 請替換為實際系統報表網頁連結

class alarm_information:
    def __init__(self, factory_name: str):
        self.alarm_mail = self._get_alarm_mail(factory_name)
        self.cc = [ "Easonch_Tsai@aseglobal.com", "YT_Chien@aseglobal.com", "VincentYR_Lee@aseglobal.com", "Chariss_Hsu@aseglobal.com"]

    def _get_alarm_mail(self, factory_name: str):
        mapping = {
            "B1": ["ASEK_Bumping_Eng3_EEB1@aseglobal.com"],
        }
        return mapping.get(factory_name, ["Easonch_Tsai@aseglobal.com"])


class mail_content:
    def __init__(self, sender: str, title: str, msg_content: str = ""):
        self.sender = sender
        self.subject = title
        self.content = f"""
        <html>
        <head>
            <meta charset=\"UTF-8\">
        </head>
        <body style=\"font-family: 'Microsoft JhengHei', '微軟正黑體', sans-serif; font-size: 12pt; line-height: 1.5;\">
            <p><strong>** Security C **</strong></p>
            {msg_content}
            <br><br>
            <div style=\"font-size: 11pt;\">
                ASE Group<br>
                Tel : 02-77517872 .Fax : +886.2.27186076<br>
                Email Address : Easonch_Tsai@aseglobal.com
            </div>
            <br>
            <div style=\"font-size: 11pt; border-top: 1px solid #ccc; padding-top: 10px; margin-top: 20px; white-space: pre-line;\">
            ----- ASE Confidentiality Notice -----<br>
            The preceding message (including any attachments) contains proprietary information that may be confidential, privileged, or constitute non-public information. It is to be read and used solely by the intended recipient(s) or conveyed only to the designated recipient(s). If you are not an intended recipient of this message, please notify the author or sender immediately either by replying to this message or by telephone at 886-7-3617131 and delete this message (including any attachments hereto) immediately from your system. You should not read ,retain, disseminate, distribute, copy or use this message in whole or in part for any purpose, not disclose all or any part of its content to any other person.<br>
            ----- ASE Confidentiality Notice -----<br>
            <br>
            </div>
        </body>
        </html>
        """


def smtp_send(content: mail_content, receiver: list[str], cc: list[str]) -> bool:
    try:
        smtp = smtplib.SMTP("10.12.10.31")

        message = MIMEMultipart()
        message["From"] = Header(f"{content.sender} <Easonch_Tsai@aseglobal.com>", "utf-8")
        message["To"] = ", ".join(receiver)
        message["Cc"] = ", ".join(cc)
        message["Subject"] = Header(content.subject, "utf-8")

        html_part = MIMEText(content.content, "html", "utf-8")
        message.attach(html_part)

        all_recipients = receiver + cc
        smtp.sendmail(content.sender, all_recipients, message.as_string())
        smtp.quit()
        return True
    except Exception as ex:
        print(f"smtp_send 發生錯誤: {ex}")
        return False


def _default_cache_json_path() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "analysis_results_cache.json")

def _find_latest_week_key(by_week: dict) -> str | None:
    candidates = []
    for key in by_week.keys():
        match = re.match(r"^W(\d+)$", str(key).strip(), flags=re.IGNORECASE)
        if match:
            candidates.append((int(match.group(1)), f"W{int(match.group(1))}"))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _read_exec_report_from_cache(cache_path: str, week: str = "") -> tuple[str, str]:
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    by_week = data.get("by_week")
    if not isinstance(by_week, dict) or not by_week:
        raise ValueError("analysis_results_cache.json 缺少 by_week 區塊或內容為空")

    selected_week = week.strip().upper() if week else ""
    if not selected_week:
        selected_week = _find_latest_week_key(by_week)
        if not selected_week:
            raise ValueError("by_week 內找不到有效週別（例如 W21）")

    week_data = by_week.get(selected_week)
    if not isinstance(week_data, dict):
        raise ValueError(f"找不到週別資料: {selected_week}")

    exec_report = week_data.get("exec_report", "")
    if not isinstance(exec_report, str) or not exec_report.strip():
        raise ValueError(f"{selected_week} 缺少 exec_report 內容")

    return exec_report, selected_week


def _emoji_to_text(text: str) -> str:
    """將常見 emoji 轉為可讀文字標記，避免郵件客戶端顯示異常。"""
    replacements = {
        "0️⃣": "[0]",
        "1️⃣": "[1]",
        "2️⃣": "[2]",
        "3️⃣": "[3]",
        "4️⃣": "[4]",
        "5️⃣": "[5]",
        "6️⃣": "[6]",
        "7️⃣": "[7]",
        "8️⃣": "[8]",
        "9️⃣": "[9]",
        "🔟": "[10]",
        "✅": "[APPROVED]",
        "⏸️": "[ON_HOLD]",
        "⏸": "[ON_HOLD]",
        "⏳": "[PENDING]",
        "⚠️": "[WARNING]",
        "⚠": "[WARNING]",
        "📝": "[NOTE]",
        "📄": "[REPORT]",
        "💬": "[CHAT]",
        "🔗": "[ASSIGN]",
        "💾": "[SAVE]",
        "⬇️": "[DOWNLOAD]",
        "⬇": "[DOWNLOAD]",
        "📅": "[WEEK]",
        "🔍": "[ANALYSIS]",
        "⚖️": "[COMPARE]",
        "⚖": "[COMPARE]",
        "📉": "[TREND]",
    }

    normalized = text
    for k, v in replacements.items():
        normalized = normalized.replace(k, v)
    return normalized


def _normalize_problematic_symbols(text: str) -> str:
    """將常見郵件客戶端易顯示為方框的符號轉為 ASCII。"""
    replacements = {
        "≥": ">=",
        "≤": "<=",
        "≧": ">=",
        "≦": "<=",
        "→": "->",
        "←": "<-",
        "–": "-",
        "—": "-",
        "−": "-",
    }

    normalized = text
    for k, v in replacements.items():
        normalized = normalized.replace(k, v)
    return normalized


def _remove_remaining_emoji(text: str) -> str:
    """移除仍未被轉換的 emoji / pictographs。"""
    emoji_pattern = re.compile(
        "["
        "\U0001F000-\U0001FAFF"
        "\U00002600-\U000027BF"
        "\U0001F1E6-\U0001F1FF"
        "\U0000FE0F"
        "\u200d"
        "\u20e3"
        "]",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub("", text)


def _normalize_emoji_for_email(text: str) -> str:
    """先將 emoji 轉為文字標記，再移除剩餘 emoji。"""
    normalized = _emoji_to_text(text)
    normalized = _normalize_problematic_symbols(normalized)
    return _remove_remaining_emoji(normalized)


def _repair_priority_tsv_rows(markdown_text: str) -> str:
    """將以 Tab 分隔的 P1/P2... 列修復為 Markdown 表格，避免單列解析失敗。"""
    headers = ["優先序", "機台", "決策指令", "決策原因", "主責部門", "協作部門", "時限", "預期效果"]
    separator = ["--------", "------", "----------", "----------", "----------", "----------", "------", "----------"]

    lines = markdown_text.splitlines()
    out_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if "\t" in line and re.match(r"^P\d+\b", stripped, flags=re.IGNORECASE):
            block_rows = []

            while i < len(lines):
                row = lines[i]
                row_stripped = row.strip()
                if "\t" not in row or not re.match(r"^P\d+\b", row_stripped, flags=re.IGNORECASE):
                    break

                cells = [c.strip() for c in row.split("\t")]
                if len(cells) < 8:
                    cells += [""] * (8 - len(cells))
                elif len(cells) > 8:
                    cells = cells[:7] + [" ".join(cells[7:]).strip()]

                block_rows.append(cells)
                i += 1

            out_lines.append("| " + " | ".join(headers) + " |")
            out_lines.append("| " + " | ".join(separator) + " |")
            for row_cells in block_rows:
                out_lines.append("| " + " | ".join(row_cells) + " |")
            continue

        out_lines.append(line)
        i += 1

    return "\n".join(out_lines)


def _strip_markdown_code_fence(text: str) -> str:
    """移除 LLM 可能包上的 ```html code fence。"""
    fenced = re.match(r"^\s*```(?:html)?\s*(.*?)\s*```\s*$", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return text.strip()


def _render_markdown_via_llm(report_text: str) -> str | None:
    repaired_text = _repair_priority_tsv_rows(report_text)
    safe_text = _normalize_emoji_for_email(repaired_text)

    system_prompt = (
        "你是專業文件排版助手。"
        "請把使用者提供的 Markdown 轉成可直接嵌入 email body 的 HTML 片段，"
        "保留標題、段落、清單、程式碼區塊與表格語意。"
        "若遇到 emoji，請轉為方括號英文標記（如 [WARNING]）。"
        "請僅輸出 HTML，不要輸出 ``` code fence、不要輸出額外說明。"
    )
    user_prompt = f"以下是 Markdown 內容，請直接轉成 HTML：\n\n{safe_text}"

    payload = {
        "model": f"{LLM_MODEL}",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # "temperature": 0.1,
    }
    headers = {
        "Content-Type" : "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    try:
        # print(payload)
        # print(headers)
        # print(LLM_API_URL)
        response = requests.post(
            LLM_API_URL,
            json=payload,
            timeout=120,
            proxies={"http": None, "https": None},
            headers = headers
        )
        response.raise_for_status()
        data = response.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not content:
            return None
        return _strip_markdown_code_fence(content)
    except Exception as ex:
        print(f"LLM 轉換失敗，改用本地 fallback：{ex}")
        return None


def _render_executive_summary_via_llm(
    report_text: str,
    system_url: str = SYSTEM_URL,
) -> str | None:
    """呼叫 LLM 將完整決策報告提煉為 300-400 字的 Executive Summary，輸出 HTML 片段。"""
    safe_text = _normalize_emoji_for_email(_repair_priority_tsv_rows(report_text))
    escaped_url = html.escape(system_url)

    system_prompt = (
        "# Role\n"
        "你是一位資深的半導體智慧製造決策特助（Executive Assistant AI），"
        "擅長從複雜的跨部門（EE/PE/QA）Agent 診斷報告中，"
        "提煉出對廠長與高階主管最具價值的關鍵洞察。\n\n"

        "# Task\n"
        "請將輸入的「決策報告」去蕪存菁，"
        "改寫成一封精簡、直接、行動導向的 Executive Summary Email。"
        "詳細的機台清單與歷史 Action Items 請精簡，引導讀者點擊網頁查看。\n\n"

        "# Constraints\n"
        "1. 語言：繁體中文（專業半導體工廠術語，如 Lot、Yield、OOB、Rework 等維持英文，例外->WRI要翻譯為綜合風險指數）。\n"
        "2. 篇幅：控制在 400 - 500 字以內，讀者能在 30 秒內抓到重點。\n"
        "3. 格式：使用清晰的列點，避免大塊表格，確保上易於閱讀。\n"
        # "4. 嚴格禁止：不要把所有 P2/P3 機台的具體保養步驟（如清潔濾網、換刷子）寫出來，"
        "只抓核心異常與 P1/P2 級別行動。\n\n"

        "# Output Format\n"
        "請輸出可直接嵌入 email body 的 HTML 片段，"
        "不要加 ``` code fence，不要輸出額外說明。\n"
        "結構依序為：\n"
        "<p><strong>【本週核心摘要】</strong><br>（一句話總結當前廠區整體狀況）</p>\n"
        "<p><strong>【Top 3 關鍵風險與立即行動 】</strong></p><ul</ul>\n"
        "<p><strong>【決策原因 / 衝突原因】</strong></p><ul>（列點）</ul>\n"
        # "<p><strong>【Action Items 逾期風險】</strong></p><ul>（列點）</ul>\n"

        "<p>詳細交叉比對數據、完整機台指令及完整會議紀錄，請至系統網頁查閱：<br>"
        "<a href='[SYSTEM_URL]'>[SYSTEM_URL]</a></p>\n"
        "<p><br>APX Agent</p>"
    )
    
    user_prompt = (
        f"以下是本週決策報告原文，請依格式提煉為 Executive Summary HTML 片段。\n"
        f"系統網頁連結請使用：{escaped_url}\n\n"
        f"{safe_text}"
    )

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # "temperature": 0.3,
    }

    headers = {
        "Content-Type" : "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    try:
        response = requests.post(
            LLM_API_URL,
            json=payload,
            timeout=120,
            proxies={"http": None, "https": None},
            headers = headers
        )
        response.raise_for_status()
        data = response.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not content:
            return None
        return _strip_markdown_code_fence(content)
    except Exception as ex:
        print(f"Executive Summary LLM 失敗，改用完整報告模式：{ex}")
        return None


def _report_text_to_html(report_text: str, use_llm: bool = True) -> str:
    repaired_text = _repair_priority_tsv_rows(report_text)
    safe_text = _normalize_emoji_for_email(repaired_text)

    _DIR_REPORT_STYLE = (
        "<style>"
        ".dir-report table { border-collapse: collapse; width: 100%; margin: 8px 0; }"
        ".dir-report th, .dir-report td { border: 1px solid #666; padding: 6px 8px; vertical-align: top; }"
        ".dir-report th { background: #f3f3f3; }"
        ".dir-report pre { background: #f8f8f8; padding: 8px; border: 1px solid #ddd; overflow-x: auto; }"
        ".dir-report code { font-family: Consolas, monospace; }"
        "</style>"
        "<div class='dir-report' style='font-family: Microsoft JhengHei, sans-serif; font-size: 11pt; line-height: 1.6;'>"
    )

    if use_llm:
        # 優先：大局摘要模式（300-400 字 Executive Summary + 網頁連結）
        summary_html = _render_executive_summary_via_llm(report_text)
        if summary_html:
            return (
                "<p><b>Dir sir：</b></p>"
                + _DIR_REPORT_STYLE
                + summary_html
                + "</div>"
            )

        # Fallback：完整報告轉 HTML
        llm_html = _render_markdown_via_llm(report_text)
        if llm_html:
            return (
                "<p><b>Dir sir：</b></p>"
                + _DIR_REPORT_STYLE
                + llm_html
                + "</div>"
            )

    # Fallback: 純文字模式，仍可寄出內容。
    return (
        "<p><b>Dir sir：</b></p>"
        f"<pre style='font-family: Consolas, monospace; font-size: 10.5pt; white-space: pre-wrap;'>{html.escape(safe_text)}</pre>"
    )


def main():
    cache_json_path = _default_cache_json_path()
    if not os.path.exists(cache_json_path):
        print(f"找不到分析快取檔: {cache_json_path}")
        raise SystemExit(1)

    try:
        report_text, selected_week = _read_exec_report_from_cache(cache_json_path, FIXED_WEEK)
    except Exception as ex:
        print(f"讀取 exec_report 失敗: {ex}")
        raise SystemExit(1)

    msg_content = _report_text_to_html(report_text, use_llm=True)

    alarm_info = alarm_information(FIXED_FACTORY)
    content = mail_content(FIXED_SENDER, FIXED_SUBJECT, msg_content)

    success = smtp_send(content, alarm_info.alarm_mail, alarm_info.cc)
    if success:
        print("郵件發送成功")
        print(f"來源 JSON: {cache_json_path}")
        print(f"週別: {selected_week}")
        print(f"To: {', '.join(alarm_info.alarm_mail)}")
        print(f"Cc: {', '.join(alarm_info.cc)}")
    else:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
