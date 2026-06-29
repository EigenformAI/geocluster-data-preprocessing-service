"""Mistral client factory shared by the OCR and vision stages."""

from __future__ import annotations

import os


class MissingKey(RuntimeError):
    pass


def get_client():
    from dotenv import load_dotenv

    load_dotenv(override=True)
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise MissingKey(
            "MISTRAL_API_KEY is not set. Export it before running the OCR/vision "
            "stages (3 and 6). Deterministic stages 0-2 do not need it."
        )
    try:  # SDK layout differs across mistralai versions
        from mistralai import Mistral
    except ImportError:
        from mistralai.client import Mistral

    return Mistral(api_key=key)
