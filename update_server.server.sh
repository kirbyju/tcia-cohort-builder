#!/usr/bin/env bash

# Update the TCIA query services and Participant Explorer on the alpha server.
#
# Run without arguments for a deployment. Run with --preflight to verify the
# repositories, Python environments, service units, and shared environment file
# without changing source code, dependencies, bundle data, or services. Run with
# --cleanup-only to remove old verified versioned bundles without using network.

set -Eeuo pipefail
IFS=$'\n\t'
umask 027

QUERY_ROOT="${TCIA_QUERY_SKILL_ROOT:-/home/exouser/tcia-query-skill}"
COHORT_ROOT="${TCIA_COHORT_BUILDER_ROOT:-/home/exouser/tcia-cohort-builder}"
ENV_FILE="${TCIA_ENV_FILE:-/home/exouser/.config/tcia/tcia.env}"
MCP_PYTHON="${TCIA_MCP_PYTHON:-/home/exouser/.venvs/tcia-query-mcp/bin/python}"
COHORT_PYTHON="${TCIA_COHORT_PYTHON:-/home/exouser/.venvs/tcia-cohort-builder/bin/python}"

COHORT_HEALTH_URL="${TCIA_COHORT_HEALTH_URL:-https://tcia-p-explorer.duckdns.org/_stcore/health}"
REST_HEALTH_URL="${TCIA_REST_HEALTH_URL:-https://tcia.duckdns.org/v2/health}"
REST_READY_URL="${TCIA_REST_READY_URL:-https://tcia.duckdns.org/v2/ready}"
REST_BUNDLE_URL="${TCIA_REST_BUNDLE_URL:-https://tcia.duckdns.org/v2/bundle}"
REST_LOCAL_HEALTH_URL="${TCIA_REST_LOCAL_HEALTH_URL:-http://127.0.0.1:8766/v2/health}"
MCP_HEALTH_URL="${TCIA_MCP_HEALTH_URL:-http://127.0.0.1:8765/mcp}"
MCP_HEALTH_HOST="${TCIA_MCP_HEALTH_HOST:-tcia.duckdns.org}"
MCP_PUBLIC_URL="${TCIA_MCP_PUBLIC_URL:-https://tcia.duckdns.org/mcp}"
MCP_PUBLIC_HOST="${TCIA_MCP_PUBLIC_HOST:-tcia.duckdns.org}"

BUNDLE_TAG="${TCIA_METADATA_V2_RELEASE_TAG:-tcia-metadata-v2-latest}"
BUNDLE_MANIFEST_URL="${TCIA_V2_BUNDLE_MANIFEST_URL:-https://github.com/kirbyju/tcia-query-skill/releases/download/$BUNDLE_TAG/tcia_metadata_v2_bundle_manifest.json}"
RETAIN_RELEASES="${TCIA_V2_RETAIN_RELEASES:-2}"
RUN_TESTS="${TCIA_RUN_TESTS:-1}"
HEALTH_ATTEMPTS="${TCIA_HEALTH_ATTEMPTS:-30}"
HEALTH_DELAY_SECONDS="${TCIA_HEALTH_DELAY_SECONDS:-2}"
PROGRESS_HEARTBEAT_SECONDS="${TCIA_PROGRESS_HEARTBEAT_SECONDS:-30}"

SERVICES=(
  tcia-query-mcp
  tcia-query-rest
  tcia-cohort-builder
)

MODE="deploy"
if [[ ${1:-} == "--preflight" ]]; then
  MODE="preflight"
elif [[ ${1:-} == "--cleanup-only" ]]; then
  MODE="cleanup"
elif [[ -n ${1:-} ]]; then
  echo "Usage: $0 [--preflight|--cleanup-only]" >&2
  exit 2
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TEMP_ROOT=""
QUERY_BEFORE="unknown"
COHORT_BEFORE="unknown"
QUERY_AFTER="unknown"
COHORT_AFTER="unknown"
NEW_INSTALL_DIR=""
CURRENT_LINK=""
PREVIOUS_BUNDLE_TARGET=""
ENV_BACKUP=""
SERVICE_SCOPE=""
SYSTEMCTL_CMD=()
MCP_PROTOCOL_VERSION=""
REMOTE_BUNDLE_FINGERPRINT=""
RELEASE_PRUNE_ATTEMPTED=0
HEARTBEAT_PID=""
BUNDLE_REUSED=0
ACTIVATION_STARTED=0
ACTIVATION_VALIDATED=0
ROLLBACK_COMPLETED=0

