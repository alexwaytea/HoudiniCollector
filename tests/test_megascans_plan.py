import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "houdini_collector" / "python3.11libs"))
sys.modules.setdefault("hou", types.SimpleNamespace())

import houdini_collector.core as core  # noqa: E402
from houdini_collector.core import (  # noqa: E402
    CollectorError,
    _assign_asset_texture_owners,
    _assign_destinations,
    _copy_file,
    assign_destinations,
    execute_plan,
    find_relink_candidates,
)
from houdini_collector.model import CollectPlan, FileReference  # noqa: E402


class MegascansPlanTests(unittest.TestCase):
    def test_only_referenced_package_files_are_planned(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "Megascans_Rock"
            package.mkdir()
            model = package / "rock.fbx"
            used_texture = package / "rock_albedo.jpg"
            unused_texture = package / "rock_roughness.jpg"
            metadata = package / "rock.json"
            for path in (model, used_texture, unused_texture, metadata):
                path.touch()

            model_ref = FileReference(
                "/obj/rock/file", "/obj/rock", str(model), str(model),
                category="megascans", asset_name=package.name,
                source_files=(model,), exists=True, package_root=package,
            )
            texture_ref = FileReference(
                "/mat/rock/tex", "/mat/rock", str(used_texture), str(used_texture),
                category="material", asset_name="rock",
                source_files=(used_texture,), exists=True,
            )
            output = Path(folder) / "collect"
            _assign_destinations([model_ref, texture_ref], output)

            planned_sources = set(model_ref.source_files + texture_ref.source_files)
            self.assertEqual(planned_sources, {model, used_texture})
            self.assertNotIn(unused_texture, planned_sources)
            self.assertNotIn(metadata, planned_sources)
            self.assertIn("geo/models/Megascans_Rock", texture_ref.destination_pattern)

    def test_model_owned_textures_are_grouped_without_material_subfolder(self):
        with tempfile.TemporaryDirectory() as folder:
            asset = Path(folder) / "camera"
            textures = asset / "textures"
            textures.mkdir(parents=True)
            model = asset / "camera.fbx"
            normal = textures / "aiStandard1SG_normal.png"
            model.touch()
            normal.touch()

            model_ref = FileReference(
                "/obj/geo1/file1/file", "/obj/geo1/file1", str(model), str(model),
                category="model", asset_name="camera", source_files=(model,), exists=True,
            )
            texture_ref = FileReference(
                "/obj/geo1/matnet1/camera/normal/tex0",
                "/obj/geo1/matnet1/camera/normal",
                str(normal), str(normal), category="texture", asset_name="textures",
                source_files=(normal,), exists=True,
            )
            output = Path(folder) / "collect"
            refs = [model_ref, texture_ref]
            _assign_asset_texture_owners(refs)
            _assign_destinations(refs, output)

            self.assertEqual(texture_ref.category, "asset_texture")
            self.assertEqual(
                texture_ref.destination_pattern,
                "$HIP/geo/models/camera/textures/aiStandard1SG_normal.png",
            )

    def test_ambiguous_shared_texture_stays_in_materials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            texture = root / "shared" / "metal.png"
            texture.parent.mkdir()
            texture.touch()
            refs = []
            for name in ("chair", "table"):
                model = root / name / f"{name}.fbx"
                model.parent.mkdir()
                model.touch()
                refs.append(FileReference(
                    f"/obj/set/{name}/file", f"/obj/set/{name}", str(model), str(model),
                    category="model", asset_name=name, source_files=(model,), exists=True,
                ))
            material_ref = FileReference(
                "/mat/metal/image/file", "/mat/metal/image", str(texture), str(texture),
                category="material", asset_name="metal", source_files=(texture,), exists=True,
            )
            refs.append(material_ref)

            _assign_asset_texture_owners(refs)
            self.assertEqual(material_ref.category, "material")

    def test_missing_sequence_relink_preserves_token(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            found = root / "library" / "smoke"
            found.mkdir(parents=True)
            for tile in (1001, 1002, 1003):
                (found / f"smoke.{tile}.exr").touch()
            ref = FileReference(
                "/mat/smoke/image/file", "/mat/smoke/image",
                "Z:/missing/smoke.<UDIM>.exr", "Z:/missing/smoke.1001.exr",
                category="material", asset_name="smoke", exists=False,
            )
            candidates = find_relink_candidates(ref, (root,))
            self.assertEqual(len(candidates), 1)
            self.assertTrue(candidates[0].path_pattern.endswith("smoke.<UDIM>.exr"))
            self.assertEqual(len(candidates[0].source_files), 3)

    def test_report_only_writes_manifest_without_hip_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "report"
            source = root / "scene.hip"
            plan = CollectPlan(
                source_hip=source,
                output_root=output,
                collected_hip=output / "scene_collect.hip",
                selected_nodes=["/obj/geo1"],
                kept_nodes={"/obj/geo1"},
                references=[],
                hdas=[],
                collect_mode="report_only",
            )
            old_version = getattr(core.hou, "applicationVersionString", None)
            core.hou.applicationVersionString = lambda: "21.0.671"
            try:
                result = execute_plan(plan)
            finally:
                if old_version is None:
                    del core.hou.applicationVersionString
                else:
                    core.hou.applicationVersionString = old_version
            self.assertEqual(result, output / "manifest" / "collect.json")
            self.assertFalse((output / "scene_collect.hip").exists())
            data = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(data["collect_mode"], "report_only")
            self.assertIsNone(data["collected_hip"])

    def test_manual_asset_override_rebuilds_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            texture = root / "normal.png"
            texture.touch()
            ref = FileReference(
                "/mat/car/normal/file", "/mat/car/normal", str(texture), str(texture),
                category="asset_texture", asset_name="car", source_files=(texture,), exists=True,
                auto_asset_name="car",
            )
            ref.asset_name = "camera"
            ref.asset_overridden = True
            assign_destinations([ref], root / "collect")
            self.assertEqual(ref.destination_pattern, "$HIP/geo/models/camera/textures/normal.png")

    def test_incremental_copy_reuses_unchanged_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.bin"
            destination = root / "collect" / "source.bin"
            source.write_bytes(b"first")
            self.assertTrue(_copy_file(source, destination, incremental=True))
            self.assertFalse(_copy_file(source, destination, incremental=True))
            source.write_bytes(b"changed content")
            self.assertTrue(_copy_file(source, destination, incremental=True))
            self.assertEqual(destination.read_bytes(), b"changed content")

    def test_incremental_update_replaces_hip_and_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "scene.hip"
            source.write_bytes(b"source")
            output = root / "existing_collect"
            output.mkdir()
            collected = output / "scene_collect.hip"
            collected.write_bytes(b"old")
            (output / "manifest").mkdir()
            (output / "manifest" / "collect.json").write_text(
                json.dumps({"tool": "Houdini Collector", "collected_hip": collected.name}),
                encoding="utf-8",
            )

            class FakeHipFile:
                current = str(source)

                @classmethod
                def save(cls, path, save_to_recent_files=False):
                    Path(path).write_bytes(b"new collected hip")
                    cls.current = str(path)

                @classmethod
                def path(cls):
                    return cls.current

                @classmethod
                def load(cls, path, **_kwargs):
                    cls.current = str(path)

            plan = CollectPlan(
                source_hip=source, output_root=output, collected_hip=collected,
                selected_nodes=["/obj/geo1"], kept_nodes={"/obj/geo1"},
                references=[], hdas=[], collect_mode="selected", incremental_update=True,
            )
            old_values = {
                name: getattr(core.hou, name, None)
                for name in ("hipFile", "node", "applicationVersionString")
            }
            core.hou.hipFile = FakeHipFile
            core.hou.node = lambda _path: None
            core.hou.applicationVersionString = lambda: "21.0.671"
            try:
                result = execute_plan(plan)
            finally:
                for name, value in old_values.items():
                    if value is None:
                        delattr(core.hou, name)
                    else:
                        setattr(core.hou, name, value)
            self.assertEqual(result, collected)
            self.assertEqual(collected.read_bytes(), b"new collected hip")
            manifest = json.loads((output / "manifest" / "collect.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["incremental_update"])

    def test_incremental_update_rejects_arbitrary_existing_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "scene.hip"
            source.touch()
            output = root / "not_a_collect"
            output.mkdir()
            plan = CollectPlan(
                source_hip=source, output_root=output,
                collected_hip=output / "scene_collect.hip",
                selected_nodes=[], kept_nodes=set(), references=[], hdas=[],
                collect_mode="selected", incremental_update=True,
            )
            with self.assertRaises(CollectorError):
                execute_plan(plan)


if __name__ == "__main__":
    unittest.main()
