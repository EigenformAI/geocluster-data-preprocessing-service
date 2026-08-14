"""Prompt-enhanced QA output validation and deterministic MVP memo rendering."""

from __future__ import annotations

from collections import defaultdict

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
        "origin": "metric",
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
        "origin": "metric",
        "citations": citations,
        "confidence": row.get("confidence", "medium"),
    }


def claims_from_text_recall(injected_knowledge: dict, limit: int = 5) -> list[dict]:
    bundle = ((injected_knowledge.get("selected") or {}).get("text_recall") or {})
    claims: list[dict] = []
    selected_items: list[dict] = []
    seen_predicates: set[str] = set()
    for item in bundle.get("items", []):
        predicate = item.get("predicate")
        if predicate and predicate not in seen_predicates:
            selected_items.append(item)
            seen_predicates.add(predicate)
        if len(selected_items) >= limit:
            break
    if len(selected_items) < limit:
        selected_ids = {id(item) for item in selected_items}
        for item in bundle.get("items", []):
            if id(item) in selected_ids:
                continue
            selected_items.append(item)
            if len(selected_items) >= limit:
                break

    for item in selected_items:
        evidence_id = item.get("evidence_id")
        text = item.get("text") or item.get("span")
        if not evidence_id or not text:
            continue
        claims.append({
            "text": text,
            "evidence_origin": "report",
            "origin": "recall",
            "citations": [{"evidence_id": evidence_id}],
            "confidence": "medium",
            "predicate": item.get("predicate"),
            "matched_keywords": item.get("matched_keywords", []),
            "section_slug": item.get("section_slug"),
            "ni43101_item": item.get("ni43101_item"),
            "page": item.get("page"),
        })
    return claims


def claims_from_assertions(metadata: dict, limit: int | None = None) -> list[dict]:
    claims: list[dict] = []
    seen: set[tuple[str | None, str]] = set()
    for item in metadata.get("assertions", []):
        evidence_id = item.get("evidence_id")
        text = item.get("span") or item.get("text") or item.get("object_span")
        if not evidence_id or not text:
            continue
        key = (evidence_id, text)
        if key in seen:
            continue
        seen.add(key)
        claims.append({
            "text": text,
            "evidence_origin": "report",
            "origin": "recall",
            "citations": [{"evidence_id": evidence_id}],
            "confidence": "medium",
            "assertion_id": item.get("id"),
            "predicate": item.get("predicate"),
            "modality": item.get("modality"),
            "section_slug": item.get("section_slug"),
            "ni43101_item": item.get("ni43101_item"),
            "page": item.get("page"),
            "question_class": item.get("question_class"),
        })
        if limit is not None and len(claims) >= limit:
            break
    return claims


def _append_unique_claims(claims: list[dict], candidates: list[dict]) -> None:
    seen = {claim.get("text") for claim in claims}
    for candidate in candidates:
        if candidate.get("text") in seen:
            continue
        claims.append(candidate)
        seen.add(candidate.get("text"))


def _limitations_from_text_recall(injected_knowledge: dict) -> list[str]:
    bundle = ((injected_knowledge.get("selected") or {}).get("text_recall") or {})
    limitations: list[str] = []
    for coverage in bundle.get("coverage", []):
        items = coverage.get("items_covered") or []
        absent = coverage.get("predicates_absent") or []
        if items and absent:
            limitations.append(
                "No text-recall evidence for "
                + ", ".join(absent)
                + " after scanning NI 43-101 Items "
                + ", ".join(str(item) for item in items)
                + "."
            )
        required_items = coverage.get("predicate_items_required") or {}
        for predicate in coverage.get("predicates_unassessed") or []:
            required = required_items.get(predicate) or coverage.get("items_targeted") or []
            limitations.append(
                "Text recall did not assess "
                + str(predicate)
                + " because NI 43-101 Items "
                + ", ".join(str(item) for item in required)
                + " were not scanned."
            )
    return limitations


def _limitations_from_assertions(injected_knowledge: dict) -> list[str]:
    bundle = ((injected_knowledge.get("selected") or {}).get("assertion_graph") or {})
    limitations: list[str] = []
    coverage = (bundle.get("coverage") or [{}])[0]
    items = coverage.get("items_covered") or []
    item_text = ", ".join(str(item) for item in items) if items else "the harvested sections"
    for gap in bundle.get("gaps") or []:
        predicate = gap.get("predicate")
        status = gap.get("status")
        question_class = gap.get("question_class")
        prefix = f"{question_class} assertion graph" if question_class else "Assertion graph"
        if status == "gap":
            limitations.append(f"{prefix} has no {predicate} assertion after scanning NI 43-101 Items {item_text}.")
        elif status == "open":
            limitations.append(f"{prefix} leaves {predicate} open; supporting assertions do not retire it.")
    return limitations