log() {
  printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

warn() {
  printf '%s  WARNING: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

die() {
  printf '%s  ERROR: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
  exit 1
}

stop_heartbeat() {
  if [[ -n "$HEARTBEAT_PID" ]]; then
    kill "$HEARTBEAT_PID" >/dev/null 2>&1 || true
    wait "$HEARTBEAT_PID" 2>/dev/null || true
    HEARTBEAT_PID=""
  fi
}

start_heartbeat() {
  local label=$1
  (
    local started now stage_dir stage_size stage_file stage_file_size progress_detail
    started="$(date +%s)"
    while sleep "$PROGRESS_HEARTBEAT_SECONDS"; do
      now="$(date +%s)"
      progress_detail=""
      stage_dir="$(find "$RELEASES_ROOT" -maxdepth 1 -type d -name '.tcia-v2-stage-*' -printf '%T@\t%p\n' 2>/dev/null | sort -nr | head -n 1 | cut -f 2- || true)"
      if [[ -n "$stage_dir" ]]; then
        stage_size="$(du -sh "$stage_dir" 2>/dev/null | awk '{print $1}' || true)"
        stage_file="$(find "$stage_dir" -maxdepth 1 -type f -printf '%T@\t%f\t%s\n' 2>/dev/null | sort -nr | head -n 1 || true)"
        if [[ -n "$stage_file" ]]; then
          stage_file_size="$(awk -F $'\t' '{print $3}' <<<"$stage_file")"
          progress_detail=", staged=${stage_size:-unknown}, active=$(awk -F $'\t' '{print $2}' <<<"$stage_file") (${stage_file_size} bytes)"
        else
          progress_detail=", staging directory created; awaiting first asset"
        fi
      fi
      log "$label still running ($((now - started))s elapsed${progress_detail})"
    done
  ) &
  HEARTBEAT_PID=$!
}

run_with_heartbeat() {
  local label=$1
  local command_status=0
  shift
  start_heartbeat "$label"
  "$@" || command_status=$?
  stop_heartbeat
  return "$command_status"
}

cleanup() {
  stop_heartbeat
  if [[ -n "$TEMP_ROOT" && -d "$TEMP_ROOT" ]]; then
    rm -rf -- "$TEMP_ROOT"
  fi
}

on_error() {
  local exit_code=$?
  local line_no=${1:-unknown}
  printf '\nUpdate failed at line %s (exit %s).\n' "$line_no" "$exit_code" >&2
  printf 'Previous commits: query=%s cohort=%s\n' "$QUERY_BEFORE" "$COHORT_BEFORE" >&2
  printf 'Current commits:  query=%s cohort=%s\n' "$QUERY_AFTER" "$COHORT_AFTER" >&2
  if [[ -n "$NEW_INSTALL_DIR" ]]; then
    printf 'Candidate bundle: %s\n' "$NEW_INSTALL_DIR" >&2
  fi
  if [[ -n "$PREVIOUS_BUNDLE_TARGET" ]]; then
    printf 'Previous bundle:  %s\n' "$PREVIOUS_BUNDLE_TARGET" >&2
  fi
  if [[ -n "$ENV_BACKUP" ]]; then
    printf 'Environment backup: %s\n' "$ENV_BACKUP" >&2
  fi
  if [[ "$RELEASE_PRUNE_ATTEMPTED" == "0" ]]; then
    printf 'No previous bundle directory was deleted.\n' >&2
  else
    printf 'Managed release cleanup had started; inspect %s for the retained releases.\n' "${RELEASES_ROOT:-unknown}" >&2
  fi
  if [[ "$ACTIVATION_STARTED" == "1" && "$ACTIVATION_VALIDATED" != "1" && "$ROLLBACK_COMPLETED" != "1" ]]; then
    rollback_activation || report_bundle_recovery
  fi
  exit "$exit_code"
}

trap cleanup EXIT
trap 'on_error $LINENO' ERR

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command is not installed: $1"
}

require_file() {
  [[ -f "$1" ]] || die "Required file does not exist: $1"
}

require_executable() {
  [[ -x "$1" ]] || die "Required executable is missing or not executable: $1"
}

validate_integer() {
  local name=$1
  local value=$2
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$name must be a positive integer (got: $value)"
}

validate_env_value() {
  local name=$1
  local value=$2
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *"'"* ]] ||
    die "$name contains a character that cannot be written safely to $ENV_FILE"
}

repo_preflight() {
  local label=$1
  local root=$2
  local branch status

  [[ -d "$root/.git" ]] || die "$label checkout is not a Git repository: $root"
  branch="$(git -C "$root" branch --show-current)"
  [[ "$branch" == "main" ]] || die "$label checkout must be on main (currently: ${branch:-detached})"
  status="$(git -C "$root" status --porcelain --untracked-files=normal)"
  [[ -z "$status" ]] || {
    printf '%s\n' "$status" >&2
    die "$label checkout has local changes; preserve or remove them before deployment"
  }

  git -C "$root" fetch --prune origin
  git -C "$root" rev-parse --verify origin/main >/dev/null
  git -C "$root" merge-base --is-ancestor HEAD origin/main ||
    die "$label main is ahead of or diverged from origin/main; refusing to overwrite it"

  log "$label: $(git -C "$root" rev-parse --short HEAD) -> $(git -C "$root" rev-parse --short origin/main)"
}

