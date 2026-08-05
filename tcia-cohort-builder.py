"""TCIA Participant Explorer: branded patient-level discovery and retrieval."""

from __future__ import annotations

import hashlib
import html
import json
import logging
from pathlib import Path

import pandas as pd
import streamlit as st

from cohort_builder_data import (
    POLICY_URL,
    DataPaths,
    add_idc_viewer_urls,
    build_filtered_cohort_download,
    build_grouped_patient_index,
    build_manifest_download,
    build_patient_index,
    cart_item,
    collect_filtered_imaging_routes,
    deduplicate_cart,
    load_dataset_catalog,
    load_patient_clinical_facts,
    load_patient_controlled,
    load_patient_idc,
    load_patient_nifti,
    load_patient_pathdb,
    resolve_data_paths,
    split_tokens,
)


PATIENT_INDEX_SCHEMA_VERSION = 3
APP_DIR = Path(__file__).resolve().parent
BRAND_SKILL_DIR = Path.home() / ".codex" / "skills" / "tcia-brand-guidelines"
LOGO_PATH = BRAND_SKILL_DIR / "assets" / "tcia-logo-dark.svg"
OFFICIAL_LOGO_URL = (
    "https://www.cancerimagingarchive.net/wp-content/uploads/2021/06/"
    "TCIA-Logo-01.svg"
)


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
    for key in (
        "draft_datasets",
        "draft_access",
        "draft_imaging",
        "draft_modalities",
        "draft_body_parts",
        "draft_diagnosis",
        "draft_site",
        "draft_sex",
        "draft_vital",
    ):
        st.session_state[key] = []
    st.session_state["draft_conflicts"] = False
    st.session_state.pop("selected_patient_key", None)


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
    imaging_sources: list[str],
    modalities: list[str],
    body_parts: list[str],
) -> str:
    digest = hashlib.sha256()
    patient_keys = sorted(patients.get("patient_key", pd.Series(dtype=str)).astype(str))
    for value in patient_keys:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    for group in (imaging_sources, modalities, body_parts):
        digest.update("\x1f".join(sorted(group, key=str.casefold)).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def member_short_titles(patient: pd.Series) -> list[str]:
    value = patient.get("member_short_titles_json")
    if pd.notna(value) and str(value).strip():
        try:
            decoded = json.loads(str(value))
            if isinstance(decoded, list):
                return [str(item) for item in decoded]
        except json.JSONDecodeError:
            pass
    return [str(patient.get("short_title", ""))]


def dataset_membership_count(frame: pd.DataFrame) -> int:
    titles: set[str] = set()
    for _, row in frame.iterrows():
        titles.update(member_short_titles(row))
    return len(titles)


def render_filtered_cohort_export(
    paths: DataPaths,
    catalog: pd.DataFrame,
    patients: pd.DataFrame,
    membership_rows: pd.DataFrame,
    selected_datasets: list[str],
    imaging_sources: list[str],
    modalities: list[str],
    body_parts: list[str],
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
            imaging_sources,
            modalities,
            body_parts + selected_datasets + [str(include_related)],
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
        if modalities or body_parts:
            st.caption(
                "Modality and body-part filters are reapplied to individual imaging "
                "rows, so unrelated series or files from a matching patient are excluded."
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
                    imaging_sources=imaging_sources,
                    modalities=modalities,
                    body_parts=body_parts,
                    direct_collection_titles=direct_collection_titles,
                )
                payload, filename, mime, counts = build_filtered_cohort_download(
                    patients, routes, unrouted
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


def render_patient_summary(patient: pd.Series) -> None:
    metrics = st.columns(5)
    metrics[0].metric("DICOM", safe_int(patient.get("dicom_series")))
    metrics[1].metric("NIfTI", safe_int(patient.get("nifti_files")))
    metrics[2].metric("PathDB", safe_int(patient.get("pathdb_slides")))
    metrics[3].metric("Controlled", safe_int(patient.get("controlled_files")))
    metrics[4].metric("Conflicts", safe_int(patient.get("conflict_count")))

    facts = [
        ("Access", access_label(patient.get("resolved_access_level"))),
        ("Sex at birth", patient.get("sex_at_birth")),
        ("Age at baseline", patient.get("age_at_baseline")),
        ("Primary diagnosis", patient.get("primary_diagnosis")),
        ("Primary site", patient.get("primary_site")),
        ("Stage", patient.get("stage")),
        ("Grade", patient.get("grade")),
        ("Vital status", patient.get("vital_status")),
        ("Available imaging", patient.get("available_imaging")),
        ("Modalities", patient.get("modalities")),
    ]
    rows = [
        {"Field": label, "Resolved value": str(value)}
        for label, value in facts
        if pd.notna(value) and str(value).strip()
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    inferred = []
    if safe_int(patient.get("primary_diagnosis_is_inferred")):
        inferred.append("diagnosis")
    if safe_int(patient.get("primary_site_is_inferred")):
        inferred.append("site")
    if inferred:
        st.info("Dataset-scope inference used for: " + ", ".join(inferred) + ". Review provenance before analysis.")


def render_imaging(
    paths: DataPaths,
    catalog: pd.DataFrame,
    patient: pd.Series,
    *,
    direct_collection_only: bool = False,
) -> None:
    short_title = str(patient["short_title"])
    subject_id = str(patient["subject_id"])
    access = str(patient.get("resolved_access_level", "unknown"))
    idc_subject = str(patient.get("idc_subject_id") if pd.notna(patient.get("idc_subject_id")) else subject_id)
    pathdb_subject = str(patient.get("pathdb_subject_id") if pd.notna(patient.get("pathdb_subject_id")) else subject_id)
    nifti_subject = str(patient.get("nifti_subject_id") if pd.notna(patient.get("nifti_subject_id")) else subject_id)
    controlled_subject = str(patient.get("controlled_subject_id") if pd.notna(patient.get("controlled_subject_id")) else subject_id)

    collection_id = str(patient.get("idc_collection_id")) if pd.notna(patient.get("idc_collection_id")) else None
    analysis_result_id = (
        str(patient.get("idc_analysis_result_id"))
        if pd.notna(patient.get("idc_analysis_result_id")) and str(patient.get("idc_analysis_result_id")).strip()
        else None
    )
    dicom = load_patient_idc(
        paths,
        catalog,
        short_title,
        idc_subject,
        collection_id=collection_id,
        analysis_result_id=analysis_result_id,
        direct_collection_only=direct_collection_only,
    )
    pathdb = load_patient_pathdb(paths.snapshot_db, short_title, pathdb_subject)
    nifti = load_patient_nifti(paths.nifti_db, short_title, nifti_subject)
    controlled = load_patient_controlled(paths.controlled_db, short_title, controlled_subject)

    source_tabs = st.tabs(
        [
            f"DICOM {len(dicom):,}",
            f"NIfTI {len(nifti):,}",
            f"PathDB {len(pathdb):,}",
            f"Controlled {len(controlled):,}",
        ]
    )
    with source_tabs[0]:
        if dicom.empty:
            st.info("No public IDC DICOM series are linked to this patient.")
        else:
            shown = add_idc_viewer_urls(dicom, access)
            columns = [
                column
                for column in (
                    "study_date",
                    "Modality",
                    "SeriesDescription",
                    "BodyPartExamined",
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
                },
            )
            if st.button("Add all public DICOM series", key=f"draft_add_dicom_{patient['patient_key']}"):
                finish_cart_add(
                    add_cart_items(
                        [
                            cart_item(
                                "dicom",
                                row["SeriesInstanceUID"],
                                short_title=short_title,
                                subject_id=subject_id,
                                label=f"{row.get('Modality', 'DICOM')} · {row.get('SeriesDescription', '')}",
                                source="IDC",
                                access_level="open",
                            )
                            for _, row in shown.iterrows()
                        ]
                    )
                )

    with source_tabs[1]:
        if nifti.empty:
            st.info("No NIfTI file metadata are linked to this patient.")
        else:
            columns = [column for column in ("study_date", "modality", "series_description", "file_name", "package_path", "quality_flag_json") if column in nifti]
            st.dataframe(nifti[columns], hide_index=True, width="stretch")
            st.caption("Package paths are metadata-only until a validated Data Retriever route is available.")

    with source_tabs[2]:
        if pathdb.empty:
            st.info("No PathDB slide rows are linked to this patient.")
        else:
            columns = [column for column in ("slide_id", "modality", "data_format", "cancer_type", "viewer_url", "imageUrl") if column in pathdb]
            st.dataframe(
                pathdb[columns],
                hide_index=True,
                width="stretch",
                column_config={
                    "viewer_url": st.column_config.LinkColumn("Viewer", display_text="Open viewer"),
                    "imageUrl": st.column_config.LinkColumn("File", display_text="Open file"),
                },
            )
            routed = pathdb[pathdb["imageUrl"].fillna("").astype(str).str.strip() != ""]
            if st.button("Add routed PathDB files", disabled=routed.empty, key=f"draft_add_pathdb_{patient['patient_key']}"):
                finish_cart_add(
                    add_cart_items(
                        [
                            cart_item(
                                "pathdb",
                                row.get("imageUrl"),
                                short_title=short_title,
                                subject_id=subject_id,
                                label=f"PathDB · {row.get('slide_id', '')}",
                                source="PathDB",
                                access_level=access,
                            )
                            for _, row in routed.iterrows()
                        ]
                    )
                )

    with source_tabs[3]:
        if controlled.empty:
            st.info("No controlled-access file metadata are linked to this patient.")
        else:
            st.markdown(
                f'<div class="access-callout"><strong>Controlled metadata only.</strong> Authorization is required before retrieval. Review the <a href="{POLICY_URL}">TCIA policy</a>.</div>',
                unsafe_allow_html=True,
            )
            columns = [column for column in ("route_system", "modality", "study_date", "series_description", "file_name", "drs_uri") if column in controlled]
            st.dataframe(controlled[columns], hide_index=True, width="stretch")
            routed = controlled[controlled["drs_uri"].fillna("").astype(str).str.strip() != ""]
            if st.button("Add authorized DRS routes", disabled=routed.empty, key=f"draft_add_drs_{patient['patient_key']}"):
                finish_cart_add(
                    add_cart_items(
                        [
                            cart_item(
                                "drs",
                                row.get("drs_uri"),
                                short_title=short_title,
                                subject_id=subject_id,
                                label=f"Controlled · {row.get('file_name', '')}",
                                source=str(row.get("route_system", "controlled")),
                                access_level="controlled",
                            )
                            for _, row in routed.iterrows()
                        ]
                    )
                )


def render_patient_detail(
    paths: DataPaths,
    catalog: pd.DataFrame,
    patient: pd.Series,
    members: pd.DataFrame,
) -> None:
    subject_id = str(patient["subject_id"])
    st.markdown("<div class='section-label'>Selected patient</div>", unsafe_allow_html=True)
    st.subheader(subject_id)
    st.markdown(f'<div class="identity-line">{html.escape(subject_id)}</div>', unsafe_allow_html=True)
    st.caption(
        f"{safe_int(patient.get('dataset_count')) or 1} verified dataset "
        f"membership{'s' if safe_int(patient.get('dataset_count')) != 1 else ''}"
    )

    access = str(patient.get("resolved_access_level", "unknown"))
    if access in {"controlled", "mixed"}:
        st.markdown(
            f'<div class="access-callout"><strong>{html.escape(access_label(access))}.</strong> Metadata remain visible; controlled files require authorization and have no public viewer route.</div>',
            unsafe_allow_html=True,
        )

    summary_tab, datasets_tab, provenance_tab, imaging_tab = st.tabs(
        ["Summary", "Datasets", "Clinical provenance", "Imaging & files"]
    )
    with summary_tab:
        render_patient_summary(patient)
    with datasets_tab:
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
            "Collection and Analysis Result records remain separate source "
            "memberships beneath this grouped patient row."
        )
    with provenance_tab:
        fact_frames: list[pd.DataFrame] = []
        seen_queries: set[tuple[str, tuple[str, ...]]] = set()
        for _, member in members.iterrows():
            short_title = str(member["short_title"])
            clinical_subject = str(
                member.get("clinical_subject_id")
                if pd.notna(member.get("clinical_subject_id"))
                else member.get("subject_id", subject_id)
            )
            clinical_ids = split_tokens(member.get("clinical_subject_ids")) or [
                clinical_subject
            ]
            query_key = (short_title, tuple(clinical_ids))
            if query_key in seen_queries:
                continue
            seen_queries.add(query_key)
            facts = load_patient_clinical_facts(
                paths.clinical_db,
                short_title,
                clinical_subject,
                subject_ids=clinical_ids,
            )
            if not facts.empty:
                facts.insert(0, "dataset_context", short_title)
                fact_frames.append(facts)
        if not fact_frames:
            st.info("No patient-level clinical provenance rows are available.")
        else:
            facts = pd.concat(fact_frames, ignore_index=True, sort=False)
            columns = [
                column
                for column in (
                    "dataset_context",
                    "concept",
                    "value_text",
                    "value_number",
                    "unit",
                    "source_kind",
                    "source_priority",
                    "evidence_scope",
                    "is_inferred",
                    "source_url",
                )
                if column in facts
            ]
            st.dataframe(
                facts[columns],
                hide_index=True,
                width="stretch",
                column_config={"source_url": st.column_config.LinkColumn("Source", display_text="Open source")},
            )
            st.caption("Resolved values use source priority; disagreements remain visible here.")
    with imaging_tab:
        analysis_collection_ids = {
            str(value).strip().casefold()
            for value in members.loc[
                members["dataset_type"] == "Analysis Result", "idc_collection_id"
            ].dropna()
            if str(value).strip()
        }
        ordered_members = members.assign(
            _type_rank=members["dataset_type"].map(
                lambda value: 0 if value == "Collection" else 1
            )
        ).sort_values(["_type_rank", "short_title"], kind="stable")
        for position, (_, member) in enumerate(ordered_members.iterrows()):
            dataset_type = str(member.get("dataset_type", "Dataset"))
            short_title = str(member["short_title"])
            collection_id = str(member.get("idc_collection_id", "")).strip().casefold()
            direct_only = (
                dataset_type == "Collection"
                and collection_id in analysis_collection_ids
            )
            with st.expander(
                f"{dataset_type} · {short_title}", expanded=position == 0
            ):
                if direct_only:
                    st.caption(
                        "Showing direct Collection series here; derived series are "
                        "listed under their Analysis Result to prevent double-counting."
                    )
                render_imaging(
                    paths,
                    catalog,
                    member,
                    direct_collection_only=direct_only,
                )


def main() -> None:
    paths = resolve_data_paths(APP_DIR)
    signatures = paths.signatures()
    cache_key = (PATIENT_INDEX_SCHEMA_VERSION, signatures)
    if not paths.snapshot_db.exists():
        st.error(f"TCIA snapshot not found at {paths.snapshot_db}.")
        st.stop()

    try:
        patients, membership_rows = cached_patient_views(paths, cache_key)
        catalog = cached_catalog(paths, signatures)
    except Exception as exc:
        st.exception(exc)
        st.stop()

    st.session_state.setdefault("cart_items", [])
    render_brand_and_cart()

    st.markdown("<div class='section-label'>Cohort controls</div>", unsafe_allow_html=True)
    f1, f2, f3, f4 = st.columns([1.35, 1, .8, 1])
    search = f1.text_input("Search", placeholder="Dataset or patient ID", key="draft_search").strip()
    datasets = f2.multiselect(
        "Dataset",
        option_values(membership_rows, "short_title"),
        key="draft_datasets",
    )
    access = f3.multiselect("Access", option_values(patients, "resolved_access_level"), format_func=access_label, key="draft_access")
    imaging = f4.multiselect("Available imaging", ["IDC DICOM", "NIfTI", "PathDB", "Controlled-access file metadata"], key="draft_imaging")

    working = patients.copy()
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
    if imaging:
        source_columns = {
            "IDC DICOM": "has_public_dicom",
            "NIfTI": "has_nifti",
            "PathDB": "has_pathdb",
            "Controlled-access file metadata": "has_controlled_metadata",
        }
        mask = pd.Series(False, index=working.index)
        for source in imaging:
            mask |= working[source_columns[source]].fillna(False)
        working = working[mask]

    with st.expander("Advanced clinical and imaging filters", expanded=False):
        a1, a2, a3 = st.columns(3)
        modalities = a1.multiselect("Modality", token_options(working, "modalities"), key="draft_modalities")
        working = apply_token_filter(working, "modalities", modalities)
        body_parts = a2.multiselect("Body part", token_options(working, "body_parts"), key="draft_body_parts")
        working = apply_token_filter(working, "body_parts", body_parts)
        conflicts = a3.checkbox("Clinical conflicts only", key="draft_conflicts")
        if conflicts:
            working = working[pd.to_numeric(working["conflict_count"], errors="coerce").fillna(0) > 0]

        c1, c2, c3, c4 = st.columns(4)
        clinical_filters = [
            (c1, "Primary diagnosis", "primary_diagnosis", "draft_diagnosis"),
            (c2, "Primary site", "primary_site", "draft_site"),
            (c3, "Sex at birth", "sex_at_birth", "draft_sex"),
            (c4, "Vital status", "vital_status", "draft_vital"),
        ]
        clinical_values: dict[str, list[str]] = {}
        for container, label, column, key in clinical_filters:
            selected = container.multiselect(label, option_values(working, column), key=key)
            clinical_values[column] = selected
            if selected:
                working = working[working[column].isin(selected)]

    reset_col, count_col = st.columns([.18, .82], vertical_alignment="center")
    reset_col.button("Clear filters", on_click=clear_filters, width="stretch")
    count_col.markdown(
        f"**{len(working):,} matching patients** across "
        f"**{dataset_membership_count(working) if not working.empty else 0:,} datasets**"
    )

    render_filter_chips(
        [
            ("Search", search),
            ("Dataset", datasets),
            ("Access", [access_label(value) for value in access]),
            ("Imaging", imaging),
            ("Modality", modalities),
            ("Body part", body_parts),
            ("Diagnosis", clinical_values.get("primary_diagnosis", [])),
            ("Site", clinical_values.get("primary_site", [])),
            ("Sex", clinical_values.get("sex_at_birth", [])),
            ("Vital status", clinical_values.get("vital_status", [])),
            ("Clinical conflicts", conflicts),
        ]
    )

    visible_group_keys = set(working["patient_group_key"].astype(str))
    visible_memberships = membership_rows[
        membership_rows["patient_group_key"].astype(str).isin(visible_group_keys)
    ].copy()

    render_filtered_cohort_export(
        paths,
        catalog,
        working,
        visible_memberships,
        datasets,
        imaging,
        modalities,
        body_parts,
    )

    results_col, detail_col = st.columns([1.45, 1], gap="large")
    with results_col:
        st.markdown("<div class='section-label'>Patient results</div>", unsafe_allow_html=True)
        if working.empty:
            st.info("No patients match these filters. Remove a filter to broaden the cohort.")
        else:
            display_columns = [
                column
                for column in (
                    "dataset_memberships",
                    "subject_id",
                    "resolved_access_level",
                    "primary_diagnosis",
                    "primary_site",
                    "modalities",
                    "available_imaging",
                    "conflict_count",
                )
                if column in working
            ]
            page_size = min(50, len(working))
            result = st.dataframe(
                working[display_columns].head(page_size),
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
                    "primary_diagnosis": st.column_config.TextColumn("Diagnosis"),
                    "primary_site": st.column_config.TextColumn("Site"),
                    "conflict_count": st.column_config.NumberColumn("Conflicts"),
                },
            )
            st.caption(f"Showing the first {page_size:,} matches. Select one row to inspect it alongside the cohort.")
            if result.selection.rows:
                selected_index = working.index[result.selection.rows[0]]
                st.session_state.selected_patient_key = working.loc[selected_index, "patient_key"]

    with detail_col:
        selected_key = st.session_state.get("selected_patient_key")
        selected = patients[patients["patient_key"] == selected_key]
        visible_keys = set(working["patient_key"].tolist()) if not working.empty else set()
        if selected.empty or selected_key not in visible_keys:
            if selected_key not in visible_keys:
                st.session_state.pop("selected_patient_key", None)
            st.markdown(
                '<div class="empty-detail"><strong>Select a patient</strong><br>Clinical provenance, imaging time points, viewers, and retrieval routes will appear here.</div>',
                unsafe_allow_html=True,
            )
        else:
            selected_members = membership_rows[
                membership_rows["patient_group_key"] == selected_key
            ].copy()
            render_patient_detail(
                paths,
                catalog,
                selected.iloc[0],
                selected_members,
            )


if __name__ == "__main__":
    main()
