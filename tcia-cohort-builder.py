import streamlit as st
import pandas as pd
import plotly.express as px

# for radiology downloads
from tcia_utils import nbia

# for pathology downloads
import requests
import os
import time
from io import BytesIO
import xlsxwriter

# for debug function
from io import StringIO

# debug function
def debug_dataframe_info(df):
    st.write("### DataFrame Debug Information")
    st.write(f"Total records: {len(df)}")
    st.write("Available columns:", df.columns.tolist())
    st.write("DataFrame head:", df.head())
    st.write("DataFrame info:")
    buffer = StringIO()
    df.info(buf=buffer)
    st.text(buffer.getvalue())

    # Display statistics about age data
    st.subheader("Age Debug stats")
    total_records = len(filtered_df)
    null_ages = filtered_df['Age at Baseline'].isna().sum()
    st.write(f"Total records: {total_records}")
    st.write(f"Records with age data: {total_records - null_ages}")
    st.write(f"Records without age data: {null_ages}")

# Get current theme
is_dark_theme = st.get_option("theme.base") == "dark"

# Custom CSS for markdown-rendered table
dark_mode = """
    .dataframe-table table th {
        padding: 3px 7px;
        border: 1px solid #4a4a4a;
        background-color: #404040 !important;
        font-weight: 600;
        color: #ffffff !important;
    }

    .dataframe-table table td {
        padding: 3px 7px;
        border: 1px solid #4a4a4a;
        color: #ffffff !important;
    }

    .dataframe-table table tr:hover {
        background-color: #666666 !important;
    }

    .dataframe-table a {
        color: #66b3ff !important;
    }
"""

light_mode = """
    .dataframe-table table th {
        padding: 3px 7px;
        border: 1px solid #ddd;
        background-color: #f5f5f5 !important;
        font-weight: 600;
        color: #333;
    }

    .dataframe-table table td {
        padding: 3px 7px;
        border: 1px solid #ddd;
        color: #333;
    }

    .dataframe-table table tr:hover {
        background-color: #f8f9fa;
    }

    .dataframe-table a {
        color: #0068c9;
    }
"""

# Custom CSS to make the table scrollable
st.markdown(f"""
    <style>
    .main .block-container {{
        max-width: 95%;
        padding-top: 1rem;
        padding-right: 1rem;
        padding-left: 1rem;
        padding-bottom: 1rem;
    }}

    section.main > div {{
        padding-left: 2rem;
        padding-right: 2rem;
    }}

    .dataframe-table {{
        display: block;
        overflow-x: auto;
        white-space: nowrap;
        width: 100%;
        font-size: 13px;
        font-family: "Source Sans Pro", sans-serif;
    }}

    .dataframe-table table {{
        width: 100%;
        border-collapse: collapse;
        margin-bottom: 0;
    }}

    {dark_mode if is_dark_theme else light_mode}

    .dataframe-table a {{
        text-decoration: none;
    }}

    .dataframe-table a:hover {{
        text-decoration: underline;
    }}

    .button-container {{
        display: flex;
        align-items: flex-end;
        height: 100%;
    }}

    .stPlotlyChart {{
        width: 100%;
    }}
    </style>
    """, unsafe_allow_html=True)

# Define age unit conversion factors
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

    df['UOM_factor'] = df[uom_column].str.lower().str.strip()
    df['UOM_factor'] = df['UOM_factor'].map(lambda x:
        age_uom_factors.get('Year') if 'year' in str(x) else
        age_uom_factors.get('Month') if 'month' in str(x) else
        age_uom_factors.get('Day') if 'day' in str(x) else
        1.0
    )

    existing_age_columns = [col for col in age_columns if col in df.columns]

    for age_col in existing_age_columns:
        df[f'{age_col}_years'] = df[age_col] * df['UOM_factor']

    converted_cols = [f'{col}_years' for col in existing_age_columns]
    df['Age at Baseline'] = df[converted_cols].min(axis=1)

    df = df.drop(columns=converted_cols + ['UOM_factor'])
    df['Age at Baseline'] = df['Age at Baseline'].round(1)

    return df

# Helper function to create sorted unique values list
def get_unique_sorted_values(df, column):
    try:
        if column not in df.columns:
            st.warning(f"Column '{column}' not found in DataFrame. Available columns: {', '.join(df.columns)}")
            return []
        return sorted(df[column].unique().tolist())
    except Exception as e:
        st.error(f"Error getting unique values for {column}: {str(e)}")
        return []

@st.cache_data
def load_data():
    try:
        # includes nci projects: TCGA, CPTAC, Biobank
        df = pd.read_excel("https://github.com/kirbyju/tcia-cohort-builder/raw/refs/heads/main/crdc-clinical.xlsx")

        for col in df.columns:
            if col not in ['Age at Diagnosis', 'Age at Surgery', 'Age at Enrollment']:
                df[col] = df[col].astype(str)

        df = calculate_age_at_baseline(df)
        return df

    except Exception as e:
        st.error(f"Error loading data: {e}")
        return None


