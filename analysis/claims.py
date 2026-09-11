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


def _image_label_grounding_issues(evidence_id: str, evidence: dict, evidence_index: dict[str, dict]) -> list[dict]:
    issues: list[dict] = []
    span = _text_span_for_evidence(evidence)
    input_ids = evidence.get("input_evidence_ids") or []
    if not input_ids:
        issues.append({"evidence_id": evidence_id, "span": span, "source_ref": evidence.get("source_ref"), "reason": "missing_input_evidence"})
    for input_id in input_ids:
        input_evidence = evidence_index.get(input_id)
        if not input_evidence:
            issues.append({"evidence_id": input_id, "span": "", "source_ref": None, "reason": "missing_input_evidence"})
        elif input_evidence.get("source_kind") != "map_component":
            issues.append({"evidence_id": input_id, "span": _text_span_for_evidence(input_evidence), "source_ref": input_evidence.get("source_ref"), "reason": "input_evidence_not_map_component"})
    if evidence.get("verification_status") == "corroborated":
        verification_ids = evidence.get("verification_evidence_ids") or []
        if not verification_ids:
            issues.append({"evidence_id": evidence_id, "span": span, "source_ref": evidence.get("source_ref"), "reason": "missing_verification_evidence"})
        for verification_id in verification_ids:
            if verification_id not in evidence_index:
                issues.append({"evidence_id": verification_id, "span": "", "source_ref": None, "reason": "missing_verification_evidence"})
    return issues


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


def _grounded_text_for_evidence(evidence_id: str, evidence: dict, metadata: dict, evidence_index: dict[str, dict], seen: set[str] | None = None) -> tuple[set[str], list[dict], list[str]]:
    seen = seen or set()
    if evidence_id in seen:
        return set(), [{"evidence_id": evidence_id, "span": _text_span_for_evidence(evidence), "source_ref": evidence.get("source_ref"), "reason": "cyclic_input_evidence"}], []
    seen.add(evidence_id)
    source_kind = evidence.get("source_kind")
    allowed = set() if source_kind in {"knowledge_item", "image_label"} else _numbers_for_evidence(evidence)
    unbacked: list[dict] = []
    grounded_spans: list[str] = []
    span = _text_span_for_evidence(evidence)
    if source_kind == "text_token":
        source_text = _source_text_for_evidence(evidence, metadata)
        if not source_text or not text_backed(span, source_text):
            unbacked.append({"evidence_id": evidence_id, "span": span, "source_ref": evidence.get("source_ref")})
        else:
            grounded_spans.append(span)
    elif source_kind == "image_label":
        issues = _image_label_grounding_issues(evidence_id, evidence, evidence_index)
        unbacked.extend(issues)
        if evidence.get("verification_status") == "corroborated" and not issues:
            allowed.update(_numbers_for_evidence(evidence))
            grounded_spans.append(span)
        elif not issues:
            unbacked.append({
                "evidence_id": evidence_id,
                "span": span,
                "source_ref": evidence.get("source_ref"),
                "reason": "image_label_not_corroborated",
                "verification_status": evidence.get("verification_status"),
            })
    elif source_kind == "knowledge_item":
        input_ids = evidence.get("input_evidence_ids") or []
        if not input_ids:
            unbacked.append({"evidence_id": evidence_id, "span": span, "source_ref": evidence.get("source_ref"), "reason": "missing_input_evidence"})
        input_spans: list[str] = []
        for input_id in input_ids:
            input_evidence = evidence_index.get(input_id)
            if not input_evidence:
                unbacked.append({"evidence_id": input_id, "span": "", "source_ref": None, "reason": "missing_input_evidence"})
                continue
            input_allowed, input_unbacked, input_grounded = _grounded_text_for_evidence(input_id, input_evidence, metadata, evidence_index, seen)
            allowed.update(input_allowed)
            unbacked.extend(input_unbacked)
            input_spans.extend(input_grounded)
        if span and input_spans and not any(text_backed(span, input_span) for input_span in input_spans):
            unbacked.append({"evidence_id": evidence_id, "span": span, "source_ref": evidence.get("source_ref"), "reason": "span_not_backed_by_input_evidence"})
        grounded_spans.extend(input_spans)
    return allowed, unbacked, grounded_spans


def _normalize_citation(citation: dict, evidence_index: dict, elements: dict, evidence_aliases: dict[str, str]) -> dict:
    """Accept model-emitted keyed citation shapes, not just ``{"evidence_id": ...}``.

    Providers have emitted both ``{label: evidence_id}`` and
    ``{evidence_id: description}``. Resolve only keys or values that exactly match the
    supplied evidence index or element inventory; unknown values remain missing.
    """
    if not isinstance(citation, dict):
        return {}
    if citation.get("evidence_id"):
        evidence_id = evidence_aliases.get(citation["evidence_id"], citation["evidence_id"])
        return {"evidence_id": evidence_id}
    if citation.get("element_id"):
        return citation
    for key in citation:
        evidence_id = evidence_aliases.get(key, key)
        if evidence_id in evidence_index:
            return {"evidence_id": evidence_id}
        if key in elements or any(item.get("element_id") == key for item in evidence_index.values()):
            return {"element_id": key}
    for value in citation.values():
        if not isinstance(value, str):
            continue
        evidence_id = evidence_aliases.get(value, value)
        if evidence_id in evidence_index:
            return {"evidence_id": evidence_id}
        if value in elements or any(item.get("element_id") == value for item in evidence_index.values()):
            return {"element_id": value}
    return citation


def verify_claim(text: str, citations: list[dict], metadata: dict) -> dict:
    evidence_index = _evidence_index(metadata)
    elements = metadata.get("elements", {})
    evidence_aliases = {
        assertion["id"]: assertion["evidence_id"]
        for assertion in metadata.get("assertions", [])
        if assertion.get("id") and assertion.get("evidence_id")
    }
    allowed: set[str] = set()
    resolved: list[dict] = []
    missing: list[dict] = []
    unbacked_text: list[dict] = []
    has_evidence_citation = False

    for raw_citation in citations or []:
        citation = _normalize_citation(raw_citation, evidence_index, elements, evidence_aliases)
        if citation.get("evidence_id"):
            evidence = evidence_index.get(citation["evidence_id"])
            if not evidence:
                missing.append(citation)
                continue
            has_evidence_citation = True
            evidence_allowed, evidence_unbacked, _ = _grounded_text_for_evidence(citation["evidence_id"], evidence, metadata, evidence_index)
            allowed.update(evidence_allowed)
            unbacked_text.extend(evidence_unbacked)
            resolved.append({
                "evidence_id": citation["evidence_id"],
                "element_id": evidence.get("element_id"),
                "page": evidence.get("page"),
                "source_kind": evidence.get("source_kind"),
                "source_ref": evidence.get("source_ref"),
                "input_evidence_ids": evidence.get("input_evidence_ids", []),
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
    verified = not missing and not bad_numbers and not unbacked_text and has_evidence_citation
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
