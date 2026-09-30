# -*- coding: utf-8 -*-
"""
外部复盘报告（v1.8 / v2.0 协议）清洗与要点提取。

导入时执行：
1. 按协议章节（0-16）切分 Markdown；
2. 丢弃「过程自述类」章节（0/1/2/3/16），保留核心章节（4-15）；
3. 从核心正文提取「要点速览」结构化字段（宽松正则，抓不到即跳过）。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# 协议章节关键词（v1.8 与 v2.0 通用，按序号覆盖）
SECTION_KEYWORDS: Dict[int, str] = {
    0: "协议加载",
    1: "信息截止",
    2: "数据缺口",
    3: "检索记录",
    4: "沉没成本",
    5: "规则",
    6: "Beta",
    7: "归因",
    8: "偏差",
    9: "风险暴露",
    10: "跨市场",
    11: "反方",
    12: "情景分析",
    13: "可证伪",
    14: "决策树",
    15: "置信度",
    16: "自检",
}

# 导入时丢弃的过程章节（AI 工作过程说明，非决策信息）
DROP_SECTIONS = frozenset({0, 1, 2, 3, 16})

# 【持仓诊断模块】整体保留（v2.1 新增核心内容），内部不再按协议章节切分
DIAG_SECTION_NUM = 100
DIAG_SECTION_TITLE = "持仓诊断模块"
_DIAG_HEADER_RE = re.compile(r"^\s*【\s*(持仓诊断模块|持仓诊断)\s*】")
# 诊断模块尾部的"自检项（第 16 节）"等过程自述标题行：从此截断丢弃
_SELFCHECK_RE = re.compile(r"^\s*(自检项|协议自检|自检)[（(]?.*[）)]?\s*$")

# 章节标题行：可带 0-3 个 #，形如 "4. 资本穿透..." / "## 12. 情景分析..."
_SECTION_RE = re.compile(r"^\s*#{0,4}\s*(\d{1,2})\.\s*(.+?)\s*$")


def _classify_line(line: str) -> Optional[Tuple[int, str]]:
    m = _SECTION_RE.match(line)
    if not m:
        return None
    num = int(m.group(1))
    title = m.group(2).strip()
    kw = SECTION_KEYWORDS.get(num, "")
    if not kw:
        return None
    # 标题需命中关键词（或标题本身包含关键词），避免误伤正文以数字开头的行
    if kw in title or any(w in title for w in ("总表", "穿透", "执行清单", "诊断", "质证", "做空", "联动")):
        return num, title
    # 标题很短且与关键词同义（如 "规则审计" 命中了 "规则"）
    if len(title) <= 12 and (kw in title or title.startswith(kw)):
        return num, title
    return None


def split_sections(markdown: str) -> List[Tuple[int, str, str]]:
    """按协议章节切分，返回 [(章节号, 标题, 正文)]。

    - 协议章节 0-16 按编号识别；
    - 【持仓诊断模块】整体作为一个独立块（号 100），内部不再切分；
    - 无法识别章节时返回空列表。
    """
    lines = markdown.splitlines()
    sections: List[Tuple[int, str, str]] = []
    current: Optional[Tuple[int, str, List[str]]] = None
    in_diag = False
    for line in lines:
        if not in_diag and _DIAG_HEADER_RE.match(line):
            # 进入诊断模块：从下一行开始收纳正文，标题行不进入 body
            if current is not None:
                sections.append((current[0], current[1], "\n".join(current[2]).strip()))
            current = (DIAG_SECTION_NUM, DIAG_SECTION_TITLE, [])
            in_diag = True
            continue
        if in_diag:
            # 诊断模块内部：遇到"自检项"等过程自述标题即截断，丢弃其后内容
            if _SELFCHECK_RE.match(line):
                break
            current[2].append(line)
            continue
        cls = _classify_line(line)
        if cls is not None:
            if current is not None:
                sections.append((current[0], current[1], "\n".join(current[2]).strip()))
            current = (cls[0], cls[1], [])
        else:
            if current is not None:
                current[2].append(line)
    if current is not None:
        sections.append((current[0], current[1], "\n".join(current[2]).strip()))
    return sections


def clean_review_markdown(markdown: str) -> str:
    """清洗：保留核心章节（4-15）+ 持仓诊断模块，按原顺序拼接；无章节结构时原样返回。"""
    sections = split_sections(markdown)
    if not sections:
        return markdown
    kept = [(num, title, body) for num, title, body in sections if num not in DROP_SECTIONS]
    parts = []
    seq = 1
    for num, title, body in kept:
        if num == DIAG_SECTION_NUM:
            parts.append(f"## {title}")
        else:
            parts.append(f"## {seq}. {title}")
            seq += 1
        if body:
            parts.append(body)
    cleaned = "\n\n".join(parts).strip()
    return cleaned


def _fmt_num(raw: str) -> Optional[str]:
    s = re.sub(r"[^\d.,]", "", raw)
    return s if s else None


def extract_digest(markdown: str, sections: Optional[List[Tuple[int, str, str]]] = None) -> Dict[str, object]:
    """从全文/章节提取要点速览。宽松匹配，抓不到返回 None 字段。"""
    if sections is None:
        sections = split_sections(markdown)
    text = markdown
    body_by_num = {num: body for num, _, body in sections}

    digest: Dict[str, object] = {}

    # ---- 资产（通常在第 4 章）----
    m = re.search(r"总资产\s*[:：]?\s*¥?\s*([\d,]+(?:\.\d+)?)", text)
    if m:
        digest["total_asset"] = "¥" + _fmt_num(m.group(1))
    m = re.search(r"股票市值\s*[:：]?\s*¥?\s*([\d,]+(?:\.\d+)?)", text)
    if m:
        digest["market_value"] = "¥" + _fmt_num(m.group(1))
    m = re.search(r"现金储备\s*[:：]?\s*¥?\s*([\d,]+(?:\.\d+)?)", text)
    if m:
        digest["cash"] = "¥" + _fmt_num(m.group(1))
    m = re.search(r"(?:实际)?股票仓位\s*[:：]?\s*([\d.]+)\s*%", text)
    if m:
        digest["position_pct"] = m.group(1) + "%"

    # ---- 集中度 / 总仓位（第 9 章，每项一行：评级 + 明细）----
    risk_body = body_by_num.get(9, "")
    if risk_body:
        m = re.search(r"单票集中度[:：]\s*([^\n]+)", risk_body)
        if m:
            digest["single_conc"] = m.group(1).strip()
        m = re.search(r"行业集中度[:：]\s*([^\n]+)", risk_body)
        if m:
            digest["industry_conc"] = m.group(1).strip()
        m = re.search(r"总仓位[:：]\s*([^\n]+)", risk_body)
        if m:
            digest["total_position"] = m.group(1).strip()

    # ---- 违规项（第 5 章）----
    rule_body = body_by_num.get(5, "")
    violations = []
    if rule_body:
        for m in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9（）() ]+?)：违规([（(]?[^）)]*[）)]?)?", rule_body):
            name = m.group(1).strip()
            if name and "合规" not in name:
                violations.append(name)
    if violations:
        digest["violations"] = violations

    # ---- 置信度（第 15 章或全文）----
    conf = {}
    m = re.search(r"(?:A\.\s*)?规则审计置信度[:：]?\s*(\d{1,3})\s*/\s*100", text)
    if m:
        conf["rule"] = int(m.group(1))
    m = re.search(r"(?:B\.\s*)?市场分析置信度[:：]?\s*(\d{1,3})\s*/\s*100", text)
    if m:
        conf["market"] = int(m.group(1))
    if conf:
        digest["confidence"] = conf

    return digest


def digest_to_summary_text(digest: Dict[str, object]) -> str:
    """要点速览 → 一句话摘要（用于列表展示）。"""
    parts = []
    if digest.get("total_asset"):
        parts.append(f"总资产 {digest['total_asset']}")
    if digest.get("position_pct"):
        parts.append(f"股票仓位 {digest['position_pct']}")
    if digest.get("single_conc"):
        parts.append(f"单票集中度 {str(digest['single_conc'])[:40]}")
    conf = digest.get("confidence")
    if isinstance(conf, dict):
        parts.append(f"置信度 {conf.get('rule', '?')}/{conf.get('market', '?')}")
    violations = digest.get("violations")
    if isinstance(violations, list) and violations:
        parts.append(f"违规 {len(violations)} 项")
    return " ｜ ".join(parts) if parts else ""
