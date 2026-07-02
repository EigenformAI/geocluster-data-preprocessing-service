"""Dump the RAW opencode response for the answer() call so we can see why
_extract_structured finds no structured JSON. No caching, no retry — one shot.

Usage: python scripts/probe_answer_raw.py <slug>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from analysis.llm import ANSWER_SYSTEM, LLMAnswer, select_answerer
from analysis.paths import AnalysisPaths
from analysis.peqa import DEFAULT_QUESTION


def load_inputs(root: Path):
    paths = AnalysisPaths(root)
    metadata = json.loads(paths.report_metadata_json.read_text())
    try:
        metadata["_evidence_index"] = json.loads(paths.evidence_index_json.read_text())
    except FileNotFoundError:
        metadata.setdefault("_evidence_index", {})
    metrics = {
        name: json.loads((paths.metrics / f"{name}.json").read_text())
        for name in ("intercepts", "resource", "qaqc", "tenure")
    }
    flags = json.loads(paths.flags_json.read_text())
    injected = json.loads((paths.dki / "report.json").read_text())
    return paths, metadata, metrics, flags, injected


def main(argv: list[str]) -> int:
    slug = argv[0] if argv else "gold_x2_mining"
    root = Path("output") / slug
    paths, metadata, metrics, flags, injected = load_inputs(root)
    be = select_answerer(paths.cache / "llm")

    prompt_payload = {
        "question": DEFAULT_QUESTION,
        "metadata": {
            "report_slug": metadata.get("report_slug"),
            "project_stage": metadata.get("project_stage"),
            "location_candidates": metadata.get("location_candidates", [])[:5],
        },
        "metrics": metrics,
        "flags": flags,
        "injected_knowledge": injected,
    }
    user = json.dumps(prompt_payload, indent=2, ensure_ascii=False)
    schema = LLMAnswer.model_json_schema()
    payload = {
        "model": {"providerID": be.provider_id, "modelID": be.model_id},
        "variant": be.heavy_variant,
        "system": ANSWER_SYSTEM,
        "parts": [{"type": "text", "text": user}],
        "format": {"type": "json_schema", "schema": schema, "retryCount": 2},
    }
    print(f"slug={slug}")
    print(f"variant={be.heavy_variant}  user_chars={len(user)}  schema_chars={len(json.dumps(schema))}")
    print(f"prompt_payload sizes: metrics={len(json.dumps(metrics))} flags={len(json.dumps(flags))} injected={len(json.dumps(injected))}")
    sid = be._open_session()
    resp = be._post_json(f"/session/{sid}/message", payload)

    print("\n=== RAW RESPONSE (top-level keys) ===")
    print(list(resp.keys()) if isinstance(resp, dict) else type(resp))
    info = resp.get("info") if isinstance(resp, dict) else None
    if isinstance(info, dict):
        print("info keys:", list(info.keys()))
        print("info.tokens:", info.get("tokens"))
        print("info.cost:", info.get("cost"))
        print("info.error:", info.get("error"))
        print("info.structured present:", isinstance(info.get("structured"), dict))
    parts = resp.get("parts") if isinstance(resp, dict) else None
    if isinstance(parts, list):
        print(f"\nparts ({len(parts)}):")
        for i, p in enumerate(parts):
            if isinstance(p, dict):
                t = p.get("type")
                extra = ""
                if t == "tool":
                    extra = f" tool={p.get('tool')} state.status={(p.get('state') or {}).get('status')}"
                if t == "text":
                    extra = f" text[:200]={json.dumps((p.get('text') or ''))[:200]}"
                if t == "reasoning":
                    extra = f" reasoning_chars={len(p.get('text') or '')}"
                print(f"  [{i}] type={t}{extra}")
    print("\n=== FULL (truncated 4000) ===")
    print(json.dumps(resp, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
