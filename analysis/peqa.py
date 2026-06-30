"""Prompt-enhanced QA output validation and deterministic MVP memo rendering."""

from __future__ import annotations

from .claims import verify_claim

DEFAULT_QUESTION = "Assess the project evidence and main technical risks."


def _fmt_token(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _raw_token(metadata: dict, evidence_id: str, fallback) -> str:
    evidence = (metadata.get("_evidence_index") or {}).get(evidence_id) or {}
    return str(evidence.get("raw_token") or _fmt_token(fallback))


def _claim_from_top_intercept(metrics: dict, metadata: dict) -> dict | None:
    top = (metrics.get("intercepts") or {}).get("top_intercepts") or []
    if not top:
        return None
    row = top[0]
    evidence_ids = row.get("evidence_ids") or []
    citations = []
    for evidence_id in evidence_ids:
        if evidence_id.endswith(":interval_m") or evidence_id.endswith(":au_gpt"):
            citations.append({"evidence_id": evidence_id})
    interval_evidence = next((item["evidence_id"] for item in citations if item["evidence_id"].endswith(":interval_m")), "")
    grade_evidence = next((item["evidence_id"] for item in citations if item["evidence_id"].endswith(":au_gpt")), "")
    interval_text = _raw_token(metadata, interval_evidence, row.get("interval_m"))
    grade_text = _raw_token(metadata, grade_evidence, row.get("au_gpt"))
    if not citations:
        citations = [{"element_id": (row.get("provenance") or {}).get("element_id") or (row.get("provenance") or {}).get("table_id")}]
    return {
        "text": f"The top captured interval is {interval_text} m at {grade_text} g/t Au.",
        "evidence_origin": "report",
        "citations": citations,
        "confidence": row.get("confidence", "medium"),
    }


def _claim_from_resource(metrics: dict, metadata: dict) -> dict | None:
    rows = (metrics.get("resource") or {}).get("rows") or []
    if not rows:
        return None
    row = rows[0]
    citations = [{"evidence_id": eid} for eid in row.get("evidence_ids", []) if eid.endswith(":tonnage") or eid.endswith(":grade_gpt")]
    tonnage_evidence = next((item["evidence_id"] for item in citations if item["evidence_id"].endswith(":tonnage")), "")
    grade_evidence = next((item["evidence_id"] for item in citations if item["evidence_id"].endswith(":grade_gpt")), "")
    return {
        "text": f"A resource row reports {_raw_token(metadata, tonnage_evidence, row.get('tonnage_t'))} tonnes at {_raw_token(metadata, grade_evidence, row.get('grade_gpt'))} g/t.",
        "evidence_origin": "report",
        "citations": citations,
        "confidence": row.get("confidence", "medium"),
    }


def build_answer(question: str, metadata: dict, metrics: dict, flags: dict, injected_knowledge: dict) -> dict:
    claims = []
    for candidate in (_claim_from_top_intercept(metrics, metadata), _claim_from_resource(metrics, metadata)):
        if candidate:
            claims.append(candidate)
    if not claims:
        claims.append({
            "text": "No quantitative assay or resource claim was normalized from the report tables.",
            "evidence_origin": "report",
            "citations": [],
            "confidence": "medium",
        })
    limitations = [flag["message"] for flag in flags.get("flags", [])]
    return {
        "question": question,
        "answer": "The deterministic foundation captured the report structure, verified tabular numbers against source tokens, and surfaced technical limitations for QP review.",
        "reason": "Metrics and flags were computed in Python; prose claims are constrained to cited evidence.",
        "claims": claims,
        "limitations": limitations,
        "injected_knowledge": injected_knowledge.get("selected", {}),
        "report_context": {"project": metadata.get("report_slug"), "stage": metadata.get("project_stage")},
    }


def validate_answer(answer: dict, metadata: dict) -> tuple[dict, list[dict]]:
    validated_claims = []
    for claim in answer.get("claims", []):
        verdict = verify_claim(claim.get("text", ""), claim.get("citations", []), metadata)
        validated_claims.append({
            **claim,
            "validated": verdict["verified"],
            "bad_numbers": verdict["bad_numbers"],
            "missing_citations": verdict["missing_citations"],
            "resolved_citations": verdict["resolved_citations"],
            "tagged_text": verdict["tagged_text"],
        })
    return answer, validated_claims


def render_memo(answer: dict, validated_claims: list[dict], slug: str) -> str:
    lines = [f"# {slug} - Hierarchical QP assessment", "", "## Scope & basis", "", answer.get("answer", ""), ""]
    lines += ["## Verified Claims", ""]
    for claim in validated_claims:
        text = claim["text"] if claim["validated"] else f"**[UNVERIFIED]** {claim['text']}"
        cites = []
        for citation in claim.get("resolved_citations", []):
            if citation.get("element_id"):
                page = f" p.{citation.get('page')}" if citation.get("page") else ""
                cites.append(f"{citation['element_id']}{page}")
        suffix = f" _[{'; '.join(cites)}]_" if cites and claim["validated"] else ""
        lines.append(f"- {text}{suffix}")
    lines += ["", "## Limitations", ""]
    limitations = answer.get("limitations") or []
    if limitations:
        lines.extend(f"- {item}" for item in limitations)
    else:
        lines.append("- No deterministic flags were raised by the current rule set.")
    lines += ["", "## Reasoning Boundary", "", answer.get("reason", ""), ""]
    return "\n".join(lines)
