from __future__ import annotations

import os
import json
import subprocess
import sys
import traceback
from pathlib import Path

import hou
try:  # Houdini 21.0 ships PySide2; Houdini 22 uses PySide6.
    from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:  # Forward-compatible fallback.
    from PySide6 import QtCore, QtGui, QtWidgets

from .core import (
    CollectorError,
    assign_destinations,
    build_plan,
    execute_plan,
    find_relink_candidates,
    next_output_root,
)
from .model import CollectOptions
from .paths import human_size
from .paths import safe_name


WINDOW = None

MODE_ITEMS = (
    ("Selected Branches", "selected"),
    ("Whole Scene", "whole_scene"),
    ("Selected ROPs", "selected_rops"),
    ("Project Only", "project_only"),
    ("Report Only", "report_only"),
)

CATEGORY_FILTERS = (
    ("All categories", "all"),
    ("Models", "models"),
    ("Textures / Materials", "textures"),
    ("HDRI", "hdri"),
    ("Megascans", "megascans"),
    ("Caches", "cache"),
    ("USD", "usd"),
    ("HDA", "hda"),
    ("Misc", "misc"),
)

BUILTIN_PRESETS = {
    "Default": {
        "mode": "selected", "upstream": True, "references": True,
        "materials": True, "lights": True, "proxies": True, "hdas": False,
        "megascans": True, "sequences": True, "incremental": False,
    },
    "Client Delivery": {
        "mode": "selected", "upstream": True, "references": True,
        "materials": True, "lights": True, "proxies": True, "hdas": False,
        "megascans": True, "sequences": True, "incremental": False,
    },
    "Render Farm": {
        "mode": "selected_rops", "upstream": True, "references": True,
        "materials": True, "lights": True, "proxies": True, "hdas": False,
        "megascans": True, "sequences": True, "incremental": True,
    },
    "Archive": {
        "mode": "whole_scene", "upstream": True, "references": True,
        "materials": True, "lights": True, "proxies": True, "hdas": True,
        "megascans": True, "sequences": True, "incremental": False,
    },
    "Lightweight": {
        "mode": "selected", "upstream": True, "references": True,
        "materials": True, "lights": False, "proxies": False, "hdas": False,
        "megascans": True, "sequences": False, "incremental": False,
    },
    "Models + Materials": {
        "mode": "selected", "upstream": True, "references": True,
        "materials": True, "lights": False, "proxies": False, "hdas": False,
        "megascans": True, "sequences": True, "incremental": False,
    },
}

MODE_TOOLTIPS = {
    "selected": "Collect the selected branches and their discovered dependencies.",
    "whole_scene": "Scan and collect all supported networks in the current HIP.",
    "selected_rops": "Use selected render/ROP nodes as the collection roots.",
    "project_only": "Create a trimmed HIP without copying or relinking external files.",
    "report_only": "Write dependency reports without saving a HIP copy or changing the scene.",
}

CHECKBOX_TOOLTIPS = {
    "Upstream input branches": "Include every node connected upstream of the selected roots.",
    "Object Merge / expression references": "Follow node references such as Object Merge paths and parameter links.",
    "Assigned materials": "Include materials assigned through parameters or shop_materialpath.",
    "Lights and cameras": "Keep scene lights and cameras; this is conservative for renderer-specific linking.",
    "Redshift proxies": "Collect external Redshift proxy (.rs) files, but not Redshift vendor HDAs.",
    "Custom HDA / OTL libraries": "Copy and embed user HDA/OTL definitions. Disabled by default.",
    "Preserve Megascans folder layout": "Keep the Megascans asset layout while copying only referenced files.",
    "Collect every discovered sequence file": "Collect all matching $F, ####, printf and UDIM files instead of one frame.",
    "Incremental Update existing collect": "Update a valid existing collect and reuse files that have not changed.",
}

PRESET_TOOLTIPS = {
    "Default": "Balanced settings for selected Houdini branches.",
    "Client Delivery": "Portable selected branches with materials, lights, proxies and full sequences.",
    "Render Farm": "Selected ROP roots with incremental updates enabled.",
    "Archive": "Whole-scene archive including eligible custom HDA libraries.",
    "Lightweight": "Smaller package without lights, proxies or full sequences.",
    "Models + Materials": "Collect selected geometry and materials without scene lighting.",
}