@st.cache_data
def load_pathology_data():
    return pd.read_excel("https://github.com/kirbyju/tcia-cohort-builder/raw/refs/heads/main/pathology_image_metadata.xlsx")

# Helper function to apply filters with zero-based age filtering
def filter_dataframe(df, filters, age_range=None, is_default_age_range=True):
    filtered_df = df.copy()

    # Apply categorical filters
    for column, values in filters.items():
        if values:
            filtered_df = filtered_df[filtered_df[column].isin(values)]

    # Apply age range filter if specified and not at default values
    if age_range and 'Age at Baseline' in filtered_df.columns and not is_default_age_range:
        min_age, max_age = age_range

        # Include null ages if the range includes 0
        if min_age == 0:
            filtered_df = filtered_df[
                (filtered_df['Age at Baseline'].isna()) |
                (filtered_df['Age at Baseline'] <= max_age)
            ]
        else:
            filtered_df = filtered_df[
                (filtered_df['Age at Baseline'] >= min_age) &
                (filtered_df['Age at Baseline'] <= max_age)
            ]

    return filtered_df

def generate_pathology_manifest(filtered_df, pathology_data):
    """
    Generate an Excel file containing pathology image URLs for the filtered cases

    Parameters:
    - filtered_df: DataFrame of filtered clinical data
    - pathology_data: Original pathology data DataFrame

    Returns:
    - Pandas DataFrame with pathology image URLs
    """
    # Ensure the 'Case ID' column exists in both dataframes
    if 'Case ID' not in filtered_df.columns:
        raise ValueError("The 'Case ID' column is missing from the filtered_df dataframe.")
    if 'Case ID' not in pathology_data.columns:
        raise ValueError("The 'Case ID' column is missing from the pathology_data dataframe.")

    # Ensure the required columns are in pathology_data
    required_columns = ['Case ID', 'imageId', 'slideId', 'imageHeight', 'imagedWidth', 'physicalPixelSizeX', 'physicalPixelSizeY', 'imageUrl', 'created', 'changed']
    missing_columns = [col for col in required_columns if col not in pathology_data.columns]
    if missing_columns:
        raise ValueError(f"The following required columns are missing from pathology_data: {missing_columns}")

    # Step 1: Get Case IDs with available Pathology images
    filtered_case_ids = filtered_df[
        filtered_df['Available Images'].str.contains('Pathology', na=False)
    ]['Case ID'].unique()

    # Step 2: Filter pathology data to match filtered cases
    pathology_manifest = pathology_data[
        pathology_data['Case ID'].isin(filtered_case_ids)
    ][required_columns].copy()

    # Step 3: Merge the filtered_df with the required columns from pathology_data
    merged_manifest = filtered_df.merge(
        pathology_manifest,
        on='Case ID',
        how='left'
    )

    return merged_manifest



st.title('The Cancer Imaging Archive - Clinical Data Exploration')

# Load the clinical data
df = load_data()
if df is None:
    st.stop()

# Load the pathology data
pathology_data = load_pathology_data()

# Display debug information at the top
#debug_dataframe_info(df)

# Create filters for each relevant column
st.sidebar.header("Filters")

# Initialize filters dictionary
filters = {}

# Available Images filter
project_names = get_unique_sorted_values(df, 'Available Images')
filters['Available Images'] = st.sidebar.multiselect(
    'Available Images',
    options=project_names,
    default=[],
    help="Select image types"
)

# Project Name filter
project_names = get_unique_sorted_values(df, 'Project Short Name')
filters['Project Short Name'] = st.sidebar.multiselect(
    'Project Short Name',
    options=project_names,
    default=[],
    help="Select one or more projects"
)

# Race filter
races = get_unique_sorted_values(df, 'Race')
filters['Race'] = st.sidebar.multiselect(
    'Race',
    options=races,
    default=[],
    help="Select one or more races"
)

# Ethnicity filter
ethnicities = get_unique_sorted_values(df, 'Ethnicity')
filters['Ethnicity'] = st.sidebar.multiselect(
    'Ethnicity',
    options=ethnicities,
    default=[],
    help="Select one or more ethnicities"
)

# Sex at Birth filter
sexes = get_unique_sorted_values(df, 'Sex at Birth')
filters['Sex at Birth'] = st.sidebar.multiselect(
    'Sex at Birth',
    options=sexes,
    default=[],
    help="Select one or more sex categories"
)

# Primary Diagnosis filter
diagnoses = get_unique_sorted_values(df, 'Primary Diagnosis')
filters['Primary Diagnosis'] = st.sidebar.multiselect(
    'Primary Diagnosis',
    options=diagnoses,
    default=[],
    help="Select one or more diagnoses"
)

