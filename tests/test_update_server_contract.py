from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UpdateServerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (ROOT / "update_server.sh").read_text(encoding="utf-8")

    def test_public_script_has_no_host_specific_defaults(self) -> None:
        self.assertNotIn("/home/exouser", self.script)
        self.assertNotIn("duckdns.org", self.script)
        self.assertIn('COHORT_HEALTH_URL="${TCIA_COHORT_HEALTH_URL:-}"', self.script)
        self.assertIn('MCP_PUBLIC_URL="${TCIA_MCP_PUBLIC_URL:-}"', self.script)

    def test_server_dependencies_are_hash_locked(self) -> None:
        self.assertIn("--require-hashes --requirement", self.script)
        self.assertIn("requirements-server.lock", self.script)
        self.assertNotIn('$QUERY_ROOT/mcp_server/requirements.txt', self.script)
        self.assertIn('"$MCP_PYTHON" -m pip check', self.script)

    def test_code_only_mode_does_not_enter_artifact_install_branch(self) -> None:
        self.assertIn('elif [[ ${1:-} == "--code-only" ]]', self.script)
        self.assertIn('if [[ "$MODE" == "code-only" ]]; then', self.script)
        self.assertIn(
            "reusing the active validated V2 bundle without fetching or installing artifacts",
            self.script,
        )

    def test_protocol_revision_is_selected_after_dependency_install(self) -> None:
        install = self.script.index('log "Installing MCP/REST dependencies"')
        select = self.script.index("set_mcp_protocol_version", install)
        tests = self.script.index('if [[ "$RUN_TESTS" == "1" ]]', select)
        self.assertLess(install, select)
        self.assertLess(select, tests)


if __name__ == "__main__":
    unittest.main()
