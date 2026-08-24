from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
import time
import fnmatch
from collections import deque
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import hou

from .model import CollectOptions, CollectPlan, FileReference, HDAReference, RelinkCandidate
from .paths import (
    CACHE_EXTENSIONS,
    HDA_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MODEL_EXTENSIONS,
    USD_EXTENSIONS,
    compound_suffix,
    detect_megascans_root,
    is_renderer_hda,
    normalized_key,
    looks_like_output,
    resolve_files,
    safe_name,
    tokenized_glob,
    unique_destination,
)


class CollectorError(RuntimeError):
    pass


def _is_network(node) -> bool:
    try:
        return bool(node.isNetwork())
    except Exception:
        return False


def _node_type_name(node) -> str:
    try:
        return node.type().nameWithCategory().casefold()
    except Exception:
        try:
            return node.type().name().casefold()
        except Exception:
            return ""


def _is_light_or_camera(node) -> bool:
    name = _node_type_name(node)
    return any(token in name for token in ("light", "camera", "cam", "rslight", "rslightdome"))


def _is_material_node(node) -> bool:
    path = node.path()
    if path.startswith("/mat/") or path.startswith("/shop/"):
        return True
    name = _node_type_name(node)
    return any(token in name for token in ("materialbuilder", "material_builder", "redshift_vopnet"))


def _add_node_and_contents(node, keep: Set[str]) -> None:
    keep.add(node.path())
    if _is_network(node):
        try:
            keep.update(child.path() for child in node.allSubChildren())
        except Exception:
            pass


def _add_parents(node, keep: Set[str]) -> None:
    parent = node.parent()
    while parent is not None and parent.path() != "/":
        keep.add(parent.path())
        parent = parent.parent()


def _material_paths_from_geometry(node) -> Set[str]:
    result: Set[str] = set()
    if not isinstance(node, hou.SopNode):
        return result
    try:
        geometry = node.geometry()
        attribute = geometry.findPrimAttrib("shop_materialpath")
        if attribute is None:
            return result
        for value in attribute.strings():
            if value and value.startswith("/"):
                result.add(value)
    except Exception:
        pass
    return result


def compute_scope(selected_nodes: Sequence, options: CollectOptions) -> Tuple[Set[str], List[str]]:
    """Compute a conservative node whitelist rooted at the current selection."""
    if not selected_nodes:
        raise CollectorError("Select at least one node before scanning.")

    keep: Set[str] = set()
    warnings: List[str] = []
    queue = deque((node, True) for node in selected_nodes)
    expanded: Set[str] = set()

    while queue:
        node, include_contents = queue.popleft()
        if node is None or node.path() in expanded:
            continue
        expanded.add(node.path())
        if include_contents:
            _add_node_and_contents(node, keep)
        else:
            keep.add(node.path())
        _add_parents(node, keep)

        if options.include_upstream:
            try:
                for upstream in node.inputs():
                    if upstream is not None:
                        queue.append((upstream, True))
            except Exception:
                pass

        if options.include_references:
            try:
                for referenced in node.references():
                    if referenced is not None:
                        queue.append((referenced, True))
            except Exception as exc:
                warnings.append(f"Could not inspect node references on {node.path()}: {exc}")

        if options.include_materials:
            for material_path in _material_paths_from_geometry(node):
                material = hou.node(material_path)
                if material is not None:
                    queue.append((material, True))

    if options.include_lights_cameras:
        obj = hou.node("/obj")
        if obj is not None:
            for child in obj.children():
                if _is_light_or_camera(child):
                    _add_node_and_contents(child, keep)
                    _add_parents(child, keep)

    # A node inside a locked asset cannot be pruned safely. Preserve that asset.
    for path in tuple(keep):
        node = hou.node(path)
        if node is None:
            continue
        try:
            locked_parent = node.parent()
            while locked_parent is not None and locked_parent.path() != "/":
                if locked_parent.type().definition() and locked_parent.matchesCurrentDefinition():
                    _add_node_and_contents(locked_parent, keep)
                    _add_parents(locked_parent, keep)
                    break
                locked_parent = locked_parent.parent()
        except Exception:
            pass

    return keep, warnings