def _open_gaps_from_injected(injected_knowledge: dict) -> list[dict]:
    gaps: list[dict] = []
    for expert_name, bundle in (injected_knowledge.get("selected") or {}).items():
        if not isinstance(bundle, dict):
            continue
        for gap in bundle.get("gaps") or []:
            if gap.get("status") in {"gap", "open"}:
                gaps.append({"expert": expert_name, **gap})
    return gaps


def _confidence_from_pack_inputs(claims_available: bool, coverage: list[dict], open_gaps: list[dict]) -> dict:
    level = "medium" if claims_available else "low"
    if open_gaps:
        level = "low"
    return {
        "level": level,
        "basis": "Grounded claims require evidence verification; coverage and open gaps constrain synthesis confidence.",
        "n_coverage_records": len(coverage),
        "n_open_gaps": len(open_gaps),
    }


def build_answer_pack(question: str, metadata: dict, metrics: dict, flags: dict, injected_knowledge: dict) -> dict:
    selected = injected_knowledge.get("selected") or {}
    question_class = (injected_knowledge.get("routing") or {}).get("question_class")
    if not question_class:
        for bundle in selected.values():
            if isinstance(bundle, dict) and bundle.get("question_class"):
                question_class = bundle["question_class"]
                break
    coverage = []
    for bundle in selected.values():
        if isinstance(bundle, dict):
            coverage.extend(bundle.get("coverage") or [])
    open_gaps = _open_gaps_from_injected(injected_knowledge)
    figure_crops = []
    for element_id, element in (metadata.get("elements") or {}).items():
        map_meta = element.get("map_meta") or {}
        for component in map_meta.get("components") or []:
            figure_crops.append({"element_id": element_id, **component})
    grounded_snippets = [_digest_assertion_for_pack(item) for item in metadata.get("assertions") or []]
    source_snippets = {"grounded_digest": grounded_snippets}
    return {
        "schema_version": "peqa_answer_pack/v1",
        "question": question,
        "question_class": question_class,
        "report_context": {"project": metadata.get("report_slug"), "stage": metadata.get("project_stage")},
        "selected_assertions": list(metadata.get("assertions") or [])[:50],
        "source_snippets": source_snippets,
        "grounded_digest": _grounded_digest(metadata),
        "tables_metrics": {
            "top_intercepts": (metrics.get("intercepts") or {}).get("top_intercepts", [])[:5],
            "resource_rows": (metrics.get("resource") or {}).get("rows", [])[:5],
        },
        "figure_crops": figure_crops[:10],
        "dki_bundles": selected,
        "tool_pool": injected_knowledge.get("tool_pool", []),
        "coverage": coverage,
        "open_gaps": open_gaps,
        "confidence": _confidence_from_pack_inputs(bool(metadata.get("assertions") or source_snippets), coverage, open_gaps),
        "flags": flags.get("flags", []),
        "required_output_schema": {
            "answer": "string",
            "reason": "string",
            "claims": [{"text": "string", "citations": [{"evidence_id": "string"}]}],
            "open_gaps": [{"predicate": "string", "status": "string", "supporting_assertion_ids": ["string"]}],
            "confidence": {"level": "high|medium|low", "basis": "string"},
            "coverage": [{"items_covered": ["integer"], "predicates_assessed": ["string"]}],
            "limitations": ["string"],
        },
    }


def build_answer(question: str, metadata: dict, metrics: dict, flags: dict, injected_knowledge: dict) -> dict:
    claims = []
    for candidate in (_claim_from_top_intercept(metrics, metadata), _claim_from_resource(metrics, metadata)):
        if candidate:
            claims.append(candidate)
    _append_unique_claims(claims, claims_from_assertions(metadata))
    if not claims:
        claims.append({
            "text": "No quantitative assay or resource claim was normalized from the report tables.",
            "evidence_origin": "report",
            "origin": "metric",
            "citations": [],
            "confidence": "medium",
        })
    limitations = [flag["message"] for flag in flags.get("flags", [])]
    limitations.extend(_limitations_from_assertions(injected_knowledge))
    answer_pack = build_answer_pack(question, metadata, metrics, flags, injected_knowledge)
    return {
        "question": question,
        "answer": "The deterministic foundation captured the report structure, verified tabular numbers against source tokens, and surfaced technical limitations for QP review.",
        "reason": "Metrics and flags were computed in Python; prose claims are constrained to cited evidence.",
        "claims": claims,
        "open_gaps": answer_pack.get("open_gaps", []),
        "confidence": answer_pack.get("confidence"),
        "coverage": answer_pack.get("coverage", []),
        "limitations": limitations,
        "flags": flags.get("flags", []),
        "answerer_used": False,
        "injected_knowledge": injected_knowledge.get("selected", {}),
        "report_context": {"project": metadata.get("report_slug"), "stage": metadata.get("project_stage")},
        "answer_pack": answer_pack,
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
            "unbacked_text": verdict["unbacked_text"],
            "resolved_citations": verdict["resolved_citations"],
            "tagged_text": verdict["tagged_text"],
        })
    return answer, validated_claims


