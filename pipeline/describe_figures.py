"""Stage 6 DESCRIBE: LLM descriptions of extracted figures (ministral-14b-2512).

One vision call per figure, cached by image-content hash so re-runs never
re-bill. The description is written next to the image as <stem>.description.md.
"""

from __future__ import annotations

import base64
from pathlib import Path

from .mistral_client import get_client
from .util import Paths, mark_stage_done, read_json, sha256_bytes, write_json

STAGE = "describe"
MODEL = "ministral-14b-2512"
PROMPT_VERSION = "v1"

PROMPT = (
    "You are assisting a geologist analysing a mineral-exploration technical report. "
    "Describe this figure factually and concisely (<=120 words). State its type "
    "(geological map, cross-section, drill-plan, chart, table image, or photo), what it "
    "depicts, and any visible labels, legends, scale, coordinates, or units. Do not "
    "speculate beyond what is shown."
)

_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def _figure_files(paths: Paths) -> list[Path]:
    return sorted(p for p in paths.images.glob("fig_*.*") if p.suffix.lower() != ".md")


def _describe(client, img_bytes: bytes, mime: str) -> str:
    b64 = base64.b64encode(img_bytes).decode()
    resp = client.chat.complete(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": f"data:{mime};base64,{b64}"},
                ],
            }
        ],
        timeout_ms=180_000,
    )
    content = resp.choices[0].message.content
    if isinstance(content, list):  # content may be a list of chunks
        content = "".join(getattr(c, "text", "") or "" for c in content)
    return (content or "").strip()


def run(paths: Paths, force: bool = False) -> dict:
    figures = _figure_files(paths)
    if not figures:
        mark_stage_done(paths, STAGE, {"n_figures": 0})
        write_json(paths.root / "figures_index.json", [])
        return {"n_figures": 0}

    client = get_client()
    index = []
    described = 0
    failed = 0
    for fig in figures:
        desc_path = fig.with_suffix(fig.suffix + ".description.md")
        # store as <stem>.description.md
        desc_path = paths.images / f"{fig.stem}.description.md"
        img_bytes = fig.read_bytes()
        mime = _MIME.get(fig.suffix.lstrip(".").lower(), "image/png")
        cache_key = sha256_bytes(img_bytes, PROMPT_VERSION.encode(), MODEL.encode())
        cache_file = paths.cache / f"{cache_key}.json"

        if not force and desc_path.exists():
            text = desc_path.read_text()
        else:
            cached = read_json(cache_file)
            if cached and not force:
                text = cached["description"]
            else:
                try:
                    text = _describe(client, img_bytes, mime)
                except Exception:  # one bad figure shouldn't abort the report
                    failed += 1
                    continue
                write_json(cache_file, {"description": text, "model": MODEL})
                described += 1
            desc_path.write_text(text)

        page = int(fig.stem.split("_")[1].lstrip("p"))
        index.append(
            {
                "id": fig.stem,
                "page": page,
                "image": f"images/{fig.name}",
                "description_md": f"images/{desc_path.name}",
            }
        )

    write_json(paths.root / "figures_index.json", index)
    info = {"n_figures": len(index), "new_calls": described, "failed": failed}
    mark_stage_done(paths, STAGE, info)
    return info
