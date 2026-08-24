"""Patient-centric data access for the TCIA Participant Explorer.

The module intentionally keeps Streamlit concerns out of the data layer so the
patient index, drill-down queries, and Data Retriever manifests can be tested
without launching the web application.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import parse_qs, quote, urlparse

import pandas as pd


LOGGER = logging.getLogger(__name__)
POLICY_URL = "https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/"
OPEN_ACCESS_LEVELS = {"open", "open_noncommercial"}
DATASET_TYPE_FILTERS = ("All", "Collection", "Analysis Result")
CLINICAL_CATEGORICAL_COLUMNS = (
    "sex_at_birth",
    "race",
    "ethnicity",
    "primary_diagnosis",
    "primary_site",
    "stage",
    "grade",
    "vital_status",
    "recurrence",
    "progression",
    "response",
    "screening_result",
)
COHORT_EXPORT_COLUMNS = (
    "short_title",
    "dataset_memberships",
    "dataset_count",
    "dataset_type",
    "title",
    "subject_id",
    "resolved_access_level",
    "sex_at_birth",
    "race",
    "ethnicity",
    "age_at_baseline",
    "age_at_diagnosis",
    "age_at_enrollment_years",
    "age_at_imaging_years",
    "age_at_treatment_years",
    "primary_diagnosis",
    "primary_site",
    "stage",
    "grade",
    "vital_status",
    "days_to_death",
    "days_to_last_followup",
    "overall_survival_days",
    "progression_free_survival_days",
    "recurrence",
    "progression",
    "response",
    "screening_result",
    "primary_diagnosis_is_inferred",
    "primary_site_is_inferred",
    "source_kinds",
    "source_count",
    "conflict_count",
    "has_clinical",
    "available_imaging",
    "modalities",
    "body_parts",
    "file_formats",
    "has_annotations",
    "dicom_series",
    "dicom_timepoints",
    "public_dicom_files_outside_idc",
    "public_non_dicom_files",
    "ct_series",
    "mha_volumes",
    "nifti_files",
    "pathdb_slides",
    "pathology_images",
    "pathology_protocols",
    "pathology_magnifications",
    "controlled_files",
    "imaging_linkage_status",
    "dataset_unlinked_asset_groups",
    "participant_link_issue_count",
)
CANONICAL_CATEGORY_VALUES = {
    "sex_at_birth": {
        "m": "Male",
        "male": "Male",
        "f": "Female",
        "female": "Female",
        "o": "Other",
        "other": "Other",
        "u": "Unknown",
        "unknown": "Unknown",
    },
    "vital_status": {
        "living": "Alive",
        "alive": "Alive",
        "dead": "Dead",
        "deceased": "Dead",
    },
}


@dataclass(frozen=True)
class DataPaths:
    snapshot_db: Path
    clinical_db: Path
    nifti_db: Path
    pathology_db: Path
    controlled_db: Path
    idc_parquet: Path
    participant_db: Path | None = None
    public_non_dicom_db: Path | None = None
    bundle_manifest: Path | None = None
    install_state: Path | None = None

    def signatures(self) -> tuple[tuple[str, int, int, str], ...]:
        """Return stable cache inputs without hashing multi-gigabyte files."""
        result = []
        for path in (
            self.bundle_manifest,
            self.install_state,
            self.participant_db,
            self.snapshot_db,
            self.clinical_db,
            self.controlled_db,
            self.public_non_dicom_db,
            self.idc_parquet,
        ):
            if path is not None and path.exists():
                stat = path.stat()
                fingerprint = ""
                if path.suffix == ".json":
                    try:
                        fingerprint = str(
                            json.loads(path.read_text(encoding="utf-8")).get(
                                "release_fingerprint", ""
                            )
                        )
                    except (OSError, json.JSONDecodeError):
                        pass
                result.append((str(path), stat.st_mtime_ns, stat.st_size, fingerprint))
            else:
                result.append((str(path or ""), 0, 0, ""))
        return tuple(result)

    def status_rows(self) -> list[dict[str, object]]:
        labels = {
            "V2 Participant Inventory": self.participant_db,
            "V2 public non-DICOM detail": self.public_non_dicom_db,
            "TCIA provenance snapshot": self.snapshot_db,
            "Clinical detail": self.clinical_db,
            "Controlled-access metadata": self.controlled_db,
            "IDC series Parquet": self.idc_parquet,
        }
        rows = []
        for label, path in labels.items():
            rows.append(
                {
                    "Source": label,
                    "Available": path is not None and path.exists(),
                    "Path": str(path),
                    "Size (GB)": round(path.stat().st_size / 1_000_000_000, 3)
                    if path is not None and path.exists()
                    else None,
                }
            )
        return rows


def resolve_data_paths(
    app_dir: Path | None = None, skill_root: Path | None = None
) -> DataPaths:
    app_dir = (app_dir or Path(__file__).resolve().parent).resolve()
    configured_root = os.environ.get("TCIA_QUERY_SKILL_ROOT")
    if skill_root is None:
        skill_root = (
            Path(configured_root).expanduser()
            if configured_root
            else app_dir.parent / "tcia-query-skill"
        )
    v2_cache_dir = Path(
        os.environ.get("TCIA_METADATA_V2_CACHE")
        or os.environ.get("TCIA_V2_INSTALL_DIR")
        or skill_root / "cache" / "tcia-metadata-v2-latest"
    ).expanduser().resolve()

    def selected(env_name: str, default: Path) -> Path:
        value = os.environ.get(env_name)
        return Path(value).expanduser().resolve() if value else default.resolve()

    return DataPaths(
        snapshot_db=selected("TCIA_SNAPSHOT_DB", v2_cache_dir / "tcia_snapshot.sqlite"),
        clinical_db=selected(
            "TCIA_CLINICAL_METADATA_DB", v2_cache_dir / "clinical_metadata.sqlite"
        ),
        nifti_db=selected(
            "TCIA_NIFTI_METADATA_DB", v2_cache_dir / "nifti_metadata.sqlite"
        ),
        pathology_db=selected(
            "TCIA_PATHOLOGY_METADATA_DB", v2_cache_dir / "pathology_metadata.sqlite"
        ),
        controlled_db=selected(
            "TCIA_CONTROLLED_ACCESS_METADATA_DB",
            v2_cache_dir / "controlled_access_metadata.sqlite",
        ),
        idc_parquet=selected(
            "TCIA_IDC_METADATA_PARQUET", app_dir / "idc_metadata.parquet"
        ),
        participant_db=selected(
            "TCIA_PARTICIPANT_INVENTORY_DB", v2_cache_dir / "participant_inventory.sqlite"
        ),
        public_non_dicom_db=selected(
            "TCIA_PUBLIC_NON_DICOM_METADATA_DB",
            v2_cache_dir / "public_non_dicom_metadata.sqlite",
        ),
        bundle_manifest=v2_cache_dir / "tcia_metadata_v2_bundle_manifest.json",
        install_state=v2_cache_dir / "tcia_metadata_v2_install.json",
    )


def normalize_dataset_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def normalize_subject_key(value: object) -> str:
    """Normalize only casing/outer whitespace within a dataset scope."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip().casefold()


def subject_join_key(short_title: object, value: object) -> str:
    """Return a conservative, dataset-scoped cross-source subject key.

    Most datasets preserve the source identifier exactly, so the default is
    still case-folding and outer-whitespace removal only. The exceptions below
    are published identifier conventions verified against the corresponding
    IDC/NIfTI rows. They are intentionally explicit rather than a global fuzzy
    normalization.
    """
    key = normalize_subject_key(value)
    if not key:
        return ""
    dataset_key = normalize_dataset_key(short_title)

    if dataset_key == "cbisddsm":
        match = re.search(r"p[_-]?(\d{5})", key, flags=re.IGNORECASE)
        return f"p_{match.group(1)}" if match else key
    if dataset_key == "ispy2":
        return re.sub(r"^ispy2[-_]*", "", key, flags=re.IGNORECASE)
    if dataset_key == "lungpetctdx":
        return re.sub(r"^lung[_-]?dx[-_]*", "", key, flags=re.IGNORECASE)
    if dataset_key == "ispy1":
        return re.sub(r"^ispy1[-_]*", "", key, flags=re.IGNORECASE)
    if dataset_key in {"acrinfltbreast", "breastmrinactpilot"}:
        return re.sub(r"[^a-z0-9]+", "", key)
    if dataset_key == "cfbgbm" and key.isdigit():
        return str(int(key))
    if dataset_key == "spinalmultiplemyelomaseg":
        return re.sub(r"_[ab]$", "", key)
    return key


def subject_join_keys(frame: pd.DataFrame) -> pd.Series:
    """Vectorized form of :func:`subject_join_key` for inventory builds."""
    keys = (
        frame["subject_id"].fillna("").astype(str).str.strip().str.casefold()
    )
    titles = frame["short_title"].fillna("").astype(str)
    title_keys = titles.map(
        {
            title: normalize_dataset_key(title)
            for title in titles.drop_duplicates().tolist()
        }
    )

    mask = title_keys == "cbisddsm"
    matches = keys.str.extract(r"p[_-]?(\d{5})", expand=False)
    keys = keys.mask(mask & matches.notna(), "p_" + matches.fillna(""))

    for dataset_key, pattern in (
        ("ispy2", r"^ispy2[-_]*"),
        ("lungpetctdx", r"^lung[_-]?dx[-_]*"),
        ("ispy1", r"^ispy1[-_]*"),
    ):
        mask = title_keys == dataset_key
        keys = keys.mask(mask, keys.str.replace(pattern, "", regex=True))

    mask = title_keys.isin({"acrinfltbreast", "breastmrinactpilot"})
    keys = keys.mask(mask, keys.str.replace(r"[^a-z0-9]+", "", regex=True))

    mask = (title_keys == "cfbgbm") & keys.str.fullmatch(r"\d+")
    numeric_keys = keys.str.lstrip("0").replace("", "0")
    keys = keys.mask(mask, numeric_keys)

    mask = title_keys == "spinalmultiplemyelomaseg"
    keys = keys.mask(mask, keys.str.replace(r"_[ab]$", "", regex=True))
    return keys


