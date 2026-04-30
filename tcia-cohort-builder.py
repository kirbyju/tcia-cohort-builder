import streamlit as st

# Set page to wide layout
st.set_page_config(layout="wide", page_title="TCIA Cohort Builder")

import pandas as pd
import plotly.express as px
from tcia_utils import wordpress
from idc_index import IDCClient
import requests
import os
import time
from io import BytesIO
import xlsxwriter
from io import StringIO

# --- Custom CSS ---
st.markdown("""
    <style>
    .main .block-container {
        max-width: 95%;
        padding-top: 1rem;
    }
    .stDataFrame {
        width: 100%;
    }
    .controlled-access {
        color: #ff4b4b;
        font-weight: bold;
    }
    </style>
    """, unsafe_allow_html=True)

# --- Data Loading and Caching ---

@st.cache_data
def load_clinical_data():
    try:
        df = pd.read_excel("https://github.com/kirbyju/tcia-cohort-builder/raw/refs/heads/main/crdc-clinical.xlsx")
        for col in df.columns:
            if col not in ['Age at Diagnosis', 'Age at Surgery', 'Age at Enrollment']:
                df[col] = df[col].astype(str)
        df = calculate_age_at_baseline(df)
        return df
    except Exception as e:
        st.error(f"Error loading clinical data: {e}")
        return None

@st.cache_data
def load_pathology_data():
    try:
        return pd.read_excel("https://github.com/kirbyju/tcia-cohort-builder/raw/refs/heads/main/pathology_image_metadata.xlsx")
    except Exception as e:
        st.error(f"Error loading pathology data: {e}")
        return None

@st.cache_data
def load_wp_metadata():
    try:
        # Fetching v2 metadata to get license details
        # Using a custom fetch because tcia_utils might not support v2/v=1 yet
        def fetch_v2(endpoint):
            base_url = f"https://cancerimagingarchive.net/api/v2/{endpoint}?per_page=100&v=1"
            all_results = []
            try:
                r = requests.get(base_url, timeout=30)
                r.raise_for_status()
                data = r.json()
                all_results.extend(data.get('results', []))
                total_pages = int(data.get('total_pages', 1))
                if total_pages > 1:
                    for p in range(2, total_pages + 1):
                        r = requests.get(f"{base_url}&page={p}", timeout=30)
                        r.raise_for_status()
                        all_results.extend(r.json().get('results', []))
            except Exception as e:
                st.warning(f"Error fetching {endpoint}: {e}")
            return all_results

        c_v2 = fetch_v2('collections')
        a_v2 = fetch_v2('analysis-results')

        wp_metadata = {}

        def process_v2(results, is_collection=True):
            short_title_key = 'collection_short_title' if is_collection else 'result_short_title'
            doi_key = 'collection_doi' if is_collection else 'result_doi'
            downloads_key = 'collection_downloads' if is_collection else 'result_downloads'

            for item in results:
                short_title = str(item.get(short_title_key, ""))
                if not short_title or short_title == "nan":
                    continue

                # Check for controlled access in downloads license info
                downloads = item.get(downloads_key, [])
                is_controlled = False
                licenses = []
                if isinstance(downloads, list):
                    for d in downloads:
                        if isinstance(d, dict):
                            l_info = d.get('license', {})
                            l_label = str(l_info.get('label', '') if isinstance(l_info, dict) else l_info).lower()
                            licenses.append(l_label)
                            if any(term in l_label for term in ['controlled', 'restricted', 'limited', 'usage agreement', 'dbgap']):
                                is_controlled = True

                # Fallback to old field if no licenses found
                if not licenses:
                    access = str(item.get('collection_page_accessibility', '')).lower()
                    if any(term in access for term in ['controlled', 'restricted', 'limited']):
                        is_controlled = True

                wp_metadata[short_title] = {
                    'is_controlled': is_controlled,
                    'link': item.get('url', ''),
                    'doi': item.get(doi_key, ''),
                    'species': str(item.get('species', '')),
                    'cancer_types': str(item.get('cancer_types', '')),
                    'licenses': "; ".join(licenses)
                }

        process_v2(c_v2, True)
        process_v2(a_v2, False)

        return wp_metadata
    except Exception as e:
        st.warning(f"Error loading WordPress metadata: {e}")
        return {}