def compute_whole_scene_scope() -> Tuple[Set[str], List[str]]:
    keep: Set[str] = set()
    for root_path in ("/obj", "/mat", "/shop", "/stage", "/out"):
        root = hou.node(root_path)
        if root is None:
            continue
        for child in root.children():
            _add_node_and_contents(child, keep)
            _add_parents(child, keep)
    return keep, []


def _is_render_node(node) -> bool:
    try:
        if node.type().category() == hou.ropNodeTypeCategory():
            return True
    except Exception:
        pass
    path = node.path().casefold()
    type_name = _node_type_name(node)
    return path.startswith("/out/") or any(
        token in type_name for token in ("render", "rop", "usd_rop", "karma")
    )


def _raw_and_evaluated(parm, reported_path: str) -> Tuple[str, str, Optional[str]]:
    target = parm
    try:
        target = parm.getReferencedParm()
    except Exception:
        pass
    try:
        raw = target.unexpandedString()
    except Exception:
        raw = reported_path
    try:
        evaluated = target.evalAsString()
    except Exception:
        evaluated = reported_path
    return raw or reported_path, evaluated or reported_path, target.path() if target else None


def _material_asset_name(node) -> str:
    current = node
    candidate = None
    while current is not None and current.path() not in ("/mat", "/shop", "/"):
        candidate = current
        parent = current.parent()
        if parent is not None and parent.path() in ("/mat", "/shop"):
            break
        current = parent
    return safe_name(candidate.name() if candidate is not None else node.name(), "material")


def _classify(node, source: Path, options: CollectOptions) -> Tuple[str, str, Optional[Path]]:
    suffix = compound_suffix(source)
    type_name = _node_type_name(node)
    node_path = node.path().casefold()
    if suffix in MODEL_EXTENSIONS:
        if suffix == ".rs" and not options.include_redshift_proxies:
            return "ignored", safe_name(source.stem), None
        package_root = detect_megascans_root(source) if options.preserve_megascans_packages else None
        return ("megascans" if package_root else "model", safe_name((package_root or source).stem), package_root)
    if suffix in CACHE_EXTENSIONS:
        return "cache", safe_name(node.name(), source.stem), None
    if suffix in USD_EXTENSIONS:
        return "usd", safe_name(source.stem), None
    if suffix in IMAGE_EXTENSIONS:
        if _is_light_or_camera(node) or any(token in type_name for token in ("dome", "environment")):
            return "hdri", safe_name(source.stem), None
        if _is_material_node(node) or node_path.startswith(("/mat/", "/shop/")):
            return "material", _material_asset_name(node), None
        return "texture", safe_name(source.parent.name, "textures"), None
    if suffix in HDA_EXTENSIONS:
        return "hda", safe_name(source.stem), None
    return "misc", safe_name(source.parent.name, "misc"), None


def _category_dir(category: str, asset_name: str) -> Path:
    if category in ("model", "megascans"):
        return Path("geo") / "models" / safe_name(asset_name)
    if category == "cache":
        return Path("geo") / "cache" / safe_name(asset_name)
    if category == "material":
        return Path("tex") / "mats" / safe_name(asset_name)
    if category == "asset_texture":
        return Path("geo") / "models" / safe_name(asset_name) / "textures"
    if category == "hdri":
        return Path("tex") / "hdri"
    if category == "texture":
        return Path("tex") / "misc" / safe_name(asset_name)
    if category == "usd":
        return Path("usd") / safe_name(asset_name)
    if category == "hda":
        return Path("hda")
    return Path("misc") / safe_name(asset_name)


def _obj_branch(node_path: Optional[str]) -> str:
    """Return the owning /obj child for a node path, if it has one."""
    if not node_path or not node_path.startswith("/obj/"):
        return ""
    parts = node_path.split("/")
    return "/".join(parts[:3]) if len(parts) > 2 else ""


def _path_is_below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except (ValueError, OSError):
        return False