def normalize_category_key(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return " ".join(str(value).strip().split()).casefold()


def canonical_imaging_token(value: object) -> str:
    text = " ".join(str(value or "").strip().split())
    if text.casefold() == "whole slide image":
        return "Whole Slide Image"
    return text


def split_tokens(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        try:
            if pd.isna(value):
                return []
        except (TypeError, ValueError):
            pass
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                decoded = json.loads(text)
                raw = decoded if isinstance(decoded, list) else [text]
            except json.JSONDecodeError:
                raw = re.split(r"[;,|]", text)
        else:
            raw = re.split(r"[;,|]", text)
    return sorted(
        {
            canonical_imaging_token(item)
            for item in raw
            if canonical_imaging_token(item)
        },
        key=str.casefold,
    )


def join_tokens(values: Iterable[object]) -> str:
    tokens: set[str] = set()
    for value in values:
        tokens.update(split_tokens(value))
    return "; ".join(sorted(tokens, key=str.casefold))


def join_token_json(values: Iterable[object]) -> str:
    """Return a JSON token list without losing commas inside source values."""
    tokens: set[str] = set()
    for value in values:
        tokens.update(split_tokens(value))
    return json.dumps(sorted(tokens, key=str.casefold), separators=(",", ":"))


def canonical_pathology_protocol(value: object) -> str:
    """Standardize a small set of display-equivalent pathology labels."""
    text = " ".join(str(value or "").strip().split())
    key = text.casefold().replace("&", "and")
    if key in {"h and e", "hematoxylin and eosin"}:
        return "H&E"
    if key in {"pdl1", "pd-l1"}:
        return "PD-L1"
    return text


def canonical_pathology_magnification(value: object) -> str:
    """Normalize magnification spelling while retaining its numeric value."""
    text = " ".join(str(value or "").strip().split())
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[x×]", text, flags=re.IGNORECASE)
    if not match:
        return text
    number = match.group(1)
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return f"{number}x"


def canonicalize_patient_categories(frame: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate display-only casing while retaining unmapped coded values."""
    result = frame.copy()

    def case_rank(value: str) -> int:
        letters = "".join(character for character in value if character.isalpha())
        if not letters or (
            any(character.islower() for character in letters)
            and any(character.isupper() for character in letters)
        ):
            return 0
        return 1 if letters.isupper() else 2

    for column in CLINICAL_CATEGORICAL_COLUMNS:
        if column not in result:
            continue
        values = result[column].dropna().map(
            lambda value: " ".join(str(value).strip().split())
        )
        counts = values.value_counts()
        representatives: dict[str, str] = {}
        ranked = sorted(
            counts.items(),
            key=lambda item: (
                -item[1],
                case_rank(item[0]),
                item[0].casefold(),
                item[0],
            ),
        )
        for value, _ in ranked:
            representatives.setdefault(normalize_category_key(value), value)
        representatives.update(CANONICAL_CATEGORY_VALUES.get(column, {}))
        result[column] = result[column].map(
            lambda value: (
                representatives.get(normalize_category_key(value), value)
                if normalize_category_key(value)
                else value
            )
        )
    return result


def exclude_nlst_clinical_only(frame: pd.DataFrame) -> pd.DataFrame:
    """Exclude IDC clinical-only NLST subjects while retaining audit gaps."""
    if frame.empty or "has_any_imaging" not in frame:
        return frame.copy()
    return frame[
        (frame["short_title"].map(normalize_dataset_key) != "nlst")
        | frame["has_any_imaging"].fillna(False)
    ].copy()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def sqlite_objects(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with _connect_readonly(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    return {row[0] for row in rows}


def preferred_object(path: Path, *names: str) -> str | None:
    objects = sqlite_objects(path)
    return next((name for name in names if name in objects), None)


def read_sql(
    path: Path, query: str, params: Sequence[object] | None = None
) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    with _connect_readonly(path) as connection:
        return pd.read_sql_query(query, connection, params=tuple(params or ()))


def load_dataset_catalog(path: Path) -> pd.DataFrame:
    source = preferred_object(path, "agent_dataset_access_summary", "agent_datasets")
    if not source:
        return pd.DataFrame()
    frame = read_sql(path, f"SELECT * FROM {source} WHERE hidden = 0")
    if frame.empty:
        return frame
    if "resolved_access_level" not in frame:
        frame["resolved_access_level"] = frame.get("access_level", "unknown")
    frame["resolved_access_level"] = (
        frame["resolved_access_level"].fillna(frame.get("access_level")).fillna("unknown")
    )
    if "resolved_controlled_access_policy_url" not in frame:
        frame["resolved_controlled_access_policy_url"] = frame.get(
            "controlled_access_policy_url", ""
        )
    frame["dataset_key"] = frame["short_title"].map(normalize_dataset_key)
    type_rank = frame.get("dataset_type", "").map(
        lambda value: 0 if value == "Collection" else 1
    )
    frame = (
        frame.assign(_type_rank=type_rank)
        .sort_values(["dataset_key", "_type_rank", "short_title"])
        .drop(columns="_type_rank")
    )
    return frame


def dataset_key_map(catalog: pd.DataFrame) -> dict[str, str]:
    if catalog.empty:
        return {}
    return (
        catalog.drop_duplicates("dataset_key", keep="first")
        .set_index("dataset_key")["short_title"]
        .to_dict()
    )


def _group_text(series: pd.Series) -> str:
    return join_tokens(series.dropna().tolist())


def _present(value: object) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip() != ""


def collapse_clinical_subject_aliases(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse verified cross-source aliases while retaining source IDs."""
    if frame.empty:
        return frame
    source = frame.copy()
    source["subject_join_key"] = subject_join_keys(source)
    group_columns = ["short_title", "subject_join_key"]
    duplicate_mask = source.duplicated(group_columns, keep=False)
    unique_rows = source.loc[~duplicate_mask].copy()
    unique_rows["clinical_subject_ids"] = unique_rows["subject_id"].astype(str)
    duplicate_rows = source.loc[duplicate_mask]
    if duplicate_rows.empty:
        return unique_rows

    # Alias collisions are rare relative to the full clinical inventory. Only
    # consolidate those small groups; iterating over every one-row patient
    # group made a cold Streamlit startup appear to hang.
    rows: list[dict[str, object]] = []
    for (_, _), group in duplicate_rows.groupby(
        group_columns, sort=False, dropna=False
    ):
        ordered = group.assign(
            _id_length=group["subject_id"].astype(str).str.len()
        ).sort_values(["_id_length", "subject_id"], kind="stable")
        combined = ordered.iloc[0].drop(labels="_id_length").to_dict()
        combined["clinical_subject_ids"] = "; ".join(
            dict.fromkeys(ordered["subject_id"].astype(str).tolist())
        )
        for column in ordered.columns:
            if column in {
                "_id_length",
                "short_title",
                "subject_id",
                "subject_join_key",
                "clinical_subject_ids",
            }:
                continue
            values = ordered[column]
            if column == "has_imaging":
                combined[column] = int(
                    pd.to_numeric(values, errors="coerce").fillna(0).max()
                )
            elif column in {"source_count", "conflict_count"}:
                combined[column] = int(
                    pd.to_numeric(values, errors="coerce").fillna(0).sum()
                )
            elif column == "source_kinds":
                kinds: set[str] = set()
                for value in values:
                    kinds.update(split_tokens(value))
                combined[column] = json.dumps(
                    sorted(kinds, key=str.casefold), separators=(",", ":")
                )
            elif not _present(combined.get(column)):
                combined[column] = next(
                    (value for value in values if _present(value)),
                    combined.get(column),
                )
        rows.append(combined)
    collapsed = pd.DataFrame(rows)
    return pd.concat([unique_rows, collapsed], ignore_index=True, sort=False)


def load_clinical_subjects(path: Path) -> pd.DataFrame:
    source = preferred_object(
        path, "agent_clinical_all_subjects", "clinical_subjects"
    )
    if not source:
        return pd.DataFrame()
    frame = read_sql(path, f"SELECT * FROM {source}")
    if frame.empty:
        return frame
    frame["subject_id"] = frame["subject_id"].astype(str).str.strip()
    frame = frame[frame["subject_id"] != ""].copy()
    frame["has_clinical"] = True
    for column in (
        "age_at_diagnosis",
        "age_at_enrollment_years",
        "age_at_imaging_years",
        "age_at_treatment_years",
    ):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    age_columns = [
        column
        for column in (
            "age_at_diagnosis",
            "age_at_enrollment_years",
            "age_at_imaging_years",
        )
        if column in frame
    ]
    frame["age_at_baseline"] = (
        frame[age_columns].min(axis=1, skipna=True) if age_columns else pd.NA
    )
    return collapse_clinical_subject_aliases(frame)


def load_idc_series(
    parquet_path: Path,
    catalog: pd.DataFrame | None = None,
    columns: Sequence[str] | None = None,
    filters: object | None = None,
) -> pd.DataFrame:
    if not parquet_path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(parquet_path, columns=columns, filters=filters)
    if frame.empty:
        return frame
    key_map = dataset_key_map(catalog) if catalog is not None else {}

    # Every IDC series belongs to a source collection. Derived series may also
    # belong to a TCIA Analysis Result through analysis_result_id. Preserve both
    # dataset contexts so the cohort inventory can represent the WordPress
    # Collection and Analysis Result independently.
    collection_rows = frame.copy()
    collection_rows["dataset_key"] = collection_rows["collection_id"].map(
        normalize_dataset_key
    )
    if catalog is not None and not catalog.empty:
        collection_rows["short_title"] = collection_rows["dataset_key"].map(
            key_map
        )
        collection_rows = collection_rows[
            collection_rows["short_title"].notna()
        ].copy()
    else:
        collection_rows["short_title"] = collection_rows["collection_id"]
    collection_rows["idc_analysis_result_id"] = ""

    result_rows = pd.DataFrame()
    if "analysis_result_id" in frame:
        analysis_ids = frame["analysis_result_id"].fillna("").astype(str).str.strip()
        result_rows = frame[analysis_ids != ""].copy()
        if not result_rows.empty:
            result_rows["dataset_key"] = result_rows["analysis_result_id"].map(
                normalize_dataset_key
            )
            if catalog is not None and not catalog.empty:
                result_rows["short_title"] = result_rows["dataset_key"].map(key_map)
                result_rows = result_rows[
                    result_rows["short_title"].notna()
                ].copy()
            else:
                result_rows["short_title"] = result_rows["analysis_result_id"]
            result_rows["idc_analysis_result_id"] = result_rows[
                "analysis_result_id"
            ]

    frame = pd.concat(
        [collection_rows, result_rows], ignore_index=True, sort=False
    )
    frame["subject_id"] = frame["PatientID"].fillna("").astype(str).str.strip()
    return frame[frame["subject_id"] != ""].copy()


def aggregate_idc(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    frame = frame.copy()
    if "idc_analysis_result_id" not in frame:
        frame["idc_analysis_result_id"] = pd.NA
    frame["subject_join_key"] = subject_join_keys(frame)
    frame = frame[frame["subject_join_key"] != ""]
    group_columns = ["short_title", "subject_join_key"]
    grouped = frame.groupby(group_columns, dropna=False, sort=False)
    result = grouped.agg(
        subject_id=("subject_id", "first"),
        idc_collection_id=("collection_id", "first"),
        idc_analysis_result_id=("idc_analysis_result_id", "first"),
        dicom_series=("SeriesInstanceUID", "nunique"),
        dicom_studies=("StudyInstanceUID", "nunique"),
        dicom_timepoints=("StudyDate", "nunique"),
        dicom_size_mb=("series_size_MB", "sum"),
    ).reset_index()

    def add_unique_text(source_column: str, result_column: str) -> None:
        nonlocal result
        values = frame[group_columns + [source_column]].dropna().copy()
        values[source_column] = values[source_column].astype(str).str.strip()
        values = values[values[source_column] != ""].drop_duplicates()
        values["_sort_key"] = values[source_column].str.casefold()
        values = values.sort_values(group_columns + ["_sort_key"], kind="stable")
        combined = (
            values.groupby(group_columns, sort=False)[source_column]
            .agg("; ".join)
            .rename(result_column)
            .reset_index()
        )
        result = result.merge(combined, on=group_columns, how="left")

    add_unique_text("subject_id", "idc_patient_ids")
    add_unique_text("Modality", "dicom_modalities")
    add_unique_text("BodyPartExamined", "dicom_body_parts")
    result["has_idc_dicom_metadata"] = result["dicom_series"] > 0
    return result


def load_idc_patient_search_summary(
    parquet_path: Path,
    catalog: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return the narrow participant-level DICOM search enrichment.

    Participant identity and availability remain sourced from the V2 Participant
    Inventory. IDC supplies the series count, modality, and BodyPartExamined
    values that the current Participant Inventory contract does not expose.
    """
    result_columns = [
        "short_title",
        "subject_join_key",
        "idc_subject_id",
        "idc_collection_id",
        "idc_analysis_result_id",
        "dicom_series_idc",
        "dicom_timepoints_idc",
        "dicom_modalities",
        "body_parts",
    ]
    columns = [
        "collection_id",
        "analysis_result_id",
        "PatientID",
        "SeriesInstanceUID",
        "StudyDate",
        "Modality",
        "BodyPartExamined",
    ]
    try:
        frame = load_idc_series(parquet_path, catalog, columns=columns)
    except Exception as exc:  # Keep the primary Participant Inventory usable.
        LOGGER.warning("Could not load IDC patient search metadata: %s", exc)
        return pd.DataFrame(columns=result_columns)
    if frame.empty:
        return pd.DataFrame(columns=result_columns)

    frame = frame[
        [
            "short_title",
            "subject_id",
            "collection_id",
            "idc_analysis_result_id",
            "SeriesInstanceUID",
            "StudyDate",
            "Modality",
            "BodyPartExamined",
        ]
    ].copy()
    frame["StudyDate"] = (
        frame["StudyDate"].fillna("").astype(str).str.strip().replace("", pd.NA)
    )
    frame["subject_join_key"] = subject_join_keys(frame)
    frame = frame[frame["subject_join_key"] != ""]
    if frame.empty:
        return pd.DataFrame(columns=result_columns)
    return (
        frame.groupby(["short_title", "subject_join_key"], sort=False)
        .agg(
            idc_subject_id=("subject_id", "first"),
            idc_collection_id=("collection_id", "first"),
            idc_analysis_result_id=("idc_analysis_result_id", "first"),
            dicom_series_idc=("SeriesInstanceUID", "nunique"),
            dicom_timepoints_idc=("StudyDate", "nunique"),
            dicom_modalities=("Modality", join_tokens),
            body_parts=("BodyPartExamined", join_tokens),
        )
        .reset_index()
    )


def aggregate_pathdb(path: Path) -> pd.DataFrame:
    source = preferred_object(path, "agent_pathdb_slides", "pathdb_rows")
    if not source:
        return pd.DataFrame()
    return read_sql(
        path,
        f"""
        SELECT
          collection AS short_title,
          TRIM(patient_id) AS subject_id,
          COUNT(DISTINCT slide_id) AS pathdb_slides,
          COUNT(DISTINCT camic_id) AS pathdb_viewable_slides,
          GROUP_CONCAT(DISTINCT modality) AS pathdb_modalities,
          GROUP_CONCAT(DISTINCT cancer_location) AS pathdb_body_parts
        FROM {source}
        WHERE COALESCE(TRIM(patient_id), '') <> ''
        GROUP BY collection, TRIM(patient_id)
        """,
    ).assign(has_pathdb=lambda frame: frame["pathdb_slides"] > 0)


def aggregate_nifti(path: Path) -> pd.DataFrame:
    source = preferred_object(path, "agent_nifti_files", "radiology_series")
    if not source:
        return pd.DataFrame()
    return read_sql(
        path,
        f"""
        SELECT
          short_title,
          TRIM(subject_id) AS subject_id,
          COUNT(*) AS nifti_files,
          COUNT(DISTINCT study_id) AS nifti_studies,
          COUNT(DISTINCT NULLIF(study_date, '')) AS nifti_timepoints,
          GROUP_CONCAT(DISTINCT modality) AS nifti_modalities,
          GROUP_CONCAT(DISTINCT body_part_examined) AS nifti_body_parts,
          SUM(CASE WHEN COALESCE(is_derived_object, 0) = 1 THEN 1 ELSE 0 END)
            AS nifti_derived_objects
        FROM {source}
        WHERE COALESCE(TRIM(subject_id), '') <> ''
        GROUP BY short_title, TRIM(subject_id)
        """,
    ).assign(has_nifti=lambda frame: frame["nifti_files"] > 0)


def aggregate_controlled(path: Path) -> pd.DataFrame:
    source = preferred_object(path, "agent_controlled_files", "controlled_files")
    if not source:
        return pd.DataFrame()
    return read_sql(
        path,
        f"""
        WITH scoped AS (
          SELECT *,
                 COALESCE(NULLIF(TRIM(patient_id), ''),
                          NULLIF(TRIM(participant_id), '')) AS subject_id
          FROM {source}
        )
        SELECT
          short_title,
          subject_id,
          COUNT(*) AS controlled_files,
          COUNT(DISTINCT series_instance_uid) AS controlled_series,
          COUNT(DISTINCT study_instance_uid) AS controlled_studies,
          COUNT(DISTINCT NULLIF(study_date, '')) AS controlled_timepoints,
          GROUP_CONCAT(DISTINCT COALESCE(NULLIF(modality, ''), image_modality))
            AS controlled_modalities,
          GROUP_CONCAT(DISTINCT body_part_examined) AS controlled_body_parts,
          SUM(CASE WHEN COALESCE(drs_uri, '') <> '' THEN 1 ELSE 0 END)
            AS controlled_manifest_items,
          GROUP_CONCAT(DISTINCT route_system) AS controlled_routes
        FROM scoped
        WHERE subject_id IS NOT NULL
        GROUP BY short_title, subject_id
        """,
    ).assign(has_controlled_metadata=lambda frame: frame["controlled_files"] > 0)


def load_pathology_dataset_summary(path: Path) -> pd.DataFrame:
    source = preferred_object(
        path, "agent_pathology_dataset_summary", "pathology_dataset_summary"
    )
    if not source:
        return pd.DataFrame()
    frame = read_sql(path, f"SELECT * FROM {source}")
    if not frame.empty:
        frame["has_pathology_aspera"] = frame["download_records"].fillna(0) > 0
    return frame


def _outer_merge_sources(
    named_frames: list[tuple[str, pd.DataFrame]],
) -> pd.DataFrame:
    prepared = []
    for source_name, frame in named_frames:
        if frame.empty:
            continue
        source = frame.copy()
        source["subject_join_key"] = subject_join_keys(source)
        source = source[source["subject_join_key"] != ""]
        source = source.rename(
            columns={"subject_id": f"{source_name}_subject_id"}
        )
        prepared.append(source)
    if not prepared:
        return pd.DataFrame(columns=["short_title", "subject_id"])
    result = prepared[0]
    for frame in prepared[1:]:
        result = result.merge(
            frame, on=["short_title", "subject_join_key"], how="outer"
        )
    identifier_columns = [
        column
        for column in (
            "clinical_subject_id",
            "idc_subject_id",
            "pathdb_subject_id",
            "nifti_subject_id",
            "controlled_subject_id",
        )
        if column in result
    ]
    result["subject_id"] = result[identifier_columns].bfill(axis=1).iloc[:, 0]
    return result


def load_participant_inventory(path: Path | None) -> pd.DataFrame:
    """Load the V2 participant search surface without touching detail artifacts."""
    if path is None or preferred_object(path, "agent_participant_search") is None:
        return pd.DataFrame()
    return read_sql(path, "SELECT * FROM agent_participant_search")


def load_participant_assets(
    path: Path | None, participant_key: str | None = None
) -> pd.DataFrame:
    if path is None or preferred_object(path, "agent_participant_assets") is None:
        return pd.DataFrame()
    query = "SELECT * FROM agent_participant_assets"
    params: list[object] = []
    if participant_key:
        query += " WHERE participant_key = ?"
        params.append(participant_key)
    return read_sql(path, query, params)


def load_participant_identifiers(path: Path | None, participant_key: str) -> pd.DataFrame:
    if path is None or preferred_object(path, "agent_participant_identifiers") is None:
        return pd.DataFrame()
    return read_sql(
        path,
        "SELECT * FROM agent_participant_identifiers WHERE participant_key = ? "
        "ORDER BY managed_system, identifier_namespace, raw_identifier",
        [participant_key],
    )


def load_participant_identity_evidence(
    path: Path | None, participant_key: str
) -> pd.DataFrame:
    if path is None or preferred_object(path, "agent_participant_identity_evidence") is None:
        return pd.DataFrame()
    return read_sql(
        path,
        "SELECT * FROM agent_participant_identity_evidence "
        "WHERE participant_key = ? ORDER BY resolution_method, identity_evidence_id",
        [participant_key],
    )


def load_participant_inventory_clinical_values(
    path: Path | None, participant_key: str | None = None
) -> pd.DataFrame:
    if path is None or preferred_object(path, "agent_participant_clinical_values") is None:
        return pd.DataFrame()
    query = "SELECT * FROM agent_participant_clinical_values"
    params: list[object] = []
    if participant_key:
        query += " WHERE participant_key = ?"
        params.append(participant_key)
    return read_sql(path, query, params)


def load_dataset_coverage_states(path: Path | None, short_title: str) -> pd.DataFrame:
    if path is None:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    if preferred_object(path, "agent_dataset_assets_without_participant_crosswalk"):
        unlinked = read_sql(
            path,
            "SELECT 'missing participant crosswalk' AS coverage_state, * "
            "FROM agent_dataset_assets_without_participant_crosswalk "
            "WHERE lower(short_title) = lower(?)",
            [short_title],
        )
        frames.append(unlinked)
    if preferred_object(path, "agent_participant_link_issues"):
        issues = read_sql(
            path,
            "SELECT 'participant link issue' AS coverage_state, * "
            "FROM agent_participant_link_issues WHERE lower(short_title) = lower(?)",
            [short_title],
        )
        frames.append(issues)
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


def participant_availability_rows(
    participant: Mapping[str, object],
) -> list[dict[str, str]]:
    """Translate V2 inventory flags into stable user-facing coverage states."""

    def enabled(name: str) -> bool:
        value = participant.get(name, False)
        try:
            return False if pd.isna(value) else bool(value)
        except (TypeError, ValueError):
            return bool(value)

    def count(name: str) -> int:
        value = participant.get(name, 0)
        try:
            return 0 if pd.isna(value) else int(value)
        except (TypeError, ValueError):
            return 0

    unlinked = count("dataset_unlinked_asset_groups")
    public_non_dicom = enabled("has_public_non_dicom")
    if public_non_dicom and unlinked:
        non_dicom_state = "Participant linked; dataset also has unlinked asset groups"
    elif public_non_dicom:
        non_dicom_state = "Participant linked"
    elif unlinked:
        non_dicom_state = "Dataset-level only; participant crosswalk unavailable"
    else:
        non_dicom_state = "Not represented in the participant inventory"

    return [
        {
            "Data": "Public DICOM",
            "Coverage": (
                "Participant linked"
                if enabled("has_public_dicom")
                else "Not represented in the participant inventory"
            ),
            "Detail": (
                "Includes public Aspera DICOM outside IDC; IDC series are shown when available."
                if count("public_dicom_files_outside_idc")
                else "Series details are queried from IDC."
            ),
        },
        {
            "Data": "Public non-DICOM",
            "Coverage": non_dicom_state,
            "Detail": "Includes public imaging files outside IDC DICOM.",
        },
        {
            "Data": "Clinical data",
            "Coverage": (
                "Participant linked"
                if enabled("has_clinical")
                else "Not represented in the participant inventory"
            ),
            "Detail": "Raw and standardized facts are available from the clinical detail artifact.",
        },
        {
            "Data": "Controlled access",
            "Coverage": (
                "Participant-linked metadata"
                if enabled("has_controlled_metadata")
                else "Not represented in the participant inventory"
            ),
            "Detail": "Authorization is required for controlled payloads.",
        },
    ]


def _load_participant_search_index(path: Path | None) -> pd.DataFrame:
    if path is None or preferred_object(path, "agent_participant_search") is None:
        return pd.DataFrame()
    participants = read_sql(
        path,
        """
        SELECT participant_key, dataset_type, short_title,
               display_participant_id, identity_scope,
               within_dataset_identity_status, identity_resolution_method,
               cross_dataset_identity_status
        FROM participants
        """,
    ).rename(
        columns={"display_participant_id": "subject_id"}
    )
    if participants.empty:
        return participants

    # The schema-6 gate in v2_artifacts makes agent_participant_search the
    # semantic contract. Mirror that view's predicates over indexed base tables
    # here because materializing its correlated namespace columns adds tens of
    # seconds to a cold Streamlit startup. A future schema cannot silently use
    # these predicates: it is rejected before this query runs.
    asset_counts = read_sql(
        path,
        """
        SELECT participant_key,
               COUNT(DISTINCT participant_asset_id) AS inventory_rows,
               MAX(access_level = 'open') AS has_open_data,
               MAX(access_level = 'controlled') AS has_controlled_data,
               MAX(access_level = 'open'
                   AND instr(upper(COALESCE(file_format, '')), 'DICOM') > 0)
                 AS has_public_dicom,
               MAX(source_artifact = 'public_non_dicom_metadata'
                   AND (COALESCE(file_format, '') = ''
                        OR instr(upper(file_format), 'DICOM') = 0
                        OR instr(upper(file_format), 'NIFTI') > 0))
                 AS has_public_non_dicom,
               MAX(data_domain = 'clinical') AS has_clinical,
               group_concat(DISTINCT NULLIF(data_domain, '')) AS data_domains,
               group_concat(DISTINCT NULLIF(modality, '')) AS modalities,
               group_concat(DISTINCT NULLIF(file_format, '')) AS file_formats,
               MAX(lower(COALESCE(data_domain, '')) = 'imaging_annotation'
                   OR instr(lower(COALESCE(object_role, '')), 'segmentation') > 0
                   OR lower(COALESCE(object_role, '')) = 'annotation_snapshot'
                   OR (';' || replace(replace(upper(COALESCE(modality, '')), ' ', ''), ',', ';') || ';')
                        LIKE '%;SEG;%'
                   OR (';' || replace(replace(upper(COALESCE(modality, '')), ' ', ''), ',', ';') || ';')
                        LIKE '%;RTSTRUCT;%'
                   OR (';' || replace(replace(upper(COALESCE(modality, '')), ' ', ''), ',', ';') || ';')
                        LIKE '%;SR;%'
                   OR (';' || replace(replace(upper(COALESCE(modality, '')), ' ', ''), ',', ';') || ';')
                        LIKE '%;ANN;%') AS has_annotations,
               MAX(access_level = 'controlled') AS has_controlled_metadata,
               MAX(access_level IN ('open', 'open_noncommercial')
                   AND managed_system = 'crdc_idc'
                   AND instr(upper(COALESCE(file_format, '')), 'DICOM') > 0)
                 AS has_idc_dicom_metadata,
               SUM(CASE WHEN access_level IN ('open', 'open_noncommercial')
                              AND managed_system = 'crdc_idc'
                              AND instr(upper(COALESCE(file_format, '')), 'DICOM') > 0
                        THEN COALESCE(series_count, 0) ELSE 0 END) AS dicom_series,
               SUM(CASE WHEN access_level IN ('open', 'open_noncommercial')
                              AND source_artifact = 'public_non_dicom_metadata'
                              AND instr(upper(COALESCE(file_format, '')), 'DICOM') > 0
                        THEN COALESCE(file_count, 0) ELSE 0 END)
                 AS public_dicom_files_outside_idc,
               SUM(CASE WHEN source_artifact = 'public_non_dicom_metadata'
                              AND (COALESCE(file_format, '') = ''
                                   OR instr(upper(file_format), 'DICOM') = 0
                                   OR instr(upper(file_format), 'NIFTI') > 0)
                        THEN COALESCE(file_count, 0) ELSE 0 END) AS public_non_dicom_files,
               SUM(CASE WHEN managed_system = 'crdc_idc' AND upper(COALESCE(modality, '')) = 'CT'
                        THEN COALESCE(series_count, 0) ELSE 0 END) AS ct_series,
               SUM(CASE WHEN upper(COALESCE(file_format, '')) IN ('MHA', 'MHD')
                        THEN COALESCE(file_count, 0) ELSE 0 END) AS mha_volumes,
               SUM(CASE WHEN upper(COALESCE(file_format, '')) IN ('NIFTI', 'NII', 'NII.GZ')
                        THEN COALESCE(file_count, 0) ELSE 0 END) AS nifti_files,
               SUM(CASE WHEN lower(COALESCE(data_domain, '')) = 'pathology'
                          OR lower(COALESCE(media_kind, '')) IN ('whole_slide_image', 'microscopy_image')
                        THEN COALESCE(file_count, 0) ELSE 0 END) AS pathology_images,
               SUM(CASE WHEN access_level = 'controlled' THEN COALESCE(file_count, 0) ELSE 0 END) AS controlled_files
        FROM agent_participant_assets
        GROUP BY participant_key
        """,
    )
    result = participants.merge(asset_counts, on="participant_key", how="left")
    for column in (
        "has_open_data", "has_controlled_data", "has_public_dicom",
        "has_public_non_dicom", "has_clinical", "has_annotations",
    ):
        result[column] = pd.to_numeric(result.get(column), errors="coerce").fillna(0)
    for column in (
        "dicom_series", "public_dicom_files_outside_idc", "public_non_dicom_files",
        "ct_series", "mha_volumes", "nifti_files", "pathology_images", "controlled_files",
    ):
        result[column] = pd.to_numeric(result.get(column), errors="coerce").fillna(0)
    result["has_idc_dicom_metadata"] = pd.to_numeric(
        result.get("has_idc_dicom_metadata"), errors="coerce"
    ).fillna(0)
    return result


def enrich_participants_with_clinical_detail(
    participants: pd.DataFrame, path: Path
) -> pd.DataFrame:
    """Add filterable clinical summaries after the detail artifact is installed."""
    result = participants.copy()
    clinical_columns = [
        "sex_at_birth", "race", "ethnicity", "age_at_baseline",
        "age_at_diagnosis", "age_at_enrollment_years", "age_at_imaging_years",
        "age_at_treatment_years",
        "primary_diagnosis", "primary_site", "stage", "grade", "vital_status",
        "recurrence", "progression", "response", "screening_result",
        "days_to_death", "days_to_last_followup", "overall_survival_days",
        "progression_free_survival_days", "primary_diagnosis_value_role",
        "primary_site_value_role", "primary_diagnosis_is_inferred",
        "primary_site_is_inferred", "source_kinds", "source_count", "conflict_count",
        "clinical_subject_ids",
    ]
    if not path.exists():
        for column in clinical_columns:
            if column not in result:
                result[column] = pd.NA
        return result

    clinical = load_clinical_subjects(path)
    if clinical.empty:
        for column in clinical_columns:
            if column not in result:
                result[column] = pd.NA
        return result

    result["_clinical_subject_key"] = subject_join_keys(result)
    clinical["_clinical_subject_key"] = subject_join_keys(clinical)
    available = [column for column in clinical_columns if column in clinical]
    detail = clinical[["short_title", "_clinical_subject_key", *available]].copy()
    detail = detail.drop_duplicates(["short_title", "_clinical_subject_key"], keep="first")
    result = result.merge(
        detail,
        on=["short_title", "_clinical_subject_key"],
        how="left",
        validate="many_to_one",
    ).drop(columns="_clinical_subject_key")
    for column in clinical_columns:
        if column not in result:
            result[column] = pd.NA
    return result


def load_participant_pathology_facets(path: Path | None) -> pd.DataFrame:
    """Return additive participant-level pathology filter values from detail."""
    columns = [
        "short_title",
        "subject_join_key",
        "pathology_protocols",
        "pathology_magnifications",
    ]
    source = (
        preferred_object(path, "agent_public_non_dicom_image_metadata")
        if path is not None
        else None
    )
    if not source:
        return pd.DataFrame(columns=columns)

    def aggregate_field(
        field: str,
        output: str,
        canonicalizer,
    ) -> pd.DataFrame:
        frame = read_sql(
            path,
            f"""
            SELECT DISTINCT short_title, TRIM(subject_id) AS subject_id,
                            TRIM({field}) AS value
            FROM {source}
            WHERE COALESCE(TRIM(subject_id), '') <> ''
              AND COALESCE(TRIM({field}), '') <> ''
            """,
        )
        if frame.empty:
            return pd.DataFrame(columns=["short_title", "subject_join_key", output])
        frame["subject_join_key"] = subject_join_keys(frame)
        frame["value"] = frame["value"].map(canonicalizer)
        frame = frame[
            (frame["subject_join_key"] != "") & (frame["value"] != "")
        ].drop_duplicates(["short_title", "subject_join_key", "value"])
        return (
            frame.groupby(["short_title", "subject_join_key"], sort=False)["value"]
            .agg(
                lambda values: json.dumps(
                    sorted(set(values), key=str.casefold), separators=(",", ":")
                )
            )
            .rename(output)
            .reset_index()
        )

    protocols = aggregate_field(
        "pathology_protocol", "pathology_protocols", canonical_pathology_protocol
    )
    magnifications = aggregate_field(
        "magnification",
        "pathology_magnifications",
        canonical_pathology_magnification,
    )
    if protocols.empty:
        result = magnifications
    elif magnifications.empty:
        result = protocols
    else:
        result = protocols.merge(
            magnifications,
            on=["short_title", "subject_join_key"],
            how="outer",
        )
    for column in columns:
        if column not in result:
            result[column] = pd.NA
    return result[columns]


def build_patient_index(paths: DataPaths) -> pd.DataFrame:
    """Create one row per V2 dataset-scoped participant.

    The compact Participant Inventory is the primary search/summary source.
    IDC contributes narrow participant-level DICOM search enrichments. Clinical
    summaries and pathology facets are merged additively from research-detail
    artifacts that the Participant Explorer prepares during startup.
    """
    build_started = time.perf_counter()
    patients = _load_participant_search_index(paths.participant_db)
    if patients.empty:
        raise RuntimeError(
            f"No V2 participant inventory found in {paths.participant_db}"
        )
    patients = enrich_participants_with_clinical_detail(patients, paths.clinical_db)
    pathology_facets = load_participant_pathology_facets(
        paths.public_non_dicom_db
    )
    if not pathology_facets.empty:
        patients["subject_join_key"] = subject_join_keys(patients)
        patients = patients.merge(
            pathology_facets,
            on=["short_title", "subject_join_key"],
            how="left",
            validate="many_to_one",
        ).drop(columns="subject_join_key")
    for column in ("pathology_protocols", "pathology_magnifications"):
        if column not in patients:
            patients[column] = pd.NA
    open_mask = patients["has_open_data"].astype(bool)
    controlled_mask = patients["has_controlled_data"].astype(bool)
    patients["resolved_access_level"] = "unknown"
    patients.loc[open_mask, "resolved_access_level"] = "open"
    patients.loc[controlled_mask, "resolved_access_level"] = "controlled"
    patients.loc[open_mask & controlled_mask, "resolved_access_level"] = "mixed"

    available = pd.Series("", index=patients.index, dtype="object")
    for label, mask in (
        ("Public DICOM", patients["has_public_dicom"].astype(bool)),
        ("MHA volumes", patients["mha_volumes"] > 0),
        ("NIfTI files", patients["nifti_files"] > 0),
        ("Pathology images", patients["pathology_images"] > 0),
        ("Annotations / segmentations", patients["has_annotations"].astype(bool)),
        ("Clinical data", patients["has_clinical"].astype(bool)),
    ):
        separator = available.where(available == "", "; ")
        available = available.where(~mask, available + separator + label)
    patients["available_imaging"] = available

    catalog = load_dataset_catalog(paths.snapshot_db)
    if not catalog.empty:
        keep = [
            column
            for column in (
                "short_title", "title", "doi", "link", "species", "cancer_types",
                "cancer_locations", "program", "source_collections",
                "resolved_controlled_access_policy_url", "licenses",
            )
            if column in catalog
        ]
        patients = patients.merge(
            catalog[keep].drop_duplicates("short_title"), on="short_title", how="left"
        )

    idc_search = load_idc_patient_search_summary(paths.idc_parquet, catalog)
    if not idc_search.empty:
        patients["subject_join_key"] = subject_join_keys(patients)
        patients = patients.merge(
            idc_search,
            on=["short_title", "subject_join_key"],
            how="left",
        ).drop(columns="subject_join_key")
        patients["dicom_series"] = pd.concat(
            [
                pd.to_numeric(patients["dicom_series"], errors="coerce"),
                pd.to_numeric(patients["dicom_series_idc"], errors="coerce"),
            ],
            axis=1,
        ).max(axis=1, skipna=True)
        patients["dicom_timepoints"] = pd.to_numeric(
            patients["dicom_timepoints_idc"], errors="coerce"
        ).fillna(0)
        patients["modalities"] = patients.apply(
            lambda row: join_tokens(
                [row.get("modalities"), row.get("dicom_modalities")]
            ),
            axis=1,
        )
        patients = patients.drop(
            columns=[
                "dicom_series_idc",
                "dicom_timepoints_idc",
                "dicom_modalities",
            ]
        )

    unlinked = read_sql(
        paths.participant_db,
        "SELECT short_title, COUNT(*) AS dataset_unlinked_asset_groups "
        "FROM agent_dataset_assets_without_participant_crosswalk GROUP BY short_title",
    )
    issues = read_sql(
        paths.participant_db,
        "SELECT short_title, lower(trim(raw_identifier)) AS _issue_subject_key, "
        "COUNT(*) AS participant_link_issue_count FROM agent_participant_link_issues "
        "WHERE COALESCE(trim(raw_identifier), '') <> '' "
        "GROUP BY short_title, lower(trim(raw_identifier))",
    )
    if not unlinked.empty:
        patients = patients.merge(unlinked, on="short_title", how="left")
    if not issues.empty:
        patients["_issue_subject_key"] = patients["subject_id"].astype(str).str.strip().str.casefold()
        patients = patients.merge(
            issues, on=["short_title", "_issue_subject_key"], how="left"
        ).drop(columns="_issue_subject_key")

    for column in (
        "dicom_series", "dicom_timepoints", "public_dicom_files_outside_idc", "public_non_dicom_files",
        "ct_series", "mha_volumes", "nifti_files", "pathology_images", "controlled_files",
        "dataset_unlinked_asset_groups", "participant_link_issue_count",
    ):
        if column not in patients:
            patients[column] = 0
        patients[column] = pd.to_numeric(patients[column], errors="coerce").fillna(0).astype(int)
    for column in (
        "has_public_dicom", "has_public_non_dicom", "has_clinical",
        "has_controlled_metadata", "has_annotations",
    ):
        if column not in patients:
            patients[column] = False
        patients[column] = patients[column].astype("boolean").fillna(False).astype(bool)
    patients["has_idc_dicom_metadata"] = patients[
        "has_idc_dicom_metadata"
    ].astype("boolean").fillna(False).astype(bool)
    patients["has_public_dicom_outside_idc"] = (
        patients["public_dicom_files_outside_idc"] > 0
    )
    patients["has_nifti"] = patients["nifti_files"] > 0
    patients["has_pathdb"] = patients["pathology_images"] > 0
    patients["has_pathology_aspera"] = False
    patients["has_multiple_imaging_dates"] = patients["dicom_timepoints"] >= 2
    patients["pathdb_slides"] = patients["pathology_images"]
    for concept in ("primary_diagnosis", "primary_site"):
        flag_column = f"{concept}_is_inferred"
        role_column = f"{concept}_value_role"
        inferred = pd.to_numeric(
            patients.get(flag_column, pd.Series(pd.NA, index=patients.index)),
            errors="coerce",
        ).astype("boolean")
        role_inferred = patients.get(
            role_column, pd.Series("", index=patients.index)
        ).fillna("").astype(str).str.casefold().eq("inferred")
        patients[flag_column] = (
            inferred.fillna(role_inferred).fillna(False).astype(bool)
        )
    patients["has_any_imaging"] = patients[
        ["has_public_dicom", "has_public_non_dicom", "has_controlled_metadata"]
    ].any(axis=1)
    patients["imaging_linkage_status"] = "Participant-linked inventory"
    patients.loc[
        (patients["dataset_unlinked_asset_groups"] > 0)
        | (patients["participant_link_issue_count"] > 0),
        "imaging_linkage_status",
    ] = "Partial coverage or linkage review"
    patients["patient_key"] = patients["participant_key"]
    patients["dataset_memberships"] = patients.apply(
        lambda row: f"{row['short_title']} [{row['dataset_type']}]", axis=1
    )
    patients["dataset_count"] = 1
    patients["member_short_titles_json"] = patients["short_title"].map(
        lambda value: json.dumps([str(value)], separators=(",", ":"))
    )
    patients["member_patient_keys_json"] = patients["participant_key"].map(
        lambda value: json.dumps([str(value)], separators=(",", ":"))
    )
    patients["member_subject_ids_json"] = patients["subject_id"].map(
        lambda value: json.dumps([str(value)], separators=(",", ":"))
    )
    patients["is_grouped_patient"] = False
    if "body_parts" not in patients:
        patients["body_parts"] = ""
    patients["body_parts"] = patients["body_parts"].fillna("")
    patients = canonicalize_patient_categories(patients)
    patients = patients.sort_values(
        ["short_title", "subject_id"], key=lambda series: series.astype(str).str.casefold()
    ).reset_index(drop=True)
    LOGGER.info(
        "Participant Inventory: loaded %s dataset-scoped participants in %.1fs",
        f"{len(patients):,}", time.perf_counter() - build_started,
    )
    return patients


def build_grouped_patient_index(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Group verified Collection/Analysis Result memberships for presentation.

    The returned membership frame preserves every dataset-scoped source row.
    Grouping is allowed when IDC supplies the same collection identifier and
    normalized PatientID, or when an Analysis Result explicitly names one
    source Collection in WordPress and the normalized PatientID matches exactly.
    """
    if frame.empty:
        empty = frame.copy()
        empty["patient_group_key"] = pd.Series(dtype=str)
        return empty, empty

    members = frame.copy()
    members["patient_group_key"] = members["patient_key"].astype(str)
    collection_ids = members.get(
        "idc_collection_id", pd.Series(index=members.index, dtype=str)
    ).fillna("").astype(str).str.strip().str.casefold()
    idc_subjects = members.get(
        "idc_subject_id", pd.Series(index=members.index, dtype=str)
    ).fillna("").astype(str).str.strip().str.casefold()
    candidates = "idc|" + collection_ids + "|" + idc_subjects
    eligible = (collection_ids != "") & (idc_subjects != "")

    candidate_rows = members.loc[eligible, ["dataset_type"]].copy()
    candidate_rows["candidate_key"] = candidates.loc[eligible]
    type_sets = candidate_rows.groupby("candidate_key")["dataset_type"].agg(
        lambda values: frozenset(str(value) for value in values)
    )
    verified_keys = set(
        type_sets[
            type_sets.map(
                lambda values: {"Collection", "Analysis Result"}.issubset(values)
            )
        ].index
    )
    verified = eligible & candidates.isin(verified_keys)
    members.loc[verified, "patient_group_key"] = candidates.loc[verified]

    # Non-DICOM Analysis Results such as NIfTI packages may not have IDC
    # collection identifiers even though WordPress explicitly identifies their
    # source Collection. Extend grouping only for exact subject matches with one
    # unambiguous, explicitly named Collection membership.
    def explicitly_names_collection(evidence: object, short_title: object) -> bool:
        if not _present(evidence) or not _present(short_title):
            return False
        pattern = (
            r"(?<![A-Za-z0-9_-])"
            + re.escape(str(short_title).strip())
            + r"(?![A-Za-z0-9_-])"
        )
        return re.search(pattern, str(evidence), flags=re.IGNORECASE) is not None

    collection_rows_by_subject: dict[str, list[tuple[str, str]]] = {}
    collection_rows = members[members["dataset_type"] == "Collection"]
    for row in collection_rows.itertuples(index=False):
        subject_key = normalize_subject_key(getattr(row, "subject_id", ""))
        if not subject_key:
            continue
        collection_rows_by_subject.setdefault(subject_key, []).append(
            (str(row.short_title), str(row.patient_group_key))
        )

    ungrouped_results = members[
        (members["dataset_type"] == "Analysis Result")
        & (members["patient_group_key"] == members["patient_key"])
    ]
    for row in ungrouped_results.itertuples(index=True):
        subject_key = normalize_subject_key(getattr(row, "subject_id", ""))
        evidence = getattr(row, "source_collections", "")
        matched_group_keys = {
            group_key
            for short_title, group_key in collection_rows_by_subject.get(
                subject_key, []
            )
            if explicitly_names_collection(evidence, short_title)
        }
        if len(matched_group_keys) == 1:
            members.at[row.Index, "patient_group_key"] = next(
                iter(matched_group_keys)
            )

    type_rank = members.get("dataset_type", "").map(
        lambda value: 0 if value == "Collection" else 1
    )
    clinical_rank = ~members.get(
        "has_clinical", pd.Series(False, index=members.index)
    ).fillna(False).astype(bool)
    ordered = (
        members.assign(_type_rank=type_rank, _clinical_rank=clinical_rank)
        .sort_values(
            ["patient_group_key", "_clinical_rank", "_type_rank", "short_title"],
            kind="stable",
        )
        .drop(columns=["_type_rank", "_clinical_rank"])
    )

    def decorate(group: pd.DataFrame) -> dict[str, object]:
        combined = group.iloc[0].to_dict()
        memberships = [
            f"{row.short_title} [{row.dataset_type}]"
            for row in group[["short_title", "dataset_type"]]
            .drop_duplicates()
            .itertuples(index=False)
        ]
        member_titles = list(dict.fromkeys(group["short_title"].astype(str)))
        member_keys = list(dict.fromkeys(group["patient_key"].astype(str)))
        combined["patient_group_key"] = str(group.iloc[0]["patient_group_key"])
        combined["patient_key"] = combined["patient_group_key"]
        combined["dataset_memberships"] = "; ".join(memberships)
        combined["dataset_count"] = len(member_titles)
        combined["member_short_titles_json"] = json.dumps(
            member_titles, separators=(",", ":")
        )
        combined["member_patient_keys_json"] = json.dumps(
            member_keys, separators=(",", ":")
        )
        combined["member_subject_ids_json"] = json.dumps(
            list(dict.fromkeys(group["subject_id"].astype(str))),
            separators=(",", ":"),
        )
        combined["is_grouped_patient"] = len(member_titles) > 1

        for column in (
            "available_imaging",
            "modalities",
            "body_parts",
            "file_formats",
            "source_kinds",
        ):
            if column in group:
                combined[column] = join_tokens(group[column].tolist())
        for column in (
            "pathology_protocols",
            "pathology_magnifications",
        ):
            if column in group:
                combined[column] = join_token_json(group[column].tolist())
        for column in (
            "dicom_series",
            "dicom_timepoints",
            "public_non_dicom_files",
            "dicom_studies",
            "nifti_files",
            "nifti_studies",
            "nifti_timepoints",
            "pathdb_slides",
            "pathdb_viewable_slides",
            "controlled_files",
            "controlled_series",
            "controlled_studies",
            "controlled_timepoints",
            "conflict_count",
            "source_count",
        ):
            if column in group:
                combined[column] = pd.to_numeric(
                    group[column], errors="coerce"
                ).max(skipna=True)
        for column in (
            "has_clinical",
            "has_idc_dicom_metadata",
            "has_public_dicom",
            "has_pathdb",
            "has_nifti",
            "has_controlled_metadata",
            "has_pathology_aspera",
            "has_any_imaging",
            "has_annotations",
            "has_multiple_imaging_dates",
        ):
            if column in group:
                combined[column] = bool(
                    group[column].astype("boolean").fillna(False).any()
                )

        access_values = {
            str(value).strip()
            for value in group.get(
                "resolved_access_level", pd.Series(dtype=str)
            ).dropna()
            if str(value).strip()
        }
        if len(access_values) == 1:
            combined["resolved_access_level"] = next(iter(access_values))
        elif access_values and access_values.issubset(OPEN_ACCESS_LEVELS):
            combined["resolved_access_level"] = (
                "open_noncommercial"
                if "open_noncommercial" in access_values
                else "open"
            )
        elif access_values:
            combined["resolved_access_level"] = "mixed"
        return combined

    group_sizes = ordered["patient_group_key"].value_counts()
    single_keys = set(group_sizes[group_sizes == 1].index)
    singles = ordered[ordered["patient_group_key"].isin(single_keys)].copy()
    singles["dataset_memberships"] = singles.apply(
        lambda row: f"{row['short_title']} [{row.get('dataset_type', 'Dataset')}]",
        axis=1,
    )
    singles["dataset_count"] = 1
    singles["member_short_titles_json"] = singles["short_title"].map(
        lambda value: json.dumps([str(value)], separators=(",", ":"))
    )
    singles["member_patient_keys_json"] = singles["patient_key"].map(
        lambda value: json.dumps([str(value)], separators=(",", ":"))
    )
    singles["member_subject_ids_json"] = singles["subject_id"].map(
        lambda value: json.dumps([str(value)], separators=(",", ":"))
    )
    singles["is_grouped_patient"] = False

    grouped_rows = [
        decorate(group)
        for _, group in ordered[
            ~ordered["patient_group_key"].isin(single_keys)
        ].groupby("patient_group_key", sort=False)
    ]
    grouped = pd.concat(
        [singles, pd.DataFrame(grouped_rows)], ignore_index=True, sort=False
    )
    grouped = grouped.sort_values(
        ["short_title", "subject_id"],
        key=lambda series: series.astype(str).str.casefold(),
    ).reset_index(drop=True)
    return grouped, members


def filter_patient_groups_by_dataset_type(
    patients: pd.DataFrame,
    memberships: pd.DataFrame,
    dataset_type: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Filter displayed patient groups and source memberships by dataset type."""
    if dataset_type not in DATASET_TYPE_FILTERS:
        raise ValueError(f"Unsupported dataset type filter: {dataset_type}")
    if dataset_type == "All":
        return patients, memberships
    scoped_memberships = memberships[
        memberships["dataset_type"].astype(str).eq(dataset_type)
    ].copy()
    group_keys = set(scoped_memberships["patient_group_key"].astype(str))
    scoped_patients = patients[
        patients["patient_group_key"].astype(str).isin(group_keys)
    ].copy()
    return scoped_patients, scoped_memberships


def count_visible_dataset_contexts(
    patients: pd.DataFrame,
    memberships: pd.DataFrame,
    selected_titles: Sequence[str] = (),
) -> int:
    """Count logical dataset contexts represented by the visible patients.

    When users explicitly select datasets, the count describes those selected
    contexts instead of also expanding related memberships attached to the same
    grouped patient rows.
    """
    if patients.empty or memberships.empty:
        return 0
    visible_keys = set(patients["patient_group_key"].astype(str))
    visible = memberships[
        memberships["patient_group_key"].astype(str).isin(visible_keys)
    ]
    if selected_titles:
        wanted = {str(value) for value in selected_titles}
        visible = visible[visible["short_title"].astype(str).isin(wanted)]
    return int(visible["short_title"].astype(str).nunique())


def load_patient_clinical_facts(
    path: Path,
    short_title: str,
    subject_id: str,
    subject_ids: Sequence[str] | None = None,
) -> pd.DataFrame:
    source = preferred_object(path, "agent_clinical_facts", "clinical_facts")
    if not source:
        return pd.DataFrame()
    identifiers = [
        str(value).strip()
        for value in (subject_ids or [subject_id])
        if str(value).strip()
    ]
    if not identifiers:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in identifiers)
    return read_sql(
        path,
        f"""
        SELECT concept, value_text, value_number, unit, source_kind,
               source_priority, source_url, source_date, original_column,
               evidence_scope, is_inferred, provenance_json
        FROM {source}
        WHERE short_title = ? AND subject_id IN ({placeholders})
        ORDER BY concept, source_priority DESC, source_kind
        """,
        (short_title, *identifiers),
    )


def load_patient_clinical_longitudinal(
    path: Path,
    short_title: str,
    subject_ids: Sequence[str],
) -> pd.DataFrame:
    """Return visit-, scanner-, and file-grain observations without collapsing time."""
    source = preferred_object(
        path,
        "agent_clinical_longitudinal_observations",
        "clinical_longitudinal_observations",
    )
    identifiers = [
        str(value).strip() for value in subject_ids if str(value).strip()
    ]
    if not source or not identifiers:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in identifiers)
    return read_sql(
        path,
        f"""
        SELECT observation_type, study_datetime, file_name,
               age_at_imaging_years, manufacturer, manufacturer_model_name,
               magnetic_field_strength_t, acquisition_dimensionality,
               scanner_site, sequence_class, sequence_tags,
               slice_thickness_mm, spacing_between_slices_mm,
               repetition_time_ms, echo_time_ms, inversion_time_ms
        FROM {source}
        WHERE lower(short_title) = lower(?)
          AND subject_id IN ({placeholders})
        ORDER BY COALESCE(study_datetime, ''), observation_type,
                 COALESCE(file_name, '')
        """,
        (short_title, *identifiers),
    )


def load_patient_idc(
    paths: DataPaths,
    catalog: pd.DataFrame,
    short_title: str,
    subject_id: str,
    collection_id: str | None = None,
    analysis_result_id: str | None = None,
    direct_collection_only: bool = False,
) -> pd.DataFrame:
    if not paths.idc_parquet.exists():
        return pd.DataFrame()
    if collection_id:
        parquet_filters = [("collection_id", "==", collection_id)]
        if analysis_result_id:
            parquet_filters.append(
                ("analysis_result_id", "==", analysis_result_id)
            )
        frame = pd.read_parquet(
            paths.idc_parquet,
            filters=parquet_filters,
        )
        if not frame.empty:
            frame["short_title"] = short_title
            frame["subject_id"] = frame["PatientID"].astype(str).str.strip()
    else:
        frame = load_idc_series(paths.idc_parquet, catalog)
    if frame.empty:
        return frame
    if direct_collection_only and not analysis_result_id and "analysis_result_id" in frame:
        result_ids = frame["analysis_result_id"].fillna("").astype(str).str.strip()
        frame = frame[result_ids == ""].copy()
    selected_key = subject_join_key(short_title, subject_id)
    frame_subject_keys = frame.apply(
        lambda row: subject_join_key(row["short_title"], row["subject_id"]),
        axis=1,
    )
    return frame[
        (frame["short_title"] == short_title)
        & (frame_subject_keys == selected_key)
    ].copy()


def load_patient_idc_scope(
    paths: DataPaths,
    catalog: pd.DataFrame,
    members: pd.DataFrame,
    *,
    include_all_related: bool = False,
) -> pd.DataFrame:
    """Return IDC series for one explicit participant imaging scope.

    IDC stores derived Analysis Result series inside the source collection.  A
    Collection-only scope therefore removes rows with ``analysis_result_id``;
    an Analysis Result scope selects its exact identifier; and an all-related
    scope reads the physical collection once without duplicating derived rows.
    """
    if members.empty:
        return pd.DataFrame()

    ordered = members.sort_values(
        ["dataset_type", "short_title"],
        key=lambda series: series.astype(str).str.casefold(),
        kind="stable",
    )
    selected_rows = ordered
    if include_all_related:
        collection_rows = ordered[ordered["dataset_type"] == "Collection"]
        if not collection_rows.empty:
            selected_rows = collection_rows

    frames: list[pd.DataFrame] = []
    seen_physical_scopes: set[tuple[str, str]] = set()

    def first_present(*values: object) -> str:
        for value in values:
            if pd.notna(value) and str(value).strip():
                return str(value).strip()
        return ""

    for row in selected_rows.to_dict("records"):
        dataset_type = str(row.get("dataset_type", ""))
        short_title = str(row.get("short_title", ""))
        subject_id = first_present(
            row.get("idc_subject_id"), row.get("subject_id")
        )
        collection_id = first_present(row.get("idc_collection_id")) or None
        analysis_result_id = first_present(
            row.get("idc_analysis_result_id")
        ) or None
        if include_all_related and dataset_type == "Collection":
            analysis_result_id = None
            physical_scope = (
                (collection_id or short_title).casefold(),
                subject_id.casefold(),
            )
            if physical_scope in seen_physical_scopes:
                continue
            seen_physical_scopes.add(physical_scope)

        frame = load_patient_idc(
            paths,
            catalog,
            short_title,
            subject_id,
            collection_id=collection_id,
            analysis_result_id=(
                analysis_result_id if dataset_type == "Analysis Result" else None
            ),
            direct_collection_only=(
                dataset_type == "Collection" and not include_all_related
            ),
        )
        if not frame.empty:
            frames.append(frame)

    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True, sort=False)
    if "SeriesInstanceUID" in result:
        result = result.drop_duplicates(subset=["SeriesInstanceUID"], keep="first")
    return result.reset_index(drop=True)


def load_patient_pathdb(
    path: Path, short_title: str, subject_id: str
) -> pd.DataFrame:
    source = preferred_object(path, "agent_pathdb_slides", "pathdb_rows")
    if not source:
        return pd.DataFrame()
    return read_sql(
        path,
        f"""
        SELECT collection AS short_title, patient_id AS subject_id, slide_id,
               camic_id,
               CASE WHEN COALESCE(TRIM(camic_id), '') <> '' THEN
                 'https://pathdb.cancerimagingarchive.net/caMicroscope/apps/mini/viewer.html?mode=pathdb&slideId='
                   || camic_id
               ELSE '' END AS viewer_url,
               wsiimage_url AS imageUrl, species, cancer_type, cancer_location,
               data_format, modality, protocol, magnification, "update"
        FROM {source}
        WHERE lower(collection) = lower(?) AND lower(patient_id) = lower(?)
        ORDER BY "update", slide_id
        """,
        (short_title, subject_id),
    )


def load_patient_nifti(
    path: Path, short_title: str, subject_id: str
) -> pd.DataFrame:
    source = preferred_object(path, "agent_nifti_files", "radiology_series")
    if not source:
        return pd.DataFrame()
    return read_sql(
        path,
        f"""
        SELECT short_title, subject_id, file_name, package_path, modality,
               body_part_examined, study_date, series_date, study_description,
               series_description, study_id, study_id_source, series_id,
               series_id_source, object_type, is_derived_object,
               quality_flag_json
        FROM {source}
        WHERE lower(short_title) = lower(?) AND lower(subject_id) = lower(?)
        ORDER BY COALESCE(NULLIF(study_date, ''), NULLIF(series_date, '')),
                 study_id, series_id, file_name
        """,
        (short_title, subject_id),
    )


def load_patient_nifti_packages(
    path: Path, short_title: str, subject_id: str
) -> pd.DataFrame:
    """Return verified public Aspera packages containing a patient's NIfTI files."""
    file_source = preferred_object(path, "agent_nifti_files", "radiology_series")
    download_source = preferred_object(path, "agent_nifti_downloads", "nifti_downloads")
    columns = [
        "download_id",
        "download_label",
        "download_title",
        "download_url",
        "download_size",
        "download_size_unit",
        "access_level",
    ]
    if not file_source or not download_source:
        return pd.DataFrame(columns=columns)

    file_columns = read_sql(path, f"SELECT * FROM {file_source} LIMIT 0").columns
    download_id_column = next(
        (name for name in ("download_ids", "download_id") if name in file_columns),
        None,
    )
    if not download_id_column:
        return pd.DataFrame(columns=columns)

    file_downloads = read_sql(
        path,
        f"""
        SELECT {download_id_column} AS download_ids
        FROM {file_source}
        WHERE lower(short_title) = lower(?) AND lower(subject_id) = lower(?)
        """,
        (short_title, subject_id),
    )
    associated_ids = {
        token.casefold()
        for value in file_downloads.get("download_ids", pd.Series(dtype="object"))
        for token in split_tokens(value)
    }
    if not associated_ids:
        return pd.DataFrame(columns=columns)

    download_columns = read_sql(
        path, f"SELECT * FROM {download_source} LIMIT 0"
    ).columns
    required = {"short_title", "download_id", "download_url"}
    if not required.issubset(download_columns):
        return pd.DataFrame(columns=columns)
    select_columns = [column for column in columns if column in download_columns]
    packages = read_sql(
        path,
        f"""
        SELECT {', '.join(select_columns)}
        FROM {download_source}
        WHERE lower(short_title) = lower(?)
          AND COALESCE(TRIM(download_url), '') <> ''
        """,
        (short_title,),
    )
    if packages.empty:
        return pd.DataFrame(columns=columns)

    package_ids = packages["download_id"].fillna("").astype(str).str.strip().str.casefold()
    packages = packages[package_ids.isin(associated_ids)].copy()
    if packages.empty:
        return pd.DataFrame(columns=columns)

    packages = packages[packages["download_url"].map(is_public_aspera_package_url)]
    packages = packages.drop_duplicates(subset=["download_url"], keep="first")
    for column in columns:
        if column not in packages:
            packages[column] = ""
    return packages[columns].sort_values(
        ["download_label", "download_id"], kind="stable", na_position="last"
    ).reset_index(drop=True)


def is_public_aspera_package_url(value: object) -> bool:
    """Accept only the two public TCIA Faspex URL shapes exposed by WordPress."""
    parsed = urlparse(str(value).strip())
    allowed_paths = {"", "/aspera/faspex/public/package"}
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold()
        == "faspex.cancerimagingarchive.net"
        and parsed.path.rstrip("/") in allowed_paths
        and bool(parse_qs(parsed.query).get("context"))
    )


def load_dataset_aspera_packages(path: Path, short_title: str) -> pd.DataFrame:
    """Return current public WordPress Aspera packages for one TCIA dataset."""
    columns = [
        "download_id", "download_title", "download_url", "download_size",
        "download_size_unit", "subjects", "images", "file_types", "data_types",
        "requirements_label", "requirements_url", "license_label", "access_level",
    ]
    source = preferred_object(path, "agent_current_downloads")
    if not source:
        return pd.DataFrame(columns=columns)
    available = read_sql(path, f"SELECT * FROM {source} LIMIT 0").columns
    selected = [column for column in columns if column in available]
    packages = read_sql(
        path,
        f"SELECT {', '.join(selected)} FROM {source} "
        "WHERE short_title = ? AND COALESCE(hidden, 0) = 0",
        (short_title,),
    )
    if packages.empty or "download_url" not in packages:
        return pd.DataFrame(columns=columns)
    if "controlled_access" in available:
        # The public snapshot contract should already resolve this through
        # access_level, but retain the source flag as a second safety gate.
        controlled = read_sql(
            path,
            f"SELECT download_id, controlled_access FROM {source} "
            "WHERE short_title = ? AND COALESCE(hidden, 0) = 0",
            (short_title,),
        )
        packages = packages.merge(controlled, on="download_id", how="left")
        packages = packages[
            pd.to_numeric(packages["controlled_access"], errors="coerce").fillna(0) == 0
        ].drop(columns=["controlled_access"])
    if "access_level" in packages:
        packages = packages[
            packages["access_level"].fillna("").astype(str).str.casefold().isin(
                OPEN_ACCESS_LEVELS
            )
        ]
    packages = packages[packages["download_url"].map(is_public_aspera_package_url)]
    packages = packages.drop_duplicates(subset=["download_url"], keep="first")
    for column in columns:
        if column not in packages:
            packages[column] = ""
    return packages[columns].reset_index(drop=True)


def load_patient_controlled(
    path: Path, short_title: str, subject_id: str
) -> pd.DataFrame:
    source = preferred_object(path, "agent_controlled_files", "controlled_files")
    if not source:
        return pd.DataFrame()
    return read_sql(
        path,
        f"""
        SELECT route_system, short_title, download_title, access_level,
               controlled_access_policy_url, license_label, drs_uri, file_id,
               file_name, file_type, file_size_bytes, participant_id,
               patient_id, patient_age, patient_sex, race, ethnicity,
               diagnosis, study_data_type, image_modality, study_instance_uid,
               series_instance_uid, modality, body_part_examined, study_date,
               series_date, study_description, series_description,
               protocol_name, image_count, quality_flag_json
        FROM {source}
        WHERE lower(short_title) = lower(?)
          AND lower(COALESCE(NULLIF(TRIM(patient_id), ''),
                            NULLIF(TRIM(participant_id), ''))) = lower(?)
        ORDER BY study_date, study_instance_uid, series_instance_uid, file_name
        """,
        (short_title, subject_id),
    )


def load_patient_public_non_dicom(
    path: Path | None,
    short_title: str,
    subject_id: str,
    *,
    include_dicom: bool = False,
) -> pd.DataFrame:
    """Return logical public assets once each, not once per delivery location.

    The physical V2 component retains its historical public-non-DICOM name but
    also carries reviewed Aspera-only public DICOM exceptions. Callers must opt
    into those DICOM rows so they are never presented as non-DICOM files.
    """
    if path is None or preferred_object(
        path, "agent_public_non_dicom_asset_participants"
    ) is None:
        return pd.DataFrame()
    format_predicate = (
        "instr(upper(COALESCE(a.file_format, '')), 'DICOM') > 0"
        if include_dicom
        else "instr(upper(COALESCE(a.file_format, '')), 'DICOM') = 0"
    )
    return read_sql(
        path,
        f"""
        SELECT a.asset_id, a.dataset_type, a.short_title, a.asset_name,
               a.file_name, a.package_path, a.file_format, a.media_kind,
               a.imaging_domain, a.modality, a.object_role,
               a.represented_file_count, a.size_bytes,
               a.participant_link_status, a.representation_provenance_class,
               a.source_system, a.source_url, a.quality_flag_json,
               ap.raw_subject_id, ap.subject_id_namespace, ap.link_status,
               COUNT(DISTINCT l.location_id) AS location_count,
               group_concat(DISTINCT l.representation_provenance_class)
                 AS available_representations
        FROM public_non_dicom_asset_participants ap
        JOIN public_non_dicom_assets a USING(asset_id)
        LEFT JOIN public_non_dicom_locations l USING(asset_id)
        WHERE a.short_title = ?
          AND ap.subject_id = ?
          AND {format_predicate}
        GROUP BY a.asset_id, ap.asset_participant_id
        ORDER BY lower(COALESCE(a.file_name, a.asset_name, a.asset_id))
        """,
        (short_title, subject_id),
    )


def load_public_non_dicom_image_metadata(
    path: Path | None, asset_ids: Sequence[str]
) -> pd.DataFrame:
    if (
        path is None
        or not asset_ids
        or preferred_object(path, "agent_public_non_dicom_image_metadata") is None
    ):
        return pd.DataFrame()
    unique_ids = list(dict.fromkeys(str(value) for value in asset_ids if str(value)))
    placeholders = ",".join("?" for _ in unique_ids)
    return read_sql(
        path,
        f"""
        SELECT asset_id, modality AS metadata_modality, body_part_examined,
               study_description, series_description, manufacturer,
               manufacturer_model_name, magnetic_field_strength_t,
               study_datetime, acquisition_dimensionality, scanner_site,
               sequence_class, sequence_tags, slice_thickness_mm,
               spacing_between_slices_mm, repetition_time_ms, echo_time_ms,
               inversion_time_ms, pre_included, post_included, t2_included,
               flair_included, sequences_present, rows, columns,
               number_of_slices, pixel_spacing_mm, pathology_protocol,
               magnification, field_source_ids_json, populated_field_count,
               conflict_field_count
        FROM agent_public_non_dicom_image_metadata
        WHERE asset_id IN ({placeholders})
        ORDER BY asset_id
        """,
        unique_ids,
    )


def load_public_non_dicom_metadata_coverage(
    path: Path | None, short_title: str
) -> pd.DataFrame:
    if path is None or preferred_object(
        path, "agent_public_non_dicom_metadata_field_coverage"
    ) is None:
        return pd.DataFrame()
    return read_sql(
        path,
        "SELECT * FROM agent_public_non_dicom_metadata_field_coverage "
        "WHERE lower(short_title) = lower(?) ORDER BY field_name",
        [short_title],
    )


def load_public_non_dicom_metadata_notes(
    path: Path | None, short_title: str
) -> pd.DataFrame:
    if path is None or preferred_object(
        path, "agent_public_non_dicom_dataset_metadata_notes"
    ) is None:
        return pd.DataFrame()
    return read_sql(
        path,
        "SELECT * FROM agent_public_non_dicom_dataset_metadata_notes "
        "WHERE lower(short_title) = lower(?) "
        "ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
        "field_name, note_code",
        [short_title],
    )


def load_public_non_dicom_locations(
    path: Path | None, asset_ids: str | Sequence[str]
) -> pd.DataFrame:
    source = (
        preferred_object(path, "agent_public_non_dicom_locations")
        if path is not None
        else None
    )
    values = [asset_ids] if isinstance(asset_ids, str) else list(asset_ids)
    unique_ids = list(dict.fromkeys(str(value) for value in values if str(value)))
    if not source or not unique_ids:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in unique_ids)
    return read_sql(
        path,
        f"SELECT * FROM {source} WHERE asset_id IN ({placeholders}) "
        "ORDER BY file_name, managed_system, location_id",
        unique_ids,
    )


def load_pathology_package_summary(path: Path, short_title: str) -> pd.DataFrame:
    summary_source = preferred_object(
        path, "agent_pathology_dataset_summary", "pathology_dataset_summary"
    )
    if not summary_source:
        return pd.DataFrame()
    summary = read_sql(
        path,
        f"SELECT * FROM {summary_source} WHERE short_title = ?",
        (short_title,),
    )
    # Avoid scanning the multi-gigabyte file-object table on every Streamlit
    # rerun. Newer agent summaries expose status directly; older schema-v2
    # releases keep the package-wide status in pathology_meta.
    if (
        not summary.empty
        and "package_inventory_status" not in summary
        and "pathology_meta" in sqlite_objects(path)
    ):
        meta = read_sql(
            path,
            """
            SELECT value
            FROM pathology_meta
            WHERE key = 'package_inventory_status'
            LIMIT 1
            """,
        )
        if not meta.empty:
            value = str(meta.iloc[0]["value"]).strip().strip('"')
            summary["package_inventory_status"] = value
    return summary


def normalize_study_date(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "Unknown"
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "nat"}:
        return "Unknown"
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text[:10]


def idc_viewer_url(
    study_uid: object,
    series_uid: object,
    modality: object,
    access_level: str | None = None,
) -> str:
    study = str(study_uid or "").strip()
    series = str(series_uid or "").strip()
    if not study or not series:
        return ""
    if str(modality or "").upper() == "SM":
        return (
            "https://viewer.imaging.datacommons.cancer.gov/slim/studies/"
            f"{quote(study, safe='.')}/series/{quote(series, safe='.')}"
        )
    return (
        "https://viewer.imaging.datacommons.cancer.gov/v3/viewer/"
        f"?StudyInstanceUIDs={quote(study, safe='.')}"
        f"&initialSeriesInstanceUID={quote(series, safe='.')}"
    )


def add_idc_viewer_urls(
    frame: pd.DataFrame, access_level: str | None = None
) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    result["viewer_url"] = result.apply(
        lambda row: idc_viewer_url(
            row.get("StudyInstanceUID"),
            row.get("SeriesInstanceUID"),
            row.get("Modality"),
            access_level,
        ),
        axis=1,
    )
    result["study_date"] = result["StudyDate"].map(normalize_study_date)
    return result


def cart_item(
    route: str,
    value: object,
    *,
    short_title: str,
    subject_id: str,
    label: str,
    source: str,
    access_level: str,
) -> dict[str, str] | None:
    route_headers = {"dicom": "SeriesInstanceUID", "pathdb": "imageUrl", "drs": "drs_uri"}
    header = route_headers.get(route)
    clean_value = str(value or "").strip()
    if not header or not clean_value:
        return None
    item_id = f"{header}|{clean_value}"
    return {
        "item_id": item_id,
        "route": route,
        "manifest_header": header,
        "value": clean_value,
        "short_title": short_title,
        "subject_id": subject_id,
        "label": label,
        "source": source,
        "access_level": access_level,
    }


def deduplicate_cart(items: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    for item in items:
        item_id = str(item.get("item_id", ""))
        if item_id:
            unique[item_id] = dict(item)
    return list(unique.values())


def _manifest_csv(header: str, values: Iterable[str]) -> bytes:
    text = io.StringIO(newline="")
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow([header])
    for value in sorted(set(values)):
        writer.writerow([value])
    return text.getvalue().encode("utf-8")


def build_manifest_download(
    items: Iterable[Mapping[str, str]],
) -> tuple[bytes, str, str, dict[str, int]]:
    """Create one route-specific CSV or a ZIP containing separate route CSVs."""
    clean = deduplicate_cart(items)
    if not clean:
        raise ValueError("The shopping cart is empty.")
    groups: dict[str, list[str]] = {}
    for item in clean:
        groups.setdefault(item["manifest_header"], []).append(item["value"])
    counts = {header: len(set(values)) for header, values in groups.items()}
    names = {
        "SeriesInstanceUID": "tcia_dicom_series.csv",
        "imageUrl": "tcia_pathdb_files.csv",
        "drs_uri": "tcia_controlled_drs.csv",
    }
    if len(groups) == 1:
        header, values = next(iter(groups.items()))
        return _manifest_csv(header, values), names[header], "text/csv", counts

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for header, values in groups.items():
            archive.writestr(names[header], _manifest_csv(header, values))
        archive.writestr(
            "README.txt",
            "Extract the archive and open one CSV at a time with TCIA Data "
            "Retriever. Each CSV intentionally contains exactly one supported "
            "route column.\n",
        )
    return (
        buffer.getvalue(),
        "tcia_data_retriever_manifests.zip",
        "application/zip",
        counts,
    )


def _cohort_export_patients(frame: pd.DataFrame) -> pd.DataFrame:
    """Return the resolved patient-level fields intended for cohort export."""
    columns = [column for column in COHORT_EXPORT_COLUMNS if column in frame]
    return frame[columns].copy()


def _match_export_rows_to_patients(
    rows: pd.DataFrame, patients: pd.DataFrame
) -> pd.DataFrame:
    if rows.empty or patients.empty:
        return rows.iloc[0:0].copy()
    source = rows.copy()
    source["subject_join_key"] = subject_join_keys(source)
    selected = patients[["short_title", "subject_id"]].copy()
    selected["subject_join_key"] = subject_join_keys(selected)
    selected = selected[["short_title", "subject_join_key"]].drop_duplicates()
    return source.merge(
        selected,
        on=["short_title", "subject_join_key"],
        how="inner",
        validate="many_to_one",
    )


def _filter_export_tokens(
    frame: pd.DataFrame, column: str, selected: Sequence[str]
) -> pd.DataFrame:
    if frame.empty or not selected or column not in frame:
        return frame
    wanted = {
        str(value).strip().casefold() for value in selected if str(value).strip()
    }
    return frame[
        frame[column].map(
            lambda value: bool(
                wanted.intersection(token.casefold() for token in split_tokens(value))
            )
        )
    ].copy()


def _read_export_rows(
    path: Path,
    source_names: Sequence[str],
    select_sql: str,
    dataset_column: str,
    short_titles: Sequence[str],
) -> pd.DataFrame:
    source = preferred_object(path, *source_names)
    if not source or not short_titles:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in short_titles)
    return read_sql(
        path,
        f"""
        SELECT {select_sql}
        FROM {source}
        WHERE {dataset_column} IN ({placeholders})
        """,
        tuple(short_titles),
    )


def collect_filtered_imaging_routes(
    paths: DataPaths,
    catalog: pd.DataFrame,
    patients: pd.DataFrame,
    *,
    imaging_sources: Sequence[str] = (),
    modalities: Sequence[str] = (),
    body_parts: Sequence[str] = (),
    direct_collection_titles: Sequence[str] = (),
) -> tuple[dict[str, list[str]], pd.DataFrame]:
    """Collect route values and auditable unrouted rows for a filtered cohort."""
    routes: dict[str, list[str]] = {}
    unrouted: list[pd.DataFrame] = []
    if patients.empty:
        return routes, pd.DataFrame()

    source_aliases = {
        "DICOM series": "IDC DICOM",
        "Public DICOM": "IDC DICOM",
        "MHA volumes": "Public non-DICOM",
        "NIfTI files": "Public non-DICOM",
        "Pathology images": "Public non-DICOM",
        "Controlled access": "Controlled-access file metadata",
    }
    requested = {source_aliases.get(value, value) for value in imaging_sources} or {
        "IDC DICOM",
        "Public non-DICOM",
        "Controlled-access file metadata",
    }
    short_titles = sorted(set(patients["short_title"].dropna().astype(str)))

    def filter_rows(frame: pd.DataFrame) -> pd.DataFrame:
        matched = _match_export_rows_to_patients(frame, patients)
        matched = _filter_export_tokens(matched, "modality", modalities)
        return _filter_export_tokens(matched, "body_part_examined", body_parts)

    def retain_unrouted(
        frame: pd.DataFrame, source: str, label_column: str, reason: str
    ) -> None:
        if frame.empty:
            return
        result = pd.DataFrame(
            {
                "source": source,
                "short_title": frame["short_title"],
                "subject_id": frame["subject_id"],
                "modality": frame.get("modality", ""),
                "body_part_examined": frame.get("body_part_examined", ""),
                "item": frame.get(label_column, ""),
                "reason": reason,
            }
        )
        unrouted.append(result)

    if "IDC DICOM" in requested and paths.idc_parquet.exists():
        analysis_values = patients.get(
            "idc_analysis_result_id", pd.Series(index=patients.index, dtype=str)
        ).fillna("").astype(str).str.strip()
        collection_ids = (
            sorted(
                {
                    str(value).strip()
                    for value in patients.loc[
                        analysis_values == "", "idc_collection_id"
                    ].dropna()
                    if str(value).strip()
                }
            )
            if "idc_collection_id" in patients
            else []
        )
        analysis_ids = sorted(
            {
                str(value).strip()
                for value in analysis_values
                if str(value).strip()
            }
        )
        parquet_filters = []
        if collection_ids:
            parquet_filters.append([("collection_id", "in", collection_ids)])
        if analysis_ids:
            parquet_filters.append([("analysis_result_id", "in", analysis_ids)])
        dicom = load_idc_series(
            paths.idc_parquet,
            catalog,
            columns=[
                "collection_id",
                "analysis_result_id",
                "PatientID",
                "SeriesInstanceUID",
                "Modality",
                "BodyPartExamined",
            ],
            filters=parquet_filters or None,
        ).rename(
            columns={"Modality": "modality", "BodyPartExamined": "body_part_examined"}
        )
        if direct_collection_titles and "analysis_result_id" in dicom:
            derived_ids = (
                dicom["analysis_result_id"].fillna("").astype(str).str.strip()
            )
            derived_collection_rows = (
                dicom["short_title"].isin(direct_collection_titles)
                & (derived_ids != "")
            )
            dicom = dicom[~derived_collection_rows].copy()
        dicom = filter_rows(dicom)
        routed = dicom["SeriesInstanceUID"].fillna("").astype(str).str.strip() != ""
        routes["SeriesInstanceUID"] = (
            dicom.loc[routed, "SeriesInstanceUID"].astype(str).tolist()
        )
        retain_unrouted(
            dicom.loc[~routed],
            "IDC DICOM",
            "SeriesInstanceUID",
            "Series Instance UID is missing.",
        )

    if "Controlled-access file metadata" in requested:
        controlled = _read_export_rows(
            paths.controlled_db,
            ("agent_controlled_files", "controlled_files"),
            (
                "short_title, COALESCE(NULLIF(TRIM(patient_id), ''), "
                "NULLIF(TRIM(participant_id), '')) AS subject_id, file_name, "
                "drs_uri, COALESCE(NULLIF(modality, ''), image_modality) AS modality, "
                "body_part_examined"
            ),
            "short_title",
            short_titles,
        )
        controlled = filter_rows(controlled)
        if not controlled.empty and "drs_uri" in controlled:
            routed = controlled["drs_uri"].fillna("").astype(str).str.strip() != ""
            routes["drs_uri"] = controlled.loc[routed, "drs_uri"].astype(str).tolist()
            retain_unrouted(
                controlled.loc[~routed],
                "Controlled-access metadata",
                "file_name",
                "No authorized DRS route is available in the metadata.",
            )

    if (
        "Public non-DICOM" in requested
        and paths.public_non_dicom_db is not None
        and paths.public_non_dicom_db.exists()
    ):
        public_non_dicom = _read_export_rows(
            paths.public_non_dicom_db,
            ("agent_public_non_dicom_asset_participants",),
            (
                "short_title, TRIM(subject_id) AS subject_id, file_name, package_path, "
                "asset_id, file_format, media_kind, imaging_domain, modality, "
                "'' AS body_part_examined"
            ),
            "short_title",
            short_titles,
        )
        public_non_dicom = filter_rows(public_non_dicom)
        public_non_dicom = public_non_dicom[
            ~public_non_dicom["file_format"]
            .fillna("")
            .astype(str)
            .str.upper()
            .str.contains("DICOM", regex=False)
        ].copy()
        if imaging_sources:
            format_values = public_non_dicom["file_format"].fillna("").astype(str).str.upper()
            domain_values = public_non_dicom["imaging_domain"].fillna("").astype(str).str.casefold()
            selected_mask = pd.Series(False, index=public_non_dicom.index)
            if "MHA volumes" in imaging_sources:
                selected_mask |= format_values.str.contains(
                    r"(?:^|[,; ])MH[AD](?:$|[,; ])", regex=True
                )
            if "NIfTI files" in imaging_sources:
                selected_mask |= format_values.str.contains("NIFTI", regex=False)
            if "Pathology images" in imaging_sources:
                selected_mask |= domain_values.eq("pathology")
            if any(
                value in imaging_sources
                for value in ("MHA volumes", "NIfTI files", "Pathology images")
            ):
                public_non_dicom = public_non_dicom[selected_mask].copy()

        locations = load_public_non_dicom_locations(
            paths.public_non_dicom_db,
            public_non_dicom.get("asset_id", pd.Series(dtype=str)).astype(str).tolist(),
        )
        direct_locations = pd.DataFrame()
        if not locations.empty:
            direct_locations = locations[
                locations["managed_system"].fillna("").astype(str).eq("tcia_pathdb")
                & locations["access_url"].fillna("").astype(str).str.strip().ne("")
            ].copy()
            routes.setdefault("imageUrl", []).extend(
                direct_locations["access_url"].astype(str).tolist()
            )
        directly_routed = set(
            direct_locations.get("asset_id", pd.Series(dtype=str)).astype(str)
        )
        retain_unrouted(
            public_non_dicom[
                ~public_non_dicom["asset_id"].astype(str).isin(directly_routed)
            ],
            "Public non-DICOM",
            "file_name",
            "Available through a dataset-level package; not a TCIA Data Retriever route.",
        )

    if (
        "IDC DICOM" in requested
        and paths.public_non_dicom_db is not None
        and paths.public_non_dicom_db.exists()
    ):
        packaged_dicom = _read_export_rows(
            paths.public_non_dicom_db,
            ("agent_public_non_dicom_asset_participants",),
            (
                "short_title, TRIM(subject_id) AS subject_id, file_name, package_path, "
                "file_format, modality, '' AS body_part_examined"
            ),
            "short_title",
            short_titles,
        )
        packaged_dicom = packaged_dicom[
            packaged_dicom["file_format"]
            .fillna("")
            .astype(str)
            .str.upper()
            .str.contains("DICOM", regex=False)
        ].copy()
        packaged_dicom = filter_rows(packaged_dicom)
        retain_unrouted(
            packaged_dicom,
            "Public DICOM outside IDC",
            "file_name",
            "Available through a dataset-level Aspera package; not an IDC series route.",
        )

    clean_routes = {
        header: sorted(set(value for value in values if str(value).strip()))
        for header, values in routes.items()
        if values
    }
    unrouted_frame = (
        pd.concat(unrouted, ignore_index=True, sort=False).drop_duplicates()
        if unrouted
        else pd.DataFrame()
    )
    return clean_routes, unrouted_frame


def build_filtered_cohort_download(
    patients: pd.DataFrame,
    routes: Mapping[str, Sequence[str]],
    unrouted: pd.DataFrame | None = None,
) -> tuple[bytes, str, str, dict[str, int]]:
    """Build a clinical CSV plus route-specific Data Retriever manifests."""
    if patients.empty:
        raise ValueError("The filtered cohort is empty.")
    route_names = {
        "SeriesInstanceUID": "tcia_dicom_series.csv",
        "imageUrl": "tcia_pathdb_files.csv",
        "drs_uri": "tcia_controlled_drs.csv",
    }
    counts = {"patients": len(patients)}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        patient_csv = (
            _cohort_export_patients(patients).to_csv(index=False).encode("utf-8")
        )
        archive.writestr("tcia_filtered_patients.csv", patient_csv)
        for header, values in routes.items():
            clean_values = sorted(
                set(str(value).strip() for value in values if str(value).strip())
            )
            if not clean_values or header not in route_names:
                continue
            archive.writestr(route_names[header], _manifest_csv(header, clean_values))
            counts[header] = len(clean_values)
        if unrouted is not None and not unrouted.empty:
            archive.writestr(
                "tcia_unrouted_imaging_inventory.csv",
                unrouted.to_csv(index=False).encode("utf-8"),
            )
            counts["unrouted_imaging"] = len(unrouted)
        archive.writestr(
            "README.txt",
            (
                "This package contains one row per filtered dataset-scoped patient in "
                "tcia_filtered_patients.csv. Extract the package and open one route-specific "
                "CSV at a time with TCIA Data Retriever. SeriesInstanceUID routes public "
                "DICOM, imageUrl routes direct public files, and drs_uri routes authorized "
                "controlled files. Controlled routes still require approval and API-key "
                "configuration. The unrouted inventory, when present, is metadata for imaging "
                "that has no supported Data Retriever route; it is not a download manifest.\n"
            ),
        )
    return (
        buffer.getvalue(),
        "tcia_filtered_cohort_export.zip",
        "application/zip",
        counts,
    )
