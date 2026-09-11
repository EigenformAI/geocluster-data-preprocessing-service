"""Stage 6 DESCRIBE: LLM descriptions of extracted figures (ministral-14b-2512).

One vision call per figure, cached by image-content hash so re-runs never
re-bill. The description is written next to the image as <stem>.description.md.
"""

from __future__ import annotations

import base64
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .mistral_client import get_client
from .util import Paths, mark_stage_done, read_json, sha256_bytes, write_json

STAGE = "describe"
MISTRAL_MODEL = "ministral-14b-2512"
OPENCODE_MODEL = "openai/gpt-5.6-luna"
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


def _describe_mistral(client, img_bytes: bytes, mime: str) -> str:
    b64 = base64.b64encode(img_bytes).decode()
    resp = client.chat.complete(
        model=MISTRAL_MODEL,
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

    backend = os.environ.get("PIPELINE_VISION_BACKEND", "mistral").strip().lower()
    if backend not in {"mistral", "opencode"}:
        raise ValueError("PIPELINE_VISION_BACKEND must be one of mistral, opencode")
    model = MISTRAL_MODEL if backend == "mistral" else os.environ.get("PIPELINE_VISION_MODEL", OPENCODE_MODEL)
    if backend == "opencode":
        if not os.environ.get("OPENCODE_SERVER_URL"):
            raise ValueError("OPENCODE_SERVER_URL is required when PIPELINE_VISION_BACKEND=opencode")
        if "/" not in model:
            raise ValueError("PIPELINE_VISION_MODEL must use provider/model format")
    client = get_client() if backend == "mistral" else None
    try:
        workers = max(1, int(os.environ.get("PIPELINE_VISION_WORKERS", "1")))
    except ValueError as exc:
        raise ValueError("PIPELINE_VISION_WORKERS must be an integer") from exc
    if backend == "mistral":
        workers = 1
    cache_dir = paths.root / ".cache" / "vision" / backend
    cache_dir.mkdir(parents=True, exist_ok=True)

    def describe_one(fig: Path) -> tuple[dict | None, bool, bool]:
        desc_path = fig.with_suffix(fig.suffix + ".description.md")
        # store as <stem>.description.md
        desc_path = paths.images / f"{fig.stem}.description.md"
        img_bytes = fig.read_bytes()
        mime = _MIME.get(fig.suffix.lstrip(".").lower(), "image/png")
        cache_key = sha256_bytes(img_bytes, PROMPT_VERSION.encode(), backend.encode(), model.encode())
        cache_file = cache_dir / f"{cache_key}.json"

        if not force and desc_path.exists():
            text = desc_path.read_text()
            was_described = False
        else:
            cached = read_json(cache_file)
            if cached and not force:
                text = cached["description"]
                was_described = False
            else:
                try:
                    if backend == "mistral":
                        text = _describe_mistral(client, img_bytes, mime)
                    else:
                        from .opencode_client import describe_image

                        text = describe_image(
                            fig,
                            PROMPT,
                            model=model,
                            variant=os.environ.get("PIPELINE_VISION_VARIANT", "low"),
                        )
                except Exception:  # one bad figure shouldn't abort the report
                    return None, False, True
                write_json(cache_file, {"description": text, "backend": backend, "model": model})
                was_described = True
            desc_path.write_text(text)

        page = int(fig.stem.split("_")[1].lstrip("p"))
        return ({
            "id": fig.stem,
            "page": page,
            "image": f"images/{fig.name}",
            "description_md": f"images/{desc_path.name}",
        }, was_described, False)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(describe_one, figures))
    index = [record for record, _, _ in results if record is not None]
    described = sum(was_described for _, was_described, _ in results)
    failed = sum(was_failed for _, _, was_failed in results)

    write_json(paths.root / "figures_index.json", index)
    info = {"backend": backend, "model": model, "n_figures": len(index), "new_calls": described, "failed": failed}
    mark_stage_done(paths, STAGE, info)
    return info
