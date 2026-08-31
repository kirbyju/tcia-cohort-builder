"""TCIA Participant Explorer: branded patient-level discovery and retrieval."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
from dataclasses import replace
from pathlib import Path

import pandas as pd
import streamlit as st

from cohort_builder_data import (
    DATASET_TYPE_FILTERS,
    GEOMETRY_FILTER_OPTIONS,
    add_idc_imaging_facets,
    POLICY_URL,
    DataPaths,
    add_idc_viewer_urls,
    build_filtered_cohort_download,
    build_grouped_patient_index,
    build_manifest_download,
    build_patient_index,
    cart_item,
    collect_filtered_imaging_routes,
    count_visible_dataset_contexts,
    deduplicate_cart,
    filter_patient_groups_by_dataset_type,
    filter_patient_groups_by_asset_facets,
    filter_imaging_rows,
    load_dataset_catalog,
    load_dataset_aspera_packages,
    load_dataset_coverage_states,
    load_patient_clinical_facts,
    load_patient_clinical_longitudinal,
    load_patient_controlled,
    load_patient_idc_scope,
    load_patient_public_non_dicom,
    load_participant_assets,
    load_participant_asset_facets,
    load_participant_identity_evidence,
    load_participant_identifiers,
    load_public_non_dicom_image_metadata,
    load_public_non_dicom_locations,
    load_public_non_dicom_metadata_coverage,
    load_public_non_dicom_metadata_notes,
    participant_availability_rows,
    resolve_data_paths,
    split_tokens,
)
from v2_artifacts import (
    INSTALL_STATE_ASSET,
    V2_RELEASE_TAG,
    ensure_bundle_profile,
    installed_component,
    load_bundle_installation,
    require_installed_component,
)


PATIENT_INDEX_SCHEMA_VERSION = 14
APP_DIR = Path(__file__).resolve().parent
BRAND_SKILL_DIR = Path.home() / ".codex" / "skills" / "tcia-brand-guidelines"
LOGO_PATH = BRAND_SKILL_DIR / "assets" / "tcia-logo-dark.svg"
OFFICIAL_LOGO_URL = (
    "https://www.cancerimagingarchive.net/wp-content/uploads/2021/06/"
    "TCIA-Logo-01.svg"
)
PATIENT_RESULTS_PAGE_SIZES = (25, 50, 100, 250)


st.set_page_config(
    page_title="TCIA Participant Explorer",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="auto",
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

st.markdown(
    """
    <style>
    :root {
        --tcia-blue-light: #5bc6ff;
        --tcia-blue-bright: #51a6fa;
        --tcia-blue: #2467a8;
        --tcia-navy: #042b5b;
        --tcia-green: #52f355;
        --tcia-amber: #fdb835;
        --tcia-coral: #ff5773;
        --tcia-purple: #8d44ce;
        --tcia-grey-100: #e9e9e9;
        --tcia-grey-600: #666666;
        --tcia-grey-700: #444444;
        --tcia-grey-900: #222222;
        --tcia-surface: #f7f9fc;
        --tcia-border: #d8e1eb;
    }
    .main .block-container {
        max-width: 98%;
        padding: 1rem 1.5rem 2rem;
    }
    [data-testid="stSidebar"] {
        border-right: 1px solid var(--tcia-border);
        background: #f5f8fc;
    }
    [data-testid="stSidebar"] .block-container {padding-top: 1.2rem;}
    h1, h2, h3 {color: var(--tcia-navy); letter-spacing: -0.02em;}
    a {color: var(--tcia-blue);}
    :focus-visible {outline: 3px solid var(--tcia-blue-bright) !important; outline-offset: 2px;}
    .filter-summary {display: flex; flex-wrap: wrap; gap: .35rem; margin: .25rem 0 .65rem;}
    .filter-chip {
        background: #eaf3fb;
        border: 1px solid #bdd4e8;
        border-radius: 999px;
        color: var(--tcia-navy);
        font-size: .76rem;
        padding: .23rem .55rem;
    }
    .section-label {
        color: var(--tcia-grey-600);
        font-size: .72rem;
        font-weight: 750;
        letter-spacing: .09em;
        margin-bottom: .25rem;
        text-transform: uppercase;
    }
    .identity-line {
        color: var(--tcia-grey-600);
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: .8rem;
        overflow-wrap: anywhere;
    }
    .access-callout {
        background: #fff8e1;
        border-left: .3rem solid var(--tcia-amber);
        color: var(--tcia-grey-900);
        margin: .5rem 0 .75rem;
        padding: .6rem .75rem;
    }
    .empty-detail {
        background: var(--tcia-surface);
        border: 1px dashed #aebdca;
        border-radius: .5rem;
        color: var(--tcia-grey-600);
        padding: 2.5rem 1.25rem;
        text-align: center;
    }
    [data-testid="stMetricValue"] {color: var(--tcia-navy); font-size: 1.4rem;}
    [data-testid="stMetricLabel"] {font-size: .76rem;}
    [data-testid="stDataFrame"] {border: 1px solid var(--tcia-border); border-radius: .45rem;}
    @media (max-width: 900px) {
        .main .block-container {padding-left: .8rem; padding-right: .8rem;}
        [data-testid="stHorizontalBlock"] {flex-wrap: wrap;}
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex: 1 1 100% !important;
            min-width: 100% !important;
            width: 100% !important;
        }
    }
    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after {scroll-behavior: auto !important; transition: none !important;}
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner="Building the patient-level index…")
def cached_patient_views(
    paths: DataPaths, signatures: tuple
) -> tuple[pd.DataFrame, pd.DataFrame]:
    patients = build_patient_index(paths)
    return build_grouped_patient_index(patients)


@st.cache_data(show_spinner=False)
def cached_catalog(paths: DataPaths, signatures: tuple) -> pd.DataFrame:
    return load_dataset_catalog(paths.snapshot_db)


@st.cache_data(show_spinner=False)
def cached_participant_asset_facets(
    paths: DataPaths, signatures: tuple
) -> pd.DataFrame:
    return load_participant_asset_facets(paths.participant_db)


@st.cache_resource(show_spinner="Preparing clinical and non-DICOM metadata…")
def prepare_v2_release(
    skill_root: str, cache_dir: str
) -> tuple[str, str, int, str, str, str, str]:
    directory = Path(cache_dir)
    ensure_bundle_profile(
        Path(skill_root),
        directory,
        profile="research_detail",
    )
    installation = load_bundle_installation(directory)
    participant = require_installed_component(directory, "participant_inventory")
    snapshot = require_installed_component(directory, "snapshot")
    require_installed_component(directory, "public_non_dicom")
    require_installed_component(directory, "controlled_access")
    require_installed_component(directory, "clinical")
    return (
        participant.release_fingerprint,
        snapshot.release_fingerprint,
        participant.schema_version,
        installation.installed_profile,
        installation.release_contract,
        installation.release_fingerprint,
        installation.generated_at_utc,
    )


def render_detail_install_notice(message: str) -> None:
    st.warning(
        message
        + " The app normally installs research detail during startup. Ask the "
        "service operator to check the startup log and restart the app."
    )


def option_values(frame: pd.DataFrame, column: str) -> list[str]:
    if column not in frame:
        return []
    return sorted(
        {
            str(value).strip()
            for value in frame[column].dropna()
            if str(value).strip()
        },
        key=str.casefold,
    )


def populated_columns(frame: pd.DataFrame, candidates: tuple[str, ...]) -> list[str]:
    """Keep only columns with at least one visible value."""
    result: list[str] = []
    for column in candidates:
        if column not in frame:
            continue
        values = frame[column]
        visible = values.notna() & values.astype(str).str.strip().ne("")
        if visible.any():
            result.append(column)
    return result


def token_options(frame: pd.DataFrame, column: str) -> list[str]:
    if column not in frame:
        return []
    values: set[str] = set()
    for value in frame[column].dropna():
        values.update(split_tokens(value))
    return sorted(values, key=str.casefold)


def apply_token_filter(
    frame: pd.DataFrame, column: str, selected: list[str]
) -> pd.DataFrame:
    if not selected or column not in frame:
        return frame
    wanted = set(selected)
    return frame[
        frame[column].map(lambda value: bool(wanted.intersection(split_tokens(value))))
    ]


def safe_int(value: object) -> int:
    try:
        return 0 if pd.isna(value) else int(value)
    except (TypeError, ValueError):
        return 0