# Primary Site filter
sites = get_unique_sorted_values(df, 'Primary Site')
filters['Primary Site'] = st.sidebar.multiselect(
    'Primary Site',
    options=sites,
    default=[],
    help="Select one or more primary sites"
)

# Add Age Range Filter starting from 0
# Get max age, excluding null values
valid_ages = df['Age at Baseline'].dropna()
max_age = float(valid_ages.max())

# Start range from 0 to include null values when 0 is selected
age_range = st.sidebar.slider(
    'Age at Baseline (years)',
    min_value=0.0,
    max_value=max_age,
    value=(0.0, max_age),
    step=0.1,
    help="Select age range for filtering. Set minimum to 0 to include records with no age data."
)

# Check if the age range is at its default values
is_default_age_range = (age_range[0] == 0.0) and (age_range[1] == max_age)

# Add explanation for null age handling
#if age_range[0] == 0:
#    st.sidebar.info("Including records with no age data (minimum age set to 0)")

# Apply all filters including enhanced age filtering
filtered_df = filter_dataframe(df, filters, age_range, is_default_age_range)

# Prepare CSV data for download
csv = filtered_df.to_csv(index=False)

# Function to display paginated data
def display_page(page_number, page_size):
    start_idx = page_number * page_size
    end_idx = start_idx + page_size

    # Get the page data
    page_data = filtered_df[start_idx:end_idx].copy()

    # List of columns to hide in display
    columns_to_hide = ['Age UOM', 'Age at Diagnosis', 'Age at Enrollment']

    # Create display version with hidden columns removed
    display_data = page_data.drop(columns=columns_to_hide)

    # Reorder columns to put Available Images third
    all_columns = display_data.columns.tolist()
    all_columns.remove('Available Images')
    reordered_columns = ['Project Short Name', 'Case ID', 'Available Images'] + [col for col in all_columns if col not in ['Project Short Name', 'Case ID']]
    display_data = display_data[reordered_columns]

    # Function to create the clickable links for viewing images prior to download via NBIA/caMicroscope (latter not yet added)
    def create_linked_images(row):
        available = row['Available Images']
        case_id = row['Case ID']
        url = f"https://nbia.cancerimagingarchive.net/nbia-search/?PatientCriteria={case_id}"

        # reminder of URL structure for camicroscope viewer
        #pathology_url = f"https://pathdb.cancerimagingarchive.net/caMicroscope/apps/mini/viewer.html?mode=pathdb&slideid=211646"

        if available == 'Radiology; Pathology':
            return f'<a href="{url}" target="_blank">Radiology</a> / Pathology'
        elif available == 'Radiology':
            return f'<a href="{url}" target="_blank">Radiology</a>'
        return available

    # Create a display version of Available Images column
    display_data['Available Images'] = display_data.apply(create_linked_images, axis=1)

    # Convert to HTML and display
    st.markdown(f'<div class="dataframe-table">{display_data.to_html(index=True, escape=False)}</div>',
               unsafe_allow_html=True)

st.markdown("Use the filters on the left to select your cohort. Then, export a CSV of the table or generate a TCIA manifest file to download the radiology data.")
st.markdown("Images may also be viewed for specific subjects before downloading by clicking the links in the Available Images columns.")

