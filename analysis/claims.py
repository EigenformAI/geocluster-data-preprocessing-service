"""Claim citation and numeric-token verification."""

from __future__ import annotations

from . import numguard
from .source_paths import safe_source_path
from .textguard import text_backed


def _evidence_index(metadata: dict) -> dict[str, dict]:
    return metadata.get("_evidence_index") or metadata.get("evidence_index") or {}


def _numbers_for_evidence(evidence: dict) -> set[str]:
    values = [evidence.get("raw_token")]
    if evidence.get("source_kind") == "computed_metric":
        values.append(evidence.get("value"))
    return set(numguard.numbers_in(values))


def _text_span_for_evidence(evidence: dict) -> str:
    return str(evidence.get("span") or evidence.get("raw_token") or "")


def _source_text_for_evidence(evidence: dict, metadata: dict) -> str | None:
    report_root = metadata.get("_report_root") or metadata.get("report_root")
    source_ref = evidence.get("source_ref")
    if not report_root or not source_ref:
        return None
    path = safe_source_path(report_root, source_ref)
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
    except OSError:
        return None


def verify_claim(text: str, citations: list[dict], metadata: dict) -> dict:
    evidence_index = _evidence_index(metadata)
    allowed: set[str] = set()
    resolved: list[dict] = []
    missing: list[dict] = []
    unbacked_text: list[dict] = []

    for citation in citations or []:
        if citation.get("evidence_id"):
            evidence = evidence_index.get(citation["evidence_id"])
            if not evidence:
                missing.append(citation)
                continue
            allowed.update(_numbers_for_evidence(evidence))
            if evidence.get("source_kind") == "text_token":
                span = _text_span_for_evidence(evidence)
                source_text = _source_text_for_evidence(evidence, metadata)
                if not source_text or not text_backed(span, source_text):
                    unbacked_text.append({"evidence_id": citation["evidence_id"], "span": span, "source_ref": evidence.get("source_ref")})
            resolved.append({
                "evidence_id": citation["evidence_id"],
                "element_id": evidence.get("element_id"),
                "page": evidence.get("page"),
                "source_kind": evidence.get("source_kind"),
                "source_ref": evidence.get("source_ref"),
            })
        elif citation.get("element_id"):
            element_id = citation["element_id"]
            if element_id not in metadata.get("elements", {}) and not any(item.get("element_id") == element_id for item in evidence_index.values()):
                missing.append(citation)
                continue
            resolved.append({"element_id": element_id})
        else:
            missing.append(citation)

    claim_numbers = numguard.numbers_in([text])
    bad_numbers = [number for number in claim_numbers if number not in allowed]
    verified = not missing and not bad_numbers and not unbacked_text and bool(resolved)
    return {
        "verified": verified,
        "text": text,
        "tagged_text": text if verified else f"[UNVERIFIED] {text}",
        "numbers": claim_numbers,
        "bad_numbers": bad_numbers,
        "unbacked_text": unbacked_text,
        "missing_citations": missing,
        "resolved_citations": resolved,
    }