def _render_claim_line(claim: dict) -> str:
    text = str(claim.get("text", ""))
    # A verbatim recall span may itself begin with a source bullet ("- "); strip one leading
    # marker so it does not double-render under the memo's own "- " bullet.
    display = text[2:].lstrip() if text[:2] in ("- ", "* ") else text
    body = display if claim.get("validated") else f"**[UNVERIFIED]** {display}"
    cites = []
    for citation in claim.get("resolved_citations", []):
        if citation.get("element_id"):
            page = f" p.{citation.get('page')}" if citation.get("page") else ""
            cites.append(f"{citation['element_id']}{page}")
    suffix = f" _[{'; '.join(cites)}]_" if cites and claim.get("validated") else ""
    return f"- {body}{suffix}"


def _digest_assertion_for_pack(item: dict) -> dict:
    out = {
        "id": item.get("id"),
        "evidence_id": item.get("evidence_id"),
        "predicate": item.get("predicate"),
        "modality": item.get("modality"),
        "ni43101_item": item.get("ni43101_item"),
        "question_class": item.get("question_class"),
        "page": item.get("page"),
        "section_slug": item.get("section_slug"),
        "span": item.get("span") or item.get("object_span"),
    }
    return {key: value for key, value in out.items() if value is not None}


def _grounded_digest(metadata: dict) -> dict:
    by_item: dict[str, int] = defaultdict(int)
    by_class: dict[str, int] = defaultdict(int)
    assertions = []
    for item in metadata.get("assertions") or []:
        digest = _digest_assertion_for_pack(item)
        if not digest.get("evidence_id"):
            continue
        assertions.append(digest)
        if digest.get("ni43101_item") is not None:
            by_item[str(digest["ni43101_item"])] += 1
        if digest.get("question_class"):
            by_class[str(digest["question_class"])] += 1
    return {
        "assertions": assertions,
        "assertions_by_item": dict(sorted(by_item.items(), key=lambda pair: int(pair[0]))),
        "assertions_by_class": dict(sorted(by_class.items())),
    }


def _group_recall_by_item(claims: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for claim in claims:
        item = claim.get("ni43101_item")
        key = str(item) if item is not None else "Unassigned"
        grouped[key].append(claim)
    return dict(sorted(grouped.items(), key=lambda pair: (999 if pair[0] == "Unassigned" else int(pair[0]), pair[0])))


def render_memo(answer: dict, validated_claims: list[dict], slug: str) -> str:
    # The load-bearing artifact is the grounded digest. QP prose, when present, is
    # advisory and rendered after the digest so downstream consumers never depend on it.
    recalled = [claim for claim in validated_claims if claim.get("origin") == "recall"]
    metric_claims = [claim for claim in validated_claims if claim.get("origin") == "metric"]
    qp_claims = [claim for claim in validated_claims if claim.get("origin") not in {"recall", "metric"}]
    lines = [f"# {slug} - Grounded evidence digest", "", "## Grounded Evidence Digest", ""]
    if recalled:
        for item, claims in _group_recall_by_item(recalled).items():
            heading = f"### NI 43-101 Item {item}" if item != "Unassigned" else "### Unassigned NI 43-101 Item"
            lines += [heading, ""]
            lines.extend(_render_claim_line(claim) for claim in claims)
            lines.append("")
    else:
        lines += ["- No grounded prose assertions were harvested for the covered taxonomy items.", ""]

    lines += ["## Verified Metric Claims", ""]
    if metric_claims:
        lines.extend(_render_claim_line(claim) for claim in metric_claims)
    else:
        lines.append("- No verified metric claims were available.")

    lines += ["", "## Deterministic Flags", ""]
    flags = answer.get("flags") or []
    if flags:
        lines.extend(f"- {flag.get('message') or flag.get('id') or flag}" for flag in flags)
    else:
        lines.append("- No deterministic flags were raised by the current rule set.")

    if answer.get("answerer_used") and answer.get("answer"):
        lines += ["", "## QP Assessment", "", answer.get("answer", ""), ""]
        if qp_claims:
            lines += ["### Validated QP Claims", ""]
            lines.extend(_render_claim_line(claim) for claim in qp_claims)

    lines += ["", "## Limitations", ""]
    limitations = answer.get("limitations") or []
    if limitations:
        lines.extend(f"- {item}" for item in limitations)
    else:
        lines.append("- No deterministic limitations were reported.")
    lines += ["", "## Reasoning Boundary", "", answer.get("reason", ""), ""]
    return "\n".join(lines)