verify_service_configuration() {
  local service=$1
  local unit_text home_form

  unit_text="$("${SYSTEMCTL_CMD[@]}" cat "$service")" || die "Cannot read systemd unit: $service"
  home_form=""
  if [[ "$ENV_FILE" == "$HOME/"* ]]; then
    home_form="%h/${ENV_FILE#"$HOME/"}"
  fi
  if ! grep -Fq "$ENV_FILE" <<<"$unit_text" &&
     { [[ -z "$home_form" ]] || ! grep -Fq "$home_form" <<<"$unit_text"; }; then
    die "$service does not reference the shared environment file $ENV_FILE"
  fi

  if grep -Eq 'Environment="?TCIA_(V2_INSTALL_DIR|METADATA_V2_CACHE|SNAPSHOT_DB|PARTICIPANT_INVENTORY_DB|PUBLIC_NON_DICOM_METADATA_DB|CONTROLLED_ACCESS_METADATA_DB|CLINICAL_METADATA_DB|V2_BUNDLE_MANIFEST|NIFTI_METADATA_DB|PATHOLOGY_METADATA_DB)' <<<"$unit_text"; then
    die "$service hard-codes V2 artifact paths; remove those unit-level assignments and use $ENV_FILE only"
  fi
}

detect_service_manager() {
  local service

  if systemctl cat "${SERVICES[0]}" >/dev/null 2>&1; then
    SERVICE_SCOPE="system"
    SYSTEMCTL_CMD=(systemctl)
  elif systemctl --user cat "${SERVICES[0]}" >/dev/null 2>&1; then
    SERVICE_SCOPE="user"
    SYSTEMCTL_CMD=(systemctl --user)
  else
    die "Cannot find ${SERVICES[0]} as either a system or per-user systemd unit"
  fi

  for service in "${SERVICES[@]}"; do
    "${SYSTEMCTL_CMD[@]}" cat "$service" >/dev/null 2>&1 ||
      die "$service is not installed in the detected $SERVICE_SCOPE systemd manager"
  done
  log "Using $SERVICE_SCOPE systemd service units"
}

update_repo() {
  local label=$1
  local root=$2

  log "Fast-forwarding $label"
  git -C "$root" merge --ff-only origin/main
}

wait_for() {
  local label=$1
  local check_function=$2
  local attempt

  for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
    if "$check_function"; then
      log "$label is healthy"
      return 0
    fi
    sleep "$HEALTH_DELAY_SECONDS"
  done
  warn "$label did not become healthy after $HEALTH_ATTEMPTS attempts"
  return 1
}

check_service_units() {
  local service
  for service in "${SERVICES[@]}"; do
    "${SYSTEMCTL_CMD[@]}" is-active --quiet "$service" || return 1
  done
}

check_cohort_health() {
  curl -fsS --max-time 15 -o /dev/null "$COHORT_HEALTH_URL"
}

check_rest_local_health() {
  curl -fsS --max-time 15 -o /dev/null "$REST_LOCAL_HEALTH_URL"
}

check_rest_public_health() {
  curl -fsS --max-time 15 -o /dev/null "$REST_HEALTH_URL"
}

check_rest_public_ready() {
  curl -fsS --max-time 30 -o /dev/null "$REST_READY_URL"
}

check_mcp_endpoint() {
  local url=$1
  local host_header=$2
  local payload
  printf -v payload '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"%s","capabilities":{},"clientInfo":{"name":"tcia-update-check","version":"1.0"}}}' "$MCP_PROTOCOL_VERSION"
  curl -fsS --max-time 15 \
    -X POST "$url" \
    -H "Host: $host_header" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    --data "$payload" \
    -o /dev/null
}

check_mcp_health() {
  check_mcp_endpoint "$MCP_HEALTH_URL" "$MCP_HEALTH_HOST"
}

check_mcp_public_health() {
  check_mcp_endpoint "$MCP_PUBLIC_URL" "$MCP_PUBLIC_HOST"
}

show_service_logs() {
  local service
  for service in "${SERVICES[@]}"; do
    printf '\n===== %s =====\n' "$service" >&2
    if [[ "$SERVICE_SCOPE" == "system" ]]; then
      sudo journalctl -u "$service" -n 80 --no-pager >&2 || true
    else
      journalctl --user -u "$service" -n 80 --no-pager >&2 || true
    fi
  done
}

restart_services() {
  if [[ "$SERVICE_SCOPE" == "system" ]]; then
    sudo systemctl restart "${SERVICES[@]}"
  else
    systemctl --user restart "${SERVICES[@]}"
  fi
}

report_bundle_recovery() {
  if [[ -n "$PREVIOUS_BUNDLE_TARGET" && -n "$CURRENT_LINK" ]]; then
    warn "The previous bundle is still available at $PREVIOUS_BUNDLE_TARGET"
    warn "Bundle-only rollback: repoint $CURRENT_LINK to that directory, then restart all three services"
  fi
  if [[ -n "$ENV_BACKUP" ]]; then
    warn "The pre-update shared environment is backed up at $ENV_BACKUP"
  fi
}

