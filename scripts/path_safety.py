"""Path-safe names for artifacts keyed by externally supplied material IDs."""

import os
import re
from pathlib import Path


_MATERIAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def validate_material_id(value):
    material_id = str(value or "").strip()
    if _MATERIAL_ID.fullmatch(material_id) is None:
        raise ValueError("materialId must contain only ASCII letters, digits, '_' or '-'")
    return material_id


def artifact_path(root, material_id, suffix=""):
    root_path = Path(root).absolute()
    target = root_path / (validate_material_id(material_id) + suffix)
    try:
        if os.path.commonpath((str(root_path.resolve()), str(target.resolve()))) != str(root_path.resolve()):
            raise ValueError("artifact path escapes its root directory")
    except ValueError:
        raise ValueError("artifact path escapes its root directory") from None
    return target
