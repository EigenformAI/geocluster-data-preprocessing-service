"""Stage F ASSESS: conservative-QP memo + machine-validated citations + findings.json.

The model sees only computed metrics, flags, and section text — never raw OCR.
Every claim it makes is re-validated in Python: citations must resolve in the
manifest and every number in the claim must come from the evidence we supplied
(or a high-confidence verified table). Unsupported claims are marked [UNVERIFIED].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
import sys

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.util import read_json, write_json  # noqa: E402

from . import astate, numguard
from .apaths import APaths
from .llm import parse_cached
from .source import Report

STAGE = "assess"
MODEL = "mistral-large-2512"
PROMPT_VERSION = "v1"

SYSTEM = (
    "You are a conservative Qualified Person (QP) performing technical due diligence on a "
    "mineral-exploration project. Be skeptical and precise. Rules:\n"
    "- Use ONLY the supplied metrics, flags, and section context. Make no quantitative claim "
    "that is not present in the evidence.\n"
    "- Every claim that states a number MUST include at least one citation {table_id, page} "
    "taken from the evidence's provenance.\n"
    "- Quote numbers exactly as given in the evidence; do not compute, round, or infer new ones.\n"
    "- Where evidence is missing, say so and treat it as a gap — do not speculate. Treat any "
    "low/unverifiable input as not-yet-verified.\n"
    "- Be cautious on grade/resource claims; do not editorialize upside.\n"
    "- scope and conclusion are qualitative prose (avoid introducing numbers there)."
)


class Citation(BaseModel):
    table_id: Optional[str] = None
    page: Optional[int] = None


class Claim(BaseModel):
    text: str
    citations: list[Citation] = []


class MemoSection(BaseModel):
    heading: str
    claims: list[Claim] = []


class Memo(BaseModel):
    scope: str
    sections: list[MemoSection] = []
    conclusion: str


def _evidence(report: Report, apaths: APaths) -> dict:
    m = apaths.metrics
    return {
        "report": report.slug,
        "stage": (read_json(m / "_stage.json", default={}) or {}).get("stage"),
        "intercepts": read_json(m / "intercepts.json", default={}) or {},
        "resource": read_json(m / "resource.json", default={}) or {},
        "qaqc": read_json(m / "qaqc.json", default={}) or {},
        "tenure": read_json(m / "tenure.json", default={}) or {},
        "flags": (read_json(apaths.flags_json, default={}) or {}).get("flags", []),
    }


def _allowed_numbers(report: Report, evidence: dict) -> set[str]:
    """Numbers the memo may cite: everything in the evidence pack + high-confidence table cells."""
    allowed = set(numguard.numbers_in([json.dumps(evidence)]))
    for t in report.tables:
        if t.get("confidence") == "high":
            allowed |= numguard.number_set(report.read_table(t))
    return allowed


def _validate(memo: dict, report: Report, allowed: set[str]) -> tuple[dict, list[dict]]:
    table_page = {t["id"]: t["page"] for t in report.tables}
    validated_claims = []

    def check(text: str, citations: list[dict]) -> tuple[bool, str]:
        nums = numguard.numbers_in([text])
        bad_nums = [n for n in nums if n not in allowed]
        good_cites = [c for c in citations
                      if c.get("table_id") in table_page
                      and (c.get("page") in (None, table_page[c["table_id"]]))]
        ok = (not bad_nums) and (not nums or good_cites)
        cite_str = "; ".join(f"{c['table_id']} p.{c.get('page')}" for c in good_cites)
        return ok, cite_str

    for sec in memo.get("sections", []):
        for cl in sec.get("claims", []):
            ok, cite_str = check(cl["text"], cl.get("citations", []))
            validated_claims.append({"section": sec["heading"], "text": cl["text"],
                                     "citations": cl.get("citations", []), "cite_str": cite_str,
                                     "validated": ok})
    return memo, validated_claims


def _render(memo: dict, claims: list[dict], slug: str) -> str:
    lines = [f"# {slug} — Conservative QP assessment", "", "## Scope & basis", "", memo.get("scope", ""), ""]
    by_section: dict[str, list[dict]] = {}
    for c in claims:
        by_section.setdefault(c["section"], []).append(c)
    for heading, cls in by_section.items():
        lines += [f"## {heading}", ""]
        for c in cls:
            tag = f"  _[{c['cite_str']}]_" if c["validated"] and c["cite_str"] else (
                "" if c["validated"] else "  **[UNVERIFIED]**")
            lines.append(f"- {c['text']}{tag}")
        lines.append("")
    lines += ["## Conclusion", "", memo.get("conclusion", ""), ""]
    return "\n".join(lines)


def run(report: Report, apaths: APaths, force: bool = False) -> dict:
    evidence = _evidence(report, apaths)
    parts = [b"assessF", report.slug.encode(), json.dumps(evidence, sort_keys=True).encode(),
             PROMPT_VERSION.encode(), MODEL.encode()]
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            "Write a conservative QP due-diligence memo from this evidence. Sections to cover (skip "
            "any marked not-applicable, stating why): Project stage; Mineral resources; Drilling & "
            "significant intercepts; QA/QC; Tenure; Red flags & data-quality limitations.\n\n"
            "EVIDENCE:\n" + json.dumps(evidence, indent=2)},
    ]
    memo = parse_cached(apaths, parts, MODEL, messages, Memo)

    allowed = _allowed_numbers(report, evidence)
    memo, claims = _validate(memo, report, allowed)
    apaths.memo_md.write_text(_render(memo, claims, report.slug))

    findings = {
        "report_slug": report.slug,
        "project_stage": evidence["stage"],
        "resource": evidence["resource"].get("rows", []),
        "intercepts": evidence["intercepts"].get("top_intercepts", []),
        "qaqc": {"overall": evidence["qaqc"].get("overall"), "crms": evidence["qaqc"].get("crms", [])},
        "tenure": evidence["tenure"],
        "flags": evidence["flags"],
        "claims": claims,
        "unverified_claims": [c for c in claims if not c["validated"]],
    }
    write_json(apaths.findings_json, findings)

    info = {"n_claims": len(claims), "n_unverified": sum(1 for c in claims if not c["validated"])}
    astate.mark_done(apaths, STAGE, info)
    return info