rollback_activation() {
  local temporary_link

  if [[ "$ACTIVATION_STARTED" != "1" || "$ACTIVATION_VALIDATED" == "1" ]]; then
    return 0
  fi
  if [[ -z "$PREVIOUS_BUNDLE_TARGET" || ! -d "$PREVIOUS_BUNDLE_TARGET" ]]; then
    warn "Automatic rollback unavailable: the previous bundle target is missing"
    return 1
  fi
  warn "Restoring the previous validated bundle after failed deployment verification"
  temporary_link="${CURRENT_LINK}.rollback.$$"
  if [[ -e "$temporary_link" || -L "$temporary_link" ]]; then
    warn "Automatic rollback blocked by existing temporary link: $temporary_link"
    return 1
  fi
  if ! ln -s "$PREVIOUS_BUNDLE_TARGET" "$temporary_link"; then
    warn "Automatic rollback could not create the replacement bundle link"
    return 1
  fi
  if ! mv -Tf -- "$temporary_link" "$CURRENT_LINK"; then
    warn "Automatic rollback could not replace the active bundle link"
    return 1
  fi
  if [[ -n "$ENV_BACKUP" && -f "$ENV_BACKUP" ]]; then
    if ! cp -p -- "$ENV_BACKUP" "$ENV_FILE"; then
      warn "Automatic rollback restored the bundle link but not the shared environment"
      return 1
    fi
  else
    warn "No shared-environment backup was created; leaving the existing environment unchanged"
  fi
  if ! restart_services; then
    warn "Automatic rollback restored bundle/environment state but service restart failed"
    return 1
  fi
  if ! wait_for "rolled-back systemd services" check_service_units; then
    warn "Automatic rollback restarted services, but their units did not become active"
    return 1
  fi
  if ! wait_for "rolled-back public REST /v2/health" check_rest_public_health; then
    warn "Automatic rollback restored services, but public REST health did not recover"
    return 1
  fi
  if ! wait_for "rolled-back public REST /v2/ready" check_rest_public_ready; then
    warn "Automatic rollback restored services, but public REST readiness did not recover"
    return 1
  fi
  ROLLBACK_COMPLETED=1
  warn "Automatic rollback complete; active bundle restored to $PREVIOUS_BUNDLE_TARGET"
}

verification_failed() {
  local label=$1
  show_service_logs
  warn "Deployment verification failed: $label"
  if ! rollback_activation; then
    report_bundle_recovery
  fi
  exit 1
}

write_shared_environment() {
  local current_link=$1
  local env_dir env_tmp old_mode

  env_dir="$(dirname "$ENV_FILE")"
  env_tmp="$(mktemp "$env_dir/.tcia.env.XXXXXX")"
  ENV_BACKUP="$ENV_FILE.backup.$TIMESTAMP"
  old_mode="$(stat -c '%a' "$ENV_FILE")"

  cp -p -- "$ENV_FILE" "$ENV_BACKUP"

  awk '
    BEGIN { in_managed = 0 }
    $0 == "# BEGIN TCIA V2 MANAGED" { in_managed = 1; next }
    $0 == "# END TCIA V2 MANAGED" { in_managed = 0; next }
    in_managed { next }
    {
      candidate = $0
      sub(/^[[:space:]]*export[[:space:]]+/, "", candidate)
      if (candidate ~ /^(TCIA_QUERY_SKILL_ROOT|TCIA_V2_INSTALL_DIR|TCIA_METADATA_V2_CACHE|TCIA_METADATA_V2_RELEASE_TAG|TCIA_SNAPSHOT_DB|TCIA_PARTICIPANT_INVENTORY_DB|TCIA_PUBLIC_NON_DICOM_METADATA_DB|TCIA_CONTROLLED_ACCESS_METADATA_DB|TCIA_CLINICAL_METADATA_DB|TCIA_V2_BUNDLE_MANIFEST|TCIA_ENABLE_LEGACY_MCP_TOOLS|TCIA_NIFTI_METADATA_DB|TCIA_PATHOLOGY_METADATA_DB)=/) next
      print
    }
  ' "$ENV_FILE" >"$env_tmp"

  {
    printf '\n# BEGIN TCIA V2 MANAGED\n'
    printf "TCIA_QUERY_SKILL_ROOT='%s'\n" "$QUERY_ROOT"
    printf "TCIA_V2_INSTALL_DIR='%s'\n" "$current_link"
    printf "TCIA_METADATA_V2_CACHE='%s'\n" "$current_link"
    printf "TCIA_METADATA_V2_RELEASE_TAG='%s'\n" "$BUNDLE_TAG"
    printf "TCIA_SNAPSHOT_DB='%s/tcia_snapshot.sqlite'\n" "$current_link"
    printf "TCIA_PARTICIPANT_INVENTORY_DB='%s/participant_inventory.sqlite'\n" "$current_link"
    printf "TCIA_PUBLIC_NON_DICOM_METADATA_DB='%s/public_non_dicom_metadata.sqlite'\n" "$current_link"
    printf "TCIA_CONTROLLED_ACCESS_METADATA_DB='%s/controlled_access_metadata.sqlite'\n" "$current_link"
    printf "TCIA_CLINICAL_METADATA_DB='%s/clinical_metadata.sqlite'\n" "$current_link"
    printf "TCIA_V2_BUNDLE_MANIFEST='%s/tcia_metadata_v2_bundle_manifest.json'\n" "$current_link"
    printf "TCIA_ENABLE_LEGACY_MCP_TOOLS='false'\n"
    printf '# END TCIA V2 MANAGED\n'
  } >>"$env_tmp"

  chmod "$old_mode" "$env_tmp"
  mv -f -- "$env_tmp" "$ENV_FILE"
  log "Updated shared V2 environment (backup: $ENV_BACKUP)"
}

