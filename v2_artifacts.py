"""Read and validate an installed TCIA Metadata Artifact Model V2 bundle.

The authoritative installer lives in the tcia-query-skill repository.  This
module deliberately does not download or replace multi-gigabyte artifacts from
inside a Streamlit rerun; it verifies the install receipt and exposes the
installed components to the application.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPOSITORY = "kirbyju/tcia-query-skill"
V2_RELEASE_TAG = os.environ.get(
    "TCIA_METADATA_V2_RELEASE_TAG", "tcia-metadata-v2-latest"
)
BUNDLE_MANIFEST_ASSET = "tcia_metadata_v2_bundle_manifest.json"
INSTALL_STATE_ASSET = "tcia_metadata_v2_install.json"
BUNDLE_SCHEMA_VERSION = 2
SUPPORTED_RELEASE_CONTRACTS = {"full", "streamlined"}
SUPPORTED_COMPONENTS = {
    "snapshot": {"schema_version": 7, "profile": "research_core"},
    "participant_inventory": {"schema_version": 6, "profile": "research_core"},
    "public_non_dicom": {"schema_version": 7, "profile": "research_detail"},
    "controlled_access": {"schema_version": 2, "profile": "research_detail"},
    "clinical": {"schema_version": 17, "profile": "research_detail"},
}


@dataclass(frozen=True)
class BundleInstallation:
    directory: Path
    manifest_path: Path
    state_path: Path
    release_tag: str
    release_contract: str
    release_fingerprint: str
    generated_at_utc: str
    installed_profile: str
    installed_assets: frozenset[str]
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ComponentCache:
    name: str
    database: Path
    manifest: Path
    release_fingerprint: str
    schema_version: int
    profile: str
    storage_contract: dict[str, Any]
    provenance: dict[str, Any]


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing {label}: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must contain a JSON object: {path}")
    return payload


def _validate_bundle_manifest(payload: dict[str, Any]) -> None:
    if payload.get("artifact") != "tcia_metadata_v2_bundle":
        raise RuntimeError("Unexpected TCIA metadata bundle artifact")
    if payload.get("release_tag") != V2_RELEASE_TAG:
        raise RuntimeError(
            f"Expected V2 release {V2_RELEASE_TAG}, got {payload.get('release_tag')}"
        )
    if payload.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise RuntimeError(
            "Unsupported V2 bundle schema: "
            f"expected {BUNDLE_SCHEMA_VERSION}, got {payload.get('schema_version')}"
        )
    release_contract = str(payload.get("release_contract") or "")
    if release_contract not in SUPPORTED_RELEASE_CONTRACTS:
        raise RuntimeError(f"Unsupported V2 release contract: {release_contract or 'missing'}")
    if not isinstance(payload.get("assets"), dict) or not isinstance(
        payload.get("components"), dict
    ):
        raise RuntimeError("V2 bundle manifest is missing its asset/component contract")
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or "research_core" not in profiles:
        raise RuntimeError("V2 bundle manifest is missing its profile contract")


def _validate_component_contract(name: str, component: dict[str, Any]) -> None:
    expected = SUPPORTED_COMPONENTS[name]
    if component.get("schema_version") != expected["schema_version"]:
        raise RuntimeError(
            f"Unsupported {name} schema: expected {expected['schema_version']}, "
            f"got {component.get('schema_version')}"
        )
    if component.get("profile") != expected["profile"]:
        raise RuntimeError(
            f"Unexpected {name} profile: expected {expected['profile']}, "
            f"got {component.get('profile')}"
        )


def load_bundle_installation(cache_dir: Path) -> BundleInstallation:
    """Load an official bundle install after checking its local receipt."""
    directory = cache_dir.expanduser().resolve()
    manifest_path = directory / BUNDLE_MANIFEST_ASSET
    state_path = directory / INSTALL_STATE_ASSET
    manifest = _load_json(manifest_path, "V2 bundle manifest")
    _validate_bundle_manifest(manifest)
    state = _load_json(state_path, "V2 install receipt")
    if state.get("artifact") != "tcia_metadata_v2_install":
        raise RuntimeError("Unexpected V2 install receipt artifact")
    for field in ("release_tag", "release_fingerprint"):
        if state.get(field) != manifest.get(field):
            raise RuntimeError(f"V2 install receipt disagrees with bundle field {field}")
    installed_assets = state.get("installed_assets")
    if not isinstance(installed_assets, list) or not all(
        isinstance(value, str) for value in installed_assets
    ):
        raise RuntimeError("V2 install receipt has no valid installed_assets list")
    return BundleInstallation(
        directory=directory,
        manifest_path=manifest_path,
        state_path=state_path,
        release_tag=str(manifest["release_tag"]),
        release_contract=str(manifest["release_contract"]),
        release_fingerprint=str(manifest["release_fingerprint"]),
        generated_at_utc=str(manifest.get("generated_at_utc") or ""),
        installed_profile=str(state.get("installed_profile") or ""),
        installed_assets=frozenset(installed_assets),
        manifest=manifest,
    )


def installed_component(cache_dir: Path, name: str) -> ComponentCache | None:
    """Return a component installed and verified by the official bundle installer."""
    if name not in SUPPORTED_COMPONENTS:
        raise ValueError(f"Unsupported V2 component: {name}")
    try:
        installation = load_bundle_installation(cache_dir)
    except RuntimeError:
        return None
    component = (installation.manifest.get("components") or {}).get(name)
    if not isinstance(component, dict):
        return None
    try:
        _validate_component_contract(name, component)
    except RuntimeError:
        return None
    database_asset = str(component.get("database_asset") or "")
    if not database_asset or database_asset not in installation.installed_assets:
        return None
    database = installation.directory / database_asset.removesuffix(".gz")
    if not database.is_file():
        return None
    return ComponentCache(
        name=name,
        database=database,
        manifest=installation.manifest_path,
        release_fingerprint=str(component.get("release_fingerprint") or ""),
        schema_version=int(component["schema_version"]),
        profile=str(component["profile"]),
        storage_contract=dict(component.get("storage_contract") or {}),
        provenance=dict(component.get("provenance") or {}),
    )


def require_installed_component(cache_dir: Path, name: str) -> ComponentCache:
    component = installed_component(cache_dir, name)
    if component is None:
        profile = str(SUPPORTED_COMPONENTS[name]["profile"])
        raise RuntimeError(
            f"The {name} component is not installed from the current stable V2 bundle. "
            f"Run scripts/tcia_v2_bundle.py install --profile {profile} from the "
            "tcia-query-skill checkout, then refresh this app."
        )
    return component
