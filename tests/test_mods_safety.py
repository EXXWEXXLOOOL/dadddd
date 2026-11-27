from __future__ import annotations

import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import app


class ModsOnlyPackageTests(unittest.TestCase):
    def test_update_rpf_is_copied_and_backed_up_inside_mods(self):
        with tempfile.TemporaryDirectory() as temp:
            game = Path(temp) / "Grand Theft Auto V"
            original = game / "update" / "update.rpf"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"original-rpf-bytes")

            mods_archive, copied = app.ensure_mods_update_rpf(game)
            self.assertTrue(copied)
            self.assertEqual(mods_archive.read_bytes(), b"original-rpf-bytes")
            self.assertEqual(original.read_bytes(), b"original-rpf-bytes")

            backup = app.backup_mods_update_rpf(mods_archive, game / "mods")
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_bytes(), b"original-rpf-bytes")
            self.assertTrue(backup.resolve().is_relative_to((game / "mods").resolve()))

    def test_oiv_uses_game_relative_paths_for_openiv_mods_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dlc = root / "BMW_M5"
            dlc.mkdir()
            (dlc / "dlc.rpf").write_bytes(b"fake-dlc")
            (dlc / "setup2.xml").write_text("<CDataFileMgr />", encoding="utf-8")
            package_path = root / "install.oiv"

            app.build_openiv_package([(dlc, "BMW_M5")], package_path)

            with zipfile.ZipFile(package_path) as package_zip:
                assembly_bytes = package_zip.read("assembly.xml")
                assembly_text = assembly_bytes.decode("utf-8")
                self.assertIn('version="2.2"', assembly_text)
                self.assertIn('footerLink="https://github.com/EXXWEXXLOOOL/dadddd"', assembly_text)
                manifest = ET.fromstring(assembly_bytes)
                top_level_targets = [
                    (node.text or "").replace("/", "\\")
                    for node in manifest.findall("./content/add")
                ]
                self.assertEqual(len(top_level_targets), 2)
                self.assertTrue(all(path.lower().startswith("update\\x64\\dlcpacks\\") for path in top_level_targets))
                package_members = set(package_zip.namelist())
                package_sources = [
                    node.attrib["source"].replace("\\\\", "/")
                    for node in manifest.findall("./content/add")
                ]
                self.assertTrue(
                    all(f"content/{source}" in package_members for source in package_sources)
                )

                archives = manifest.findall("./content/archive")
                self.assertEqual(len(archives), 1)
                self.assertEqual(
                    archives[0].attrib["path"].lower(),
                    "update\\update.rpf",
                )
                xml_command = archives[0].find("./xml")
                self.assertIsNotNone(xml_command)
                self.assertEqual(xml_command.attrib["path"].lower(), "common\\data\\dlclist.xml")

                xml_adds = xml_command.findall("./add")
                self.assertEqual(len(xml_adds), 1)
                self.assertEqual(xml_adds[0].attrib.get("append"), "Last")
                self.assertEqual(
                    xml_adds[0].findtext("./Item"),
                    "dlcpacks:/BMW_M5/",
                )

    def test_enhanced_game_is_blocked_before_modifications(self):
        with tempfile.TemporaryDirectory() as temp:
            game = Path(temp)
            (game / "GTA5_Enhanced.exe").write_bytes(b"placeholder")
            with self.assertRaises(RuntimeError):
                app.validate_game_edition(game)

    def test_legacy_game_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            game = Path(temp)
            (game / "GTA5.exe").write_bytes(b"placeholder")
            self.assertEqual(app.validate_game_edition(game), "Legacy")

    def test_enhanced_marker_takes_priority_if_both_executables_exist(self):
        with tempfile.TemporaryDirectory() as temp:
            game = Path(temp)
            (game / "GTA5.exe").write_bytes(b"placeholder")
            (game / "GTA5_Enhanced.exe").write_bytes(b"placeholder")
            with self.assertRaises(RuntimeError):
                app.validate_game_edition(game)

    def test_redirected_mods_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "mods_elsewhere"
            target.mkdir()
            mods = root / "mods"
            try:
                mods.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Directory symlinks are unavailable on this runner.")
            with self.assertRaises(PermissionError):
                app.ensure_mods_update_rpf(root)

    def test_path_guard_rejects_outside_mods(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mods = root / "mods"
            mods.mkdir()
            outside = root / "update.rpf"
            with self.assertRaises(PermissionError):
                app.ensure_inside_mods(outside, mods)


if __name__ == "__main__":
    unittest.main()