switch_current_bundle() {
  local target=$1
  local current_link=$2
  local temporary_link

  if [[ -e "$current_link" && ! -L "$current_link" ]]; then
    die "Current-bundle path exists but is not a symlink: $current_link"
  fi

  temporary_link="${current_link}.new.$$"
  [[ ! -e "$temporary_link" && ! -L "$temporary_link" ]] ||
    die "Temporary symlink path already exists: $temporary_link"
  ln -s "$target" "$temporary_link"
  mv -Tf -- "$temporary_link" "$current_link"
  log "Current V2 bundle now points to $target"
}

validate_installed_bundle() {
  local install_dir=$1
  "$MCP_PYTHON" - "$install_dir" "$BUNDLE_TAG" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
expected_tag = sys.argv[2]
manifest_path = root / "tcia_metadata_v2_bundle_manifest.json"
state_path = root / "tcia_metadata_v2_install.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
state = json.loads(state_path.read_text(encoding="utf-8"))

if manifest.get("release_contract") not in {"streamlined", "full"}:
    raise SystemExit(f"Unsupported release contract: {manifest.get('release_contract')}")
if state.get("release_tag") != expected_tag:
    raise SystemExit(f"Unexpected release tag: {state.get('release_tag')}")
if state.get("installed_profile") != "research_detail":
    raise SystemExit(f"Unexpected installed profile: {state.get('installed_profile')}")
if state.get("release_fingerprint") != manifest.get("release_fingerprint"):
    raise SystemExit("Install receipt and bundle manifest fingerprints differ")

required = {
    "tcia_snapshot.sqlite",
    "participant_inventory.sqlite",
    "public_non_dicom_metadata.sqlite",
    "controlled_access_metadata.sqlite",
    "clinical_metadata.sqlite",
}
missing = sorted(name for name in required if not (root / name).is_file())
if missing:
    raise SystemExit(f"Required research_detail files are missing: {missing}")

print(f"release_fingerprint={manifest.get('release_fingerprint')}")
print(f"generated_at_utc={manifest.get('generated_at_utc')}")
print(f"installed_profile={state.get('installed_profile')}")
PY
}

bundle_fingerprint() {
  local manifest_path=$1
  "$MCP_PYTHON" - "$manifest_path" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
fingerprint = str(manifest.get("release_fingerprint") or "")
if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
    raise SystemExit("Bundle manifest has no valid release fingerprint")
print(fingerprint)
PY
}

validate_cohort_bundle_contract() {
  local install_dir=$1
  TCIA_METADATA_V2_RELEASE_TAG="$BUNDLE_TAG" \
    "$COHORT_PYTHON" - "$COHORT_ROOT" "$install_dir" <<'PY'
import pathlib
import sys

cohort_root = pathlib.Path(sys.argv[1]).resolve()
install_dir = pathlib.Path(sys.argv[2]).resolve()
sys.path.insert(0, str(cohort_root))

from v2_artifacts import load_bundle_installation, require_installed_component

installation = load_bundle_installation(install_dir)
schemas = {}
for name in (
    "snapshot",
    "participant_inventory",
    "public_non_dicom",
    "controlled_access",
    "clinical",
):
    schemas[name] = require_installed_component(install_dir, name).schema_version

print(
    "Participant Explorer bundle compatibility: "
    f"fingerprint={installation.release_fingerprint} schemas={schemas}"
)
PY
}