@st.cache_data
def load_idc_metadata(project_list):
    try:
        client = IDCClient()
        # Map project names to IDC collection IDs
        idc_collections = [p.lower().replace('-', '_') for p in project_list]

        query = f"""
        SELECT
            collection_id,
            PatientID,
            StudyInstanceUID,
            StudyDate,
            StudyDescription,
            BodyPartExamined,
            SeriesInstanceUID,
            Modality,
            SeriesDescription,
            instanceCount,
            series_size_MB
        FROM index
        WHERE collection_id IN ({','.join([repr(c) for c in idc_collections])})
        """
        df = client.sql_query(query)
        if df is not None and not df.empty:
            # Normalize StudyDate to YYYY-MM-DD
            def normalize_date(d):
                d = str(d)
                if d in ['None', 'nan', '', 'NaN']:
                    return 'Unknown'
                if len(d) == 8 and d.isdigit():
                    return f"{d[:4]}-{d[4:6]}-{d[6:]}"
                return d
            df['StudyDate'] = df['StudyDate'].apply(normalize_date)
        return df
    except Exception as e:
        st.error(f"Error loading IDC metadata: {e}")
        return pd.DataFrame()

# --- Helper Functions ---

age_uom_factors = {
    'Year': 1.0,
    'Month': 1/12,
    'Day': 1/365.25,
}

def calculate_age_at_baseline(df, age_columns=['Age at Diagnosis', 'Age at Surgery', 'Age at Enrollment'], uom_column='Age UOM'):
    df = df.copy()
    for col in age_columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    df['Age at Baseline'] = pd.NA
    if uom_column in df.columns:
        df['UOM_factor'] = df[uom_column].str.lower().str.strip()
        df['UOM_factor'] = df['UOM_factor'].map(lambda x:
            age_uom_factors.get('Year') if 'year' in str(x) else
            age_uom_factors.get('Month') if 'month' in str(x) else
            age_uom_factors.get('Day') if 'day' in str(x) else
            1.0
        )
    else:
        df['UOM_factor'] = 1.0

    existing_age_columns = [col for col in age_columns if col in df.columns]
    for age_col in existing_age_columns:
        df[f'{age_col}_years'] = df[age_col] * df['UOM_factor']

    converted_cols = [f'{col}_years' for col in existing_age_columns]
    if converted_cols:
        df['Age at Baseline'] = df[converted_cols].min(axis=1)
    df = df.drop(columns=[c for c in converted_cols if c in df.columns] + ['UOM_factor'])
    df['Age at Baseline'] = pd.to_numeric(df['Age at Baseline'], errors='coerce').round(1)
    return df

def get_unique_sorted_values(df, column):
    try:
        if column not in df.columns:
            return []
        vals = df[column].dropna().unique().tolist()
        return sorted([str(v) for v in vals])
    except Exception:
        return []

@st.cache_data
def filter_dataframe(df, filters, age_range=None, is_default_age_range=True):
    if df.empty:
        return df
    filtered_df = df.copy()
    for column, values in filters.items():
        if values:
            filtered_df = filtered_df[filtered_df[column].isin(values)]

    if age_range and 'Age at Baseline' in filtered_df.columns and not is_default_age_range:
        min_age, max_age = age_range
        # Filter numeric values
        mask = (filtered_df['Age at Baseline'] >= min_age) & (filtered_df['Age at Baseline'] <= max_age)
        if min_age == 0:
            # Include NaN if min_age is 0
            mask = mask | filtered_df['Age at Baseline'].isna()
        filtered_df = filtered_df[mask]
    return filtered_df