class RelinkDialog(QtWidgets.QDialog):
    def __init__(self, matches, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Relink Missing Assets")
        self.resize(1050, 430)
        self.matches = matches
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "Review proposed replacements. Sequence and UDIM tokens are preserved. "
            "Only checked rows will modify the current source scene."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QtWidgets.QTableWidget(len(matches), 4)
        self.table.setHorizontalHeaderLabels(("Apply", "Missing", "Proposed", "Files"))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        for row, (ref, candidates) in enumerate(matches):
            enabled = bool(candidates)
            check = QtWidgets.QTableWidgetItem()
            check.setFlags(check.flags() | QtCore.Qt.ItemIsUserCheckable)
            check.setCheckState(QtCore.Qt.Checked if enabled else QtCore.Qt.Unchecked)
            if not enabled:
                check.setFlags(check.flags() & ~QtCore.Qt.ItemIsEnabled)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(ref.raw_path))
            combo = QtWidgets.QComboBox()
            if candidates:
                for candidate in candidates:
                    combo.addItem(candidate.path_pattern, candidate)
            else:
                combo.addItem("No match found", None)
                combo.setEnabled(False)
            self.table.setCellWidget(row, 2, combo)
            count = len(candidates[0].source_files) if candidates else 0
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(str(count)))
            combo.currentIndexChanged.connect(
                lambda _index, r=row: self._candidate_changed(r)
            )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Apply | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.Apply).setText("Apply Relink")
        buttons.button(QtWidgets.QDialogButtonBox.Apply).clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _candidate_changed(self, row):
        combo = self.table.cellWidget(row, 2)
        candidate = combo.currentData() if combo is not None else None
        self.table.item(row, 3).setText(str(len(candidate.source_files)) if candidate else "0")

    def selected_matches(self):
        result = []
        for row, (ref, _candidates) in enumerate(self.matches):
            if self.table.item(row, 0).checkState() != QtCore.Qt.Checked:
                continue
            combo = self.table.cellWidget(row, 2)
            candidate = combo.currentData() if combo is not None else None
            if candidate is not None:
                result.append((ref, candidate))
        return result