prune_release_directories() {
  local releases_root=$1
  local current_link=$2
  local retain_count=$3
  "$MCP_PYTHON" - "$releases_root" "$current_link" "$retain_count" <<'PY'
import json
import pathlib
import re
import shutil
import sys
import time

root = pathlib.Path(sys.argv[1]).resolve()
current_link = pathlib.Path(sys.argv[2])
retain_count = int(sys.argv[3])
current = current_link.resolve(strict=True)
pattern = re.compile(r"[0-9]{8}T[0-9]{6}Z-query-[0-9a-f]{12}")

if root == pathlib.Path(root.anchor) or current.parent != root:
    raise SystemExit(
        f"Refusing release cleanup outside the direct release root: root={root} current={current}"
    )

complete = []
incomplete = []
for path in root.iterdir():
    if path.is_symlink() or not path.is_dir() or not pattern.fullmatch(path.name):
        continue
    try:
        manifest = json.loads(
            (path / "tcia_metadata_v2_bundle_manifest.json").read_text(encoding="utf-8")
        )
        state = json.loads(
            (path / "tcia_metadata_v2_install.json").read_text(encoding="utf-8")
        )
        if (
            manifest.get("artifact") != "tcia_metadata_v2_bundle"
            or state.get("artifact") != "tcia_metadata_v2_install"
            or manifest.get("release_fingerprint") != state.get("release_fingerprint")
        ):
            raise ValueError("manifest/receipt contract mismatch")
    except (OSError, ValueError, json.JSONDecodeError):
        incomplete.append(path)
    else:
        complete.append(path)

complete.sort(key=lambda path: path.name, reverse=True)
keep = {current}
for path in complete:
    if path != current and len(keep) < retain_count:
        keep.add(path)

remove = [path for path in complete if path not in keep]
stale_before = time.time() - 24 * 60 * 60
remove.extend(
    path for path in incomplete if path.stat().st_mtime <= stale_before
)

removed_bytes = 0
for path in remove:
    removed_bytes += sum(
        child.stat().st_size for child in path.rglob("*") if child.is_file()
    )
    shutil.rmtree(path)

recent_incomplete = sorted(
    str(path) for path in incomplete if path not in remove
)
print(
    json.dumps(
        {
            "retained": sorted(str(path) for path in keep if path.exists()),
            "removed": sorted(str(path) for path in remove),
            "removed_bytes": removed_bytes,
            "recent_incomplete_retained": recent_incomplete,
        },
        sort_keys=True,
    )
)
PY
}

validate_rest_bundle() {
  local response_file="$TEMP_ROOT/rest-bundle.json"
  curl -fsS --max-time 20 "$REST_BUNDLE_URL" -o "$response_file"
  "$MCP_PYTHON" - "$response_file" "$NEW_INSTALL_DIR/tcia_metadata_v2_install.json" <<'PY'
import json
import pathlib
import sys

remote = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
local_state = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
bundle = remote.get("v2_bundle") or {}
install = remote.get("v2_install") or {}
capabilities = remote.get("v2_capabilities") or {}

if bundle.get("release_fingerprint") != local_state.get("release_fingerprint"):
    raise SystemExit("REST is not serving the newly installed bundle fingerprint")
if install.get("installed_profile") != "research_detail":
    raise SystemExit("REST is not serving the research_detail profile")
for name in ("participant_search", "public_non_dicom_detail", "controlled_access_detail", "clinical_detail"):
    if capabilities.get(name) is not True:
        raise SystemExit(f"REST capability is unavailable: {name}")
print(f"REST bundle fingerprint: {bundle.get('release_fingerprint')}")
PY
}

require_command flock
require_file "$ENV_FILE"

if [[ "$MODE" != "cleanup" ]]; then
  require_command git
  require_command curl
  require_command systemctl
  require_command journalctl
  require_command stat
  require_command awk
  require_command readlink
fi

exec 9>/tmp/tcia-update-server.lock
flock -n 9 || die "Another TCIA server update is already running"

TEMP_ROOT="$(mktemp -d /tmp/tcia-update-server.XXXXXX)"

log "Loading shared artifact configuration"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# The stable V2 contract intentionally ignores and removes legacy standalone
# NIfTI/pathology variables. The unified public non-DICOM database replaces both.
unset TCIA_NIFTI_METADATA_DB TCIA_PATHOLOGY_METADATA_DB

RELEASES_ROOT="${TCIA_V2_RELEASES_ROOT:-$QUERY_ROOT/cache/v2-releases}"
CURRENT_LINK="${TCIA_V2_CURRENT_LINK:-$QUERY_ROOT/cache/tcia-metadata-v2-current}"
validate_env_value TCIA_V2_RELEASES_ROOT "$RELEASES_ROOT"
validate_env_value TCIA_V2_CURRENT_LINK "$CURRENT_LINK"

if [[ "$MODE" == "cleanup" ]]; then
  require_executable "$MCP_PYTHON"
  validate_integer TCIA_V2_RETAIN_RELEASES "$RETAIN_RELEASES"
  [[ -d "$RELEASES_ROOT" ]] || die "Versioned release root does not exist: $RELEASES_ROOT"
  [[ -L "$CURRENT_LINK" ]] || die "Current bundle is not a symlink: $CURRENT_LINK"
  log "Cleanup-only mode: retaining $RETAIN_RELEASES managed versioned bundles"
  RELEASE_PRUNE_ATTEMPTED=1
  prune_release_directories "$RELEASES_ROOT" "$CURRENT_LINK" "$RETAIN_RELEASES"
  df -h / || true
  log "Cleanup-only mode complete; no repositories, dependencies, services, or bundle contents were changed"
  exit 0
fi

