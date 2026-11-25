from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UpdateServerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (ROOT / "update_server.sh").read_text(encoding="utf-8")

    def run_heartbeat_fixture(self, body: str) -> subprocess.CompletedProcess[str]:
        functions = self.script[
            self.script.index("stop_heartbeat() {"):self.script.index("cleanup() {")
        ]
        return subprocess.run(
            ["bash", "-c", """
set -Eeuo pipefail
HEARTBEAT_PID=""
PROGRESS_HEARTBEAT_SECONDS=0.01
NEW_INSTALL_DIR=/nonexistent-heartbeat-fixture
trap 'printf "DEPLOYMENT_ERROR\\n" >&2' ERR
trap 'printf "DEPLOYMENT_CLEANUP\\n" >&2' EXIT
log() { printf '%s\\n' "$*"; }
""" + functions + body],
            capture_output=True, text=True, timeout=5,
        )

    def test_heartbeat_sigpipe_does_not_report_deployment_failure(self) -> None:
        result = self.run_heartbeat_fixture("""
date() { return 141; }
run_with_heartbeat fixture sleep 0.1
printf 'INSTALL_SUCCEEDED\\n'
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INSTALL_SUCCEEDED", result.stdout)
        self.assertNotIn("DEPLOYMENT_ERROR", result.stderr)
        self.assertEqual(result.stderr.count("DEPLOYMENT_CLEANUP"), 1)

    def test_stopping_heartbeat_during_date_keeps_deployment_successful(self) -> None:
        result = self.run_heartbeat_fixture("""
date() { sleep 0.1; printf '100'; }
run_with_heartbeat fixture sleep 0.03
printf 'INSTALL_SUCCEEDED\\n'
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INSTALL_SUCCEEDED", result.stdout)
        self.assertNotIn("DEPLOYMENT_ERROR", result.stderr)
        self.assertEqual(result.stderr.count("DEPLOYMENT_CLEANUP"), 1)

    def test_real_install_failure_still_reaches_deployment_handler(self) -> None:
        result = self.run_heartbeat_fixture("""
date() { printf '100'; }
run_with_heartbeat fixture bash -c 'exit 7'
""")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("DEPLOYMENT_ERROR", result.stderr)
        self.assertEqual(result.stderr.count("DEPLOYMENT_CLEANUP"), 1)

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