def _assign_asset_texture_owners(references: List[FileReference]) -> None:
    """Place unambiguous model-owned textures beside their source model.

    This is intentionally conservative. A texture is moved only when one model
    is a strictly better owner than every other model. Shared or ambiguous
    materials retain the normal tex/mats or tex/misc destination.
    """
    models = [
        ref for ref in references
        if ref.category == "model" and ref.source_files and not ref.is_output
    ]
    if not models:
        return

    for texture in references:
        if texture.category not in ("material", "texture") or not texture.source_files:
            continue
        texture_source = texture.source_files[0]
        texture_branch = _obj_branch(texture.node_path)
        scores = []
        for model in models:
            model_source = model.source_files[0]
            model_name = safe_name(model.asset_name).casefold()
            score = 0

            # Strongest signal: textures live inside the model package folder.
            if _path_is_below(texture_source, model_source.parent):
                score += 100

            # A material network inside the same OBJ asset is also strong.
            model_branch = _obj_branch(model.node_path)
            if texture_branch and texture_branch == model_branch:
                score += 70

            # Names are supporting evidence, never sufficient on their own.
            if safe_name(texture.asset_name).casefold() == model_name:
                score += 30
            if any(safe_name(part).casefold() == model_name for part in texture_source.parts[:-1]):
                score += 20
            scores.append((score, model))

        scores.sort(key=lambda pair: pair[0], reverse=True)
        best_score, owner = scores[0]
        next_score = scores[1][0] if len(scores) > 1 else -1
        if best_score >= 70 and best_score > next_score:
            texture.category = "asset_texture"
            texture.asset_name = owner.asset_name
            texture.used_by = tuple(dict.fromkeys(
                ((owner.node_path,) if owner.node_path else ()) + texture.used_by
            ))


def _iter_node_file_references(node):
    try:
        return node.fileReferences(recurse=False, project_dir_variable="HIP", include_all_refs=True)
    except TypeError:
        try:
            return node.fileReferences(recurse=False, include_all_refs=True)
        except Exception:
            return ()
    except Exception:
        return ()


def scan_file_references(keep: Set[str], options: CollectOptions) -> Tuple[List[FileReference], List[str]]:
    references: List[FileReference] = []
    warnings: List[str] = []
    seen_parms: Set[str] = set()

    for node_path in sorted(keep):
        node = hou.node(node_path)
        if node is None:
            continue
        for parm, reported_path in _iter_node_file_references(node):
            if parm is None or not reported_path:
                continue
            raw, evaluated, parm_path = _raw_and_evaluated(parm, reported_path)
            if not parm_path or parm_path in seen_parms:
                continue
            seen_parms.add(parm_path)
            lowered = raw.strip().casefold()
            if lowered.startswith(("op:", "opdef:", "http:", "https:", "data:")):
                continue
            if options.collect_all_sequence_files:
                files = resolve_files(raw, evaluated)
            else:
                evaluated_file = Path(evaluated)
                files = (evaluated_file,) if evaluated_file.is_file() else ()
            try:
                is_driver = node.type().category() == hou.ropNodeTypeCategory()
            except Exception:
                is_driver = False
            is_output = is_driver and looks_like_output(parm.name(), raw, bool(files))
            if is_driver and any(token in parm.name().casefold() for token in ("output", "picture", "render", "diskfile")):
                is_output = True
            source_for_class = files[0] if files else Path(evaluated)
            category, asset_name, package_root = _classify(node, source_for_class, options)
            if category == "ignored":
                continue
            ref = FileReference(
                parm_path=parm_path,
                node_path=node.path(),
                raw_path=raw,
                evaluated_path=evaluated,
                category=category,
                asset_name=asset_name,
                source_files=files,
                exists=bool(files),
                package_root=package_root,
                is_output=is_output,
                enabled=not is_output,
                used_by=(node.path(),),
            )
            if is_output:
                ref.warning = "Generated output (not collected)"
            elif not files:
                ref.warning = "Missing file or unresolved sequence"
                warnings.append(f"Missing: {raw} ({parm_path})")
            references.append(ref)

    _assign_asset_texture_owners(references)
    for ref in references:
        ref.auto_asset_name = ref.asset_name
    _assign_destinations(references, options.output_root)
    return references, warnings


