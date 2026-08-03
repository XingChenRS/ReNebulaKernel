import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sync_google_gki  # noqa: E402


SHA1_A = "a" * 40
SHA1_B = "b" * 40
SHA1_C = "c" * 40
SHA256 = "d" * 64
LOCK_ID = "gki.android14-6.1.test"


def valid_lock():
    return {
        "id": LOCK_ID,
        "family_id": "android14-6.1",
        "release_id": "android14-6.1-test",
        "source_mode": "manifest-superproject-v1",
        "manifest": {
            "url": "https://android.googlesource.com/kernel/manifest",
            "commit": SHA1_A,
            "file": "default.xml",
            "sha256": SHA256,
        },
        "superproject": {
            "url": "https://android.googlesource.com/kernel/superproject",
            "commit": SHA1_B,
            "manifest_ref": "refs/heads/common-android14-6.1-lts",
        },
        "materialization": {
            "expected_project_count": 2,
            "required_paths": ["build/kernel", "common"],
        },
        "common": {"path": "common", "commit": SHA1_C},
        "version": {"expected_base_release": "6.1.175"},
    }


class SourceLockTests(unittest.TestCase):
    def write_lock_document(self, directory: Path, lock):
        path = directory / "sources.lock.json"
        path.write_text(
            json.dumps({"schema": 2, "locks": {LOCK_ID: lock}}), encoding="utf-8"
        )
        return path

    def test_valid_schema2_lock_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = self.write_lock_document(Path(temporary), valid_lock())
            loaded = sync_google_gki.load_lock(lock_path, LOCK_ID)

        self.assertEqual(loaded["id"], LOCK_ID)
        self.assertEqual(loaded["common"]["path"], "common")

    def test_non_google_source_url_is_rejected(self):
        lock = valid_lock()
        lock["manifest"]["url"] = "https://example.invalid/kernel/manifest"

        with self.assertRaises(sync_google_gki.LockError):
            sync_google_gki.validate_lock(lock, LOCK_ID)

    def test_common_must_be_a_required_materialized_project(self):
        lock = valid_lock()
        lock["materialization"]["required_paths"] = ["build/kernel"]

        with self.assertRaises(sync_google_gki.LockError):
            sync_google_gki.validate_lock(lock, LOCK_ID)

    def test_manifest_accepts_the_official_superproject_and_root_linkfile(self):
        projects, linkfiles = sync_google_gki.parse_manifest(
            b"""<manifest>
  <superproject name="kernel/superproject" remote="aosp" />
  <project name="kernel/common" path="common">
    <linkfile src="." dest="Makefile" />
  </project>
</manifest>"""
        )

        self.assertEqual(
            projects,
            [
                {
                    "path": "common",
                    "name": "kernel/common",
                    "url": "https://android.googlesource.com/kernel/common",
                }
            ],
        )
        self.assertEqual(linkfiles, [{"source": "common", "dest": "Makefile"}])

    def test_manifest_rejects_an_unexpected_superproject(self):
        with self.assertRaisesRegex(RuntimeError, "unexpected superproject"):
            sync_google_gki.parse_manifest(
                b"""<manifest>
  <superproject name="kernel/not-the-superproject" />
  <project name="kernel/common" path="common" />
</manifest>"""
            )