def generate_pathology_manifest(filtered_df, pathology_data):
    if pathology_data is None:
        return pd.DataFrame()
    required_columns = ['Case ID', 'imageId', 'slideId', 'imageHeight', 'imagedWidth', 'physicalPixelSizeX', 'physicalPixelSizeY', 'imageUrl', 'created', 'changed']
    filtered_case_ids = filtered_df['Case ID'].unique()
    pathology_manifest = pathology_data[
        pathology_data['Case ID'].isin(filtered_case_ids)
    ].copy()
    # Only keep requested columns if they exist
    cols_to_keep = [c for c in required_columns if c in pathology_manifest.columns]
    pathology_manifest = pathology_manifest[cols_to_keep]
    merged_manifest = filtered_df[['Project Short Name', 'Case ID']].merge(pathology_manifest, on='Case ID', how='inner')
    return merged_manifest

# --- Page Content ---

st.title('TCIA Cohort Builder')

# Load data
df_clin = load_clinical_data()
if df_clin is None:
    st.stop()

pathology_data = load_pathology_data()
wp_metadata = load_wp_metadata()
# Only load IDC metadata for projects present in clinical data
all_projects = df_clin['Project Short Name'].unique()
idc_data = load_idc_metadata(all_projects)

# Sidebar Filters
st.sidebar.header("Filters")
filters = {}

with st.sidebar.expander("Image and Project Filters"):
    filters['Available Images'] = st.multiselect('Available Images', options=get_unique_sorted_values(df_clin, 'Available Images'))

    # Modality and Body Part filters from IDC data
    if not idc_data.empty:
        # Modality
        all_modalities = get_unique_sorted_values(idc_data, 'Modality')
        if pathology_data is not None:
            all_modalities.append('Pathology')
        selected_modalities = st.multiselect('Modality', options=sorted(list(set(all_modalities))))

        # Body Part
        all_body_parts = get_unique_sorted_values(idc_data, 'BodyPartExamined')
        selected_body_parts = st.multiselect('Body Part Examined', options=all_body_parts)

        if selected_modalities or selected_body_parts:
            # Filter patients based on these attributes
            filtered_idc = idc_data.copy()
            if selected_modalities:
                rad_mods = [m for m in selected_modalities if m != 'Pathology']
                mask = pd.Series(False, index=filtered_idc.index)
                if rad_mods:
                    mask = mask | filtered_idc['Modality'].isin(rad_mods)

                path_patients = set()
                if 'Pathology' in selected_modalities and pathology_data is not None:
                    path_patients = set(pathology_data['Case ID'].unique())

                rad_patients = set(filtered_idc[mask]['PatientID'].unique())
                valid_patients = rad_patients | path_patients
                df_clin = df_clin[df_clin['Case ID'].isin(valid_patients)]
                filtered_idc = filtered_idc[filtered_idc['PatientID'].isin(valid_patients)]

            if selected_body_parts:
                rad_patients = set(filtered_idc[filtered_idc['BodyPartExamined'].isin(selected_body_parts)]['PatientID'].unique())
                df_clin = df_clin[df_clin['Case ID'].isin(rad_patients)]

    # Species filter from WP metadata
    if wp_metadata:
        all_species = sorted(list(set([m['species'] for m in wp_metadata.values() if m.get('species') and m['species'] != 'nan'])))
        selected_species = st.multiselect('Species', options=all_species)
        if selected_species:
            valid_projects = [p for p, m in wp_metadata.items() if m.get('species') in selected_species]
            df_clin = df_clin[df_clin['Project Short Name'].isin(valid_projects)]

    filters['Project Short Name'] = st.multiselect('Project Short Name', options=get_unique_sorted_values(df_clin, 'Project Short Name'))

with st.sidebar.expander("Demographic Filters"):
    filters['Race'] = st.multiselect('Race', options=get_unique_sorted_values(df_clin, 'Race'))
    filters['Ethnicity'] = st.multiselect('Ethnicity', options=get_unique_sorted_values(df_clin, 'Ethnicity'))
    filters['Sex at Birth'] = st.multiselect('Sex at Birth', options=get_unique_sorted_values(df_clin, 'Sex at Birth'))