def _assign_destinations(references: List[FileReference], output_root: Path) -> None:
    occupied: Dict[str, str] = {}
    package_roots = sorted(
        {ref.package_root for ref in references if ref.package_root is not None},
        key=lambda item: len(str(item)),
        reverse=True,
    )

    for ref in references:
        # A texture belonging to a detected Megascans package stays beside its FBX.
        containing_root = ref.package_root
        if containing_root is None and ref.source_files:
            source = ref.source_files[0]
            for root in package_roots:
                try:
                    source.relative_to(root)
                    containing_root = root
                    ref.category = "megascans"
                    ref.asset_name = safe_name(root.name)
                    break
                except ValueError:
                    continue

        base = _category_dir(ref.category, ref.asset_name)
        source_files = list(ref.source_files)

        # Keep sequence tokens valid when two sources would otherwise land on
        # the same names. Move the entire later reference into one stable
        # subfolder instead of renaming individual frames independently.
        collides = False
        for source in source_files:
            if containing_root is not None:
                try:
                    probe = base / source.relative_to(containing_root)
                except ValueError:
                    probe = base / source.name
            else:
                probe = base / source.name
            previous = occupied.get(str(probe).casefold())
            if previous is not None and previous != normalized_key(source):
                collides = True
                break
        if collides:
            signature_source = normalized_key(source_files[0].parent) if source_files else ref.raw_path
            signature = hashlib.sha1(signature_source.encode("utf-8", "replace")).hexdigest()[:8]
            base = base / f"source_{signature}"

        destinations: List[Path] = []
        for source in source_files:
            if containing_root is not None:
                try:
                    relative = base / source.relative_to(containing_root)
                except ValueError:
                    relative = base / source.name
            else:
                relative = base / source.name
            destinations.append(output_root / unique_destination(relative, source, occupied))
        ref.destination_files = tuple(destinations)

        raw_name = Path(ref.raw_path.replace("\\", "/")).name
        if containing_root is not None:
            eval_source = Path(ref.evaluated_path)
            try:
                rel_pattern = base / eval_source.relative_to(containing_root)
            except ValueError:
                rel_pattern = base / raw_name
        else:
            rel_pattern = base / raw_name
        ref.destination_pattern = "$HIP/" + rel_pattern.as_posix()


def assign_destinations(references: List[FileReference], output_root: Path) -> None:
    """Rebuild destinations after manual Asset group overrides."""
    _assign_destinations(references, output_root)


def scan_hdas(keep: Set[str], output_root: Path) -> List[HDAReference]:
    hfs = Path(hou.expandString("$HFS"))
    grouped: Dict[str, HDAReference] = {}
    for node_path in sorted(keep):
        node = hou.node(node_path)
        if node is None:
            continue
        try:
            definition = node.type().definition()
            if definition is None:
                continue
            library = definition.libraryFilePath()
            node_type_name = node.type().nameWithCategory()
        except Exception:
            continue
        if not library or library == "Embedded":
            continue
        if is_renderer_hda(library, node_type_name):
            continue
        path = Path(hou.expandString(library))
        try:
            path.resolve().relative_to(hfs.resolve())
            continue
        except (ValueError, OSError):
            pass
        if not path.is_file():
            continue
        key = normalized_key(path)
        item = grouped.setdefault(key, HDAReference(library_path=path))
        item.node_types.add(node_type_name)
        item.destination = output_root / "hda" / path.name
    return list(grouped.values())


def next_output_root(source_hip: Path) -> Path:
    stem = safe_name(source_hip.stem)
    parent = source_hip.parent
    for version in range(1, 1000):
        candidate = parent / f"{stem}_collect_v{version:03d}"
        if not candidate.exists():
            return candidate
    raise CollectorError("Could not find a free collect version folder.")