# Define a single row for page size and pagination controls
with st.container():
    col1, col2, col3 = st.columns([.75, 3, 1])  # Adjust column widths as needed

    # Page size
    with col1:
        page_size = st.number_input('Page Size', min_value=1, max_value=100, value=10)

    # Manifest Generation/Download button
    with col2:
        # empty placeholder -- add intro instructions here later?
        st.write("")

    # Page Number control
    with col3:
        # Calculate total number of pages
        max_page = max(0, (len(filtered_df) - 1) // page_size)

        # Initialize page number in session state for navigation
        if 'page_number' not in st.session_state:
            st.session_state.page_number = 0

        # Page navigation controls with compact buttons
        nav_col1, nav_col2, nav_col3 = st.columns([0.5, 1, 0.5])

        # Compact '<' button to go to the previous page
        with nav_col1:
            # add empty lines for vertical alignment with results/page widget
            st.write("")
            st.write("")
            if st.button("‹") and st.session_state.page_number > 0:
                st.session_state.page_number -= 1

        with nav_col2:
            # add empty lines for vertical alignment with results/page widget
            st.write("")
            st.write("")
            # display info about what page you're on
            st.markdown(
                f"<div style='text-align: center; padding-top: 10px;'>"
                f"Page {st.session_state.page_number + 1} of {max_page + 1}"
                f"</div>",
                unsafe_allow_html=True
            )

        # Compact '>' button to go to the next page
        with nav_col3:
            # add empty lines for vertical alignment with results/page widget
            st.write("")
            st.write("")
            if st.button("›") and st.session_state.page_number < max_page:
                st.session_state.page_number += 1

# Display the current page with scrollable table
page_number = st.session_state.page_number
display_page(page_number, page_size)

# summarize total records
st.write(f"<div style='text-align: right;'>{len(filtered_df)} total records</div>", unsafe_allow_html=True)

# Define a container for download buttons
with st.container():
    col1, col2, col3 = st.columns([1, 1, 1])  # Adjust column widths as needed

    # Page size
    with col1:
        st.download_button(
            label="Download Clinical CSV",
            data=csv,
            file_name="filtered_cancer_imaging_data.csv",
            mime="text/csv",
            key="csv_download"
        )

    with col2:
        if st.button('Generate Radiology Manifest'):
            try:
                # Fetch patient IDs from the filtered DataFrame
                patientIds = filtered_df['Case ID'].unique().tolist()
                # Retrieve manifest text
                manifest_text = nbia.getSimpleSearchWithModalityAndBodyPartPaged(
                    patients=patientIds,
                    format="manifest_text"
                )
                st.success("Manifest generated successfully! Click 'Download Manifest' to save.")

                # Display download button for manifest if generated
                if isinstance(manifest_text, str):
                    st.download_button(
                        label="Download Radiology Manifest",
                        data=manifest_text,
                        file_name="radiology_manifest.tcia",
                        mime="text/plain",
                        key="manifest_download"
                    )
                else:
                    raise ValueError("No radiology data found.")
            except Exception as e:
                st.error(f"Error generating manifest: {str(e)}")

    with col3:
        # Step 1: Automatically filter for Case IDs with available Pathology images
        filtered_case_ids = filtered_df[
            filtered_df['Available Images'].str.contains('Pathology', na=False)
        ]['Case ID'].unique()

        # Generate Pathology Manifest Button
        if st.button("Generate Pathology Manifest"):
            try:
                # Generate the manifest
                pathology_manifest = generate_pathology_manifest(filtered_df, pathology_data)

                # Download manifest
                excel_buffer = BytesIO()
                with pd.ExcelWriter(excel_buffer, engine='xlsxwriter') as writer:
                    pathology_manifest.to_excel(writer, index=False, sheet_name='Pathology_Images')
                excel_buffer.seek(0)

                st.download_button(
                    label="Download Pathology Manifest (Excel)",
                    data=excel_buffer,
                    file_name="tcia_pathology_manifest.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="pathology_manifest_excel"
                )

                # Provide instructions
                st.info(
                    "Use this manifest with the [TCIA Download Manager](https://github.com/kirbyju/tcia_download_manager) to start downloading."
                )

            except Exception as e:
                st.error(f"Error generating pathology manifest: {str(e)}")


# Visualizations
st.subheader("Data Visualizations")

# Helper function for visualizations
def create_bar_chart(df, column, title):
    counts = df[column].value_counts()
    fig = px.bar(x=counts.index, y=counts.values)
    fig.update_layout(
        xaxis_title=column,
        yaxis_title="Count",
        title=title,
        xaxis_tickangle=-45
    )
    return fig

# Bar chart of Primary Diagnosis
st.plotly_chart(create_bar_chart(filtered_df, 'Primary Diagnosis', "Distribution of Primary Diagnoses"))

# Bar chart of Primary Site
site_fig = create_bar_chart(filtered_df, 'Primary Site', "Distribution of Primary Sites")
st.plotly_chart(site_fig)

# Pie chart of Sex at Birth
sex_counts = filtered_df['Sex at Birth'].value_counts()
fig_sex = px.pie(
    values=sex_counts.values,
    names=sex_counts.index,
    title="Distribution of Sex at Birth"
)
st.plotly_chart(fig_sex)

# Stacked bar chart of Race and Ethnicity
race_ethnicity = filtered_df.groupby(['Race', 'Ethnicity']).size().reset_index(name='Count')
fig_race_ethnicity = px.bar(
    race_ethnicity,
    x="Race",
    y="Count",
    color="Ethnicity",
    barmode="stack",
    title="Distribution of Race and Ethnicity"
)
fig_race_ethnicity.update_layout(xaxis_tickangle=-45)
st.plotly_chart(fig_race_ethnicity)

# Age at Baseline visualization
age_data = filtered_df[filtered_df['Age at Baseline'].notna()]
fig_age = px.histogram(
    age_data,
    x="Age at Baseline",
    nbins=30,
    title=f"Distribution of Age at Baseline (excluding {len(filtered_df) - len(age_data)} records with no age data)"
)
fig_age.update_layout(
    xaxis_title="Age (years)",
    yaxis_title="Count"
)
st.plotly_chart(fig_age)
