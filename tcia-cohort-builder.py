"""TCIA Cohort Builder v2: patient-centric multi-source cohort discovery."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import streamlit as st

from cohort_builder_v2_data import (
    OPEN_ACCESS_LEVELS,
    POLICY_URL,
    DataPaths,
    add_idc_viewer_urls,
    build_manifest_download,
    build_patient_index,
    cart_item,
    deduplicate_cart,
    load_dataset_catalog,
    load_pathology_package_summary,
    load_patient_clinical_facts,
    load_patient_controlled,
    load_patient_idc,
    load_patient_nifti,
    load_patient_pathdb,
    normalize_study_date,
    resolve_data_paths,
    split_tokens,
)


PATIENT_INDEX_SCHEMA_VERSION = 2


st.set_page_config(
    page_title="TCIA Cohort Builder v2",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="expanded",
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

st.markdown(
    """
    <style>
    .main .block-container {max-width: 96%; padding-top: 1.1rem;}
    [data-testid="stMetricValue"] {font-size: 1.45rem;}
    .access-note {
        border-left: 0.35rem solid #d97706;
        padding: 0.55rem 0.8rem;
        background: rgba(217, 119, 6, 0.08);
        margin: 0.4rem 0 0.9rem 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(
    show_spinner=(
        "Building the patient-level index… A cold start processes about one "
        "million IDC series and can take roughly 30 seconds; later reruns use "
        "the cached index."
    )
)
def cached_patient_index(paths: DataPaths, signatures: tuple) -> pd.DataFrame:
    return build_patient_index(paths)


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


def add_cart_items(items: list[dict[str, str] | None]) -> int:
    current = st.session_state.get("cart_items", [])
    st.session_state.cart_items = deduplicate_cart(
        current + [item for item in items if item is not None]
    )
    return len(st.session_state.cart_items) - len(current)


def finish_cart_add(added: int) -> None:
    if added:
        st.session_state.cart_notice = f"Added {added} new item(s) to the cart."
    else:
        st.session_state.cart_notice = "Those items were already in the cart."
    # The sidebar was rendered before the detail-tab button was handled. Start
    # a new run so it immediately reflects the updated session state.
    st.rerun()


def safe_int(value: object) -> int:
    try:
        if pd.isna(value):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def render_cart() -> None:
    st.sidebar.divider()
    st.sidebar.subheader("Shopping cart")
    items = st.session_state.get("cart_items", [])
    notice = st.session_state.pop("cart_notice", None)
    if notice:
        st.sidebar.success(notice)
    st.sidebar.caption(
        "The export separates DICOM UIDs, PathDB URLs, and controlled DRS URIs "
        "so each Data Retriever CSV has one unambiguous route."
    )
    if not items:
        st.sidebar.info("No scans or files have been added.")
        return

    cart_frame = pd.DataFrame(items)
    route_counts = cart_frame["manifest_header"].value_counts()
    st.sidebar.write(
        " · ".join(f"{header}: {count}" for header, count in route_counts.items())
    )
    remove_ids = st.sidebar.multiselect(
        "Remove items",
        options=cart_frame["item_id"].tolist(),
        format_func=lambda item_id: cart_frame.loc[
            cart_frame["item_id"] == item_id, "label"
        ].iloc[0],
    )
    remove_col, clear_col = st.sidebar.columns(2)
    if remove_col.button("Remove", disabled=not remove_ids, use_container_width=True):
        st.session_state.cart_items = [
            item for item in items if item["item_id"] not in set(remove_ids)
        ]
        st.rerun()
    if clear_col.button("Clear", use_container_width=True):
        st.session_state.cart_items = []
        st.rerun()

    payload, filename, mime, counts = build_manifest_download(items)
    st.sidebar.download_button(
        "Download Data Retriever manifest",
        data=payload,
        file_name=filename,
        mime=mime,
        use_container_width=True,
        type="primary",
    )
    if "drs_uri" in counts:
        st.sidebar.warning(
            "Controlled DRS files require authorization and TCIA Data Retriever "
            "API-key configuration."
        )


def render_patient_summary(patient: pd.Series) -> None:
    metrics = st.columns(5)
    metrics[0].metric("DICOM series", safe_int(patient.get("dicom_series", 0)))
    metrics[1].metric("NIfTI files", safe_int(patient.get("nifti_files", 0)))
    metrics[2].metric("PathDB slides", safe_int(patient.get("pathdb_slides", 0)))
    metrics[3].metric("Controlled files", safe_int(patient.get("controlled_files", 0)))
    metrics[4].metric("Clinical conflicts", safe_int(patient.get("conflict_count", 0)))

    summary_columns = [
        "short_title",
        "subject_id",
        "resolved_access_level",
        "sex_at_birth",
        "race",
        "ethnicity",
        "age_at_baseline",
        "primary_diagnosis",
        "primary_site",
        "stage",
        "grade",
        "vital_status",
        "response",
        "screening_result",
        "source_kinds",
        "available_imaging",
        "modalities",
        "body_parts",
    ]
    summary = {
        column: patient.get(column)
        for column in summary_columns
        if column in patient.index and pd.notna(patient.get(column))
    }
    st.json(summary)

    if safe_int(patient.get("primary_diagnosis_is_inferred", 0)):
        st.caption(
            "Primary diagnosis is a dataset-scope fallback, not a confirmed "
            "patient-level observation."
        )
    if safe_int(patient.get("primary_site_is_inferred", 0)):
        st.caption(
            "Primary site is a dataset-scope fallback, not a confirmed "
            "patient-level observation."
        )


def selectable_cart_table(
    frame: pd.DataFrame,
    display_columns: list[str],
    *,
    key: str,
    button_label: str,
    item_builder,
) -> None:
    if frame.empty:
        return
    shown = frame.reset_index(drop=True).copy()
    shown.insert(0, "Add", False)
    edited = st.data_editor(
        shown[["Add"] + display_columns],
        hide_index=True,
        width="stretch",
        disabled=display_columns,
        key=key,
        column_config={
            "Add": st.column_config.CheckboxColumn(required=True),
            "viewer_url": st.column_config.LinkColumn(
                "Viewer", display_text="Open viewer"
            ),
            "imageUrl": st.column_config.LinkColumn(
                "Image URL", display_text="Open file"
            ),
            "drs_uri": st.column_config.TextColumn("DRS URI"),
        },
    )
    selected = edited.index[edited["Add"]].tolist()
    if st.button(button_label, disabled=not selected, key=f"{key}_button"):
        added = add_cart_items(
            [item_builder(shown.iloc[index]) for index in selected]
        )
        finish_cart_add(added)


def render_dicom(
    frame: pd.DataFrame, patient: pd.Series, access_level: str
) -> None:
    if frame.empty:
        st.info("No IDC DICOM series were found for this patient.")
        return
    viewable = add_idc_viewer_urls(frame, access_level)
    timepoints = sorted(viewable["study_date"].unique(), reverse=True)
    st.caption(f"{len(timepoints)} imaging time point(s), {len(viewable)} series.")
    if access_level == "controlled":
        st.warning(
            "The current TCIA dataset-level download is controlled, but the "
            "series listed below are independently present in IDC's public "
            "index and remain available through IDC."
        )
    elif access_level == "mixed":
        st.warning(
            "The dataset has mixed access. Viewer/cart actions below apply only "
            "to the series present in the IDC-derived public index."
        )
    columns = [
        "study_date",
        "Modality",
        "StudyDescription",
        "SeriesDescription",
        "BodyPartExamined",
        "instanceCount",
        "series_size_MB",
        "SeriesInstanceUID",
        "viewer_url",
    ]
    columns = [column for column in columns if column in viewable]
    st.dataframe(
        viewable[columns],
        hide_index=True,
        width="stretch",
        column_config={
            "viewer_url": st.column_config.LinkColumn(
                "Viewer", display_text="Open viewer"
            )
        },
    )
    if st.button(
        "Add all of this patient's public DICOM series",
        key=f"add_dicom_{patient['patient_key']}",
    ):
        added = add_cart_items(
            [
                cart_item(
                    "dicom",
                    row["SeriesInstanceUID"],
                    short_title=patient["short_title"],
                    subject_id=patient["subject_id"],
                    label=f"{row.get('Modality', 'DICOM')} · {row.get('SeriesDescription', '')}",
                    source="IDC",
                    access_level="open",
                )
                for _, row in viewable.iterrows()
            ]
        )
        finish_cart_add(added)


def render_pathdb(
    frame: pd.DataFrame, patient: pd.Series, access_level: str
) -> None:
    if frame.empty:
        st.info("No PathDB slide rows were found for this patient.")
        return
    shown = frame.copy()
    if access_level == "controlled":
        shown["viewer_url"] = ""
        shown["imageUrl"] = ""
        st.warning("Public PathDB viewer/download routes are suppressed.")
    columns = [
        "slide_id",
        "modality",
        "data_format",
        "cancer_type",
        "cancer_location",
        "magnification",
        "update",
        "viewer_url",
        "imageUrl",
    ]
    selectable_cart_table(
        shown,
        [column for column in columns if column in shown],
        key=f"pathdb_{patient['patient_key']}",
        button_label="Add selected PathDB files",
        item_builder=lambda row: cart_item(
            "pathdb",
            row.get("imageUrl"),
            short_title=patient["short_title"],
            subject_id=patient["subject_id"],
            label=f"PathDB · {row.get('slide_id', '')}",
            source="PathDB",
            access_level=access_level,
        ),
    )


def render_nifti(frame: pd.DataFrame) -> None:
    if frame.empty:
        st.info("No NIfTI file rows were found for this patient.")
        return
    shown = frame.copy()
    shown["timepoint"] = shown["study_date"].map(normalize_study_date)
    columns = [
        "timepoint",
        "modality",
        "body_part_examined",
        "study_description",
        "series_description",
        "file_name",
        "package_path",
        "study_id_source",
        "series_id_source",
        "is_derived_object",
        "quality_flag_json",
    ]
    st.dataframe(
        shown[[column for column in columns if column in shown]],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "The NIfTI SQLite supplies mined file metadata but not a validated "
        "Data Retriever route for these package paths, so these rows are not "
        "added to the manifest cart."
    )


def render_controlled(frame: pd.DataFrame, patient: pd.Series) -> None:
    if frame.empty:
        st.info("No controlled-access file metadata were found for this patient.")
        return
    st.markdown(
        f'<div class="access-note"><strong>Controlled access:</strong> Metadata '
        f'is visible, but files require authorization. Review the '
        f'<a href="{POLICY_URL}">TCIA policy</a>. No public viewer links are '
        "created.</div>",
        unsafe_allow_html=True,
    )
    shown = frame.copy()
    shown["timepoint"] = shown["study_date"].map(normalize_study_date)
    columns = [
        "timepoint",
        "route_system",
        "modality",
        "study_description",
        "series_description",
        "file_name",
        "file_type",
        "file_size_bytes",
        "drs_uri",
    ]
    selectable_cart_table(
        shown[shown["drs_uri"].fillna("").astype(str).str.strip() != ""],
        [column for column in columns if column in shown],
        key=f"controlled_{patient['patient_key']}",
        button_label="Add selected authorized DRS items",
        item_builder=lambda row: cart_item(
            "drs",
            row.get("drs_uri"),
            short_title=patient["short_title"],
            subject_id=patient["subject_id"],
            label=f"Controlled · {row.get('file_name', '')}",
            source=str(row.get("route_system", "controlled")),
            access_level="controlled",
        ),
    )


def main() -> None:
    st.title("TCIA Cohort Builder v2")
    st.caption(
        "Patient-level clinical filtering with public DICOM, NIfTI, PathDB, "
        "pathology package, and controlled-access metadata."
    )

    paths = resolve_data_paths(Path(__file__).resolve().parent)
    signatures = paths.signatures()
    patient_index_cache_key = (PATIENT_INDEX_SCHEMA_VERSION, signatures)
    if not paths.snapshot_db.exists():
        st.error(
            f"TCIA snapshot not found at {paths.snapshot_db}. Set "
            "`TCIA_QUERY_SKILL_ROOT` or `TCIA_SNAPSHOT_DB`, then run "
            "`python scripts/tcia_snapshot.py ensure` in tcia-query-skill."
        )
        st.stop()

    try:
        patients = cached_patient_index(paths, patient_index_cache_key)
        catalog = cached_catalog(paths, signatures)
    except Exception as exc:
        st.exception(exc)
        st.stop()

    if "cart_items" not in st.session_state:
        st.session_state.cart_items = []

    with st.sidebar.expander("Metadata sources", expanded=False):
        st.dataframe(
            pd.DataFrame(paths.status_rows()),
            hide_index=True,
            width="stretch",
            column_config={"Available": st.column_config.CheckboxColumn()},
        )
        if st.button("Clear cached metadata index", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    st.sidebar.header("Patient and imaging filters")
    working = patients.copy()

    search = st.sidebar.text_input(
        "Search dataset or patient", placeholder="e.g. RADCURE or subject ID"
    ).strip()
    if search:
        pattern = search.lower()
        working = working[
            working["short_title"].astype(str).str.lower().str.contains(pattern, regex=False)
            | working["subject_id"].astype(str).str.lower().str.contains(pattern, regex=False)
            | working.get("title", "").astype(str).str.lower().str.contains(pattern, regex=False)
        ]

    datasets = st.sidebar.multiselect(
        "TCIA dataset", options=option_values(working, "short_title")
    )
    if datasets:
        working = working[working["short_title"].isin(datasets)]

    access = st.sidebar.multiselect(
        "Access level", options=option_values(working, "resolved_access_level")
    )
    if access:
        working = working[working["resolved_access_level"].isin(access)]

    image_sources = st.sidebar.multiselect(
        "Available imaging",
        options=[
            "IDC DICOM",
            "NIfTI",
            "PathDB",
            "Controlled-access file metadata",
        ],
    )
    if image_sources:
        source_columns = {
            "IDC DICOM": "has_public_dicom",
            "NIfTI": "has_nifti",
            "PathDB": "has_pathdb",
            "Controlled-access file metadata": "has_controlled_metadata",
        }
        mask = pd.Series(False, index=working.index)
        for source in image_sources:
            mask |= working[source_columns[source]].fillna(False)
        working = working[mask]

    modalities = st.sidebar.multiselect(
        "Modality", options=token_options(working, "modalities")
    )
    working = apply_token_filter(working, "modalities", modalities)
    body_parts = st.sidebar.multiselect(
        "Body part examined", options=token_options(working, "body_parts")
    )
    working = apply_token_filter(working, "body_parts", body_parts)

    with st.sidebar.expander("Clinical characteristics", expanded=True):
        include_inferred = st.checkbox(
            "Include dataset-scope inferred diagnosis/site",
            value=True,
            help=(
                "When disabled, inferred diagnosis and site values are blanked "
                "before filtering. Patients remain available through imaging."
            ),
        )
        if not include_inferred:
            if "primary_diagnosis_is_inferred" in working:
                working.loc[
                    working["primary_diagnosis_is_inferred"].fillna(0).astype(int) == 1,
                    "primary_diagnosis",
                ] = pd.NA
            if "primary_site_is_inferred" in working:
                working.loc[
                    working["primary_site_is_inferred"].fillna(0).astype(int) == 1,
                    "primary_site",
                ] = pd.NA

        clinical_filters = {
            "Sex at birth": "sex_at_birth",
            "Race": "race",
            "Ethnicity": "ethnicity",
            "Primary diagnosis": "primary_diagnosis",
            "Primary site": "primary_site",
            "Stage": "stage",
            "Grade": "grade",
            "Vital status": "vital_status",
            "Response": "response",
            "Screening result": "screening_result",
        }
        for label, column in clinical_filters.items():
            selected = st.multiselect(label, options=option_values(working, column))
            if selected:
                working = working[working[column].isin(selected)]

        ages = pd.to_numeric(working.get("age_at_baseline"), errors="coerce")
        if ages.notna().any():
            age_min = float(max(0, ages.min()))
            age_max = float(ages.max())
            if age_min < age_max:
                selected_age = st.slider(
                    "Age at baseline (years)",
                    min_value=age_min,
                    max_value=age_max,
                    value=(age_min, age_max),
                    step=0.5,
                )
                if selected_age != (age_min, age_max):
                    working = working[
                        pd.to_numeric(
                            working["age_at_baseline"], errors="coerce"
                        ).between(*selected_age)
                    ]
            else:
                st.caption(f"Age at baseline: {age_min:g} years")

        conflict_only = st.checkbox("Only patients with clinical conflicts")
        if conflict_only and "conflict_count" in working:
            working = working[
                pd.to_numeric(working["conflict_count"], errors="coerce").fillna(0) > 0
            ]

    render_cart()

    patient_count = len(working)
    dataset_count = working["short_title"].nunique() if not working.empty else 0
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Matching patients", f"{patient_count:,}")
    m2.metric("Datasets", f"{dataset_count:,}")
    m3.metric(
        "With public DICOM",
        f"{int(working['has_public_dicom'].sum()) if not working.empty else 0:,}",
    )
    m4.metric(
        "With clinical metadata",
        f"{int(working['has_clinical'].sum()) if not working.empty else 0:,}",
    )
    m5.metric(
        "Imaging linkage review",
        f"{int((~working['has_any_imaging']).sum()) if not working.empty else 0:,}",
        help=(
            "Non-NLST patient records retained for audit because the current "
            "artifacts do not yet link them to a patient-level imaging source."
        ),
    )

    st.subheader("1. Select a patient")
    if working.empty:
        st.info("No patients match the current filters.")
        return

    page_size = st.selectbox("Rows per page", [25, 50, 100], index=0)
    total_pages = max(1, (patient_count + page_size - 1) // page_size)
    page = st.number_input("Page", min_value=1, max_value=total_pages, value=1)
    start = (page - 1) * page_size
    page_frame = working.iloc[start : start + page_size]
    display_columns = [
        "short_title",
        "subject_id",
        "resolved_access_level",
        "sex_at_birth",
        "age_at_baseline",
        "primary_diagnosis",
        "primary_site",
        "stage",
        "available_imaging",
        "imaging_linkage_status",
        "modalities",
        "conflict_count",
    ]
    display_columns = [column for column in display_columns if column in page_frame]
    event = st.dataframe(
        page_frame[display_columns],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="patient_table",
    )
    if event.selection.rows:
        selected_index = page_frame.index[event.selection.rows[0]]
        st.session_state.selected_patient_key = working.loc[
            selected_index, "patient_key"
        ]

    selected_key = st.session_state.get("selected_patient_key")
    selected_rows = patients[patients["patient_key"] == selected_key]
    if selected_rows.empty:
        st.caption("Select a row to inspect clinical facts, time points, and scans.")
        return

    patient = selected_rows.iloc[0]
    short_title = str(patient["short_title"])
    subject_id = str(patient["subject_id"])
    clinical_subject_id = str(
        patient.get("clinical_subject_id")
        if pd.notna(patient.get("clinical_subject_id"))
        else subject_id
    )
    clinical_subject_ids = split_tokens(patient.get("clinical_subject_ids"))
    if not clinical_subject_ids:
        clinical_subject_ids = [clinical_subject_id]
    idc_subject_id = str(
        patient.get("idc_subject_id")
        if pd.notna(patient.get("idc_subject_id"))
        else subject_id
    )
    pathdb_subject_id = str(
        patient.get("pathdb_subject_id")
        if pd.notna(patient.get("pathdb_subject_id"))
        else subject_id
    )
    nifti_subject_id = str(
        patient.get("nifti_subject_id")
        if pd.notna(patient.get("nifti_subject_id"))
        else subject_id
    )
    controlled_subject_id = str(
        patient.get("controlled_subject_id")
        if pd.notna(patient.get("controlled_subject_id"))
        else subject_id
    )
    access_level = str(patient.get("resolved_access_level", "unknown"))

    st.divider()
    st.subheader(f"2. Patient details · {short_title} / {subject_id}")
    if pd.notna(patient.get("link")) and str(patient.get("link")).strip():
        st.markdown(f"[Open the TCIA dataset page]({patient['link']})")
    if access_level in {"controlled", "mixed"}:
        st.markdown(
            f'<div class="access-note">This dataset is <strong>{access_level}'
            f'</strong>. Review the <a href="{POLICY_URL}">TCIA NIH Controlled '
            "Data Access Policy</a>. Controlled files have no public viewer "
            "route and are never downloaded by this app.</div>",
            unsafe_allow_html=True,
        )

    summary_tab, facts_tab, scans_tab, package_tab = st.tabs(
        ["Patient summary", "Clinical provenance", "Imaging time points & scans", "Pathology package"]
    )
    with summary_tab:
        render_patient_summary(patient)

    with facts_tab:
        facts = load_patient_clinical_facts(
            paths.clinical_db,
            short_title,
            clinical_subject_id,
            subject_ids=clinical_subject_ids,
        )
        if facts.empty:
            st.info("No patient-level clinical fact rows are available.")
        else:
            st.dataframe(
                facts,
                hide_index=True,
                width="stretch",
                column_config={
                    "source_url": st.column_config.LinkColumn(
                        "Source", display_text="Open source"
                    )
                },
            )
            st.caption(
                "All sourced values are retained. Source priority resolves the "
                "summary row; disagreements remain visible here."
            )

    with scans_tab:
        idc_collection_id = (
            str(patient.get("idc_collection_id"))
            if pd.notna(patient.get("idc_collection_id"))
            else None
        )
        idc_analysis_result_id = (
            str(patient.get("idc_analysis_result_id"))
            if pd.notna(patient.get("idc_analysis_result_id"))
            and str(patient.get("idc_analysis_result_id")).strip()
            else None
        )
        dicom = load_patient_idc(
            paths,
            catalog,
            short_title,
            idc_subject_id,
            collection_id=idc_collection_id,
            analysis_result_id=idc_analysis_result_id,
        )
        pathdb = load_patient_pathdb(
            paths.snapshot_db, short_title, pathdb_subject_id
        )
        nifti = load_patient_nifti(paths.nifti_db, short_title, nifti_subject_id)
        controlled = load_patient_controlled(
            paths.controlled_db, short_title, controlled_subject_id
        )
        source_tabs = st.tabs(
            [
                f"IDC DICOM ({len(dicom):,})",
                f"NIfTI ({len(nifti):,})",
                f"PathDB ({len(pathdb):,})",
                f"Controlled ({len(controlled):,})",
            ]
        )
        with source_tabs[0]:
            render_dicom(dicom, patient, access_level)
        with source_tabs[1]:
            render_nifti(nifti)
        with source_tabs[2]:
            render_pathdb(pathdb, patient, access_level)
        with source_tabs[3]:
            render_controlled(controlled, patient)

    with package_tab:
        package = load_pathology_package_summary(paths.pathology_db, short_title)
        if package.empty:
            st.info("No public pathology Aspera package is scoped to this dataset.")
        else:
            st.dataframe(package, hide_index=True, width="stretch")
            st.caption(
                "Package counts are dataset-level. PathDB rows are used for "
                "patient/slide drill-down; Aspera packages remain the original "
                "submitter-provided copy and are not assumed byte-identical to "
                "PathDB viewer files."
            )


if __name__ == "__main__":
    main()