def build_plan(options: CollectOptions, selected_nodes: Optional[Sequence] = None) -> CollectPlan:
    source_text = hou.hipFile.path()
    if not source_text or source_text.casefold().endswith("untitled.hip"):
        raise CollectorError("Save the HIP file before collecting.")
    source_hip = Path(source_text)
    selected = tuple(selected_nodes) if selected_nodes is not None else tuple(hou.selectedNodes())
    if options.collect_mode == "whole_scene":
        keep, scope_warnings = compute_whole_scene_scope()
    else:
        if options.collect_mode == "selected_rops":
            if not selected:
                raise CollectorError("Select at least one render/ROP node before scanning.")
            invalid = [node.path() for node in selected if not _is_render_node(node)]
            if invalid:
                raise CollectorError("Selected ROPs mode contains non-render nodes: " + ", ".join(invalid))
        keep, scope_warnings = compute_scope(selected, options)
    references, ref_warnings = scan_file_references(keep, options)
    hdas = scan_hdas(keep, options.output_root) if options.include_hdas else []
    collected_hip = options.output_root / f"{safe_name(source_hip.stem)}_collect{source_hip.suffix}"
    return CollectPlan(
        source_hip=source_hip,
        output_root=options.output_root,
        collected_hip=collected_hip,
        selected_nodes=[node.path() for node in selected],
        kept_nodes=keep,
        references=references,
        hdas=hdas,
        collect_mode=options.collect_mode,
        incremental_update=options.incremental_update,
        warnings=scope_warnings + ref_warnings,
    )


def find_relink_candidates(
    reference: FileReference,
    search_roots: Sequence[Path],
    max_results: int = 20,
) -> Tuple[RelinkCandidate, ...]:
    """Find exact-name or sequence/UDIM candidates below search roots."""
    raw_name = Path(reference.raw_path.replace("\\", "/")).name
    if not raw_name:
        return ()
    filename_pattern, tokenized = tokenized_glob(raw_name)
    matches: List[Path] = []
    for root in search_roots:
        root = Path(root)
        if not root.is_dir():
            continue
        try:
            for folder, _dirs, names in os.walk(str(root)):
                for name in names:
                    matches_name = (
                        fnmatch.fnmatchcase(name.casefold(), filename_pattern.casefold())
                        if tokenized else name.casefold() == raw_name.casefold()
                    )
                    if matches_name:
                        matches.append(Path(folder) / name)
        except OSError:
            continue

    grouped: Dict[str, List[Path]] = {}
    for path in matches:
        key = normalized_key(path.parent) if tokenized else normalized_key(path)
        grouped.setdefault(key, []).append(path)

    original_parent = safe_name(Path(reference.evaluated_path).parent.name, "").casefold()
    candidates: List[RelinkCandidate] = []
    for files in grouped.values():
        files.sort(key=lambda item: item.name.casefold())
        path_pattern = str(files[0].parent / raw_name) if tokenized else str(files[0])
        score = len(files) if tokenized else 1
        if safe_name(files[0].parent.name, "").casefold() == original_parent:
            score += 100
        candidates.append(RelinkCandidate(path_pattern, tuple(files), score))
    candidates.sort(key=lambda item: (-item.score, item.path_pattern.casefold()))
    return tuple(candidates[:max_results])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_file(source: Path, destination: Path) -> bool:
    if not destination.is_file():
        return False
    try:
        source_stat = source.stat()
        destination_stat = destination.stat()
        if source_stat.st_size != destination_stat.st_size:
            return False
        if source_stat.st_mtime_ns == destination_stat.st_mtime_ns:
            return True
        return _sha256(source) == _sha256(destination)
    except OSError:
        return False


def _copy_file(source: Path, destination: Path, incremental: bool) -> bool:
    """Copy one file and return False when an incremental copy can be reused."""
    if incremental and _same_file(source, destination):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if incremental:
        partial = destination.with_name(f".{destination.name}.partial")
        try:
            shutil.copy2(source, partial, follow_symlinks=True)
            os.replace(str(partial), str(destination))
        finally:
            try:
                partial.unlink()
            except OSError:
                pass
    else:
        shutil.copy2(source, destination, follow_symlinks=True)
    return True


