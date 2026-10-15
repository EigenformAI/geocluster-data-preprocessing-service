from preprocessing.errors import ServiceError


def bounds(page, bbox) -> dict:
    return {"bbox": [float(bbox[0] - page.bbox[0]), float(bbox[1] - page.bbox[1]),
                     float(bbox[2] - page.bbox[0]), float(bbox[3] - page.bbox[1])],
            "units": "pt", "origin": "top-left", "pageWidth": float(page.width), "pageHeight": float(page.height),
            "rotation": page.rotation, "coordinateSpace": "rotated-media-box",
            "transform": "pdfplumber coordinates translated by media-box top-left; rotation already applied"}


def raise_if_fatal(exc: Exception) -> None:
    if isinstance(exc, (OSError, MemoryError)) or (
        isinstance(exc, ServiceError) and exc.code in {"RESOURCE_LIMIT", "INPUT_LIMIT", "CANCELLED", "JOB_CANCELLED"}
    ):
        raise exc