if [[ -z "$MCP_HEALTH_HOST" && -n ${TCIA_MCP_ALLOWED_HOSTS:-} ]]; then
  IFS=',' read -r -a configured_mcp_hosts <<<"$TCIA_MCP_ALLOWED_HOSTS"
  for configured_mcp_host in "${configured_mcp_hosts[@]}"; do
    configured_mcp_host="${configured_mcp_host#"${configured_mcp_host%%[![:space:]]*}"}"
    configured_mcp_host="${configured_mcp_host%"${configured_mcp_host##*[![:space:]]}"}"
    if [[ -n "$configured_mcp_host" && "$configured_mcp_host" != *'*'* ]]; then
      MCP_HEALTH_HOST="$configured_mcp_host"
      break
    fi
  done
fi
if [[ -n "$MCP_HEALTH_HOST" ]]; then
  [[ "$MCP_HEALTH_HOST" =~ ^[A-Za-z0-9._:-]+$ ]] ||
    die "TCIA_MCP_HEALTH_HOST is not a valid concrete HTTP Host value: $MCP_HEALTH_HOST"
  log "MCP health probe will use allowed Host: $MCP_HEALTH_HOST"
fi
[[ "$MCP_PUBLIC_HOST" =~ ^[A-Za-z0-9._:-]+$ ]] ||
  die "TCIA_MCP_PUBLIC_HOST is not a valid concrete HTTP Host value: $MCP_PUBLIC_HOST"

if [[ -n ${TCIA_V2_INSTALL_DIR:-} && -e ${TCIA_V2_INSTALL_DIR:-} ]]; then
  PREVIOUS_BUNDLE_TARGET="$(readlink -f "$TCIA_V2_INSTALL_DIR")"
fi

require_executable "$MCP_PYTHON"
require_executable "$COHORT_PYTHON"
MCP_PROTOCOL_VERSION="$("$MCP_PYTHON" -c 'from mcp.types import LATEST_PROTOCOL_VERSION; print(LATEST_PROTOCOL_VERSION)')"
[[ "$MCP_PROTOCOL_VERSION" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] ||
  die "Could not determine a valid MCP protocol version from $MCP_PYTHON"
validate_integer TCIA_HEALTH_ATTEMPTS "$HEALTH_ATTEMPTS"
validate_integer TCIA_HEALTH_DELAY_SECONDS "$HEALTH_DELAY_SECONDS"
validate_integer TCIA_V2_RETAIN_RELEASES "$RETAIN_RELEASES"
validate_integer TCIA_PROGRESS_HEARTBEAT_SECONDS "$PROGRESS_HEARTBEAT_SECONDS"

[[ "$RUN_TESTS" == "0" || "$RUN_TESTS" == "1" ]] || die "TCIA_RUN_TESTS must be 0 or 1"
[[ "$REST_HEALTH_URL" != *"/v1/"* ]] || die "REST health must use /v2/health, not the compatibility /v1 endpoint"
[[ "$REST_READY_URL" != *"/v1/"* ]] || die "REST readiness must use /v2/ready, not the compatibility /v1 endpoint"
[[ -O "$ENV_FILE" && -w "$ENV_FILE" ]] || die "The deployment user must own and be able to update $ENV_FILE"

validate_env_value TCIA_QUERY_SKILL_ROOT "$QUERY_ROOT"
validate_env_value TCIA_V2_RELEASE_TAG "$BUNDLE_TAG"

require_file "$QUERY_ROOT/mcp_server/requirements.txt"
require_file "$QUERY_ROOT/scripts/tcia_v2_bundle.py"
require_file "$COHORT_ROOT/requirements.txt"
require_file "$COHORT_ROOT/tcia-cohort-builder.py"

detect_service_manager
verify_service_configuration tcia-query-mcp
verify_service_configuration tcia-query-rest
verify_service_configuration tcia-cohort-builder

repo_preflight "TCIA query skill" "$QUERY_ROOT"
repo_preflight "Participant Explorer" "$COHORT_ROOT"

QUERY_BEFORE="$(git -C "$QUERY_ROOT" rev-parse HEAD)"
COHORT_BEFORE="$(git -C "$COHORT_ROOT" rev-parse HEAD)"
QUERY_AFTER="$QUERY_BEFORE"
COHORT_AFTER="$COHORT_BEFORE"

if [[ "$MODE" == "preflight" ]]; then
  log "Preflight passed; no source, dependency, bundle, environment, or service changes were made"
  exit 0
fi

if [[ "$SERVICE_SCOPE" == "system" ]]; then
  require_command sudo
  sudo -v
fi

update_repo "TCIA query skill" "$QUERY_ROOT"
update_repo "Participant Explorer" "$COHORT_ROOT"
QUERY_AFTER="$(git -C "$QUERY_ROOT" rev-parse HEAD)"
COHORT_AFTER="$(git -C "$COHORT_ROOT" rev-parse HEAD)"

log "Installing MCP/REST dependencies"
"$MCP_PYTHON" -m pip install --disable-pip-version-check -r "$QUERY_ROOT/mcp_server/requirements.txt"
log "Installing Participant Explorer dependencies"
"$COHORT_PYTHON" -m pip install --disable-pip-version-check -r "$COHORT_ROOT/requirements.txt"

if [[ "$RUN_TESTS" == "1" ]]; then
  log "Running MCP/REST unit tests"
  (
    cd "$QUERY_ROOT"
    "$MCP_PYTHON" -m unittest discover -s mcp_server/tests -v
  )
  log "Running Participant Explorer unit tests"
  (
    cd "$COHORT_ROOT"
    "$COHORT_PYTHON" -m unittest discover -s tests -v
  )
else
  warn "Unit tests skipped because TCIA_RUN_TESTS=0"
fi

mkdir -p -- "$RELEASES_ROOT" "$(dirname "$CURRENT_LINK")"

REMOTE_MANIFEST="$TEMP_ROOT/tcia_metadata_v2_bundle_manifest.json"
log "Fetching the stable V2 bundle manifest"
curl -fsSL --max-time 60 "$BUNDLE_MANIFEST_URL" -o "$REMOTE_MANIFEST"
REMOTE_BUNDLE_FINGERPRINT="$(bundle_fingerprint "$REMOTE_MANIFEST")"
log "Stable V2 bundle fingerprint: $REMOTE_BUNDLE_FINGERPRINT"

if [[ -n "$PREVIOUS_BUNDLE_TARGET" && -d "$PREVIOUS_BUNDLE_TARGET" ]] &&
   validate_installed_bundle "$PREVIOUS_BUNDLE_TARGET" >/dev/null 2>&1 &&
   [[ "$(bundle_fingerprint "$PREVIOUS_BUNDLE_TARGET/tcia_metadata_v2_bundle_manifest.json")" == "$REMOTE_BUNDLE_FINGERPRINT" ]]; then
  NEW_INSTALL_DIR="$PREVIOUS_BUNDLE_TARGET"
  BUNDLE_REUSED=1
  log "Reusing the current validated V2 bundle because its fingerprint is unchanged"
else
  NEW_INSTALL_DIR="$RELEASES_ROOT/$TIMESTAMP-query-${QUERY_AFTER:0:12}"
  [[ ! -e "$NEW_INSTALL_DIR" ]] || die "Versioned install directory already exists: $NEW_INSTALL_DIR"
fi

if [[ "$BUNDLE_REUSED" == "1" ]]; then
  log "Skipping V2 installation because the active research_detail bundle is already validated at this fingerprint"
else
  log "Installing V2 research_detail bundle into $NEW_INSTALL_DIR"
  run_with_heartbeat "V2 research_detail installation" \
    "$MCP_PYTHON" "$QUERY_ROOT/scripts/tcia_v2_bundle.py" install \
    --tag "$BUNDLE_TAG" \
    --profile research_detail \
    --install-dir "$NEW_INSTALL_DIR" \
    --manifest-url "file://$REMOTE_MANIFEST"
fi

log "Validating the official bundle manifest and install receipt"
validate_installed_bundle "$NEW_INSTALL_DIR"
log "Validating Participant Explorer compatibility with the installed bundle"
validate_cohort_bundle_contract "$NEW_INSTALL_DIR"

switch_current_bundle "$NEW_INSTALL_DIR" "$CURRENT_LINK"
ACTIVATION_STARTED=1
write_shared_environment "$CURRENT_LINK"

log "Restarting TCIA services"
if ! restart_services; then
  verification_failed "one or more TCIA services failed to restart"
fi

if ! wait_for "systemd services" check_service_units; then
  verification_failed "systemd services"
fi
if ! wait_for "local REST /v2/health" check_rest_local_health; then
  verification_failed "local REST /v2/health"
fi
if ! wait_for "public REST /v2/health" check_rest_public_health; then
  verification_failed "public REST /v2/health"
fi
if ! wait_for "public REST /v2/ready" check_rest_public_ready; then
  verification_failed "public REST /v2/ready"
fi
if ! wait_for "MCP initialize handshake" check_mcp_health; then
  verification_failed "MCP initialize handshake using protocol $MCP_PROTOCOL_VERSION"
fi
if ! wait_for "public MCP nginx route" check_mcp_public_health; then
  verification_failed "public MCP nginx route at $MCP_PUBLIC_URL"
fi
if ! wait_for "Participant Explorer" check_cohort_health; then
  verification_failed "Participant Explorer"
fi

log "Validating the REST bundle fingerprint and detail capabilities"
if ! validate_rest_bundle; then
  verification_failed "REST bundle fingerprint and detail capabilities"
fi

ACTIVATION_VALIDATED=1

log "Pruning old versioned V2 bundle directories; retaining $RETAIN_RELEASES"
RELEASE_PRUNE_ATTEMPTED=1
prune_release_directories "$RELEASES_ROOT" "$CURRENT_LINK" "$RETAIN_RELEASES"

printf '\nDeployment complete.\n'
printf 'Query commit:        %s\n' "$QUERY_AFTER"
printf 'Cohort commit:       %s\n' "$COHORT_AFTER"
printf 'Active bundle:       %s\n' "$NEW_INSTALL_DIR"
printf 'Current bundle link: %s\n' "$CURRENT_LINK"
printf 'Shared environment:  %s\n' "$ENV_FILE"
printf 'Versioned bundles retained: %s (active included; remaining slots are newest verified rollbacks).\n' "$RETAIN_RELEASES"