def _copy_plan_files(
    plan: CollectPlan,
    staging: Path,
    progress=None,
    incremental: bool = False,
) -> Dict[str, str]:
    copied: Dict[str, str] = {}
    seen_pairs: Set[str] = set()
    total = sum(
        len(ref.source_files) for ref in plan.references if ref.enabled
    ) + sum(1 for hda in plan.hdas if hda.enabled)
    completed_count = 0
    copied_count = 0
    reused_count = 0
    for ref in plan.references:
        if not ref.enabled:
            continue
        for source, planned_destination in zip(ref.source_files, ref.destination_files):
            relative = planned_destination.relative_to(plan.output_root)
            destination = staging / relative
            key = normalized_key(source) + "|" + relative.as_posix().casefold()
            completed_count += 1
            if key in seen_pairs:
                if progress:
                    progress(completed_count / max(1, total), f"Already copied {source.name}")
                continue
            seen_pairs.add(key)
            did_copy = _copy_file(source, destination, incremental)
            if did_copy:
                copied_count += 1
                copied[f"{source} -> {relative.as_posix()}"] = relative.as_posix()
            else:
                reused_count += 1
            if progress:
                verb = "Copying" if did_copy else "Reusing"
                progress(completed_count / max(1, total), f"{verb} {source.name}")
    for hda in plan.hdas:
        if not hda.enabled or hda.destination is None:
            continue
        destination = staging / hda.destination.relative_to(plan.output_root)
        did_copy = _copy_file(hda.library_path, destination, incremental)
        if did_copy:
            copied_count += 1
            copied[normalized_key(hda.library_path)] = destination.relative_to(staging).as_posix()
        else:
            reused_count += 1
        completed_count += 1
        if progress:
            verb = "Copying" if did_copy else "Reusing"
            progress(completed_count / max(1, total), f"{verb} {hda.library_path.name}")
    plan.copy_stats = {"copied": copied_count, "reused": reused_count}
    return copied


def _relink_parameters(plan: CollectPlan) -> List[str]:
    warnings: List[str] = []
    for ref in plan.references:
        if not ref.enabled or not ref.exists or not ref.parm_path:
            continue
        parm = hou.parm(ref.parm_path)
        if parm is None:
            warnings.append(f"Parameter disappeared before relink: {ref.parm_path}")
            continue
        try:
            if parm.keyframes():
                warnings.append(f"Animated/expression parameter was not relinked: {ref.parm_path}")
                continue
            parm.set(ref.destination_pattern, follow_parm_reference=False)
        except Exception as exc:
            warnings.append(f"Could not relink {ref.parm_path}: {exc}")
    return warnings


def _embed_hdas(plan: CollectPlan) -> List[str]:
    """Embed custom definitions without making Embedded the current library.

    Keeping the external definition current avoids Houdini's modal warning when
    "Save Definitions to Hip File" is disabled. If the collected HIP is opened
    on a machine without that library, the embedded fallback is still present.
    """
    warnings: List[str] = []
    copied_types: Set[str] = set()
    for item in plan.hdas:
        if not item.enabled:
            continue
        try:
            definitions = hou.hda.definitionsInFile(str(item.library_path))
        except Exception as exc:
            warnings.append(f"Could not inspect HDA library {item.library_path}: {exc}")
            continue
        for definition in definitions:
            try:
                key = definition.nodeTypeCategory().name() + "/" + definition.nodeTypeName()
                if key not in item.node_types or key in copied_types:
                    continue
                definition.copyToHDAFile("Embedded")
                copied_types.add(key)
            except Exception as exc:
                warnings.append(f"Could not embed a definition from {item.library_path}: {exc}")
    return warnings


def _has_kept_descendant(path: str, keep: Set[str]) -> bool:
    prefix = path.rstrip("/") + "/"
    return path in keep or any(item.startswith(prefix) for item in keep)


def _prune_network(network, keep: Set[str], warnings: List[str]) -> None:
    for child in tuple(network.children()):
        path = child.path()
        if not _has_kept_descendant(path, keep):
            try:
                child.destroy()
            except Exception as exc:
                warnings.append(f"Could not remove {path}: {exc}")
            continue
        if _is_network(child):
            try:
                if child.type().definition() and child.matchesCurrentDefinition():
                    continue
            except Exception:
                pass
            _prune_network(child, keep, warnings)


