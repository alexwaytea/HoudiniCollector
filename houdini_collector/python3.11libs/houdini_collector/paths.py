from __future__ import annotations

import glob
import hashlib
import os
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple


IMAGE_EXTENSIONS = {
    ".bmp", ".cin", ".dds", ".dpx", ".exr", ".gif", ".hdr", ".jpeg",
    ".jpg", ".pic", ".png", ".rat", ".tga", ".tif", ".tiff", ".tx",
}
MODEL_EXTENSIONS = {".abc", ".dae", ".fbx", ".gltf", ".glb", ".obj", ".ply", ".rs"}
CACHE_EXTENSIONS = {".bgeo", ".bgeo.sc", ".geo", ".geo.sc", ".vdb", ".nvdb", ".sim"}
USD_EXTENSIONS = {".usd", ".usda", ".usdc", ".usdz"}
HDA_EXTENSIONS = {".hda", ".hdalc", ".hdanc", ".otl"}


def is_renderer_hda(library_path: str, node_type_name: str = "") -> bool:
    """Return True for renderer-owned libraries that must not be collected.

    A user HDA which merely contains Redshift nodes remains collectible; the
    filter targets the vendor library path and Redshift's own type namespace.
    """
    normalized_path = library_path.replace("\\", "/").casefold()
    type_name = node_type_name.casefold().split("/", 1)[-1]
    path_markers = (
        "/redshift/",
        "redshift4houdini",
        "/maxon/redshift",
        "/maxon_applications/redshift",
    )
    redshift_type = (
        type_name.startswith("redshift::")
        or type_name.startswith("redshift_")
        or type_name.startswith("rslight")
        or type_name.startswith("rsproxy")
    )
    return any(marker in normalized_path for marker in path_markers) or redshift_type


def compound_suffix(path: Path) -> str:
    name = path.name.lower()
    for suffix in (".bgeo.sc", ".geo.sc"):
        if name.endswith(suffix):
            return suffix
    return path.suffix.lower()


def safe_name(value: str, fallback: str = "asset") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    value = value.strip("._-")
    return value or fallback


def normalized_key(path: Path) -> str:
    try:
        value = str(path.resolve())
    except OSError:
        value = str(path.absolute())
    return os.path.normcase(value)


def tokenized_glob(path: str) -> Tuple[str, bool]:
    """Convert common Houdini/renderer sequence tokens to a filesystem glob."""
    result = path
    had_token = False
    replacements = (
        (r"<UDIM>", "[0-9][0-9][0-9][0-9]"),
        (r"%\(UDIM\)d", "[0-9][0-9][0-9][0-9]"),
        (r"\$UDIM\b", "[0-9][0-9][0-9][0-9]"),
        (r"\$\{F(\d*)\}", None),
        (r"\$F(\d*)\b", None),
        (r"%(0?)(\d*)d", None),
        (r"#+", None),
    )
    for pattern, replacement in replacements:
        if replacement is not None:
            result, count = re.subn(pattern, replacement, result, flags=re.IGNORECASE)
            had_token = had_token or bool(count)
            continue

        def repl(match):
            nonlocal had_token
            had_token = True
            groups = match.groups()
            width_text = groups[-1] if groups else ""
            if pattern == r"#+":
                width = len(match.group(0))
            else:
                width = int(width_text) if width_text else 1
            return "[0-9]" * max(1, width)

        result = re.sub(pattern, repl, result)
    return result, had_token


def resolve_files(path: str, evaluated_path: str = "") -> Tuple[Path, ...]:
    candidate = os.path.expandvars(os.path.expanduser(path))
    glob_pattern, tokenized = tokenized_glob(candidate)
    # An unresolved environment variable is different from a known sequence token.
    # Only fall back to the evaluated current-frame value after token conversion.
    if "$" in glob_pattern and evaluated_path:
        glob_pattern, evaluated_tokens = tokenized_glob(evaluated_path)
        tokenized = tokenized or evaluated_tokens
    if tokenized:
        return tuple(Path(item) for item in sorted(glob.glob(glob_pattern)) if Path(item).is_file())
    item = Path(candidate)
    if item.is_file():
        return (item,)
    if evaluated_path:
        evaluated = Path(evaluated_path)
        if evaluated.is_file():
            return (evaluated,)
    return ()


def looks_like_output(parm_name: str, raw_path: str, exists: bool) -> bool:
    if exists:
        return False
    name = parm_name.casefold()
    output_tokens = (
        "output", "outfile", "sopoutput", "lopoutput", "picture", "vm_picture",
        "renderfile", "cachefile", "filecache", "diskfile", "export",
    )
    return any(token in name for token in output_tokens)


def detect_megascans_root(path: Path) -> Optional[Path]:
    """Return a conservative Quixel/Megascans asset folder, if recognizable."""
    parent = path.parent
    try:
        names = [item.name.casefold() for item in parent.iterdir() if item.is_file()]
    except OSError:
        return None
    has_json = any(name.endswith(".json") for name in names)
    image_count = sum(Path(name).suffix in IMAGE_EXTENSIONS for name in names)
    ancestry = "/".join(part.casefold() for part in path.parts[-6:])
    labeled = "megascans" in ancestry or "quixel" in ancestry
    if (has_json and image_count >= 2) or (labeled and image_count >= 2):
        return parent
    return None


def unique_destination(relative: Path, source: Path, occupied: dict) -> Path:
    key = str(relative).casefold()
    source_key = normalized_key(source)
    if key not in occupied or occupied[key] == source_key:
        occupied[key] = source_key
        return relative
    digest = hashlib.sha1(source_key.encode("utf-8", "replace")).hexdigest()[:8]
    suffix = compound_suffix(relative)
    stem = relative.name[:-len(suffix)] if suffix else relative.name
    candidate = relative.with_name(f"{stem}_{digest}{suffix}")
    occupied[str(candidate).casefold()] = source_key
    return candidate


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"