with st.sidebar.expander("Clinical Filters"):
    filters['Primary Diagnosis'] = st.multiselect('Primary Diagnosis', options=get_unique_sorted_values(df_clin, 'Primary Diagnosis'))
    filters['Primary Site'] = st.multiselect('Primary Site', options=get_unique_sorted_values(df_clin, 'Primary Site'))

# Age Filter
valid_ages = df_clin['Age at Baseline'].dropna()
max_age_val = float(valid_ages.max()) if not valid_ages.empty else 100.0
age_range = st.sidebar.slider('Age at Baseline (years)', 0.0, max_age_val, (0.0, max_age_val), 0.1)
is_default_age_range = (age_range[0] == 0.0) and (age_range[1] == max_age_val)

filtered_df = filter_dataframe(df_clin, filters, age_range, is_default_age_range)

st.write(f"Showing {len(filtered_df)} patients based on filters.")

# --- Hierarchy: Patient Selection ---

st.subheader("1. Select a Patient")

def get_project_display(project):
    meta = wp_metadata.get(project, {})
    if meta.get('is_controlled'):
        return f"⚠️ {project} (Controlled)"
    return project

display_df = filtered_df[['Project Short Name', 'Case ID', 'Available Images', 'Sex at Birth', 'Age at Baseline']].copy()
display_df['Project'] = display_df['Project Short Name'].apply(get_project_display)

# Reorder columns
cols = ['Project', 'Case ID', 'Available Images', 'Sex at Birth', 'Age at Baseline']
display_df = display_df[cols]

