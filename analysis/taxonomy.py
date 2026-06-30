"""NI 43-101 item and keyword taxonomy for deterministic prose recall."""

from __future__ import annotations

import html
import re
import unicodedata


def _fold(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).casefold().strip()


QUESTION_TAXONOMY: dict[str, dict] = {
    "design_maturity": {
        "items": [25, 26, 15, 16, 22],
        "question_terms": [
            "construction",
            "design",
            "engineering",
            "feasibility",
            "pfs",
            "scoping",
            "mine plan",
            "economic analysis",
            "reserves",
            "remain",
            "recommendation",
        ],
        "predicates": {
            "design_maturity": ["feasibility", "pfs", "scoping study", "engineering design", "trade-off", "aace class", "mine plan", "construction", "economic analysis"],
            "additional_work": ["further work", "additional work", "next phase", "recommend", "recommended", "recommendations", "budget", "outstanding"],
            "modifying_factors": ["modifying factor", "environmental", "hydrogeology", "metallurgical", "permitting", "social and community"],
        },
        "predicate_items": {
            "design_maturity": [15, 16, 22, 25, 26],
            "additional_work": [25, 26],
            "modifying_factors": [15, 16, 22, 25],
        },
    },
    "legal_jurisdictional": {
        "items": [4, 20, 25],
        "question_terms": [
            "legal",
            "jurisdiction",
            "title",
            "tenure",
            "surface rights",
            "royalty",
            "permit",
            "environmental",
            "licence",
            "license",
            "approval",
            "eis",
            "eia",
            "epbc",
            "mining lease",
            "tailings",
            "closure",
            "rehabilitation",
            "indigenous",
            "first nation",
            "native title",
            "ilua",
            "indigenous land use agreement",
            "land council",
            "heritage",
            "aboriginal",
            "rule of law",
            "political risk",
        ],
        "predicates": {
            "title_or_tenure": ["nature of title", "mineral tenure", "tenure", "claim", "claims", "patent", "patents", "active", "good standing", "ownership", "interest"],
            "surface_rights": ["surface rights", "surface occupation", "access", "right to perform work"],
            "royalty_or_encumbrance": ["royalty", "royalties", "encumbrance", "nsr", "net smelter", "buyback"],
            "permit_or_environmental": [
                "permit",
                "permits",
                "permitting",
                "environmental",
                "water licence",
                "water license",
                "licence",
                "license",
                "road permit",
                "exploration plan",
                "early exploration",
                "authorization",
                "authorisation",
                "approval",
                "approvals",
                "EIS",
                "EIA",
                "EPBC",
                "environmental impact statement",
                "environmental impact assessment",
                "tailings",
                "closure",
                "rehabilitation",
                "mining lease",
            ],
            "indigenous_or_consultation": [
                "first nation",
                "indigenous",
                "treaty",
                "traditional territories",
                "aboriginal",
                "consultation",
                "consult",
                "metis",
                "native title",
                "ILUA",
                "Indigenous Land Use Agreement",
                "land council",
                "heritage",
                "cultural heritage",
                "traditional owner",
                "traditional owners",
            ],
            "legal_risk": ["significant factors", "risk", "risks", "legal", "rule of law", "political risk", "access, title", "ability to perform"],
        },
        "predicate_items": {
            "title_or_tenure": [4],
            "surface_rights": [4],
            "royalty_or_encumbrance": [4],
            "permit_or_environmental": [20],
            "indigenous_or_consultation": [4, 20],
            "legal_risk": [25],
        },
    },
    "geological_risk": {
        "items": [14, 12, 25, 11, 7, 8, 9, 10],
        "question_terms": [
            "geological",
            "geology",
            "risk",
            "uncertainty",
            "resource",
            "qa/qc",
            "qaqc",
            "data verification",
            "grade continuity",
            "drill spacing",
        ],
        "predicates": {
            "geological_risk": ["significant risks", "uncertainties", "geological setting", "structural", "fault", "grade continuity", "mineralization"],
            "data_verification": ["data verification", "verify", "verified", "verification", "validate", "validated", "historical data"],
            "qaqc": ["qa/qc", "qaqc", "quality assurance", "quality control", "blank", "blanks", "standards", "duplicates"],
            "resource_confidence": ["measured", "indicated", "inferred", "classification", "mineral resource", "reasonable prospects"],
            "drill_spacing": ["drill spacing", "infill", "twin holes", "twinning", "confirmatory drilling"],
            "recommendation": ["further work", "additional work", "recommend", "recommended", "recommendations", "mitigate", "mitigation"],
        },
        "predicate_items": {
            "geological_risk": [7, 8, 9, 10, 14, 25],
            "data_verification": [12],
            "qaqc": [11, 12],
            "resource_confidence": [14],
            "drill_spacing": [10, 25],
            "recommendation": [25],
        },
    },
}


def item_for_section(section: dict) -> int | None:
    """Extract the NI 43-101 item number from a section record."""

    for value in (section.get("number"), section.get("title"), section.get("slug")):
        text = str(value or "").replace("_", " ").replace("-", " ")
        match = re.search(r"(?<!\d)(\d{1,2})(?:\.\d+)?(?!\d)", text)
        if match:
            item = int(match.group(1))
            if 1 <= item <= 28:
                return item
    return None


def taxonomy_for_question(question: str) -> dict | None:
    """Return the best taxonomy bundle for a question, or None if unsupported."""

    folded = _fold(question)
    for question_class in ("legal_jurisdictional", "design_maturity", "geological_risk"):
        taxonomy = QUESTION_TAXONOMY[question_class]
        if any(term in folded for term in taxonomy["question_terms"]):
            return {
                "question_class": question_class,
                "items": list(taxonomy["items"]),
                "predicates": {key: list(value) for key, value in taxonomy["predicates"].items()},
                "predicate_items": {key: list(value) for key, value in taxonomy.get("predicate_items", {}).items()},
            }
    return None


def matched_predicates(question_class: str, text: str) -> dict[str, list[str]]:
    """Return predicates and keywords matched in a candidate prose span."""

    folded = _fold(text)
    taxonomy = QUESTION_TAXONOMY.get(question_class)
    if not taxonomy:
        return {}
    matches: dict[str, list[str]] = {}
    for predicate, keywords in taxonomy["predicates"].items():
        found = [keyword for keyword in keywords if _fold(keyword) in folded]
        if found:
            matches[predicate] = found
    return matches


def predicate_for_text(question_class: str, text: str) -> str | None:
    matches = matched_predicates(question_class, text)
    return next(iter(matches), None)
