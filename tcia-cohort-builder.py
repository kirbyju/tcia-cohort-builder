import streamlit as st
import pandas as pd
import plotly.express as px
from tcia_utils import nbia

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

@st.cache_data
def load_data():
    try:
        df = pd.read_excel("https://github.com/kirbyju/tcia-cohort-builder/raw/refs/heads/main/clinical-data.xlsx")

        for col in df.columns:
            if col not in ['Age at Diagnosis', 'Age at Surgery', 'Age at Enrollment']:
                df[col] = df[col].astype(str)

        df = calculate_age_at_baseline(df)

        # Function to create the clickable link
        def create_nbia_link(case_id):
            url = f"https://nbia.cancerimagingarchive.net/nbia-search/?PatientCriteria={case_id}"
            return f'<a href="{url}" target="_blank">View {case_id} in NBIA</a>'

        # Add a new column for links
        df['NBIA Link'] = df['Case ID'].apply(create_nbia_link)

        return df

    except Exception as e:
        st.error(f"Error loading data: {e}")
        return None

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

st.title('The Cancer Imaging Archive - Clinical Data Exploration')

# Load the data
df = load_data()
if df is None:
    st.stop()

# Display basic info about the dataset
st.write(f"Total records: {len(df)}")
st.write(f"Columns: {', '.join(df.columns)}")

# Create filters for each relevant column
st.sidebar.header("Filters")

# Helper function to create sorted unique values list
def get_unique_sorted_values(column):
    return sorted(df[column].unique().tolist())

# Initialize filters dictionary
filters = {}

# Project Name filter
project_names = get_unique_sorted_values('Project Short Name')
filters['Project Name'] = st.sidebar.multiselect(
    'Project Name',
    options=project_names,
    default=[],
    help="Select one or more projects"
)

# Race filter
races = get_unique_sorted_values('Race')
filters['Race'] = st.sidebar.multiselect(
    'Race',
    options=races,
    default=[],
    help="Select one or more races"
)

# Ethnicity filter
ethnicities = get_unique_sorted_values('Ethnicity')
filters['Ethnicity'] = st.sidebar.multiselect(
    'Ethnicity',
    options=ethnicities,
    default=[],
    help="Select one or more ethnicities"
)

# Sex at Birth filter
sexes = get_unique_sorted_values('Sex at Birth')
filters['Sex at Birth'] = st.sidebar.multiselect(
    'Sex at Birth',
    options=sexes,
    default=[],
    help="Select one or more sex categories"
)

# Primary Diagnosis filter
diagnoses = get_unique_sorted_values('Primary Diagnosis')
filters['Primary Diagnosis'] = st.sidebar.multiselect(
    'Primary Diagnosis',
    options=diagnoses,
    default=[],
    help="Select one or more diagnoses"
)

# Primary Site filter
sites = get_unique_sorted_values('Primary Site')
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
if age_range[0] == 0:
    st.sidebar.info("Including records with no age data (minimum age set to 0)")

# Apply all filters including enhanced age filtering
filtered_df = filter_dataframe(df, filters, age_range, is_default_age_range)

# Display statistics about age data
st.subheader("Debug stats")
total_records = len(filtered_df)
null_ages = filtered_df['Age at Baseline'].isna().sum()
st.write(f"Total records: {total_records}")
st.write(f"Records with age data: {total_records - null_ages}")
st.write(f"Records without age data: {null_ages}")

# Option to download the filtered dataset
st.write(f"Save your filtered dataset and then export a spreadsheet or NBIA manifest to download the related images.")

#if st.button("Save Filtered Dataset"):

col1, col2 = st.columns(2)

with col1:
    csv = filtered_df.to_csv(index=False)
    st.download_button(
        label="Download data as CSV",
        data=csv,
        file_name="filtered_cancer_imaging_data.csv",
        mime="text/csv",
    )

with col2:
    if st.button('Generate Radiology Manifest'):
        try:
            # Fetch patient IDs from the filtered DataFrame
            patientIds = filtered_df['Case ID'].unique().tolist()
            # Attempt to retrieve the manifest text
            manifest_text = nbia.getSimpleSearchWithModalityAndBodyPartPaged(
                patients=patientIds,
                format="manifest_text"
            )
            # Ensure manifest_text is a valid string (indicating success)
            if isinstance(manifest_text, str):
                st.download_button(
                    label="Download Radiology Manifest",
                    data=manifest_text,
                    file_name="radiology_manifest.tcia",
                    mime="text/plain"
                )
            else:
                raise ValueError("No radiology data were found for these subjects.")
        except Exception as e:
            st.error(f"Error generating manifest: {str(e)}")


# Display filtered dataframe without index
st.subheader("Filtered Data")
st.write(f"Showing {len(filtered_df)} records")
st.dataframe(filtered_df.reset_index(drop=True), hide_index=True)
#st.markdown(filtered_df.to_html(escape=False), unsafe_allow_html=True)


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

# 1. Bar chart of Primary Diagnosis
st.plotly_chart(create_bar_chart(filtered_df, 'Primary Diagnosis', "Distribution of Primary Diagnoses"))

# 2. Pie chart of Sex at Birth
sex_counts = filtered_df['Sex at Birth'].value_counts()
fig_sex = px.pie(
    values=sex_counts.values,
    names=sex_counts.index,
    title="Distribution of Sex at Birth"
)
st.plotly_chart(fig_sex)

# 3. Stacked bar chart of Race and Ethnicity
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

# 4. Bar chart of Primary Site
site_fig = create_bar_chart(filtered_df, 'Primary Site', "Distribution of Primary Sites")
st.plotly_chart(site_fig)

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
