"""Claim citation and numeric-token verification."""

from __future__ import annotations

from . import numguard


def _evidence_index(metadata: dict) -> dict[str, dict]:
    return metadata.get("_evidence_index") or metadata.get("evidence_index") or {}


def _numbers_for_evidence(evidence: dict) -> set[str]:
    values = [evidence.get("raw_token")]
    if evidence.get("source_kind") == "computed_metric":
        values.append(evidence.get("value"))
    return set(numguard.numbers_in(values))


def verify_claim(text: str, citations: list[dict], metadata: dict) -> dict:
    evidence_index = _evidence_index(metadata)
    allowed: set[str] = set()
    resolved: list[dict] = []
    missing: list[dict] = []

    for citation in citations or []:
        if citation.get("evidence_id"):
            evidence = evidence_index.get(citation["evidence_id"])
            if not evidence:
                missing.append(citation)
                continue
            allowed.update(_numbers_for_evidence(evidence))
            resolved.append({"evidence_id": citation["evidence_id"], "element_id": evidence.get("element_id"), "page": evidence.get("page"), "source_kind": evidence.get("source_kind")})
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
    verified = not missing and not bad_numbers and bool(resolved)
    return {
        "verified": verified,
        "text": text,
        "tagged_text": text if verified else f"[UNVERIFIED] {text}",
        "numbers": claim_numbers,
        "bad_numbers": bad_numbers,
        "missing_citations": missing,
        "resolved_citations": resolved,
    }