# Pagination for the table
page_size = 10
total_pages = max(1, (len(display_df) - 1) // page_size + 1)
page_num = st.number_input("Page", min_value=1, max_value=total_pages, step=1)
start_idx = (page_num - 1) * page_size
end_idx = start_idx + page_size

# State for selected patient
if 'selected_patient' not in st.session_state:
    st.session_state.selected_patient = None

# Using selection mode in st.dataframe (requires Streamlit >= 1.35.0)
event = st.dataframe(
    display_df.iloc[start_idx:end_idx],
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row"
)

if event and len(event.selection.rows) > 0:
    row_idx = event.selection.rows[0]
    st.session_state.selected_patient = display_df.iloc[start_idx + row_idx]['Case ID']
    st.session_state.selected_project = filtered_df.iloc[start_idx + row_idx]['Project Short Name']

# --- Hierarchy: Study & Series Details ---

if st.session_state.selected_patient:
    st.divider()
    patient_id = st.session_state.selected_patient
    project_id = st.session_state.selected_project

    st.subheader(f"2. Imaging Details for Patient: {patient_id}")

    meta = wp_metadata.get(project_id, {})
    if meta.get('is_controlled'):
        st.warning(f"**{project_id}** is a **Controlled Access** dataset. "
                   "Visualization is not available in the browser. "
                   "Please follow the [TCIA Controlled Data Access Policy](https://www.cancerimagingarchive.net/nih-controlled-data-access-policy/) to request access.")

        # Still show metadata summary even for controlled access
        st.write("**Patient Metadata Summary**")
        p_meta = filtered_df[filtered_df['Case ID'] == patient_id].iloc[0]
        st.json(p_meta.to_dict())
    else:
        col_detail1, col_detail2 = st.columns([1, 2])

        with col_detail1:
            st.write("**Studies & Images**")

            # Get radiology data for this patient
            p_idc = idc_data[idc_data['PatientID'] == patient_id] if not idc_data.empty else pd.DataFrame()
            # Get pathology data for this patient
            p_path = pathology_data[pathology_data['Case ID'] == patient_id] if pathology_data is not None else pd.DataFrame()

            # Combine all unique dates
            all_dates = set()
            if not p_idc.empty:
                all_dates.update(p_idc['StudyDate'].unique())

            if not p_path.empty:
                # Try to use 'created' date from pathology metadata if possible
                path_dates = p_path['created'].astype(str).str[:10].unique()
                all_dates.update(path_dates)

            if not all_dates:
                st.info("No radiology or pathology imaging found for this patient.")
            else:
                # Sort dates, keep 'Unknown' and 'Pathology' at the end
                date_list = list(all_dates)
                special = [d for d in date_list if d in ['Unknown', 'Pathology']]
                regular = sorted([d for d in date_list if d not in ['Unknown', 'Pathology']], reverse=True)
                sorted_dates = regular + sorted(special)

                for date in sorted_dates:
                    with st.expander(f"Study Date: {date}"):
                        # Radiology Series
                        rad_series = p_idc[p_idc['StudyDate'] == date] if not p_idc.empty else pd.DataFrame()
                        if not rad_series.empty:
                            st.write("**Radiology (IDC)**")
                            for _, s_row in rad_series.iterrows():
                                ohif_url = f"https://viewer.imaging.datacommons.cancer.gov/v3/viewer/?StudyInstanceUIDs={s_row['StudyInstanceUID']}&SeriesInstanceUIDs={s_row['SeriesInstanceUID']}"
                                st.markdown(f"- **{s_row['Modality']}**: {s_row['SeriesDescription']} ([View in OHIF]({ohif_url}))")

                        # Pathology Images
                        path_imgs = p_path[p_path['created'].astype(str).str[:10] == date] if not p_path.empty else pd.DataFrame()
                        if not path_imgs.empty:
                            st.write("**Pathology (PathDB)**")
                            for _, p_row in path_imgs.iterrows():
                                view_url = f"https://pathdb.cancerimagingarchive.net/caMicroscope/apps/mini/viewer.html?mode=pathdb&slideId={p_row['slideId']}"
                                st.markdown(f"- **{p_row['imageId']}** ([View in caMicroscope]({view_url}))")

        with col_detail2:
            st.write("**Patient Metadata Summary**")
            p_meta = filtered_df[filtered_df['Case ID'] == patient_id].iloc[0]
            st.json(p_meta.to_dict())

# --- Exports ---

st.divider()
st.subheader("3. Export Cohort")
e_col1, e_col2, e_col3 = st.columns(3)

with e_col1:
    st.download_button(
        "Download Clinical CSV",
        filtered_df.to_csv(index=False),
        "cohort_clinical.csv",
        "text/csv"
    )

with e_col2:
    if st.button("Generate Radiology Manifest"):
        # Filtered patients
        # Check if any controlled access projects are in the filtered cohort
        controlled_projects = [p for p in filtered_df['Project Short Name'].unique() if wp_metadata.get(p, {}).get('is_controlled')]
        if controlled_projects:
            st.info(f"Note: Cohort contains controlled access data from: {', '.join(controlled_projects)}. Ensure you have appropriate permissions.")

        ids = filtered_df['Case ID'].unique()
        uids = idc_data[idc_data['PatientID'].isin(ids)]['SeriesInstanceUID'].unique()
        if len(uids) > 0:
            m_df = pd.DataFrame({'SeriesInstanceUID': uids})
            st.download_button(
                "Download Radiology Manifest (CSV)",
                m_df.to_csv(index=False),
                "radiology_manifest.csv",
                "text/csv"
            )
            st.success(f"Generated manifest with {len(uids)} series.")
        else:
            st.error("No radiology data found for current cohort.")

with e_col3:
    if st.button("Generate Pathology Manifest"):
        p_manifest = generate_pathology_manifest(filtered_df, pathology_data)
        if not p_manifest.empty:
            buf = BytesIO()
            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                p_manifest.to_excel(writer, index=False)
            st.download_button(
                "Download Pathology Manifest (Excel)",
                buf.getvalue(),
                "pathology_manifest.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            st.error("No pathology data found for current cohort.")

# --- Visualizations ---
st.divider()
st.subheader("Data Insights")
v_col1, v_col2 = st.columns(2)

with v_col1:
    diag_counts = filtered_df['Primary Diagnosis'].value_counts()
    st.plotly_chart(px.bar(diag_counts, title="Distribution of Diagnoses"), use_container_width=True)

with v_col2:
    site_counts = filtered_df['Primary Site'].value_counts()
    st.plotly_chart(px.pie(values=site_counts.values, names=site_counts.index, title="Distribution of Primary Sites"), use_container_width=True)
