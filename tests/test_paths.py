import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "houdini_collector" / "python3.11libs"))

from houdini_collector.paths import (  # noqa: E402
    detect_megascans_root,
    human_size,
    is_renderer_hda,
    resolve_files,
    safe_name,
    tokenized_glob,
)
from houdini_collector.model import CollectOptions, FileReference  # noqa: E402


class PathTests(unittest.TestCase):
    def test_tokens(self):
        pattern, found = tokenized_glob("cache.$F4.bgeo.sc")
        self.assertTrue(found)
        self.assertEqual(pattern, "cache.[0-9][0-9][0-9][0-9].bgeo.sc")
        pattern, found = tokenized_glob("rock.<UDIM>.exr")
        self.assertTrue(found)
        self.assertEqual(pattern, "rock.[0-9][0-9][0-9][0-9].exr")

    def test_safe_name(self):
        self.assertEqual(safe_name("My Asset: 01"), "My_Asset_01")
        self.assertEqual(safe_name("***", "fallback"), "fallback")

    def test_megascans_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "Quixel_Rock"
            root.mkdir()
            model = root / "rock.fbx"
            model.touch()
            (root / "rock.json").touch()
            (root / "rock_albedo.jpg").touch()
            (root / "rock_normal.jpg").touch()
            self.assertEqual(detect_megascans_root(model), root)

    def test_human_size(self):
        self.assertEqual(human_size(1024), "1.0 KB")

    def test_hda_collection_is_disabled_by_default(self):
        self.assertFalse(CollectOptions(Path("collect")).include_hdas)

    def test_parameter_name_does_not_repeat_node_path(self):
        ref = FileReference(
            parm_path="/obj/geo1/file1/file",
            node_path="/obj/geo1/file1",
            raw_path="model.fbx",
            evaluated_path="model.fbx",
        )
        self.assertEqual(ref.parm_name, "file")

    def test_resolve_houdini_sequence(self):
        with tempfile.TemporaryDirectory() as folder:
            old = os.environ.get("HC_TEST_ROOT")
            os.environ["HC_TEST_ROOT"] = folder
            try:
                Path(folder, "cache.0001.bgeo.sc").touch()
                Path(folder, "cache.0002.bgeo.sc").touch()
                result = resolve_files("$HC_TEST_ROOT/cache.$F4.bgeo.sc")
                self.assertEqual(len(result), 2)
            finally:
                if old is None:
                    os.environ.pop("HC_TEST_ROOT", None)
                else:
                    os.environ["HC_TEST_ROOT"] = old

    def test_redshift_hdas_are_vendor_dependencies(self):
        self.assertTrue(is_renderer_hda(
            r"C:\ProgramData\Redshift\Plugins\Houdini\21.0.671\otls\Redshift4Houdini.hda",
            "Vop/redshift::TextureSampler",
        ))
        self.assertTrue(is_renderer_hda(
            "/opt/maxon/redshift/redshift4houdini.hda",
            "Object/rslightdome::2.0",
        ))
        self.assertFalse(is_renderer_hda(
            "D:/studio/hda/megascans_asset.hda",
            "Sop/studio::megascans_asset::1.0",
        ))
        self.assertFalse(is_renderer_hda(
            "D:/studio/hda/redshift_wrapper.hda",
            "Sop/studio::redshift_wrapper::1.0",
        ))


if __name__ == "__main__":
    unittest.main()