def _prune_scene(plan: CollectPlan) -> List[str]:
    warnings: List[str] = []
    for root_path in ("/obj", "/mat", "/shop", "/stage", "/out"):
        root = hou.node(root_path)
        if root is not None:
            _prune_network(root, plan.kept_nodes, warnings)
    return warnings


def _manifest(plan: CollectPlan, copied: Dict[str, str], warnings: Sequence[str]) -> dict:
    return {
        "tool": "Houdini Collector",
        "version": "0.3.1",
        "collect_mode": plan.collect_mode,
        "incremental_update": plan.incremental_update,
        "copy_stats": dict(plan.copy_stats),
        "created_unix": time.time(),
        "houdini_version": hou.applicationVersionString(),
        "source_hip": str(plan.source_hip),
        "collected_hip": None if plan.collect_mode == "report_only" else plan.collected_hip.name,
        "selected_nodes": plan.selected_nodes,
        "kept_nodes": sorted(plan.kept_nodes),
        "files": [
            {
                "enabled": ref.enabled,
                "category": ref.category,
                "asset": ref.asset_name,
                "asset_overridden": ref.asset_overridden,
                "used_by": list(ref.used_by),
                "parameter": ref.parm_path,
                "node": ref.node_path,
                "source_pattern": ref.raw_path,
                "destination_pattern": ref.destination_pattern,
                "sources": [str(item) for item in ref.source_files],
                "exists": ref.exists,
                "warning": ref.warning,
            }
            for ref in plan.references
        ],
        "hdas": [
            {
                "source": str(item.library_path),
                "destination": item.destination.name if item.destination else None,
                "node_types": sorted(item.node_types),
                "enabled": item.enabled,
            }
            for item in plan.hdas
        ],
        "copied_files": copied,
        "warnings": list(warnings),
    }


def _write_manifest(
    directory: Path,
    plan: CollectPlan,
    copied: Dict[str, str],
    warnings: Sequence[str],
    atomic: bool = False,
) -> Path:
    manifest_dir = directory / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    report = manifest_dir / "collect.json"
    report_text = json.dumps(_manifest(plan, copied, warnings), indent=2, ensure_ascii=False)
    log_path = manifest_dir / "collect.log"
    log_text = "\n".join(warnings) if warnings else "Collect completed without warnings.\n"
    for path, content in ((report, report_text), (log_path, log_text)):
        target = path.with_name(f".{path.name}.partial") if atomic else path
        with target.open("w", encoding="utf-8") as stream:
            stream.write(content)
        if atomic:
            os.replace(str(target), str(path))
    return report


def _write_hda_loader(directory: Path) -> None:
    manifest_dir = directory / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    loader = (
        "# Run from Houdini after opening the collected HIP if custom HDAs are unavailable.\n"
        "import glob, hou\n"
        "for path in glob.glob(hou.expandString('$HIP/hda/*')):\n"
        "    try: hou.hda.installFile(path)\n"
        "    except hou.OperationFailed: pass\n"
    )
    with (manifest_dir / "load_hdas.py").open("w", encoding="utf-8") as stream:
        stream.write(loader)


