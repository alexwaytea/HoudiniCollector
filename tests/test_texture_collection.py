"""Run with Python (hou stub) or hython (real parameter relinking)."""
import ast
from pathlib import Path
import sys
import tempfile
import types
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / "houdini_collector" / "python3.11libs"
sys.path.insert(0, str(PACKAGE))
try:
    import hou
    REAL_HOU = hasattr(hou, "StringParmTemplate")
except ImportError:
    sys.modules["hou"] = types.ModuleType("hou")
    REAL_HOU = False

from houdini_collector import core
from houdini_collector.model import CollectPlan, FileReference
from houdini_collector.paths import normalized_key, tokenized_glob


class TextureCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "collected"

    def source(self, relative, content=b"texture"):
        path = self.root / "library" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def ref(self, asset, files, raw=None, **kwargs):
        return FileReference(
            parm_path=f"/mat/{asset}/file", node_path=f"/mat/{asset}",
            raw_path=raw or str(files[0]), evaluated_path=str(files[0]),
            category=kwargs.pop("category", "material"), asset_name=asset,
            source_files=tuple(files), exists=True, **kwargs,
        )

    def plan(self, refs):
        core.assign_destinations(refs, self.out)
        return CollectPlan(self.root / "source.hip", self.out, self.out / "scene.hip",
                           [], set(), refs, [])

    def assert_linked_files(self, refs):
        for ref in refs:
            pattern = ref.destination_pattern.replace("$HIP", str(self.out))
            glob_pattern, tokenized = tokenized_glob(pattern)
            if tokenized:
                import glob
                matched = {normalized_key(Path(path)) for path in glob.glob(glob_pattern)}
                self.assertTrue({normalized_key(p) for p in ref.destination_files} <= matched)
            else:
                self.assertTrue(Path(pattern).is_file(), pattern)

    def test_shared_copper_and_scratches_copy_once(self):
        copper = self.source("Copper_Dirty_01/basecolor.png")
        rough = self.source("Copper_Dirty_01/roughness.png")
        scratches = self.source("Imperfections/scratches.jpg")
        refs = [self.ref(asset, [path]) for asset in ("boxes_metal", "logo")
                for path in (copper, rough, scratches)]
        plan = self.plan(refs)
        core._copy_plan_files(plan, self.out)
        self.assertEqual(plan.copy_stats["copied"], 3)
        self.assertEqual(len(list(self.out.rglob("*.*"))), 3)
        self.assertEqual(refs[0].destination_pattern, refs[3].destination_pattern)
        self.assertIn("/tex/mats/Copper_Dirty_01/", refs[0].destination_pattern)
        self.assertEqual([r.asset_name for r in refs], ["boxes_metal"] * 3 + ["logo"] * 3)
        self.assert_linked_files(refs)

    def test_same_filename_different_sources_kept_separate(self):
        a = self.source("vendorA/Metal/base.png", b"A")
        b = self.source("vendorB/Metal/base.png", b"B")
        refs = [self.ref("one", [a]), self.ref("two", [b])]
        plan = self.plan(refs)
        core._copy_plan_files(plan, self.out)
        self.assertNotEqual(refs[0].destination_files, refs[1].destination_files)
        self.assertEqual([r.destination_files[0].read_bytes() for r in refs], [b"A", b"B"])
        before = {r.parm_path: r.destination_pattern for r in refs}
        core.assign_destinations(list(reversed(refs)), self.out)
        self.assertEqual(before, {r.parm_path: r.destination_pattern for r in refs})

    def test_udim_overlapping_sets_and_single_tile(self):
        files = [self.source(f"Skin/color.{tile}.exr") for tile in (1001, 1002, 1003)]
        refs = [self.ref("one", files[:2], str(files[0].parent / "color.<UDIM>.exr")),
                self.ref("two", files[1:], str(files[0].parent / "color.%(UDIM)d.exr")),
                self.ref("three", files[1:2])]
        plan = self.plan(refs)
        core._copy_plan_files(plan, self.out)
        self.assertEqual(plan.copy_stats["copied"], 3)
        self.assertEqual(refs[0].destination_files[1], refs[1].destination_files[0])
        self.assertEqual(refs[2].destination_files[0], refs[1].destination_files[0])
        self.assert_linked_files(refs)

    def test_frame_sequences_keep_tokens(self):
        files = [self.source(f"Animation/smoke.{frame:04}.exr") for frame in (1, 2)]
        refs = [self.ref(str(i), files, str(files[0].parent / f"smoke.{token}.exr"))
                for i, token in enumerate(("$F4", "${F4}", "%04d", "####"))]
        plan = self.plan(refs)
        core._copy_plan_files(plan, self.out)
        self.assertEqual(plan.copy_stats["copied"], 2)
        self.assert_linked_files(refs)

    def test_unchecked_reference_does_not_prevent_copy(self):
        path = self.source("Metal/base.png")
        refs = [self.ref("one", [path], enabled=False), self.ref("two", [path])]
        plan = self.plan(refs)
        core._copy_plan_files(plan, self.out)
        self.assertEqual(plan.copy_stats["copied"], 1)
        self.assert_linked_files(refs)

    def test_normalized_paths_and_environment_spelling(self):
        path = self.source("Metal/base.png")
        alias = path.parent / ".." / "Metal" / "base.png"
        refs = [self.ref("one", [path], "$LIB/Metal/base.png"), self.ref("two", [alias])]
        plan = self.plan(refs)
        core._copy_plan_files(plan, self.out)
        self.assertEqual(plan.copy_stats["copied"], 1)
        self.assertEqual(refs[0].destination_pattern, refs[1].destination_pattern)

    def test_incremental_reuses_unique_files(self):
        path = self.source("Metal/base.png")
        plan = self.plan([self.ref("one", [path]), self.ref("two", [path])])
        core._copy_plan_files(plan, self.out)
        core._copy_plan_files(plan, self.out, incremental=True)
        self.assertEqual(plan.copy_stats, {"copied": 0, "reused": 1})

    def test_package_layout_preserved(self):
        model = self.source("QuixelRock/rock.fbx")
        texture = self.source("QuixelRock/textures/base.png")
        refs = [self.ref("QuixelRock", [model], category="megascans", package_root=model.parent),
                self.ref("one", [texture]), self.ref("two", [texture])]
        plan = self.plan(refs)
        core._copy_plan_files(plan, self.out)
        self.assertEqual(plan.copy_stats["copied"], 2)
        self.assertEqual(refs[1].destination_pattern, "$HIP/geo/models/QuixelRock/textures/base.png")
        self.assert_linked_files(refs)

    def test_model_owned_and_cross_category_shared(self):
        path = self.source("CarPaint/base.png")
        refs = [self.ref("Car", [path], category="asset_texture"),
                self.ref("Car", [path], category="asset_texture")]
        self.plan(refs)
        self.assertEqual(refs[0].destination_pattern, "$HIP/geo/models/Car/textures/base.png")
        refs.append(self.ref("other", [path], category="texture"))
        plan = self.plan(refs)
        core._copy_plan_files(plan, self.out)
        self.assertEqual(plan.copy_stats["copied"], 1)
        self.assertEqual(len({r.destination_pattern for r in refs}), 1)

    def test_manual_group_override_still_shared_and_resettable(self):
        path = self.source("Metal/base.png")
        refs = [self.ref("Custom", [path], asset_overridden=True), self.ref("logo", [path])]
        self.plan(refs)
        self.assertEqual(refs[0].destination_pattern, "$HIP/tex/mats/Custom/base.png")
        self.assertEqual(refs[0].destination_pattern, refs[1].destination_pattern)
        refs[0].asset_overridden = False
        core.assign_destinations(refs, self.out)
        self.assertEqual(refs[0].destination_pattern, "$HIP/tex/mats/Metal/base.png")

    def test_missing_and_output_references_unchanged(self):
        path = self.source("Metal/base.png")
        missing = self.ref("missing", [path])
        missing.source_files = ()
        missing.exists = False
        output = self.ref("render", [path], is_output=True, enabled=False)
        self.plan([missing, output])
        self.assertEqual(missing.destination_files, ())
        self.assertEqual(output.destination_pattern, "$HIP/tex/mats/render/base.png")

    def test_all_modules_parse(self):
        for path in PACKAGE.rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    @unittest.skipUnless(REAL_HOU, "Requires hython")
    def test_real_houdini_parameter_relink(self):
        path = self.source("Metal/base.png")
        refs = []
        for name in ("collector_test_one", "collector_test_two"):
            node = hou.node("/obj").createNode("geo", name)
            self.addCleanup(node.destroy)
            node.addSpareParmTuple(hou.StringParmTemplate("texture", "Texture", 1))
            node.parm("texture").set(str(path))
            ref = self.ref(name, [path])
            ref.parm_path = node.parm("texture").path()
            refs.append(ref)
        plan = self.plan(refs)
        core._copy_plan_files(plan, self.out)
        self.assertEqual(core._relink_parameters(plan), [])
        self.assertEqual(hou.parm(refs[0].parm_path).unexpandedString(), refs[1].destination_pattern)
        self.assertEqual(hou.parm(refs[1].parm_path).unexpandedString(), refs[0].destination_pattern)


if __name__ == "__main__":
    print("Runtime:", sys.version, "| Real Houdini:", REAL_HOU)
    unittest.main(verbosity=2)
