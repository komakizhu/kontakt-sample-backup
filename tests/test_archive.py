import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "archive.py"


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "Source"
        self.destination = self.root / "Destination"
        self.source.mkdir()
        self.destination.mkdir()
        (self.source / "Straight" / "Piano").mkdir(parents=True)
        (self.source / "Layered" / "Strings" / "Violin").mkdir(parents=True)
        (self.source / "Straight" / "Piano" / "sample.wav").write_bytes(b"sample" * 100)
        (self.source / "Layered" / "Strings" / "Violin" / "note.nki").write_bytes(b"note" * 100)
        self.rules = self.root / "rules.json"
        self.rules.write_text(json.dumps({"rules": [
            {"path": "Straight", "unit_depth": 1},
            {"path": "Layered", "unit_depth": 2}], "symlinks": "reject"}), encoding="utf-8")
        self.plan = self.root / "plan.json"

    def command(self, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                text=True, capture_output=True)
        if success and result.returncode:
            self.fail(result.stdout + result.stderr)
        return result

    def make_plan(self):
        return self.command("plan", "--source", self.source, "--destination", self.destination,
                            "--rules", self.rules, "--output", self.plan)

    def test_scan_plan_run_repeat_and_update(self):
        scan = self.command("scan", "--source", self.source, "--depth", 3)
        self.assertIn("Violin", scan.stdout)
        self.make_plan()
        planned = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(planned["counts"]["new"], 2)
        self.command("run", "--plan", self.plan)
        manifest = json.loads((self.destination / "sample_archive_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["entries"]), 2)
        for versions in manifest["entries"].values():
            package = self.destination / versions[0]["package_relpath"]
            self.assertTrue((package / "backup.zip").is_file())
            self.assertEqual(versions[0]["parts"], ["backup.zip"])
        piano = self.destination / manifest["entries"]["Straight/Piano"][0]["package_relpath"]
        restored = self.root / "Restored" / "Straight"
        restored.mkdir(parents=True)
        result = subprocess.run(["7zz", "x", "-y", str(piano / "backup.zip"), "-o" + str(restored)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((restored / "Piano" / "sample.wav").read_bytes(), b"sample" * 100)
        self.make_plan()
        planned = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(planned["counts"]["skip"], 2)
        self.command("run", "--plan", self.plan)
        (self.source / "Straight" / "Piano" / "new.wav").write_bytes(b"changed")
        self.make_plan()
        planned = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(planned["counts"]["changed"], 1)
        self.assertEqual(planned["counts"]["skip"], 1)
        self.command("run", "--plan", self.plan)
        manifest = json.loads((self.destination / "sample_archive_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["entries"]["Straight/Piano"]), 2)

    def test_plan_change_and_stage_resume(self):
        self.make_plan()
        (self.source / "Straight" / "Piano" / "late.wav").write_bytes(b"late")
        result = self.command("run", "--plan", self.plan, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed since plan", result.stderr)
        self.make_plan()
        self.command("run", "--plan", self.plan)
        manifest_path = self.destination / "sample_archive_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        package = self.destination / manifest["entries"]["Straight/Piano"][0]["package_relpath"]
        staged = self.destination / ".staging" / package.name
        staged.parent.mkdir(exist_ok=True)
        shutil.move(str(package), str(staged))
        manifest["entries"].pop("Straight/Piano")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.command("run", "--plan", self.plan)
        self.assertTrue(package.is_dir())
        self.assertFalse(staged.exists())

    def test_uncovered_file_and_symlink_rejected(self):
        (self.source / "loose.txt").write_text("important", encoding="utf-8")
        result = self.command("plan", "--source", self.source, "--destination", self.destination,
                              "--rules", self.rules, "--output", self.plan, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncovered", result.stderr)
        (self.source / "loose.txt").unlink()
        (self.source / "Straight" / "Piano" / "outside").symlink_to(self.rules)
        result = self.command("plan", "--source", self.source, "--destination", self.destination,
                              "--rules", self.rules, "--output", self.plan, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Symlink", result.stderr)

    def test_legacy_destination_blocks_run(self):
        (self.destination / "ARCHIVE_LOG.tsv").write_text("legacy", encoding="utf-8")
        self.make_plan()
        planned = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertTrue(planned["legacy_archive_detected"])
        result = self.command("run", "--plan", self.plan, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Legacy archive", result.stderr)

    def test_legacy_import_matches_content_and_skips_old_zip(self):
        old_folder = self.destination / "01_Straight"
        old_folder.mkdir()
        old_zip = old_folder / "Piano_2026-09-21.zip"
        result = subprocess.run(["zip", "-q", "-r", str(old_zip), "Piano"],
                                cwd=self.source / "Straight", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        old_folder_2 = self.destination / "02_Layered"
        old_folder_2.mkdir()
        old_zip_2 = old_folder_2 / "Violin_2026-09-21.zip"
        result = subprocess.run(["zip", "-q", "-r", str(old_zip_2), "Violin"],
                                cwd=self.source / "Layered" / "Strings", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.destination / "ARCHIVE_LOG.tsv"
        log.write_text("date\tcategory\tlibrary\tsource_bytes\tarchive_bytes\tarchive_path\n" +
                       "2026-09-21\tStraight\tPiano\t0\t0\t" + str(old_zip) + "\n" +
                       "2026-09-21\tLayered/Strings\tViolin\t0\t0\t" + str(old_zip_2) + "\n",
                       encoding="utf-8")
        self.make_plan()
        self.command("adopt-legacy", "--plan", self.plan)
        self.make_plan()
        planned = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertFalse(planned["legacy_archive_detected"])
        self.assertEqual(planned["counts"]["skip"], 2)
        self.command("run", "--plan", self.plan)
        self.assertFalse((self.destination / "packages").exists())

    def test_legacy_import_does_not_adopt_changed_source(self):
        old_folder = self.destination / "01_Straight"
        old_folder.mkdir()
        old_zip = old_folder / "Piano_2026-09-21.zip"
        result = subprocess.run(["zip", "-q", "-r", str(old_zip), "Piano"],
                                cwd=self.source / "Straight", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.source / "Straight" / "Piano" / "sample.wav").write_bytes(b"new sample")
        log = self.destination / "ARCHIVE_LOG.tsv"
        log.write_text("date\tcategory\tlibrary\tsource_bytes\tarchive_bytes\tarchive_path\n" +
                       "2026-09-21\tStraight\tPiano\t0\t0\t" + str(old_zip) + "\n",
                       encoding="utf-8")
        self.make_plan()
        self.command("adopt-legacy", "--plan", self.plan)
        self.make_plan()
        planned = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(planned["counts"]["new"], 2)
        manifest = json.loads((self.destination / "sample_archive_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["legacy_unmatched"]), 2)


if __name__ == "__main__":
    unittest.main()