def access_label(value: object) -> str:
    return {
        "open": "Open",
        "open_noncommercial": "Open · noncommercial",
        "mixed": "Mixed access",
        "controlled": "Controlled",
        "unknown": "Unknown",
    }.get(str(value or "").strip().lower(), str(value or "Unknown").title())


def add_cart_items(items: list[dict[str, str] | None]) -> int:
    current = st.session_state.get("cart_items", [])
    updated = deduplicate_cart(current + [item for item in items if item is not None])
    st.session_state.cart_items = updated
    return len(updated) - len(current)


def finish_cart_add(added: int) -> None:
    st.session_state.cart_notice = (
        f"Added {added} new item{'s' if added != 1 else ''} to the cart."
        if added
        else "Those items were already in the cart."
    )
    st.rerun()


def clear_filters() -> None:
    st.session_state["draft_search"] = ""
    st.session_state["draft_dataset_type"] = "All"
    for key in (
        "draft_datasets",
        "draft_access",
        "draft_data_categories",
        "draft_data_types",
        "draft_body_parts",
        "draft_file_formats",
        "draft_pathology_protocols",
        "draft_pathology_magnifications",
        "draft_diagnosis",
        "draft_site",
        "draft_sex",
        "draft_vital",
    ):
        st.session_state[key] = []
    st.session_state["draft_age_at_imaging"] = (0, 120)
    st.session_state["draft_multiple_imaging_dates"] = False
    st.session_state["draft_geometry"] = "Any"
    st.session_state["draft_imaging_contents"] = "All available imaging"
    st.session_state["draft_include_inferred_clinical"] = True
    st.session_state["draft_conflicts"] = False
    st.session_state["patient_results_page"] = 1
    st.session_state.pop("selected_patient_key", None)


def reset_patient_results_page() -> None:
    st.session_state["patient_results_page"] = 1


def render_brand_and_cart() -> None:
    logo_source = str(LOGO_PATH) if LOGO_PATH.exists() else OFFICIAL_LOGO_URL
    st.sidebar.image(logo_source, width="stretch")

    st.sidebar.markdown("<div class='section-label'>Participant Explorer</div>", unsafe_allow_html=True)
    items = st.session_state.get("cart_items", [])
    st.sidebar.metric("Items in cart", f"{len(items):,}")
    notice = st.session_state.pop("cart_notice", None)
    if notice:
        st.sidebar.success(notice)

    if not items:
        st.sidebar.info("Select a patient, then add viewable series or routed files.")
        st.sidebar.caption("The cart keeps public DICOM, PathDB, and controlled DRS routes separate.")
        return

    cart_frame = pd.DataFrame(items)
    counts = cart_frame["manifest_header"].value_counts()
    st.sidebar.caption(" · ".join(f"{name}: {count}" for name, count in counts.items()))
    with st.sidebar.expander("Review cart", expanded=False):
        st.dataframe(
            cart_frame[["label", "source", "access_level"]],
            hide_index=True,
            width="stretch",
        )
        remove_ids = st.multiselect(
            "Remove items",
            cart_frame["item_id"].tolist(),
            format_func=lambda item_id: cart_frame.loc[
                cart_frame["item_id"] == item_id, "label"
            ].iloc[0],
            key="draft_remove_items",
        )
        left, right = st.columns(2)
        if left.button("Remove", disabled=not remove_ids, width="stretch"):
            st.session_state.cart_items = [
                item for item in items if item["item_id"] not in set(remove_ids)
            ]
            st.rerun()
        if right.button("Clear cart", width="stretch"):
            st.session_state.cart_items = []
            st.rerun()

    payload, filename, mime, _ = build_manifest_download(items)
    st.sidebar.download_button(
        "Download manifest",
        data=payload,
        file_name=filename,
        mime=mime,
        type="primary",
        width="stretch",
    )
    if "drs_uri" in counts:
        st.sidebar.warning("Controlled DRS items require authorization and Data Retriever API-key configuration.")