class CollectorWindow(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent or hou.qt.mainWindow())
        self.setWindowTitle("Houdini Collector 0.3.1")
        self.setObjectName("houdiniCollectorWindow")
        self.resize(1120, 720)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)
        self.plan = None
        self._scan_state = {}
        self._expanded_assets = set()
        self._has_scan_state = False
        self._settings = QtCore.QSettings("HoudiniCollector", "HoudiniCollector")
        self._build_ui()
        self._set_default_destination()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)

        intro = QtWidgets.QLabel(
            "Inspect, relink and collect Houdini dependencies into a portable project. "
            "The source scene is restored automatically after collection."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        destination_row = QtWidgets.QHBoxLayout()
        destination_row.addWidget(QtWidgets.QLabel("Mode"))
        self.mode = QtWidgets.QComboBox()
        for label, value in MODE_ITEMS:
            self.mode.addItem(label, value)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.mode.setToolTip(MODE_TOOLTIPS["selected"])
        destination_row.addWidget(self.mode)
        destination_row.addWidget(QtWidgets.QLabel("Destination"))
        self.destination = QtWidgets.QLineEdit()
        self.destination.setToolTip(
            "Folder that will contain the collected HIP and assets. For Incremental Update, choose an existing collect folder."
        )
        destination_row.addWidget(self.destination, 1)
        browse = QtWidgets.QPushButton("Browse…")
        browse.setToolTip("Choose a new collect parent folder, or an existing collect when Incremental Update is enabled.")
        browse.clicked.connect(self._browse)
        destination_row.addWidget(browse)
        root.addLayout(destination_row)

        options_box = QtWidgets.QGroupBox("Include with selected nodes")
        options_layout = QtWidgets.QGridLayout(options_box)
        self.upstream = self._checkbox("Upstream input branches", True)
        self.references = self._checkbox("Object Merge / expression references", True)
        self.materials = self._checkbox("Assigned materials", True)
        self.lights = self._checkbox("Lights and cameras", True)
        self.proxies = self._checkbox("Redshift proxies", True)
        self.hdas = self._checkbox("Custom HDA / OTL libraries", False)
        self.megascans = self._checkbox("Preserve Megascans folder layout", True)
        self.sequences = self._checkbox("Collect every discovered sequence file", True)
        self.incremental = self._checkbox("Incremental Update existing collect", False)
        self.incremental.toggled.connect(self._incremental_changed)
        for index, widget in enumerate(
            (self.upstream, self.references, self.materials, self.lights,
             self.proxies, self.hdas, self.megascans, self.sequences, self.incremental)
        ):
            options_layout.addWidget(widget, index // 2, index % 2)
        root.addWidget(options_box)

        preset_row = QtWidgets.QHBoxLayout()
        preset_row.addWidget(QtWidgets.QLabel("Preset"))
        self.preset = QtWidgets.QComboBox()
        self.preset.setToolTip(PRESET_TOOLTIPS["Default"])
        preset_row.addWidget(self.preset, 1)
        save_preset = QtWidgets.QPushButton("Save Preset…")
        save_preset.setToolTip("Save the current mode and Include options as a reusable custom preset.")
        save_preset.clicked.connect(self._save_preset)
        preset_row.addWidget(save_preset)
        self.delete_preset = QtWidgets.QPushButton("Delete Preset")
        self.delete_preset.setToolTip("Delete the selected custom preset. Built-in presets cannot be deleted.")
        self.delete_preset.clicked.connect(self._delete_preset)
        preset_row.addWidget(self.delete_preset)
        root.addLayout(preset_row)
        self._refresh_presets()
        self.preset.currentIndexChanged.connect(self._apply_selected_preset)

        actions = QtWidgets.QHBoxLayout()
        self.selection_label = QtWidgets.QLabel("No scan yet")
        actions.addWidget(self.selection_label, 1)
        legend = QtWidgets.QLabel(
            "<span style='color:#68b9ef'>● HDRI</span>&nbsp;&nbsp;"
            "<span style='color:#63d17b'>● Megascans</span>&nbsp;&nbsp;"
            "<span style='color:#f0ce55'>● Textures</span>"
        )
        legend.setTextFormat(QtCore.Qt.RichText)
        actions.addWidget(legend)
        self.scan_button = QtWidgets.QPushButton("Scan Selected Nodes")
        self.scan_button.setToolTip("Scan the current roots and rebuild the dependency preview without copying files.")
        self.scan_button.clicked.connect(self.scan)
        actions.addWidget(self.scan_button)
        root.addLayout(actions)

        filters = QtWidgets.QHBoxLayout()
        filters.addWidget(QtWidgets.QLabel("Filter"))
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search asset, path, node or parameter…")
        self.search.setToolTip("Filter visible rows by asset name, file path, node path or parameter.")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filters)
        filters.addWidget(self.search, 1)
        self.category_filter = QtWidgets.QComboBox()
        self.category_filter.setToolTip("Show only dependencies from the selected category.")
        for label, value in CATEGORY_FILTERS:
            self.category_filter.addItem(label, value)
        self.category_filter.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.category_filter)
        self.status_filter = QtWidgets.QComboBox()
        self.status_filter.setToolTip("Show all rows or only Ready, Missing or Output references.")
        for label, value in (("All statuses", "all"), ("Ready", "Ready"), ("Missing", "Missing"), ("Output", "Output")):
            self.status_filter.addItem(label, value)
        self.status_filter.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.status_filter)
        self.visible_label = QtWidgets.QLabel("")
        filters.addWidget(self.visible_label)
        self.relink_button = QtWidgets.QPushButton("Find Missing Assets…")
        self.relink_button.setToolTip("Search a folder recursively and preview safe replacements for missing references.")
        self.relink_button.setEnabled(False)
        self.relink_button.clicked.connect(self._find_missing_assets)
        filters.addWidget(self.relink_button)
        root.addLayout(filters)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_menu)
        self.tree.itemDoubleClicked.connect(self._tree_double_clicked)
        self.tree.setToolTip(
            "Double-click Source to reveal the file. "
            "Double-click Node or Parameter to navigate to the node."
        )
        self.tree.setHeaderLabels((
            "Collect", "Asset", "Category", "Status", "Size", "Source",
            "Destination", "Node", "Parameter"
        ))
        header = self.tree.header()
        header.setMinimumSectionSize(45)
        header.setSectionsMovable(True)
        header.setStretchLastSection(False)
        for column in range(self.tree.columnCount()):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.Interactive)
        default_widths = (60, 150, 95, 80, 80, 290, 280, 250, 120)
        for column, width in enumerate(default_widths):
            header.resizeSection(column, width)
        saved_header = self._settings.value("asset_inspector_header")
        if saved_header:
            header.restoreState(saved_header)
        header.sectionResized.connect(self._save_header_state)
        header.sectionMoved.connect(self._save_header_state)
        self.tree.setToolTip(
            self.tree.toolTip() + " Drag column borders to resize them; drag headers to reorder columns."
        )
        root.addWidget(self.tree, 1)

        footer = QtWidgets.QHBoxLayout()
        self.summary = QtWidgets.QLabel("Scan the current selection to preview dependencies.")
        footer.addWidget(self.summary, 1)
        close_button = QtWidgets.QPushButton("Close")
        close_button.setToolTip("Close Houdini Collector without collecting. Source scene changes are not discarded.")
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        self.collect_button = QtWidgets.QPushButton("Collect")
        self.collect_button.setToolTip("Create or update the collected project using the checked dependency rows.")
        self.collect_button.setEnabled(False)
        self.collect_button.clicked.connect(self.collect)
        footer.addWidget(self.collect_button)
        root.addLayout(footer)

    @staticmethod
    def _checkbox(label, checked):
        widget = QtWidgets.QCheckBox(label)
        widget.setChecked(checked)
        widget.setToolTip(CHECKBOX_TOOLTIPS.get(label, ""))
        return widget

    def _custom_presets(self):
        try:
            data = json.loads(self._settings.value("custom_presets", "{}"))
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _save_header_state(self, *_args):
        self._settings.setValue("asset_inspector_header", self.tree.header().saveState())

    def _refresh_presets(self, select_name=None):
        current = select_name or (self.preset.currentText() if self.preset.count() else "Default")
        self.preset.blockSignals(True)
        self.preset.clear()
        for name in BUILTIN_PRESETS:
            self.preset.addItem(name, ("builtin", name))
        for name in sorted(self._custom_presets(), key=str.casefold):
            self.preset.addItem(name, ("custom", name))
        index = self.preset.findText(current)
        self.preset.setCurrentIndex(index if index >= 0 else 0)
        self.preset.blockSignals(False)
        data = self.preset.currentData()
        self.delete_preset.setEnabled(bool(data and data[0] == "custom"))
        if data:
            self.preset.setToolTip(
                PRESET_TOOLTIPS.get(data[1], "User preset containing the saved mode and Include options.")
            )

    def _preset_values(self):
        return {
            "mode": str(self.mode.currentData()),
            "upstream": self.upstream.isChecked(),
            "references": self.references.isChecked(),
            "materials": self.materials.isChecked(),
            "lights": self.lights.isChecked(),
            "proxies": self.proxies.isChecked(),
            "hdas": self.hdas.isChecked(),
            "megascans": self.megascans.isChecked(),
            "sequences": self.sequences.isChecked(),
            "incremental": self.incremental.isChecked(),
        }

    def _set_preset_values(self, values):
        mode_index = self.mode.findData(values.get("mode", "selected"))
        if mode_index >= 0:
            self.mode.setCurrentIndex(mode_index)
        for key, widget in (
            ("upstream", self.upstream), ("references", self.references),
            ("materials", self.materials), ("lights", self.lights),
            ("proxies", self.proxies), ("hdas", self.hdas),
            ("megascans", self.megascans), ("sequences", self.sequences),
            ("incremental", self.incremental),
        ):
            widget.setChecked(bool(values.get(key, widget.isChecked())))
        self._mode_changed()

    def _apply_selected_preset(self, *_args):
        data = self.preset.currentData()
        if not data:
            return
        kind, name = data
        values = BUILTIN_PRESETS.get(name) if kind == "builtin" else self._custom_presets().get(name)
        if values:
            self._set_preset_values(values)
        self.preset.setToolTip(
            PRESET_TOOLTIPS.get(name, "User preset containing the saved mode and Include options.")
        )
        self.delete_preset.setEnabled(kind == "custom")

    def _save_preset(self):
        name, accepted = QtWidgets.QInputDialog.getText(self, "Save Preset", "Preset name")
        name = name.strip() if accepted else ""
        if not name:
            return
        if name in BUILTIN_PRESETS:
            QtWidgets.QMessageBox.warning(self, "Preset", "A built-in preset already uses this name.")
            return
        presets = self._custom_presets()
        presets[name] = self._preset_values()
        self._settings.setValue("custom_presets", json.dumps(presets, ensure_ascii=False))
        self._refresh_presets(name)

    def _delete_preset(self):
        data = self.preset.currentData()
        if not data or data[0] != "custom":
            return
        answer = QtWidgets.QMessageBox.question(
            self, "Delete Preset", f"Delete preset '{data[1]}'?"
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        presets = self._custom_presets()
        presets.pop(data[1], None)
        self._settings.setValue("custom_presets", json.dumps(presets, ensure_ascii=False))
        self._refresh_presets("Default")

    def _incremental_changed(self, *_args):
        self._mode_changed()

    def _set_default_destination(self):
        try:
            hip = Path(hou.hipFile.path())
            self.destination.setText(str(next_output_root(hip)))
        except Exception:
            self.destination.setText("")

    def _mode_changed(self, *_args):
        mode = self.mode.currentData()
        self.mode.setToolTip(MODE_TOOLTIPS.get(mode, "Choose how the collection scope is built."))
        labels = {
            "selected": "Scan Selected Nodes",
            "whole_scene": "Scan Whole Scene",
            "selected_rops": "Scan Selected ROPs",
            "project_only": "Scan Selected Nodes",
            "report_only": "Scan Selected Nodes",
        }
        self.scan_button.setText(labels.get(mode, "Scan"))
        self.scan_button.setToolTip(
            "Scan dependencies for this mode without copying files. " + MODE_TOOLTIPS.get(mode, "")
        )
        supports_incremental = mode not in ("project_only", "report_only")
        self.incremental.setEnabled(supports_incremental)
        if not supports_incremental and self.incremental.isChecked():
            self.incremental.setChecked(False)
        self.collect_button.setText(
            "Save Report" if mode == "report_only" else (
                "Create Project Copy" if mode == "project_only" else (
                    "Update Collect" if self.incremental.isChecked() else "Collect"
                )
            )
        )
        if mode == "report_only":
            collect_tip = "Write manifest/collect.json and collect.log without creating a HIP copy."
        elif mode == "project_only":
            collect_tip = "Create a trimmed HIP while leaving external file paths unchanged."
        elif self.incremental.isChecked():
            collect_tip = "Update a valid existing collect and reuse unchanged files; orphan files are not deleted."
        else:
            collect_tip = "Create a new portable HIP and copy the checked external dependencies."
        self.collect_button.setToolTip(collect_tip)

    def _browse(self):
        start = self.destination.text() or hou.expandString("$HIP")
        result = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose parent folder", str(Path(start).parent))
        if result:
            if self.incremental.isChecked():
                self.destination.setText(str(Path(result)))
            else:
                hip = Path(hou.hipFile.path())
                self.destination.setText(str(Path(result) / next_output_root(hip).name))

    def _options(self):
        destination = self.destination.text().strip()
        if not destination:
            raise CollectorError("Choose a collect folder.")
        return CollectOptions(
            output_root=Path(destination),
            collect_mode=str(self.mode.currentData()),
            include_upstream=self.upstream.isChecked(),
            include_references=self.references.isChecked(),
            include_materials=self.materials.isChecked(),
            include_lights_cameras=self.lights.isChecked(),
            include_hdas=self.hdas.isChecked(),
            include_redshift_proxies=self.proxies.isChecked(),
            collect_all_sequence_files=self.sequences.isChecked(),
            preserve_megascans_packages=self.megascans.isChecked(),
            incremental_update=self.incremental.isChecked(),
        )

    def _capture_scan_state(self):
        if self.plan is None:
            return
        self._apply_row_selection()
        state = {}
        for ref in self.plan.references:
            state[("ref", ref.scan_key)] = {
                "enabled": ref.enabled,
                "asset": ref.asset_name,
                "asset_overridden": ref.asset_overridden,
            }
        for hda in self.plan.hdas:
            state[("hda", str(hda.library_path).casefold())] = {"enabled": hda.enabled}
        self._scan_state = state
        self._expanded_assets = {
            self.tree.topLevelItem(row).text(1)
            for row in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(row).isExpanded()
        }
        self._has_scan_state = True

    def _restore_scan_state(self):
        if self.plan is None or not self._has_scan_state:
            return
        destinations_changed = False
        for ref in self.plan.references:
            state = self._scan_state.get(("ref", ref.scan_key))
            if not state:
                continue
            ref.enabled = bool(state.get("enabled", ref.enabled))
            if state.get("asset_overridden"):
                ref.asset_name = safe_name(state.get("asset", ref.asset_name), ref.asset_name)
                ref.asset_overridden = True
                destinations_changed = True
        for hda in self.plan.hdas:
            state = self._scan_state.get(("hda", str(hda.library_path).casefold()))
            if state:
                hda.enabled = bool(state.get("enabled", hda.enabled))
        if destinations_changed:
            assign_destinations(self.plan.references, self.plan.output_root)

    def scan(self):
        self._capture_scan_state()
        self.collect_button.setEnabled(False)
        self.relink_button.setEnabled(False)
        self.tree.clear()
        try:
            selected = hou.selectedNodes()
            with hou.InterruptableOperation("Scanning selected Houdini branches", open_interrupt_dialog=True):
                self.plan = build_plan(self._options(), selected)
            self._restore_scan_state()
            self._populate()
        except hou.OperationInterrupted:
            self.summary.setText("Scan cancelled.")
        except Exception as exc:
            self.plan = None
            self._show_error("Scan failed", exc)

    def _populate(self):
        missing = 0
        groups = {}
        for index, ref in enumerate(self.plan.references):
            status = "Output" if ref.is_output else ("Ready" if ref.exists else "Missing")
            if not ref.exists and not ref.is_output:
                missing += 1
            source = ref.raw_path
            group_key = (ref.asset_name.casefold(), ref.asset_name)
            parent = groups.get(group_key)
            if parent is None:
                parent = QtWidgets.QTreeWidgetItem(("", ref.asset_name, "", "", "", "", "", "", ""))
                parent.setData(0, QtCore.Qt.UserRole, ("group", ref.asset_name))
                font = parent.font(1)
                font.setBold(True)
                parent.setFont(1, font)
                groups[group_key] = parent
                self.tree.addTopLevelItem(parent)
            item = QtWidgets.QTreeWidgetItem((
                "", ref.asset_name, self._category_label(ref.category), status,
                human_size(ref.size_bytes), source, ref.destination_pattern,
                ref.node_path or "", ref.parm_name,
            ))
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(0, QtCore.Qt.Checked if ref.enabled else QtCore.Qt.Unchecked)
            item.setData(0, QtCore.Qt.UserRole, ("ref", index))
            item.setData(2, QtCore.Qt.UserRole, ref.category)
            item.setData(3, QtCore.Qt.UserRole, status)
            self._apply_category_color(item, ref.category)
            if status == "Missing":
                item.setForeground(3, QtGui.QBrush(QtGui.QColor("#ff6b6b")))
            elif status == "Output":
                item.setForeground(3, QtGui.QBrush(QtGui.QColor("#d9a441")))
            parent.addChild(item)

        for index, hda in enumerate(self.plan.hdas):
            group_key = ("hda", "HDA")
            parent = groups.get(group_key)
            if parent is None:
                parent = QtWidgets.QTreeWidgetItem(("", "HDA", "", "", "", "", "", "", ""))
                parent.setData(0, QtCore.Qt.UserRole, ("group", "HDA"))
                groups[group_key] = parent
                self.tree.addTopLevelItem(parent)
            item = QtWidgets.QTreeWidgetItem((
                "", hda.library_path.stem, "HDA", "Ready",
                human_size(hda.library_path.stat().st_size), str(hda.library_path),
                "$HIP/hda/" + hda.library_path.name,
                ", ".join(sorted(hda.node_types)), "",
            ))
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(0, QtCore.Qt.Checked if hda.enabled else QtCore.Qt.Unchecked)
            item.setData(0, QtCore.Qt.UserRole, ("hda", index))
            item.setData(2, QtCore.Qt.UserRole, "hda")
            item.setData(3, QtCore.Qt.UserRole, "Ready")
            parent.addChild(item)

        for parent in groups.values():
            parent.setExpanded(
                not self._has_scan_state or parent.text(1) in self._expanded_assets
            )

        if self.plan.collect_mode == "whole_scene":
            scope_text = "Whole scene"
        elif self.plan.collect_mode == "selected_rops":
            scope_text = f"{len(self.plan.selected_nodes)} ROP(s) selected"
        else:
            scope_text = f"{len(self.plan.selected_nodes)} selected"
        self.selection_label.setText(f"{scope_text} • {len(self.plan.kept_nodes)} nodes kept")
        self.summary.setText(
            f"{len(self.plan.references)} references • {len(self.plan.hdas)} HDA libraries • "
            f"{missing} missing • approximately {human_size(self.plan.total_size_bytes)}"
        )
        self.collect_button.setEnabled(True)
        self.relink_button.setEnabled(missing > 0)
        self._apply_filters()

    @staticmethod
    def _category_label(category):
        return {
            "asset_texture": "texture",
            "material": "material",
            "megascans": "megascans",
            "hdri": "hdri",
            "model": "model",
            "cache": "cache",
            "usd": "usd",
            "hda": "hda",
            "misc": "misc",
        }.get(category, category)

    def _apply_category_color(self, item, category):
        styles = {
            "hdri": (QtGui.QColor(35, 67, 92), QtGui.QColor("#68b9ef")),
            "megascans": (QtGui.QColor(35, 75, 45), QtGui.QColor("#63d17b")),
            "material": (QtGui.QColor(82, 69, 28), QtGui.QColor("#f0ce55")),
            "texture": (QtGui.QColor(82, 69, 28), QtGui.QColor("#f0ce55")),
            "asset_texture": (QtGui.QColor(82, 69, 28), QtGui.QColor("#f0ce55")),
        }
        style = styles.get(category)
        if style is None:
            return
        background, foreground = style
        for column in range(self.tree.columnCount()):
            item.setBackground(column, QtGui.QBrush(background))
        item.setForeground(2, QtGui.QBrush(foreground))
        font = item.font(2)
        font.setBold(True)
        item.setFont(2, font)

    def _leaf_items(self):
        for row in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(row)
            if parent.childCount():
                for child_index in range(parent.childCount()):
                    yield child_index, parent.child(child_index)
            else:
                yield row, parent

    def _apply_row_selection(self):
        for _row, item in self._leaf_items():
            payload = item.data(0, QtCore.Qt.UserRole)
            if not payload:
                continue
            kind, index = payload
            enabled = item.checkState(0) == QtCore.Qt.Checked
            if kind == "ref":
                self.plan.references[index].enabled = enabled
            else:
                self.plan.hdas[index].enabled = enabled

    def _item_payload(self, item):
        if item is None or self.plan is None:
            return None, None
        payload = item.data(0, QtCore.Qt.UserRole)
        if not payload:
            return None, None
        kind, index = payload
        if kind == "group":
            return kind, None
        if kind == "ref":
            return kind, self.plan.references[index]
        return kind, self.plan.hdas[index]

    def _reference_items(self, item, include_group=True):
        candidates = []
        if include_group and item.childCount():
            candidates = [item.child(index) for index in range(item.childCount())]
        else:
            selected = self.tree.selectedItems()
            candidates = selected if item in selected else [item]
        result = []
        for candidate in candidates:
            payload = candidate.data(0, QtCore.Qt.UserRole)
            if payload and payload[0] == "ref":
                result.append(candidate)
        return result

    def _rebuild_after_asset_edit(self):
        self._apply_row_selection()
        self._expanded_assets = {
            self.tree.topLevelItem(row).text(1)
            for row in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(row).isExpanded()
        }
        self._has_scan_state = True
        assign_destinations(self.plan.references, self.plan.output_root)
        self.tree.clear()
        self._populate()

    def _move_to_asset(self, item, rename_group=False):
        items = self._reference_items(item, include_group=rename_group)
        if not items:
            return
        current_name = item.text(1)
        name, accepted = QtWidgets.QInputDialog.getText(
            self, "Asset Group", "Asset name", text=current_name
        )
        name = safe_name(name, "") if accepted else ""
        if not name:
            return
        for ref_item in items:
            payload = ref_item.data(0, QtCore.Qt.UserRole)
            ref = self.plan.references[payload[1]]
            ref.asset_name = name
            ref.asset_overridden = True
        self._rebuild_after_asset_edit()

    def _reset_auto_asset(self, item, whole_group=False):
        items = self._reference_items(item, include_group=whole_group)
        if not items:
            return
        for ref_item in items:
            payload = ref_item.data(0, QtCore.Qt.UserRole)
            ref = self.plan.references[payload[1]]
            ref.asset_name = ref.auto_asset_name or ref.asset_name
            ref.asset_overridden = False
        self._rebuild_after_asset_edit()

    def _apply_filters(self, *_args):
        if not hasattr(self, "search"):
            return
        query = self.search.text().strip().casefold()
        category_filter = self.category_filter.currentData()
        status_filter = self.status_filter.currentData()
        category_groups = {
            "models": {"model"},
            "textures": {"material", "texture", "asset_texture"},
        }
        visible = 0
        total = 0
        for row in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(row)
            parent_visible = False
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                total += 1
                category = child.data(2, QtCore.Qt.UserRole) or "misc"
                status = child.data(3, QtCore.Qt.UserRole) or ""
                allowed_categories = category_groups.get(category_filter, {category_filter})
                category_ok = category_filter == "all" or category in allowed_categories
                status_ok = status_filter == "all" or status == status_filter
                haystack = " ".join(child.text(column) for column in range(self.tree.columnCount())).casefold()
                search_ok = not query or query in haystack
                show = category_ok and status_ok and search_ok
                child.setHidden(not show)
                parent_visible = parent_visible or show
                visible += int(show)
            parent.setHidden(not parent_visible)
        self.visible_label.setText(f"{visible}/{total}")

    def _show_tree_menu(self, position):
        item = self.tree.itemAt(position)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        kind, payload = self._item_payload(item)
        if kind == "group":
            menu = QtWidgets.QMenu(self.tree)
            select_all = menu.addAction("Select Group")
            deselect_all = menu.addAction("Deselect Group")
            menu.addSeparator()
            rename_asset = menu.addAction("Rename Asset Group…")
            reset_asset = menu.addAction("Reset Group to Auto")
            toggle = menu.addAction("Collapse" if item.isExpanded() else "Expand")
            chosen = menu.exec_(self.tree.viewport().mapToGlobal(position))
            if chosen in (select_all, deselect_all):
                state = QtCore.Qt.Checked if chosen == select_all else QtCore.Qt.Unchecked
                for index in range(item.childCount()):
                    item.child(index).setCheckState(0, state)
            elif chosen == toggle:
                item.setExpanded(not item.isExpanded())
            elif chosen == rename_asset:
                self._move_to_asset(item, rename_group=True)
            elif chosen == reset_asset:
                self._reset_auto_asset(item, whole_group=True)
            return
        if payload is None:
            return
        menu = QtWidgets.QMenu(self.tree)
        go_to_node = menu.addAction("Go to Node")
        go_to_node.setEnabled(kind == "ref" and bool(payload.node_path))
        reveal_file = menu.addAction("Show in File Explorer")
        source_file = self._payload_file(payload)
        reveal_file.setEnabled(source_file is not None and source_file.is_file())
        menu.addSeparator()
        move_asset = menu.addAction("Move to Asset…")
        move_asset.setEnabled(kind == "ref")
        reset_asset = menu.addAction("Reset Asset to Auto")
        reset_asset.setEnabled(kind == "ref" and bool(payload.asset_overridden))
        chosen = menu.exec_(self.tree.viewport().mapToGlobal(position))
        if chosen == go_to_node:
            self._go_to_node(payload.node_path)
        elif chosen == reveal_file:
            self._reveal_payload_file(payload)
        elif chosen == move_asset:
            self._move_to_asset(item)
        elif chosen == reset_asset:
            self._reset_auto_asset(item)

    def _tree_double_clicked(self, item, column):
        kind, payload = self._item_payload(item)
        if kind == "group":
            item.setExpanded(not item.isExpanded())
            return
        if payload is None:
            return
        if column == 1 and kind == "ref":
            self._move_to_asset(item)
        elif column == 5:
            self._reveal_payload_file(payload)
        elif column in (7, 8) and kind == "ref":
            self._go_to_node(payload.node_path)

    @staticmethod
    def _payload_file(payload):
        if hasattr(payload, "source_files") and payload.source_files:
            return Path(payload.source_files[0])
        if hasattr(payload, "library_path") and payload.library_path:
            return Path(payload.library_path)
        return None

    def _go_to_node(self, node_path):
        node = hou.node(node_path) if node_path else None
        if node is None:
            QtWidgets.QMessageBox.warning(self, "Node not found", f"Node no longer exists:\n{node_path}")
            return
        try:
            editor = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
            if editor is None:
                QtWidgets.QMessageBox.warning(self, "Network Editor", "No visible Network Editor pane was found.")
                return
            editor.setPwd(node.parent())
            node.setCurrent(True, clear_all_selected=True)
            editor.homeToSelection()
            try:
                editor.setIsCurrentTab()
            except Exception:
                pass
        except Exception as exc:
            self._show_error("Could not navigate to node", exc)

    def _reveal_payload_file(self, payload):
        path = self._payload_file(payload)
        if path is None or not path.is_file():
            QtWidgets.QMessageBox.warning(self, "File not found", "No existing source file is available for this row.")
            return
        try:
            native_path = os.path.normpath(str(path.resolve()))
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer.exe", "/select,", native_path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", native_path])
            else:
                subprocess.Popen(["xdg-open", str(path.resolve().parent)])
        except Exception as exc:
            self._show_error("Could not open file location", exc)

    def _find_missing_assets(self):
        if self.plan is None:
            return
        missing = [
            ref for ref in self.plan.references
            if not ref.exists and not ref.is_output and ref.parm_path
        ]
        if not missing:
            QtWidgets.QMessageBox.information(self, "Relink", "No missing file references were found.")
            return
        start = hou.expandString("$HIP")
        search_root = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose a folder to search recursively", start
        )
        if not search_root:
            return
        matches = []
        try:
            with hou.InterruptableOperation(
                "Searching for missing Houdini assets",
                open_interrupt_dialog=True,
            ) as operation:
                for index, ref in enumerate(missing):
                    operation.updateLongProgress(
                        index / max(1, len(missing)),
                        f"Searching for {Path(ref.raw_path.replace(chr(92), '/')).name}",
                    )
                    matches.append((ref, find_relink_candidates(ref, (Path(search_root),))))
        except hou.OperationInterrupted:
            self.summary.setText("Missing asset search cancelled.")
            return

        dialog = RelinkDialog(matches, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        selected = dialog.selected_matches()
        if not selected:
            return
        warnings = []
        changed = 0
        for ref, candidate in selected:
            parm = hou.parm(ref.parm_path)
            if parm is None:
                warnings.append(f"Parameter no longer exists: {ref.parm_path}")
                continue
            try:
                if parm.keyframes():
                    warnings.append(f"Animated/expression parameter skipped: {ref.parm_path}")
                    continue
                parm.set(candidate.path_pattern.replace("\\", "/"), follow_parm_reference=False)
                changed += 1
            except Exception as exc:
                warnings.append(f"Could not relink {ref.parm_path}: {exc}")
        if warnings:
            QtWidgets.QMessageBox.warning(
                self,
                "Relink completed with warnings",
                f"Relinked {changed} parameter(s).\n\n" + "\n".join(warnings[:12]),
            )
        elif changed:
            QtWidgets.QMessageBox.information(
                self, "Relink complete", f"Relinked {changed} parameter(s) in the source scene."
            )
        if changed:
            self.scan()

    def collect(self):
        if self.plan is None:
            return
        self._apply_row_selection()
        mode = self.plan.collect_mode
        action_text = {
            "report_only": "write a dependency report",
            "project_only": "create a trimmed HIP without external files",
        }.get(mode, (
            "incrementally update the existing collect and reuse unchanged files"
            if self.plan.incremental_update and self.plan.output_root.exists()
            else f"create a trimmed HIP and copy approximately {human_size(self.plan.total_size_bytes)}"
        ))
        answer = QtWidgets.QMessageBox.question(
            self,
            "Run collector?",
            f"This will {action_text}.\n\nDestination:\n{self.plan.output_root}\n\n"
            "The current source HIP will be restored afterwards when a HIP copy is created.",
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self.setEnabled(False)
        try:
            with hou.InterruptableOperation(
                "Copying project assets",
                "Collecting Houdini project",
                open_interrupt_dialog=True,
            ) as operation:
                result_path = execute_plan(
                    self.plan,
                    progress=lambda fraction, message: operation.updateLongProgress(fraction, message),
                )
            QtWidgets.QMessageBox.information(
                self,
                "Collect complete",
                f"Collector completed successfully:\n{result_path}\n\n"
                f"Copied: {self.plan.copy_stats.get('copied', 0)} • "
                f"Reused: {self.plan.copy_stats.get('reused', 0)}\n\n"
                "Review manifest/collect.log for warnings.",
            )
            self.collect_button.setEnabled(False)
        except hou.OperationInterrupted:
            QtWidgets.QMessageBox.information(self, "Collect cancelled", "The partial collect was removed. The source HIP remains safe.")
        except Exception as exc:
            self._show_error("Collect failed", exc)
        finally:
            self.setEnabled(True)

    def _show_error(self, title, exc):
        detail = traceback.format_exc()
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Critical, title, str(exc), parent=self)
        box.setDetailedText(detail)
        box.exec()

    def closeEvent(self, event):
        global WINDOW
        WINDOW = None
        self.setParent(None)
        super().closeEvent(event)


def show_window():
    global WINDOW
    if WINDOW is not None:
        WINDOW.show()
        WINDOW.raise_()
        WINDOW.activateWindow()
        return WINDOW
    WINDOW = CollectorWindow()
    WINDOW.show()
    return WINDOW