def _execute_incremental(plan: CollectPlan, progress=None) -> Path:
    source_hip = str(plan.source_hip)
    warnings = list(plan.warnings)
    partial_hip = plan.output_root / (
        f".{plan.collected_hip.stem}_partial{plan.collected_hip.suffix}"
    )
    completed = False
    copied: Dict[str, str] = {}
    try:
        hou.hipFile.save(source_hip, save_to_recent_files=False)
        copied = _copy_plan_files(plan, plan.output_root, progress=progress, incremental=True)
        hou.hipFile.save(str(partial_hip), save_to_recent_files=False)
        warnings.extend(_embed_hdas(plan))
        warnings.extend(_relink_parameters(plan))
        warnings.extend(_prune_scene(plan))
        hou.hipFile.save(str(partial_hip), save_to_recent_files=False)
        completed = True
    finally:
        if Path(hou.hipFile.path()) != plan.source_hip:
            try:
                hou.hipFile.load(source_hip, suppress_save_prompt=True, ignore_load_warnings=True)
            except Exception as exc:
                warnings.append(f"IMPORTANT: Could not reload source HIP: {exc}")
        if not completed:
            try:
                partial_hip.unlink()
            except OSError:
                pass

    os.replace(str(partial_hip), str(plan.collected_hip))
    _write_manifest(plan.output_root, plan, copied, warnings, atomic=True)
    if plan.hdas:
        _write_hda_loader(plan.output_root)
    return plan.collected_hip


def _validate_incremental_destination(plan: CollectPlan) -> None:
    manifest_path = plan.output_root / "manifest" / "collect.json"
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            previous = json.load(stream)
    except (OSError, ValueError) as exc:
        raise CollectorError(
            "Incremental destination is not a valid Houdini Collector folder: "
            f"{manifest_path}"
        ) from exc
    if previous.get("tool") != "Houdini Collector":
        raise CollectorError("Incremental destination manifest belongs to another tool.")
    previous_hip = previous.get("collected_hip")
    if previous_hip and previous_hip != plan.collected_hip.name:
        raise CollectorError(
            f"Incremental destination contains {previous_hip}, expected {plan.collected_hip.name}."
        )


def execute_plan(plan: CollectPlan, progress=None) -> Path:
    if plan.output_root.exists() and not plan.incremental_update:
        raise CollectorError(f"Destination already exists: {plan.output_root}")
    if plan.output_root.exists() and not plan.output_root.is_dir():
        raise CollectorError(f"Incremental destination is not a folder: {plan.output_root}")
    if plan.output_root.exists() and plan.incremental_update:
        if plan.collect_mode in ("project_only", "report_only"):
            raise CollectorError("Incremental Update is available only for asset collection modes.")
        _validate_incremental_destination(plan)
        return _execute_incremental(plan, progress=progress)
    plan.output_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{plan.output_root.name}_partial_", dir=str(plan.output_root.parent)))
    if plan.collect_mode == "report_only":
        try:
            report = _write_manifest(staging, plan, {}, plan.warnings)
            relative_report = report.relative_to(staging)
            staging.rename(plan.output_root)
            return plan.output_root / relative_report
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    source_hip = str(plan.source_hip)
    warnings = list(plan.warnings)
    completed = False
    try:
        # Save user changes before any interruptible work. If copying is cancelled,
        # the original scene remains open and no in-memory work is lost.
        hou.hipFile.save(source_hip, save_to_recent_files=False)
        copied = (
            {} if plan.collect_mode == "project_only"
            else _copy_plan_files(plan, staging, progress=progress)
        )
        staged_hip = staging / plan.collected_hip.name

        hou.hipFile.save(str(staged_hip), save_to_recent_files=False)
        if plan.collect_mode != "project_only":
            warnings.extend(_embed_hdas(plan))
            warnings.extend(_relink_parameters(plan))
        warnings.extend(_prune_scene(plan))
        hou.hipFile.save(str(staged_hip), save_to_recent_files=False)

        _write_manifest(staging, plan, copied, warnings)
        if plan.hdas and plan.collect_mode != "project_only":
            _write_hda_loader(staging)
        completed = True
    finally:
        if Path(hou.hipFile.path()) != plan.source_hip:
            try:
                hou.hipFile.load(source_hip, suppress_save_prompt=True, ignore_load_warnings=True)
            except Exception as exc:
                warnings.append(f"IMPORTANT: Could not reload source HIP: {exc}")
        if not completed:
            shutil.rmtree(staging, ignore_errors=True)

    staging.rename(plan.output_root)
    if Path(hou.hipFile.path()) != plan.source_hip:
        try:
            hou.hipFile.setName(str(plan.output_root / plan.collected_hip.name))
        except Exception:
            pass
    return plan.output_root / plan.collected_hip.name
