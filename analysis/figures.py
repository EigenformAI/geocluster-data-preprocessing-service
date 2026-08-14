"""Figure classification derived from extraction-time description prose."""

from __future__ import annotations

import re

from .schemas import FigureClassification

FIGURE_CLASSES = [
    "geological_map",
    "location_map",
    "claim_map",
    "drill_plan",
    "cross_section",
    "stratigraphic_column",
    "chart",
    "photo",
    "other",
]
MAP_CANDIDATES = set(FIGURE_CLASSES[:6])
GEOREFERENCE_CANDIDATES = {"geological_map", "location_map", "claim_map", "drill_plan"}


def _has(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def classify_figure_description(description: str) -> FigureClassification:
    text = description.lower()
    first_line = text.splitlines()[0] if text.splitlines() else text

    if _has(first_line, "logo", "stylized text", "not a geological technical figure"):
        return FigureClassification("other", "high", "description identifies a non-technical graphic")
    if _has(text, "stratigraphic column", "stratigraphy column"):
        return FigureClassification("stratigraphic_column", "high", "stratigraphic-column keyword")
    if _has(text, "cross-section", "cross section", "section view"):
        return FigureClassification("cross_section", "high", "cross-section keyword")
    if _has(text, "drill plan", "drillhole plan", "drill hole plan"):
        return FigureClassification("drill_plan", "high", "drill-plan keyword")
    if _has(text, "claim map", "claims map", "claim boundary", "claim boundaries", "tenure map"):
        return FigureClassification("claim_map", "high", "claim/tenure map keyword")
    if _has(text, "location map", "regional location", "property location"):
        return FigureClassification("location_map", "high", "location-map keyword")
    if "geological map" in text or ("map" in first_line and _has(text, "legend", "utm", "geology", "lithology")):
        return FigureClassification("geological_map", "high", "geological-map cues")
    if re.search(r"\b(chart|graph|plot|histogram|scatterplot)\b", text):
        return FigureClassification("chart", "high", "chart/graph keyword")
    if re.search(r"\b(photo|photograph|core sample|drill core)\b", text):
        return FigureClassification("photo", "high", "photo/core keyword")
    if "map" in text and _has(text, "scale", "north arrow", "coordinate", "grid"):
        return FigureClassification("location_map", "medium", "generic map with georeference cues")
    return FigureClassification("other", "low", "no fixed taxonomy cue found")


def is_map_candidate(figure_class: str) -> bool:
    return figure_class in MAP_CANDIDATES


def is_georeference_candidate(figure_class: str) -> bool:
    return figure_class in GEOREFERENCE_CANDIDATES