def render_filter_chips(filters: list[tuple[str, object]]) -> None:
    chips: list[str] = []
    for label, value in filters:
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item not in (None, "", False):
                chips.append(
                    f'<span class="filter-chip">{html.escape(label)}: '
                    f'{html.escape(str(item))}</span>'
                )
    if chips:
        st.markdown(
            '<div class="filter-summary">' + "".join(chips) + "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.caption("No filters applied · showing the complete patient-level index")


def cohort_export_fingerprint(
    patients: pd.DataFrame,
    data_categories: list[str],
    data_types: list[str],
    file_formats: list[str],
    body_parts: list[str],
    geometry: str,
    imaging_contents: str,
) -> str:
    digest = hashlib.sha256()
    patient_keys = sorted(patients.get("patient_key", pd.Series(dtype=str)).astype(str))
    for value in patient_keys:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    for group in (data_categories, data_types, file_formats, body_parts):
        digest.update("\x1f".join(sorted(group, key=str.casefold)).encode("utf-8"))
        digest.update(b"\0")
    digest.update(geometry.encode("utf-8"))
    digest.update(b"\0")
    digest.update(imaging_contents.encode("utf-8"))
    return digest.hexdigest()


def member_json_values(
    patient: pd.Series, column: str, fallback: object
) -> list[str]:
    value = patient.get(column)
    if pd.notna(value) and str(value).strip():
        try:
            decoded = json.loads(str(value))
            if isinstance(decoded, list):
                return [str(item) for item in decoded]
        except json.JSONDecodeError:
            pass
    return [str(fallback)]


def member_short_titles(patient: pd.Series) -> list[str]:
    return member_json_values(
        patient, "member_short_titles_json", patient.get("short_title", "")
    )


def render_filtered_cohort_export(
    paths: DataPaths,
    catalog: pd.DataFrame,
    patients: pd.DataFrame,
    membership_rows: pd.DataFrame,
    selected_datasets: list[str],
    data_categories: list[str],
    data_types: list[str],
    file_formats: list[str],
    body_parts: list[str],
    geometry: str,
    imaging_contents: str,
) -> None:
    with st.expander("Download filtered cohort", expanded=False):
        include_related = False
        if selected_datasets:
            include_related = st.checkbox(
                "Include related dataset contexts",
                value=False,
                help=(
                    "When disabled, imaging manifests are limited to the selected "
                    "Collection or Analysis Result. The patient CSV still records "
                    "all verified dataset memberships."
                ),
                key="draft_export_related_contexts",
            )
        route_patients = membership_rows
        direct_collection_titles: list[str] = []
        if selected_datasets and not include_related:
            route_patients = route_patients[
                route_patients["short_title"].isin(selected_datasets)
            ]
            direct_collection_titles = route_patients.loc[
                route_patients["dataset_type"] == "Collection", "short_title"
            ].drop_duplicates().astype(str).tolist()
        fingerprint = cohort_export_fingerprint(
            patients,
            data_categories,
            data_types,
            file_formats,
            body_parts + selected_datasets + [str(include_related)],
            geometry,
            imaging_contents,
        )
        prepared = st.session_state.get("draft_cohort_export")
        stale_export = bool(
            prepared and prepared.get("fingerprint") != fingerprint
        )
        if stale_export:
            st.session_state.pop("draft_cohort_export", None)
            prepared = None
        st.write(
            "Prepare all matching patients—not only the visible table rows—as a "
            "patient-level clinical CSV plus route-specific TCIA Data Retriever manifests."
        )
        matching_only = imaging_contents == "Only imaging matching filters"
        if matching_only:
            st.caption(
                "The selected imaging filters are reapplied to individual series and "
                "files. The Imaging & Files preview uses the same facet rules."
            )
        else:
            st.caption(
                "All participant-linked imaging in the selected dataset scope is included, "
                "even when a narrower imaging filter qualified the participant."
            )
        if len(patients) > 20_000:
            st.warning(
                "This is a large cohort. Preparing all route manifests may take "
                "additional time and memory."
            )
        if st.button(
            "Prepare cohort package",
            type="primary",
            disabled=patients.empty,
            key="draft_prepare_cohort_export",
        ):
            with st.spinner("Collecting clinical rows and filtered imaging routes…"):
                routes, unrouted = collect_filtered_imaging_routes(
                    paths,
                    catalog,
                    route_patients,
                    imaging_sources=data_categories if matching_only else (),
                    data_types=data_types if matching_only else (),
                    file_formats=file_formats if matching_only else (),
                    body_parts=body_parts if matching_only else (),
                    geometry=geometry if matching_only else "Any",
                    direct_collection_titles=direct_collection_titles,
                )
                payload, filename, mime, counts = build_filtered_cohort_download(
                    patients,
                    routes,
                    unrouted,
                    selection={
                        "selected_datasets": selected_datasets,
                        "include_related_dataset_contexts": include_related,
                        "data_categories": data_categories,
                        "data_types": data_types,
                        "file_formats": file_formats,
                        "body_parts": body_parts,
                        "image_geometry": geometry,
                        "imaging_contents": imaging_contents,
                    },
                )
            prepared = {
                "fingerprint": fingerprint,
                "payload": payload,
                "filename": filename,
                "mime": mime,
                "counts": counts,
            }
            st.session_state.draft_cohort_export = prepared

        if prepared and prepared.get("fingerprint") == fingerprint:
            counts = prepared["counts"]
            route_labels = {
                "SeriesInstanceUID": "DICOM series",
                "imageUrl": "PathDB files",
                "drs_uri": "controlled DRS files",
                "unrouted_imaging": "unrouted imaging rows",
            }
            summary = [f"{counts['patients']:,} patients"]
            summary.extend(
                f"{count:,} {route_labels[key]}"
                for key, count in counts.items()
                if key in route_labels
            )
            st.success("Package ready · " + " · ".join(summary))
            st.download_button(
                "Download cohort package",
                data=prepared["payload"],
                file_name=prepared["filename"],
                mime=prepared["mime"],
                width="stretch",
                key="draft_download_cohort_export",
            )
            if "drs_uri" in counts:
                st.warning(
                    "Controlled DRS entries require authorization and TCIA Data "
                    "Retriever API-key configuration."
                )
            if not any(key in counts for key in ("SeriesInstanceUID", "imageUrl", "drs_uri")):
                st.info(
                    "No supported imaging routes match the current filters. The "
                    "package still contains the patient CSV and any unrouted imaging inventory."
                )
        elif stale_export:
            st.caption("Filters changed. Prepare a new package for the current cohort.")


def render_patient_summary(patient: pd.Series, members: pd.DataFrame) -> None:
    st.markdown("<div class='section-label'>Dataset context</div>", unsafe_allow_html=True)
    membership_columns = [
        column
        for column in (
            "dataset_type",
            "short_title",
            "title",
            "resolved_access_level",
            "link",
        )
        if column in members
    ]
    st.dataframe(
        members[membership_columns].drop_duplicates(),
        hide_index=True,
        width="stretch",
        column_config={
            "dataset_type": st.column_config.TextColumn("Type"),
            "short_title": st.column_config.TextColumn("Dataset"),
            "resolved_access_level": st.column_config.TextColumn("Access"),
            "link": st.column_config.LinkColumn(
                "TCIA page", display_text="Open dataset"
            ),
        },
    )
    st.caption(
        "Participant identity is dataset-scoped. Collection and Analysis Result "
        "contexts remain distinct unless an explicit crosswalk establishes otherwise."
    )

    st.markdown("<div class='section-label'>Data availability</div>", unsafe_allow_html=True)
    st.dataframe(
        pd.DataFrame(participant_availability_rows(patient)),
        hide_index=True,
        width="stretch",
        column_config={
            "Data": st.column_config.TextColumn("Data", width="medium"),
            "Coverage": st.column_config.TextColumn("Coverage", width="large"),
            "Detail": st.column_config.TextColumn("What this means", width="large"),
        },
    )
    unlinked = safe_int(patient.get("dataset_unlinked_asset_groups"))
    link_issues = safe_int(patient.get("participant_link_issue_count"))
    if unlinked or link_issues:
        notes = []
        if unlinked:
            notes.append(
                f"{unlinked} dataset asset group{'s' if unlinked != 1 else ''} "
                "without a participant crosswalk"
            )
        if link_issues:
            notes.append(
                f"{link_issues} participant-link issue{'s' if link_issues != 1 else ''}"
            )
        st.warning(
            "Coverage is partial: " + " and ".join(notes) + ". These are metadata "
            "coverage states, not evidence that data are absent."
        )

    st.markdown("<div class='section-label'>Clinical summary</div>", unsafe_allow_html=True)
    facts = [
        ("Sex at birth", patient.get("sex_at_birth")),
        ("Age at baseline", patient.get("age_at_baseline")),
        ("Age at treatment", patient.get("age_at_treatment_years")),
        ("Primary diagnosis", patient.get("primary_diagnosis")),
        ("Primary site", patient.get("primary_site")),
        ("Stage", patient.get("stage")),
        ("Grade", patient.get("grade")),
        ("Vital status", patient.get("vital_status")),
    ]
    rows = [
        {"Field": label, "Resolved value": str(value)}
        for label, value in facts
        if pd.notna(value) and str(value).strip()
    ]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    elif bool(patient.get("has_clinical", False)):
        st.caption(
            "Clinical data are available. Load the clinical detail artifact from "
            "Clinical & provenance to view summary values."
        )
    else:
        st.caption("No participant-linked clinical data are represented in the inventory.")

    inferred = []
    if safe_int(patient.get("primary_diagnosis_is_inferred")):
        inferred.append("diagnosis")
    if safe_int(patient.get("primary_site_is_inferred")):
        inferred.append("site")
    if inferred:
        st.info("Dataset-scope inference used for: " + ", ".join(inferred) + ". Review provenance before analysis.")


def render_aspera_packages(paths: DataPaths, short_title: str, patient_key: str) -> None:
    packages = load_dataset_aspera_packages(paths.snapshot_db, short_title)
    if packages.empty:
        return
    st.markdown(f"**Dataset package · {short_title}**")
    st.caption(
        "Aspera opens the complete TCIA dataset package, not a patient-only download."
    )
    for _, package in packages.iterrows():
        title = str(package.get("download_title") or "Aspera package").strip()
        st.link_button(
            f"Open {title} in Aspera",
            str(package["download_url"]),
            key=f"aspera_{patient_key}_{package.get('download_id', '')}",
            icon=":material/download:",
            width="content",
        )
        size = str(package.get("download_size") or "").strip()
        unit = str(package.get("download_size_unit") or "").strip()
        license_label = str(package.get("license_label") or "").strip()
        details = " · ".join(
            value for value in (f"{size} {unit}".strip(), license_label) if value
        )
        if details:
            st.caption(details)


def render_imaging(
    paths: DataPaths,
    catalog: pd.DataFrame,
    members: pd.DataFrame,
    *,
    include_all_related: bool = False,
    data_categories: list[str] | None = None,
    data_types: list[str] | None = None,
    file_formats: list[str] | None = None,
    body_parts: list[str] | None = None,
    geometry: str = "Any",
    matching_only: bool = False,
) -> None:
    if members.empty:
        st.info("No dataset-scoped imaging context is available for this patient.")
        return
    primary = members.iloc[0]
    short_title = str(primary["short_title"])
    subject_id = str(primary["subject_id"])
    patient_key = str(primary["patient_key"])
    access_values = {
        str(value)
        for value in members.get(
            "resolved_access_level", pd.Series(dtype=str)
        ).dropna()
        if str(value).strip()
    }
    access = next(iter(access_values)) if len(access_values) == 1 else "mixed"
    dicom = load_patient_idc_scope(
        paths,
        catalog,
        members,
        include_all_related=include_all_related,
    )
    dicom = add_idc_imaging_facets(dicom)
    dicom_total_count = (
        int(dicom["SeriesInstanceUID"].nunique())
        if not dicom.empty and "SeriesInstanceUID" in dicom
        else 0
    )
    if matching_only:
        dicom = filter_imaging_rows(
            dicom,
            data_categories=data_categories or (),
            data_types=data_types or (),
            file_formats=file_formats or (),
            body_parts=body_parts or (),
            geometry=geometry,
        )
    dicom_count = (
        int(dicom["SeriesInstanceUID"].nunique())
        if not dicom.empty and "SeriesInstanceUID" in dicom
        else 0
    )

    def scope_count(column: str) -> int:
        if column not in members:
            return 0
        return int(pd.to_numeric(members[column], errors="coerce").fillna(0).sum())

    aspera_dicom_count = scope_count("public_dicom_files_outside_idc")
    public_non_dicom_count = scope_count("public_non_dicom_files")
    controlled_count = scope_count("controlled_files")

    if matching_only:
        st.info(
            "Showing only imaging that matches the cohort imaging filters. "
            "The cohort package uses the same facet rules."
        )

    source_tabs = st.tabs(
        [
            f"Public DICOM {dicom_count:,}",
            f"Public non-DICOM {public_non_dicom_count:,}",
            f"Controlled access {controlled_count:,}",
        ],
        key=f"imaging_sources_{patient_key}_{short_title}_{'all' if include_all_related else 'one'}",
        on_change="rerun",
    )
    if source_tabs[0].open:
      with source_tabs[0]:
        st.caption(
            f"IDC series: {dicom_count:,} shown of {dicom_total_count:,} available · "
            f"Public DICOM files outside IDC: {aspera_dicom_count:,}"
        )
        if dicom.empty:
            if aspera_dicom_count:
                st.info(
                    "No public IDC series are linked to this participant. The Participant "
                    "Inventory records public DICOM files in a TCIA Aspera package."
                )
            else:
                st.info("No public IDC DICOM series are linked to this participant.")
        else:
            shown = add_idc_viewer_urls(dicom, access)
            columns = [
                column
                for column in (
                    "study_date",
                    "Modality",
                    "SeriesDescription",
                    "BodyPartExamined",
                    "geometry_status",
                    "instanceCount",
                    "SeriesInstanceUID",
                    "viewer_url",
                )
                if column in shown
            ]
            st.dataframe(
                shown[columns],
                hide_index=True,
                width="stretch",
                column_config={
                    "viewer_url": st.column_config.LinkColumn("Viewer", display_text="Open viewer"),
                    "geometry_status": st.column_config.TextColumn("Image geometry"),
                },
            )
            add_label = (
                "Add matching public DICOM series"
                if matching_only
                else "Add all public DICOM series"
            )
            if st.button(add_label, key=f"draft_add_dicom_{patient_key}_{'all' if include_all_related else short_title}"):
                finish_cart_add(
                    add_cart_items(
                        [
                            cart_item(
                                "dicom",
                                row["SeriesInstanceUID"],
                                short_title=str(row.get("short_title", short_title)),
                                subject_id=subject_id,
                                label=f"{row.get('Modality', 'DICOM')} · {row.get('SeriesDescription', '')}",
                                source="IDC",
                                access_level="open",
                            )
                            for _, row in shown.iterrows()
                        ]
                    )
                )
        if aspera_dicom_count:
            for member in members.itertuples(index=False):
                if safe_int(getattr(member, "public_dicom_files_outside_idc", 0)):
                    render_aspera_packages(
                        paths,
                        str(member.short_title),
                        f"{patient_key}_dicom_{member.short_title}",
                    )
            if paths.public_non_dicom_db is None or not paths.public_non_dicom_db.exists():
                render_detail_install_notice(
                    "Participant-linked Aspera DICOM representation detail is not installed."
                )
            else:
                aspera_frames = []
                for member in members.itertuples(index=False):
                    frame = load_patient_public_non_dicom(
                        paths.public_non_dicom_db,
                        str(member.short_title),
                        str(member.subject_id),
                        include_dicom=True,
                    )
                    if not frame.empty:
                        frame = frame.copy()
                        frame["dataset_context"] = str(member.short_title)
                        aspera_frames.append(frame)
                aspera_dicom = (
                    pd.concat(aspera_frames, ignore_index=True, sort=False)
                    if aspera_frames
                    else pd.DataFrame()
                )
                if matching_only and not aspera_dicom.empty:
                    aspera_dicom = filter_imaging_rows(
                        aspera_dicom,
                        data_categories=data_categories or (),
                        data_types=data_types or (),
                        file_formats=file_formats or (),
                        body_parts=body_parts or (),
                        geometry=geometry,
                    )
                if not aspera_dicom.empty:
                    columns = [
                        column for column in (
                            "dataset_context", "file_format", "modality", "object_role",
                            "represented_file_count", "representation_provenance_class",
                            "participant_link_status",
                        ) if column in aspera_dicom
                    ]
                    st.dataframe(aspera_dicom[columns], hide_index=True, width="stretch")
                    st.caption(
                        "These are compact participant/modality holdings, not IDC series rows."
                    )

    if source_tabs[1].open:
      with source_tabs[1]:
        for member in members.itertuples(index=False):
            render_aspera_packages(
                paths,
                str(member.short_title),
                f"{patient_key}_non_dicom_{member.short_title}",
            )
        public_frames = []
        for member in members.itertuples(index=False):
            frame = load_patient_public_non_dicom(
                paths.public_non_dicom_db,
                str(member.short_title),
                str(member.subject_id),
            )
            if not frame.empty:
                frame = frame.copy()
                frame["dataset_context"] = str(member.short_title)
                public_frames.append(frame)
        public_detail = (
            pd.concat(public_frames, ignore_index=True, sort=False)
            if public_frames
            else pd.DataFrame()
        )
        if not public_detail.empty and "asset_id" in public_detail:
            public_detail = public_detail.drop_duplicates(
                subset=["dataset_context", "asset_id"], keep="first"
            )
        public_total_count = len(public_detail)
        if matching_only and not public_detail.empty:
            public_detail = filter_imaging_rows(
                public_detail,
                data_categories=data_categories or (),
                data_types=data_types or (),
                file_formats=file_formats or (),
                body_parts=body_parts or (),
                geometry=geometry,
            )
        if paths.public_non_dicom_db is None or not paths.public_non_dicom_db.exists():
            render_detail_install_notice(
                "Public non-DICOM file detail is not installed."
            )
        elif public_detail.empty:
            st.info(
                "No participant-linked public non-DICOM assets match the current imaging "
                "filters. This is not proof that the dataset has none; review the coverage "
                "states below."
            )
        else:
            image_metadata = load_public_non_dicom_image_metadata(
                paths.public_non_dicom_db,
                public_detail["asset_id"].astype(str).tolist(),
            )
            if not image_metadata.empty:
                public_detail = public_detail.merge(
                    image_metadata, on="asset_id", how="left", validate="one_to_one"
                )
            columns = populated_columns(
                public_detail,
                (
                    "dataset_context", "file_name", "asset_name", "file_format", "media_kind", "modality",
                    "geometry_status", "source_url",
                    "body_part_examined", "study_datetime", "sequence_class",
                    "sequence_tags", "sequences_present", "acquisition_dimensionality",
                    "scanner_site", "manufacturer", "manufacturer_model_name",
                    "magnetic_field_strength_t", "number_of_slices",
                    "slice_thickness_mm", "spacing_between_slices_mm",
                    "pixel_spacing_mm", "repetition_time_ms", "echo_time_ms",
                    "inversion_time_ms", "pre_included", "post_included",
                    "t2_included", "flair_included", "pathology_protocol",
                    "magnification", "object_role", "representation_provenance_class",
                    "location_count", "participant_link_status", "conflict_field_count",
                ),
            )
            st.dataframe(
                public_detail[columns],
                hide_index=True,
                width="stretch",
                column_config={
                    "source_url": st.column_config.LinkColumn(
                        "Dataset package", display_text="Open package"
                    ),
                    "geometry_status": st.column_config.TextColumn(
                        "Image geometry"
                    ),
                },
            )
            st.caption(
                f"Showing {len(public_detail):,} of {public_total_count:,} logical assets. "
                "The tab count is the participant-linked represented file count from "
                "the Participant Inventory. Each row is one logical asset; location "
                "count shows delivery/viewer copies without multiplying the asset count."
            )
            location_expander = st.expander(
                "Access and viewer locations",
                expanded=False,
                on_change="rerun",
            )
            if location_expander.open:
                with location_expander:
                    locations = load_public_non_dicom_locations(
                        paths.public_non_dicom_db,
                        public_detail["asset_id"].astype(str).tolist(),
                    )
                    location_columns = populated_columns(
                        locations,
                        (
                            "file_name", "managed_system", "access_level",
                            "availability_status", "representation_provenance_class",
                            "access_url", "viewer_url", "manifest_url",
                            "equivalence_status",
                        ),
                    )
                    if locations.empty:
                        st.caption("No delivery or viewer locations are represented.")
                    else:
                        st.dataframe(
                            locations[location_columns],
                            hide_index=True,
                            width="stretch",
                            column_config={
                                "access_url": st.column_config.LinkColumn(
                                    "Access", display_text="Open"
                                ),
                                "viewer_url": st.column_config.LinkColumn(
                                    "Viewer", display_text="Open viewer"
                                ),
                                "manifest_url": st.column_config.LinkColumn(
                                    "Manifest", display_text="Open manifest"
                                ),
                            },
                        )
            coverage_frames = []
            note_frames = []
            for member in members.itertuples(index=False):
                member_title = str(member.short_title)
                member_coverage = load_public_non_dicom_metadata_coverage(
                    paths.public_non_dicom_db, member_title
                )
                member_notes = load_public_non_dicom_metadata_notes(
                    paths.public_non_dicom_db, member_title
                )
                if not member_coverage.empty:
                    member_coverage = member_coverage.copy()
                    member_coverage["dataset_context"] = member_title
                    coverage_frames.append(member_coverage)
                if not member_notes.empty:
                    member_notes = member_notes.copy()
                    member_notes["dataset_context"] = member_title
                    note_frames.append(member_notes)
            coverage = (
                pd.concat(coverage_frames, ignore_index=True, sort=False)
                if coverage_frames
                else pd.DataFrame()
            )
            notes = (
                pd.concat(note_frames, ignore_index=True, sort=False)
                if note_frames
                else pd.DataFrame()
            )
            if not coverage.empty or not notes.empty:
                with st.expander("Metadata coverage and review notes", expanded=False):
                    if not coverage.empty:
                        coverage_columns = [
                            column for column in (
                                "dataset_context", "field_name", "eligible_assets", "populated_assets",
                                "source_raw_assets", "normalized_assets", "inferred_assets",
                                "distinct_value_count", "example_values_json",
                            ) if column in coverage
                        ]
                        st.dataframe(
                            coverage[coverage_columns], hide_index=True, width="stretch"
                        )
                    if not notes.empty:
                        note_columns = [
                            column for column in (
                                "dataset_context", "field_name", "note_code", "severity", "status",
                                "affected_assets", "description",
                            ) if column in notes
                        ]
                        st.dataframe(notes[note_columns], hide_index=True, width="stretch")
                    st.caption(
                        "Selected research values and compact source references are shown here; "
                        "verbose evidence remains in the optional audit companion."
                    )

    if source_tabs[2].open:
      with source_tabs[2]:
        controlled_frames = []
        for member in members.itertuples(index=False):
            frame = load_patient_controlled(
                paths.controlled_db,
                str(member.short_title),
                str(member.subject_id),
            )
            if not frame.empty:
                frame = frame.copy()
                frame["dataset_context"] = str(member.short_title)
                controlled_frames.append(frame)
        controlled = (
            pd.concat(controlled_frames, ignore_index=True, sort=False)
            if controlled_frames
            else pd.DataFrame()
        )
        controlled_total_count = len(controlled)
        if not controlled.empty:
            controlled = controlled.copy()
            controlled["file_format"] = controlled.get("file_type", "")
            controlled["geometry_status"] = "not_applicable"
        if matching_only and not controlled.empty:
            controlled = filter_imaging_rows(
                controlled,
                data_categories=data_categories or (),
                data_types=data_types or (),
                file_formats=file_formats or (),
                body_parts=body_parts or (),
                geometry=geometry,
            )
        if not paths.controlled_db.exists():
            render_detail_install_notice(
                "Controlled-access metadata are not installed; controlled payloads remain restricted."
            )
        elif controlled.empty:
            st.info(
                "No participant-linked controlled records were found. Missing links "
                "or coverage gaps do not establish that controlled data are absent."
            )
        else:
            st.markdown(
                f'<div class="access-callout"><strong>Controlled metadata only.</strong> Authorization is required before retrieval. Review the <a href="{POLICY_URL}">TCIA policy</a>.</div>',
                unsafe_allow_html=True,
            )
            columns = [column for column in ("dataset_context", "route_system", "modality", "file_format", "study_date", "series_description", "file_name", "drs_uri") if column in controlled]
            st.dataframe(controlled[columns], hide_index=True, width="stretch")
            st.caption(
                f"Showing {len(controlled):,} of {controlled_total_count:,} controlled metadata rows."
            )
            routed = controlled[controlled["drs_uri"].fillna("").astype(str).str.strip() != ""]
            if st.button("Add authorized DRS routes", disabled=routed.empty, key=f"draft_add_drs_{patient_key}_{'all' if include_all_related else short_title}"):
                finish_cart_add(
                    add_cart_items(
                        [
                            cart_item(
                                "drs",
                                row.get("drs_uri"),
                                short_title=str(row.get("dataset_context", short_title)),
                                subject_id=subject_id,
                                label=f"Controlled · {row.get('file_name', '')}",
                                source=str(row.get("route_system", "controlled")),
                                access_level="controlled",
                            )
                            for _, row in routed.iterrows()
                        ]
                    )
                )

    coverage_states = load_dataset_coverage_states(paths.participant_db, short_title)
    if not coverage_states.empty:
        with st.expander("Coverage states", expanded=False):
            st.warning(
                "Some dataset assets lack a participant crosswalk or have participant-link "
                "issues. These states describe metadata coverage, not data absence."
            )
            columns = [
                column for column in (
                    "coverage_state", "data_domain", "media_kind", "modality",
                    "file_format", "asset_count", "status", "issue_code", "description",
                    "explanation",
                ) if column in coverage_states
            ]
            st.dataframe(coverage_states[columns], hide_index=True, width="stretch")


def render_patient_detail(
    paths: DataPaths,
    catalog: pd.DataFrame,
    patient: pd.Series,
    members: pd.DataFrame,
    *,
    data_categories: list[str],
    data_types: list[str],
    file_formats: list[str],
    body_parts: list[str],
    geometry: str,
    imaging_contents: str,
) -> None:
    subject_id = str(patient["subject_id"])
    st.markdown("<div class='section-label'>Selected patient</div>", unsafe_allow_html=True)
    st.subheader(subject_id)
    st.markdown(f'<div class="identity-line">{html.escape(subject_id)}</div>', unsafe_allow_html=True)
    st.caption(
        f"{safe_int(patient.get('dataset_count')) or 1} dataset "
        f"context{'s' if safe_int(patient.get('dataset_count')) != 1 else ''}; "
        "identity remains scoped to each Collection or Analysis Result"
    )

    access = str(patient.get("resolved_access_level", "unknown"))
    if access in {"controlled", "mixed"}:
        st.markdown(
            f'<div class="access-callout"><strong>{html.escape(access_label(access))}.</strong> Metadata remain visible; controlled files require authorization and have no public viewer route.</div>',
            unsafe_allow_html=True,
        )

    summary_tab, imaging_tab, provenance_tab = st.tabs(
        ["Summary", "Imaging & files", "Clinical & provenance"],
        key=f"participant_detail_{patient['patient_key']}",
        on_change="rerun",
    )
    if summary_tab.open:
        with summary_tab:
            render_patient_summary(patient, members)
    if provenance_tab.open:
      with provenance_tab:
        participant_keys = member_json_values(
            patient,
            "member_patient_keys_json",
            patient.get("participant_key", ""),
        )
        subject_ids = member_json_values(
            patient, "member_subject_ids_json", subject_id
        )
        subject_ids = list(
            dict.fromkeys(
                [*subject_ids, *split_tokens(patient.get("clinical_subject_ids", ""))]
            )
        )

        if not paths.clinical_db.exists():
            if bool(patient.get("has_clinical", False)):
                render_detail_install_notice(
                    "Clinical data are represented for this participant, but clinical detail is not installed."
                )
            else:
                st.info("No participant-linked clinical data are represented in the inventory.")
        else:
            facts = load_patient_clinical_facts(
                paths.clinical_db,
                str(patient["short_title"]),
                subject_id,
                subject_ids=subject_ids,
            )
            if not facts.empty:
                st.dataframe(facts, hide_index=True, width="stretch")
                st.caption(
                    "Submitted values remain beside normalized, harmonized, inferred, and "
                    "resolved representations; standardized values do not overwrite originals."
                )
            else:
                st.info(
                    "The clinical detail artifact is installed, but no case-equivalent "
                    "participant facts were found for this dataset scope."
                )
            longitudinal = load_patient_clinical_longitudinal(
                paths.clinical_db,
                str(patient["short_title"]),
                subject_ids,
            )
            if not longitudinal.empty:
                st.markdown(
                    "<div class='section-label'>Longitudinal imaging context</div>",
                    unsafe_allow_html=True,
                )
                longitudinal_columns = populated_columns(
                    longitudinal,
                    (
                        "study_datetime", "observation_type", "file_name",
                        "age_at_imaging_years", "sequence_class", "sequence_tags",
                        "acquisition_dimensionality", "scanner_site", "manufacturer",
                        "manufacturer_model_name", "magnetic_field_strength_t",
                        "slice_thickness_mm", "spacing_between_slices_mm",
                        "repetition_time_ms", "echo_time_ms", "inversion_time_ms",
                    ),
                )
                st.dataframe(
                    longitudinal[longitudinal_columns],
                    hide_index=True,
                    width="stretch",
                )
                st.caption(
                    "Visit-, scanner-, and file-level observations remain separate so "
                    "expected changes over time are not reported as patient-level conflicts."
                )

        with st.expander("Provenance and troubleshooting", expanded=False):
            st.caption(
                "Managed systems and delivery locations are shown here for routing, "
                "reconciliation, and troubleshooting—not as separate logical assets."
            )
            if len(subject_ids) > 1:
                st.caption(
                    "Case-equivalent source identifiers consolidated in this row: "
                    + ", ".join(subject_ids)
                )
            asset_frames = [
                load_participant_assets(paths.participant_db, participant_key)
                for participant_key in participant_keys
            ]
            identifier_frames = [
                load_participant_identifiers(paths.participant_db, participant_key)
                for participant_key in participant_keys
            ]
            identity_frames = [
                load_participant_identity_evidence(paths.participant_db, participant_key)
                for participant_key in participant_keys
            ]
            assets = pd.concat(
                [frame for frame in asset_frames if not frame.empty],
                ignore_index=True,
                sort=False,
            ) if any(not frame.empty for frame in asset_frames) else pd.DataFrame()
            identifiers = pd.concat(
                [frame for frame in identifier_frames if not frame.empty],
                ignore_index=True,
                sort=False,
            ) if any(not frame.empty for frame in identifier_frames) else pd.DataFrame()
            identity_evidence = pd.concat(
                [frame for frame in identity_frames if not frame.empty],
                ignore_index=True,
                sort=False,
            ) if any(not frame.empty for frame in identity_frames) else pd.DataFrame()
            source_namespaces = (
                ", ".join(
                    sorted(
                        set(identifiers["identifier_namespace"].dropna().astype(str)),
                        key=str.casefold,
                    )
                )
                if "identifier_namespace" in identifiers
                else ""
            )
            identity_fields = [
                ("Within-dataset identity", patient.get("within_dataset_identity_status")),
                ("Resolution method", patient.get("identity_resolution_method")),
                ("Source namespaces", source_namespaces),
                ("Cross-dataset identity", patient.get("cross_dataset_identity_status")),
            ]
            identity_rows = [
                {"Field": label, "Value": str(value)}
                for label, value in identity_fields
                if pd.notna(value) and str(value).strip()
            ]
            if identity_rows:
                st.dataframe(pd.DataFrame(identity_rows), hide_index=True, width="stretch")
            if not assets.empty:
                asset_columns = [
                    column for column in (
                        "managed_system", "source_artifact", "access_level", "data_domain",
                        "media_kind", "modality", "file_format", "object_role",
                        "inventory_status", "source_version", "detail_pointer",
                    ) if column in assets
                ]
                st.dataframe(assets[asset_columns], hide_index=True, width="stretch")
            if not identifiers.empty:
                identifier_columns = [
                    column for column in (
                        "managed_system", "identifier_namespace", "raw_identifier",
                        "normalized_identifier", "link_evidence",
                    ) if column in identifiers
                ]
                st.dataframe(
                    identifiers[identifier_columns], hide_index=True, width="stretch"
                )
            if not identity_evidence.empty:
                evidence_columns = [
                    column for column in (
                        "resolution_scope", "resolution_method", "status", "confidence",
                        "description",
                    ) if column in identity_evidence
                ]
                st.dataframe(
                    identity_evidence[evidence_columns], hide_index=True, width="stretch"
                )
    if imaging_tab.open:
      with imaging_tab:
        ordered_members = members.assign(
            _type_rank=members["dataset_type"].map(
                lambda value: 0 if value == "Collection" else 1
            )
        ).sort_values(["_type_rank", "short_title"], kind="stable")
        ordered_members = ordered_members.reset_index(drop=True)
        scope_options = ["All data for this patient"]
        scope_options.extend(
            (
                f"Collection only · {row['short_title']}"
                if row.get("dataset_type") == "Collection"
                else f"Analysis Result only · {row['short_title']}"
            )
            for _, row in ordered_members.iterrows()
        )
        selected_scope = st.selectbox(
            "Imaging scope",
            scope_options,
            key=f"imaging_context_{patient['patient_key']}",
            help=(
                "IDC physically stores derived Analysis Result series inside the "
                "source collection. This control applies TCIA's logical dataset scope."
            ),
        )
        if selected_scope == scope_options[0]:
            scope_members = ordered_members
            include_all_related = True
            st.caption(
                "Showing the source Collection together with all related Analysis "
                "Result data linked to this patient."
            )
        else:
            scope_members = ordered_members.iloc[[scope_options.index(selected_scope) - 1]]
            include_all_related = False
            selected_type = str(scope_members.iloc[0].get("dataset_type", "Dataset"))
            if selected_type == "Collection":
                st.caption(
                    "Showing original Collection data only; series assigned to an "
                    "Analysis Result are excluded even though IDC stores them in the same collection."
                )
            else:
                st.caption(
                    "Showing only data assigned to this Analysis Result; original "
                    "Collection data are excluded."
                )
        render_imaging(
            paths,
            catalog,
            scope_members,
            include_all_related=include_all_related,
            data_categories=data_categories,
            data_types=data_types,
            file_formats=file_formats,
            body_parts=body_parts,
            geometry=geometry,
            matching_only=imaging_contents == "Only imaging matching filters",
        )


def main() -> None:
    paths = resolve_data_paths(APP_DIR)
    if paths.participant_db is None:
        st.error("The V2 Participant Inventory cache path is not configured.")
        st.stop()
    try:
        v2_cache = paths.participant_db.parent
        configured_skill_root = os.environ.get("TCIA_QUERY_SKILL_ROOT")
        skill_root = (
            Path(configured_skill_root).expanduser()
            if configured_skill_root
            else APP_DIR.parent / "tcia-query-skill"
        ).resolve()
        (
            participant_fingerprint,
            snapshot_fingerprint,
            participant_schema,
            installed_profile,
            release_contract,
            bundle_fingerprint,
            generated_at_utc,
        ) = prepare_v2_release(str(skill_root), str(v2_cache))
    except Exception as exc:
        st.error(
            "The app could not automatically prepare the stable V2 research-detail "
            "bundle. Check network access, shared-cache permissions, and "
            "TCIA_QUERY_SKILL_ROOT, then restart the app."
        )
        st.code(
            "python3 scripts/tcia_v2_bundle.py install --profile research_detail",
            language="bash",
        )
        st.exception(exc)
        st.stop()
    # All release artifacts are resolved from one official stable V2 install.
    # Legacy tcia-snapshot-latest paths are never combined with this bundle.
    public_detail = installed_component(v2_cache, "public_non_dicom")
    controlled_detail = installed_component(v2_cache, "controlled_access")
    clinical_detail = installed_component(v2_cache, "clinical")
    paths = replace(
        paths,
        snapshot_db=v2_cache / "tcia_snapshot.sqlite",
        participant_db=v2_cache / "participant_inventory.sqlite",
        public_non_dicom_db=(
            public_detail.database
            if public_detail is not None
            else v2_cache / ".public_non_dicom_metadata.not-installed.sqlite"
        ),
        controlled_db=(
            controlled_detail.database
            if controlled_detail is not None
            else v2_cache / ".controlled_access_metadata.not-installed.sqlite"
        ),
        clinical_db=(
            clinical_detail.database
            if clinical_detail is not None
            else v2_cache / ".clinical_metadata.not-installed.sqlite"
        ),
        bundle_manifest=v2_cache / "tcia_metadata_v2_bundle_manifest.json",
        install_state=v2_cache / INSTALL_STATE_ASSET,
    )
    signatures = paths.signatures()
    cache_key = (PATIENT_INDEX_SCHEMA_VERSION, signatures)
    if not paths.participant_db.exists():
        st.error(f"V2 Participant Inventory not found at {paths.participant_db}.")
        st.stop()

    try:
        patients, membership_rows = cached_patient_views(paths, cache_key)
        catalog = cached_catalog(paths, signatures)
        participant_asset_facets = cached_participant_asset_facets(paths, signatures)
    except Exception as exc:
        st.exception(exc)
        st.stop()

    st.session_state.setdefault("cart_items", [])
    render_brand_and_cart()
    st.caption(
        f"Metadata: {V2_RELEASE_TAG} · {release_contract} · "
        f"{installed_profile.replace('_', ' ')} · "
        f"Participant Inventory schema {participant_schema} · "
        f"Bundle {bundle_fingerprint[:12]} · Participant {participant_fingerprint[:12]} · "
        f"Snapshot {snapshot_fingerprint[:12]} · {generated_at_utc[:10]}"
    )

    st.markdown("<div class='section-label'>Cohort controls</div>", unsafe_allow_html=True)
    st.session_state.setdefault("draft_dataset_type", "All")
    dataset_type = st.segmented_control(
        "Dataset type",
        DATASET_TYPE_FILTERS,
        required=True,
        format_func=lambda value: {
            "All": "All",
            "Collection": "Collections",
            "Analysis Result": "Analysis results",
        }[value],
        key="draft_dataset_type",
    )
    scoped_patients, scoped_memberships = filter_patient_groups_by_dataset_type(
        patients,
        membership_rows,
        str(dataset_type),
    )
    f1, f2, f3 = st.columns([1.5, 1.1, .8])
    search = f1.text_input("Search", placeholder="Dataset or patient ID", key="draft_search").strip()
    datasets = f2.multiselect(
        "Dataset",
        option_values(scoped_memberships, "short_title"),
        key="draft_datasets",
    )
    access = f3.multiselect("Access", option_values(patients, "resolved_access_level"), format_func=access_label, key="draft_access")

    working = scoped_patients.copy()
    if search:
        pattern = search.casefold()
        working = working[
            working["dataset_memberships"].astype(str).str.casefold().str.contains(pattern, regex=False)
            | working["subject_id"].astype(str).str.casefold().str.contains(pattern, regex=False)
            | working.get("title", "").astype(str).str.casefold().str.contains(pattern, regex=False)
        ]
    if datasets:
        wanted_datasets = set(datasets)
        working = working[
            working.apply(
                lambda row: bool(
                    wanted_datasets.intersection(member_short_titles(row))
                ),
                axis=1,
            )
        ]
    if access:
        working = working[working["resolved_access_level"].isin(access)]

    c1, c2, c3, c4 = st.columns(4)
    data_categories = c1.multiselect(
        "Data category",
        token_options(working, "data_categories"),
        key="draft_data_categories",
        help="Broad content category aligned with TCIA WordPress download labels.",
    )
    data_types = c2.multiselect(
        "Data type",
        token_options(working, "data_types"),
        key="draft_data_types",
        help="Specific modality or content type, such as CT, MR, Segmentation, or Whole Slide Image.",
    )
    file_formats = c3.multiselect(
        "File format",
        token_options(working, "file_formats"),
        key="draft_file_formats",
        help="Physical encoding such as DICOM, NIfTI, MHA, SVS, CSV, or MPG.",
    )
    geometry = c4.segmented_control(
        "Image geometry",
        GEOMETRY_FILTER_OPTIONS,
        default="Any",
        required=True,
        key="draft_geometry",
        help=(
            "Filters individual series and files by assessed grid or volume regularity. "
            "Any includes unchecked, indeterminate, out-of-scope, and non-volume data. "
            "Regular includes items that passed an available geometry assessment. "
            "Irregular includes items with a known failed geometry check. Geometry "
            "assessment does not establish image quality or clinical usability."
        ),
    )
    facet_memberships = scoped_memberships
    if datasets:
        facet_memberships = facet_memberships[
            facet_memberships["short_title"].isin(datasets)
        ]
    working = filter_patient_groups_by_asset_facets(
        working,
        facet_memberships,
        participant_asset_facets,
        data_categories=data_categories,
        data_types=data_types,
        file_formats=file_formats,
        geometry=str(geometry),
    )

    imaging_contents = st.segmented_control(
        "Imaging & download contents",
        ("All available imaging", "Only imaging matching filters"),
        default="All available imaging",
        required=True,
        key="draft_imaging_contents",
        help=(
            "Controls both Imaging & Files results and the cohort package after matching "
            "participants are found. All available imaging includes every linked imaging "
            "item in the selected dataset scope. Only imaging matching filters includes "
            "series and files satisfying the selected imaging filters. Items without an "
            "individual route remain inventory-only."
        ),
    )

    with st.expander("Advanced clinical and imaging filters", expanded=False):
        st.markdown("**Imaging detail**")
        body_parts = st.multiselect(
            "Body part",
            token_options(working, "body_parts"),
            key="draft_body_parts",
            help=(
                "The cohort-wide filter uses DICOM BodyPartExamined values from IDC. "
                "File-level anatomy from public non-DICOM detail appears after drill-down "
                "and is not added to this global filter."
            ),
        )
        working = apply_token_filter(working, "body_parts", body_parts)

        multiple_imaging_dates = st.toggle(
            "Multiple imaging dates",
            key="draft_multiple_imaging_dates",
            help=(
                "Keep patients with public DICOM on at least two distinct IDC StudyDate values. "
                "Distinct dates are an imaging-time-point proxy, not a curated visit definition."
            ),
        )
        if multiple_imaging_dates:
            working = working[
                working["has_multiple_imaging_dates"].fillna(False).astype(bool)
            ]

        pathology_values: dict[str, list[str]] = {}
        if "Pathology" in data_categories:
            st.markdown("**Pathology**")
            p1, p2 = st.columns(2)
            pathology_filters = [
                (
                    p1,
                    "Protocol or stain",
                    "pathology_protocols",
                    "draft_pathology_protocols",
                ),
                (
                    p2,
                    "Magnification",
                    "pathology_magnifications",
                    "draft_pathology_magnifications",
                ),
            ]
            for container, label, column, key in pathology_filters:
                selected = container.multiselect(
                    label,
                    token_options(working, column),
                    key=key,
                    help=(
                        "Participant-linked public non-DICOM metadata only; "
                        "dataset-level files without a patient crosswalk are not assigned."
                    ),
                )
                pathology_values[column] = selected
                working = apply_token_filter(working, column, selected)

        st.markdown("**Clinical**")
        clinical_values: dict[str, list[str]] = {}
        age_at_imaging_range = (0, 120)
        include_inferred_clinical = True
        if not paths.clinical_db.exists():
            st.caption("Startup preparation did not provide clinical detail.")
            render_detail_install_notice(
                "Clinical detail is required for diagnosis, site, sex-at-birth, and "
                "vital-status filters. Participant search remains available from the core inventory."
            )
        else:
            st.caption(
                "Clinical filters use the installed detail artifact; raw and alternate "
                "values remain available in participant provenance."
            )
            clinical_settings, age_control = st.columns([1, 2])
            include_inferred_clinical = clinical_settings.toggle(
                "Include dataset-inferred diagnosis and site",
                value=True,
                key="draft_include_inferred_clinical",
                help=(
                    "When disabled, diagnosis and site choices use patient-level or "
                    "patient-inherited evidence only. Other patients remain visible unless "
                    "a diagnosis or site value is selected."
                ),
            )
            age_at_imaging_range = age_control.slider(
                "Earliest recorded age at imaging",
                min_value=0,
                max_value=120,
                value=(0, 120),
                step=1,
                format="%d years",
                key="draft_age_at_imaging",
                help=(
                    "Uses the earliest accepted age-at-imaging value. Changing the full "
                    "range excludes patients whose imaging age is unavailable."
                ),
            )
            if age_at_imaging_range != (0, 120):
                ages = pd.to_numeric(
                    working["age_at_imaging_years"], errors="coerce"
                )
                working = working[
                    ages.between(*age_at_imaging_range, inclusive="both")
                ]

            c1, c2, c3, c4 = st.columns(4)
            clinical_filters = [
                (c1, "Primary diagnosis", "primary_diagnosis", "draft_diagnosis"),
                (c2, "Primary site", "primary_site", "draft_site"),
                (c3, "Sex at birth", "sex_at_birth", "draft_sex"),
                (c4, "Vital status", "vital_status", "draft_vital"),
            ]
            for container, label, column, key in clinical_filters:
                option_frame = working
                inference_column = f"{column}_is_inferred"
                if (
                    not include_inferred_clinical
                    and inference_column in working
                ):
                    option_frame = working[
                        ~working[inference_column].fillna(False).astype(bool)
                    ]
                selected = container.multiselect(
                    label, option_values(option_frame, column), key=key
                )
                clinical_values[column] = selected
                if selected:
                    selected_mask = working[column].isin(selected)
                    if (
                        not include_inferred_clinical
                        and inference_column in working
                    ):
                        selected_mask &= ~working[inference_column].fillna(
                            False
                        ).astype(bool)
                    working = working[selected_mask]

    reset_col, count_col = st.columns([.18, .82], vertical_alignment="center")
    reset_col.button("Clear filters", on_click=clear_filters, width="stretch")
    count_col.markdown(
        f"**{len(working):,} matching patients** across "
        f"**{count_visible_dataset_contexts(working, scoped_memberships, datasets):,} datasets**"
    )

    render_filter_chips(
        [
            (
                "Dataset type",
                "" if dataset_type == "All" else {
                    "Collection": "Collections",
                    "Analysis Result": "Analysis results",
                }[str(dataset_type)],
            ),
            ("Search", search),
            ("Dataset", datasets),
            ("Access", [access_label(value) for value in access]),
            ("Data category", data_categories),
            ("Data type", data_types),
            ("Body part", body_parts),
            ("File format", file_formats),
            ("Image geometry", "" if geometry == "Any" else geometry),
            (
                "Imaging contents",
                "Matching filters only"
                if imaging_contents == "Only imaging matching filters"
                else "",
            ),
            (
                "Imaging dates",
                "Multiple dates" if multiple_imaging_dates else "",
            ),
            (
                "Pathology protocol",
                pathology_values.get("pathology_protocols", []),
            ),
            (
                "Pathology magnification",
                pathology_values.get("pathology_magnifications", []),
            ),
            (
                "Imaging age",
                (
                    f"{age_at_imaging_range[0]}–{age_at_imaging_range[1]} years"
                    if age_at_imaging_range != (0, 120)
                    else ""
                ),
            ),
            (
                "Clinical evidence",
                "Patient-level only" if not include_inferred_clinical else "",
            ),
            ("Diagnosis", clinical_values.get("primary_diagnosis", [])),
            ("Site", clinical_values.get("primary_site", [])),
            ("Sex", clinical_values.get("sex_at_birth", [])),
            ("Vital status", clinical_values.get("vital_status", [])),
        ]
    )

    visible_group_keys = set(working["patient_group_key"].astype(str))
    visible_memberships = scoped_memberships[
        scoped_memberships["patient_group_key"].astype(str).isin(visible_group_keys)
    ].copy()

    render_filtered_cohort_export(
        paths,
        catalog,
        working,
        visible_memberships,
        datasets,
        data_categories,
        data_types,
        file_formats,
        body_parts,
        str(geometry),
        str(imaging_contents),
    )

    with st.container():
        st.markdown("<div class='section-label'>Patient results</div>", unsafe_allow_html=True)
        if working.empty:
            st.info("No patients match these filters. Remove a filter to broaden the cohort.")
        else:
            result_filter_signature = (
                str(dataset_type),
                search.casefold(),
                tuple(datasets),
                tuple(access),
                tuple(data_categories),
                tuple(data_types),
                tuple(body_parts),
                tuple(file_formats),
                str(geometry),
                str(imaging_contents),
                bool(multiple_imaging_dates),
                tuple(pathology_values.get("pathology_protocols", [])),
                tuple(pathology_values.get("pathology_magnifications", [])),
                tuple(age_at_imaging_range),
                bool(include_inferred_clinical),
                tuple(clinical_values.get("primary_diagnosis", [])),
                tuple(clinical_values.get("primary_site", [])),
                tuple(clinical_values.get("sex_at_birth", [])),
                tuple(clinical_values.get("vital_status", [])),
            )
            if (
                st.session_state.get("_patient_results_filter_signature")
                != result_filter_signature
            ):
                st.session_state["_patient_results_filter_signature"] = (
                    result_filter_signature
                )
                reset_patient_results_page()

            st.session_state.setdefault("patient_results_page_size", 50)
            page_size = int(st.session_state["patient_results_page_size"])
            total_pages = max(1, (len(working) + page_size - 1) // page_size)
            current_page = min(
                max(1, int(st.session_state.get("patient_results_page", 1))),
                total_pages,
            )
            st.session_state["patient_results_page"] = current_page

            page_col, size_col = st.columns(2, vertical_alignment="bottom")
            page_col.number_input(
                "Page",
                min_value=1,
                max_value=total_pages,
                step=1,
                key="patient_results_page",
                help="Jump directly to any page in the complete result set.",
            )
            size_col.selectbox(
                "Rows per page",
                PATIENT_RESULTS_PAGE_SIZES,
                key="patient_results_page_size",
                on_change=reset_patient_results_page,
            )

            display_columns = [
                column
                for column in (
                    "dataset_memberships",
                    "subject_id",
                    "resolved_access_level",
                    "dicom_series",
                    "public_dicom_files_outside_idc",
                    "public_non_dicom_files",
                    "controlled_files",
                    "data_categories",
                    "data_types",
                    "body_parts",
                    "primary_diagnosis",
                    "primary_site",
                    "sex_at_birth",
                    "vital_status",
                )
                if column in working
            ]
            page_start = (current_page - 1) * page_size
            page_end = min(page_start + page_size, len(working))
            page_frame = working.iloc[page_start:page_end]
            result = st.dataframe(
                page_frame[display_columns],
                hide_index=True,
                width="stretch",
                height=560,
                on_select="rerun",
                selection_mode="single-row",
                key="draft_patient_table",
                column_config={
                    "dataset_memberships": st.column_config.TextColumn(
                        "Dataset memberships", pinned=True, width="large"
                    ),
                    "subject_id": st.column_config.TextColumn("Patient", pinned=True),
                    "resolved_access_level": st.column_config.TextColumn("Access"),
                    "data_categories": st.column_config.TextColumn("Data category", width="medium"),
                    "data_types": st.column_config.TextColumn("Data type", width="large"),
                    "body_parts": st.column_config.TextColumn("Body part"),
                    "primary_diagnosis": st.column_config.TextColumn("Diagnosis"),
                    "primary_site": st.column_config.TextColumn("Site"),
                    "sex_at_birth": st.column_config.TextColumn("Sex at birth"),
                    "vital_status": st.column_config.TextColumn("Vital status"),
                    "dicom_series": st.column_config.NumberColumn(
                        "IDC series",
                        help="Participant-linked public DICOM series indexed by IDC.",
                    ),
                    "public_dicom_files_outside_idc": st.column_config.NumberColumn(
                        "Other DICOM files",
                        help="Represented public DICOM files in TCIA packages but absent from IDC.",
                    ),
                    "public_non_dicom_files": st.column_config.NumberColumn(
                        "Public non-DICOM",
                        help="Participant-linked represented public non-DICOM files.",
                    ),
                    "controlled_files": st.column_config.NumberColumn(
                        "Controlled access",
                        help="Participant-linked controlled-access metadata/file records.",
                    ),
                },
            )
            if result.selection.rows:
                selected_index = page_frame.index[result.selection.rows[0]]
                st.session_state.selected_patient_key = working.loc[selected_index, "patient_key"]
            selected_result_key = st.session_state.get("selected_patient_key")
            selected_result = working[
                working["patient_key"] == selected_result_key
            ]
            selection_guidance = (
                f"Selected patient {selected_result.iloc[0]['subject_id']}; details appear below."
                if not selected_result.empty
                else "Select one row to inspect its details below the results."
            )
            st.caption(
                f"Showing {page_start + 1:,}–{page_end:,} of {len(working):,} matches "
                f"· Page {current_page:,} of {total_pages:,}. {selection_guidance}"
            )

    with st.container():
        selected_key = st.session_state.get("selected_patient_key")
        selected = patients[patients["patient_key"] == selected_key]
        visible_keys = set(working["patient_key"].tolist()) if not working.empty else set()
        if selected.empty or selected_key not in visible_keys:
            if selected_key not in visible_keys:
                st.session_state.pop("selected_patient_key", None)
        else:
            st.divider()
            selected_members = membership_rows[
                membership_rows["patient_group_key"] == selected_key
            ].copy()
            render_patient_detail(
                paths,
                catalog,
                selected.iloc[0],
                selected_members,
                data_categories=data_categories,
                data_types=data_types,
                file_formats=file_formats,
                body_parts=body_parts,
                geometry=str(geometry),
                imaging_contents=str(imaging_contents),
            )


if __name__ == "__main__":
    main()
