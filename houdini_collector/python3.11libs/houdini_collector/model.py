from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class CollectOptions:
    output_root: Path
    collect_mode: str = "selected"
    include_upstream: bool = True
    include_references: bool = True
    include_materials: bool = True
    include_lights_cameras: bool = True
    include_hdas: bool = False
    include_redshift_proxies: bool = True
    collect_all_sequence_files: bool = True
    preserve_megascans_packages: bool = True
    incremental_update: bool = False


@dataclass
class FileReference:
    parm_path: Optional[str]
    node_path: Optional[str]
    raw_path: str
    evaluated_path: str
    category: str = "misc"
    asset_name: str = "misc"
    source_files: Tuple[Path, ...] = ()
    destination_pattern: str = ""
    destination_files: Tuple[Path, ...] = ()
    exists: bool = False
    enabled: bool = True
    is_output: bool = False
    package_root: Optional[Path] = None
    warning: str = ""
    used_by: Tuple[str, ...] = ()
    auto_asset_name: str = ""
    asset_overridden: bool = False

    @property
    def parm_name(self) -> str:
        """Return the parameter token without repeating its node path."""
        return self.parm_path.rsplit("/", 1)[-1] if self.parm_path else ""

    @property
    def used_by_label(self) -> str:
        if not self.used_by:
            return self.node_path or ""
        if len(self.used_by) == 1:
            return self.used_by[0]
        return f"{self.used_by[0]} (+{len(self.used_by) - 1})"

    @property
    def scan_key(self) -> str:
        return self.parm_path or f"{self.node_path or ''}|{self.raw_path}"

    @property
    def size_bytes(self) -> int:
        total = 0
        for path in self.source_files:
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total


@dataclass
class HDAReference:
    library_path: Path
    node_types: Set[str] = field(default_factory=set)
    destination: Optional[Path] = None
    enabled: bool = True


@dataclass(frozen=True)
class RelinkCandidate:
    path_pattern: str
    source_files: Tuple[Path, ...]
    score: int = 0


@dataclass
class CollectPlan:
    source_hip: Path
    output_root: Path
    collected_hip: Path
    selected_nodes: List[str]
    kept_nodes: Set[str]
    references: List[FileReference]
    hdas: List[HDAReference]
    collect_mode: str = "selected"
    incremental_update: bool = False
    warnings: List[str] = field(default_factory=list)
    path_map: Dict[str, str] = field(default_factory=dict)
    copy_stats: Dict[str, int] = field(default_factory=dict)

    @property
    def total_size_bytes(self) -> int:
        seen = set()
        total = 0
        for ref in self.references:
            if not ref.enabled:
                continue
            for path in ref.source_files:
                key = str(path).casefold()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        for hda in self.hdas:
            key = str(hda.library_path).casefold()
            if hda.enabled and key not in seen:
                seen.add(key)
                try:
                    total += hda.library_path.stat().st_size
                except OSError:
                    pass
        return total
