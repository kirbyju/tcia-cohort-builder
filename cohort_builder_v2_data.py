"""Patient-centric data access for the TCIA Cohort Builder v2.

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
from urllib.parse import quote

import pandas as pd


LOGGER = logging.getLogger(__name__)
POLICY_URL = "https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/"
OPEN_ACCESS_LEVELS = {"open", "open_noncommercial"}
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

    def signatures(self) -> tuple[tuple[str, int, int], ...]:
        """Return stable cache inputs without hashing multi-gigabyte files."""
        result = []
        for path in (
            self.snapshot_db,
            self.clinical_db,
            self.nifti_db,
            self.pathology_db,
            self.controlled_db,
            self.idc_parquet,
        ):
            if path.exists():
                stat = path.stat()
                result.append((str(path), stat.st_mtime_ns, stat.st_size))
            else:
                result.append((str(path), 0, 0))
        return tuple(result)

    def status_rows(self) -> list[dict[str, object]]:
        labels = {
            "TCIA provenance snapshot": self.snapshot_db,
            "Clinical sidecar": self.clinical_db,
            "NIfTI sidecar": self.nifti_db,
            "Pathology sidecar": self.pathology_db,
            "Controlled-access metadata": self.controlled_db,
            "IDC series Parquet": self.idc_parquet,
        }
        rows = []
        for label, path in labels.items():
            rows.append(
                {
                    "Source": label,
                    "Available": path.exists(),
                    "Path": str(path),
                    "Size (GB)": round(path.stat().st_size / 1_000_000_000, 3)
                    if path.exists()
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
    cache_dir = skill_root.resolve() / "cache"

    def selected(env_name: str, default: Path) -> Path:
        value = os.environ.get(env_name)
        return Path(value).expanduser().resolve() if value else default.resolve()

    return DataPaths(
        snapshot_db=selected("TCIA_SNAPSHOT_DB", cache_dir / "tcia_snapshot.sqlite"),
        clinical_db=selected(
            "TCIA_CLINICAL_METADATA_DB", cache_dir / "clinical_metadata.sqlite"
        ),
        nifti_db=selected(
            "TCIA_NIFTI_METADATA_DB", cache_dir / "nifti_metadata.sqlite"
        ),
        pathology_db=selected(
            "TCIA_PATHOLOGY_METADATA_DB", cache_dir / "pathology_metadata.sqlite"
        ),
        controlled_db=selected(
            "TCIA_CONTROLLED_ACCESS_METADATA_DB",
            cache_dir / "controlled_access_metadata.sqlite",
        ),
        idc_parquet=selected(
            "TCIA_IDC_METADATA_PARQUET", app_dir / "idc_metadata.parquet"
        ),
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
    parquet_path: Path, catalog: pd.DataFrame | None = None
) -> pd.DataFrame:
    if not parquet_path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(parquet_path)
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


def build_patient_index(paths: DataPaths) -> pd.DataFrame:
    """Create exactly one row per dataset-scoped patient."""
    build_started = time.perf_counter()
    catalog = load_dataset_catalog(paths.snapshot_db)
    if catalog.empty:
        raise RuntimeError(
            f"No visible TCIA dataset catalog found in {paths.snapshot_db}"
        )

    stage_started = time.perf_counter()
    clinical = load_clinical_subjects(paths.clinical_db)
    LOGGER.info(
        "Patient index: loaded %s clinical patients in %.1fs",
        f"{len(clinical):,}",
        time.perf_counter() - stage_started,
    )
    stage_started = time.perf_counter()
    idc = aggregate_idc(load_idc_series(paths.idc_parquet, catalog))
    LOGGER.info(
        "Patient index: aggregated %s IDC patients in %.1fs",
        f"{len(idc):,}",
        time.perf_counter() - stage_started,
    )
    stage_started = time.perf_counter()
    pathdb = aggregate_pathdb(paths.snapshot_db)
    nifti = aggregate_nifti(paths.nifti_db)
    controlled = aggregate_controlled(paths.controlled_db)
    LOGGER.info(
        "Patient index: loaded PathDB, NIfTI, and controlled metadata in %.1fs",
        time.perf_counter() - stage_started,
    )

    stage_started = time.perf_counter()
    patients = _outer_merge_sources(
        [
            ("clinical", clinical),
            ("idc", idc),
            ("pathdb", pathdb),
            ("nifti", nifti),
            ("controlled", controlled),
        ]
    )
    if patients.empty:
        return patients

    catalog_columns = [
        column
        for column in (
            "short_title",
            "dataset_type",
            "title",
            "doi",
            "link",
            "species",
            "cancer_types",
            "cancer_locations",
            "program",
            "resolved_access_level",
            "resolved_controlled_access_policy_url",
            "licenses",
        )
        if column in catalog
    ]
    patients = patients.merge(
        catalog[catalog_columns].drop_duplicates("short_title"),
        on="short_title",
        how="inner",
    )

    pathology_summary = load_pathology_dataset_summary(paths.pathology_db)
    if not pathology_summary.empty:
        keep = [
            column
            for column in (
                "short_title",
                "download_records",
                "pathdb_collection_slide_count",
                "pathdb_collection_patient_count",
                "package_inventory_status",
                "has_pathology_aspera",
            )
            if column in pathology_summary
        ]
        patients = patients.merge(
            pathology_summary[keep].drop_duplicates("short_title"),
            on="short_title",
            how="left",
        )

    bool_columns = (
        "has_clinical",
        "has_idc_dicom_metadata",
        "has_pathdb",
        "has_nifti",
        "has_controlled_metadata",
        "has_pathology_aspera",
    )
    for column in bool_columns:
        if column not in patients:
            patients[column] = False
        patients[column] = patients[column].astype("boolean").fillna(False).astype(bool)

    # IDC's published index is an open-access asset inventory. Dataset-level
    # WordPress access is retained separately and must not override the access
    # mechanism or license attached to a more granular IDC series.
    patients["has_public_dicom"] = patients["has_idc_dicom_metadata"]

    if "has_imaging" not in patients:
        patients["has_imaging"] = 0
    patients["has_imaging"] = (
        pd.to_numeric(patients["has_imaging"], errors="coerce").fillna(0) > 0
    )
    patients["has_any_imaging"] = patients[
        [
            "has_idc_dicom_metadata",
            "has_pathdb",
            "has_nifti",
            "has_controlled_metadata",
        ]
    ].any(axis=1)
    patients["imaging_linkage_status"] = patients["has_any_imaging"].map(
        {True: "Linked", False: "Needs artifact linkage review"}
    )
    # NLST is a documented exception: IDC's clinical tables include subjects
    # outside TCIA's published imaging cohort. Do not present those clinical-only
    # records as TCIA patients. Other unlinked records remain visible so source
    # extraction and crosswalk gaps can be audited instead of hidden.
    patients = exclude_nlst_clinical_only(patients)

    def sources_for(row: pd.Series) -> str:
        labels = []
        if row["has_public_dicom"]:
            labels.append("IDC DICOM")
        if row["has_nifti"]:
            labels.append("NIfTI")
        if row["has_pathdb"]:
            labels.append("PathDB")
        if row["has_controlled_metadata"]:
            labels.append("Controlled-access file metadata")
        return "; ".join(labels)

    patients["available_imaging"] = patients.apply(sources_for, axis=1)
    modality_columns = [
        column
        for column in (
            "dicom_modalities",
            "nifti_modalities",
            "pathdb_modalities",
            "controlled_modalities",
        )
        if column in patients
    ]
    body_part_columns = [
        column
        for column in (
            "dicom_body_parts",
            "nifti_body_parts",
            "pathdb_body_parts",
            "controlled_body_parts",
        )
        if column in patients
    ]
    patients["modalities"] = patients[modality_columns].apply(
        lambda row: join_tokens(row.tolist()), axis=1
    )
    patients["body_parts"] = patients[body_part_columns].apply(
        lambda row: join_tokens(row.tolist()), axis=1
    )
    patients["patient_key"] = (
        patients["short_title"].astype(str)
        + "|"
        + patients["subject_id"].astype(str)
    )
    patients = patients.drop_duplicates("patient_key", keep="first")
    patients = canonicalize_patient_categories(patients)
    patients = patients.sort_values(
        ["short_title", "subject_id"], key=lambda series: series.astype(str).str.casefold()
    ).reset_index(drop=True)
    LOGGER.info(
        "Patient index: linked and normalized %s patients in %.1fs (%.1fs total)",
        f"{len(patients):,}",
        time.perf_counter() - stage_started,
        time.perf_counter() - build_started,
    )
    return patients


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


def load_patient_idc(
    paths: DataPaths,
    catalog: pd.DataFrame,
    short_title: str,
    subject_id: str,
    collection_id: str | None = None,
    analysis_result_id: str | None = None,
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
    selected_key = subject_join_key(short_title, subject_id)
    frame_subject_keys = frame.apply(
        lambda row: subject_join_key(row["short_title"], row["subject_id"]),
        axis=1,
    )
    return frame[
        (frame["short_title"] == short_title)
        & (frame_subject_keys == selected_key)
    ].copy()


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
        WHERE lower(collection) = lower(?) AND patient_id = ?
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
        WHERE short_title = ? AND subject_id = ?
        ORDER BY COALESCE(NULLIF(study_date, ''), NULLIF(series_date, '')),
                 study_id, series_id, file_name
        """,
        (short_title, subject_id),
    )


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
        WHERE short_title = ?
          AND COALESCE(NULLIF(TRIM(patient_id), ''),
                       NULLIF(TRIM(participant_id), '')) = ?
        ORDER BY study_date, study_instance_uid, series_instance_uid, file_name
        """,
        (short_title, subject_id),
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
