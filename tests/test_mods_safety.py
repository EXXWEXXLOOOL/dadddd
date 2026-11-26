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

    def test_oiv_targets_only_mods(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dlc = root / "BMW_M5"
            dlc.mkdir()
            (dlc / "dlc.rpf").write_bytes(b"fake-dlc")
            (dlc / "setup2.xml").write_text("<CDataFileMgr />", encoding="utf-8")
            package_path = root / "install.oiv"

            app.build_openiv_package([(dlc, "BMW_M5")], package_path)

            with zipfile.ZipFile(package_path) as package_zip:
                manifest = ET.fromstring(package_zip.read("assembly.xml"))
                top_level_targets = [
                    (node.text or "").replace("/", "\\")
                    for node in manifest.findall("./content/add")
                ]
                self.assertEqual(len(top_level_targets), 2)
                self.assertTrue(all(path.lower().startswith("mods\\") for path in top_level_targets))

                archives = manifest.findall("./content/archive")
                self.assertEqual(len(archives), 1)
                self.assertEqual(
                    archives[0].attrib["path"].lower(),
                    "mods\\update\\update.rpf",
                )
                xml_command = archives[0].find("./xml")
                self.assertIsNotNone(xml_command)
                self.assertEqual(xml_command.attrib["path"].lower(), "common\\data\\dlclist.xml")

                xml_adds = xml_command.findall("./add")
                self.assertEqual(len(xml_adds), 1)
                self.assertEqual(
                    xml_adds[0].findtext("./Item"),
                    "dlcpacks:/BMW_M5/",
                )

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
