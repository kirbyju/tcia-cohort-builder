import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from v2_artifacts import (
    BUNDLE_MANIFEST_ASSET,
    INSTALL_STATE_ASSET,
    ensure_bundle_profile,
    install_bundle_profile,
    installed_component,
    load_bundle_installation,
    require_installed_component,
)


def stable_bundle(**component_overrides):
    participant = {
        "database_asset": "participant_inventory.sqlite.gz",
        "release_fingerprint": "participant-fingerprint",
        "sqlite_sha256": "sqlite-hash",
        "gzip_sha256": "gzip-hash",
        "schema_version": 6,
        "profile": "research_core",
        "storage_contract": {
            "clinical_values_storage": "clinical_metadata_detail_artifact"
        },
    }
    participant.update(component_overrides)
    return {
        "artifact": "tcia_metadata_v2_bundle",
        "release_tag": "tcia-metadata-v2-latest",
        "release_channel": "stable",
        "release_contract": "streamlined",
        "release_fingerprint": "bundle-fingerprint",
        "generated_at_utc": "2026-08-19T18:35:20+00:00",
        "schema_version": 2,
        "assets": {
            "participant_inventory.sqlite.gz": {
                "profile": "research_core",
                "sha256": "gzip-hash",
            }
        },
        "profiles": {"research_core": {"assets": ["participant_inventory.sqlite.gz"]}},
        "components": {"participant_inventory": participant},
    }


def write_install(cache: Path, bundle: dict, installed_assets=None):
    (cache / BUNDLE_MANIFEST_ASSET).write_text(json.dumps(bundle), encoding="utf-8")
    state = {
        "artifact": "tcia_metadata_v2_install",
        "release_tag": bundle["release_tag"],
        "release_fingerprint": bundle["release_fingerprint"],
        "installed_profile": "research_core",
        "installed_assets": installed_assets or ["participant_inventory.sqlite.gz"],
    }
    (cache / INSTALL_STATE_ASSET).write_text(json.dumps(state), encoding="utf-8")


class V2ArtifactCacheTests(unittest.TestCase):
    @mock.patch("v2_artifacts.load_bundle_installation")
    @mock.patch("v2_artifacts.installed_component")
    @mock.patch("v2_artifacts.install_bundle_profile")
    def test_complete_research_detail_skips_downloader(
        self, install, installed, load_installation
    ):
        installed.return_value = object()
        load_installation.return_value = mock.Mock(
            directory=Path("/cache"),
            release_tag="tcia-metadata-v2-latest",
            release_fingerprint="current",
        )

        result = ensure_bundle_profile(Path("/skill"), Path("/cache"))

        self.assertEqual(result["status"], "already_installed")
        self.assertEqual(installed.call_count, 5)
        install.assert_not_called()

    @mock.patch("v2_artifacts.subprocess.run")
    def test_research_detail_install_uses_official_bundle_installer(self, run):
        run.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "unchanged",
                    "profile": "research_detail",
                    "release_tag": "tcia-metadata-v2-latest",
                }
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = root / "skill" / "scripts" / "tcia_v2_bundle.py"
            installer.parent.mkdir(parents=True)
            installer.write_text("# test installer\n", encoding="utf-8")
            cache = root / "cache"

            result = install_bundle_profile(
                root / "skill",
                cache,
                profile="research_detail",
                python_executable="/test/python",
            )

            expected_cache = cache.resolve()

        self.assertEqual(result["profile"], "research_detail")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/test/python")
        self.assertEqual(
            command[2:6],
            ["install", "--tag", "tcia-metadata-v2-latest", "--profile"],
        )
        self.assertEqual(command[6], "research_detail")
        self.assertEqual(command[7], "--install-dir")
        self.assertEqual(Path(command[8]), expected_cache)

    @mock.patch("v2_artifacts.subprocess.run")
    def test_failed_startup_install_is_reported(self, run):
        run.return_value = mock.Mock(
            returncode=1,
            stdout="",
            stderr="network unavailable",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = root / "scripts" / "tcia_v2_bundle.py"
            installer.parent.mkdir(parents=True)
            installer.write_text("# test installer\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "network unavailable"):
                install_bundle_profile(root, root / "cache")

    def test_official_install_receipt_exposes_component(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            bundle = stable_bundle()
            write_install(cache, bundle)
            with sqlite3.connect(cache / "participant_inventory.sqlite") as connection:
                connection.execute("CREATE TABLE example (value TEXT)")

            installation = load_bundle_installation(cache)
            component = require_installed_component(cache, "participant_inventory")

            self.assertEqual(installation.release_contract, "streamlined")
            self.assertEqual(installation.installed_profile, "research_core")
            self.assertEqual(component.schema_version, 6)
            self.assertEqual(
                component.manifest, (cache / BUNDLE_MANIFEST_ASSET).resolve()
            )
            self.assertEqual(
                component.storage_contract["clinical_values_storage"],
                "clinical_metadata_detail_artifact",
            )

    def test_preview_release_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            bundle = stable_bundle()
            bundle["release_tag"] = "tcia-metadata-v2-preview"
            write_install(cache, bundle)
            with self.assertRaisesRegex(RuntimeError, "Expected V2 release"):
                load_bundle_installation(cache)

    def test_full_release_contract_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            bundle = stable_bundle()
            bundle["release_contract"] = "full"
            write_install(cache, bundle)

            installation = load_bundle_installation(cache)

            self.assertEqual(installation.release_contract, "full")

    def test_install_receipt_must_match_bundle_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            bundle = stable_bundle()
            write_install(cache, bundle)
            state_path = cache / INSTALL_STATE_ASSET
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["release_fingerprint"] = "stale"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "release_fingerprint"):
                load_bundle_installation(cache)
            self.assertIsNone(installed_component(cache, "participant_inventory"))

    def test_component_must_be_listed_in_installed_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            bundle = stable_bundle()
            write_install(cache, bundle, installed_assets=["tcia_snapshot.sqlite.gz"])
            (cache / "participant_inventory.sqlite").write_bytes(b"present-but-unverified")

            self.assertIsNone(installed_component(cache, "participant_inventory"))

    def test_unsupported_component_schema_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            bundle = stable_bundle(schema_version=8)
            write_install(cache, bundle)
            (cache / "participant_inventory.sqlite").write_bytes(b"database")

            self.assertIsNone(installed_component(cache, "participant_inventory"))
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                require_installed_component(cache, "participant_inventory")

    def test_participant_schema_seven_is_accepted_during_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            bundle = stable_bundle(schema_version=7)
            write_install(cache, bundle)
            with sqlite3.connect(cache / "participant_inventory.sqlite") as connection:
                connection.execute("CREATE TABLE example (value TEXT)")

            self.assertEqual(
                require_installed_component(
                    cache, "participant_inventory"
                ).schema_version,
                7,
            )


if __name__ == "__main__":
    unittest.main()
