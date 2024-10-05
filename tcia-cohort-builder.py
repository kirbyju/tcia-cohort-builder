import streamlit as st
import pandas as pd
import plotly.express as px
import requests
from io import BytesIO

# GitHub raw content URL for your spreadsheet (replace with your actual URL)
GITHUB_RAW_URL = "https://raw.githubusercontent.com/kirbyju/tcia-cohort-builder/main/clinical-data.xlsx"

@st.cache_data
def load_data():
    response = requests.get(GITHUB_RAW_URL)
    content = BytesIO(response.content)
    df = pd.read_excel(content)

    # Convert age columns to numeric, coercing errors to NaN
    age_columns = ['Age at Diagnosis', 'Age at Enrollment', 'Age at Surgery', 'Age at Earliest Imaging (NBIA)']
    for col in age_columns:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    return df

st.title('The Cancer Imaging Archive - Clinical Data Visualization')

# Load the data
try:
    df = load_data()
    st.success("Data loaded successfully!")
except Exception as e:
    st.error(f"Error loading data: {e}")
    st.stop()

# Display basic info about the dataset
st.write(f"Total records: {len(df)}")
st.write(f"Columns: {', '.join(df.columns)}")

# Create a text input for searching
search_term = st.text_input("Enter a search term:")

# Filter the dataframe based on the search term
if search_term:
    filtered_df = df[df.apply(lambda row: row.astype(str).str.contains(search_term, case=False).any(), axis=1)]
    st.write(f"Found {len(filtered_df)} matching records:")
    st.dataframe(filtered_df)

# Visualizations
st.subheader("Data Visualizations")

# 1. Bar chart of Primary Diagnosis
st.subheader("Distribution of Primary Diagnoses")
diagnosis_counts = df['Primary Diagnosis'].value_counts()
fig_diagnosis = px.bar(x=diagnosis_counts.index, y=diagnosis_counts.values)
fig_diagnosis.update_layout(xaxis_title="Primary Diagnosis", yaxis_title="Count")
st.plotly_chart(fig_diagnosis)

# 2. Pie chart of Sex at Birth
st.subheader("Distribution of Sex at Birth")
sex_counts = df['Sex at Birth'].value_counts()
fig_sex = px.pie(values=sex_counts.values, names=sex_counts.index)
st.plotly_chart(fig_sex)

# 3. Box plot of Age at Diagnosis by Primary Site
st.subheader("Age at Diagnosis by Primary Site")
fig_age = px.box(df, x="Primary Site", y="Age at Diagnosis")
st.plotly_chart(fig_age)

# 4. Stacked bar chart of Race and Ethnicity
st.subheader("Distribution of Race and Ethnicity")
race_ethnicity = df.groupby(['Race', 'Ethnicity']).size().reset_index(name='Count')
fig_race_ethnicity = px.bar(race_ethnicity, x="Race", y="Count", color="Ethnicity", barmode="stack")
st.plotly_chart(fig_race_ethnicity)

# 5. Scatter plot of Age at Diagnosis vs Age at Surgery
st.subheader("Age at Diagnosis vs Age at Surgery")
fig_age_comparison = px.scatter(df, x="Age at Diagnosis", y="Age at Surgery",
                                hover_data=['Case ID', 'Primary Diagnosis'])
fig_age_comparison.update_layout(xaxis_title="Age at Diagnosis", yaxis_title="Age at Surgery")
st.plotly_chart(fig_age_comparison)

# Option to download the full dataset
if st.button("Download Full Dataset"):
    csv = df.to_csv(index=False)
    st.download_button(
        label="Download data as CSV",
        data=csv,
        file_name="cancer_imaging_data.csv",
        mime="text/csv",
    )
