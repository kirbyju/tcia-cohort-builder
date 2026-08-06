# TCIA Participant Explorer

The TCIA Participant Explorer is a patient-level Streamlit interface for
finding TCIA data, reviewing source-aware clinical metadata, inspecting imaging
and supporting files, and preparing retrieval manifests.

## Capabilities

- one row per dataset-scoped patient;
- verified Collection and Analysis Result memberships grouped without losing
  their separate provenance;
- basic and advanced cohort filters with a source-aware patient detail panel;
- public IDC DICOM, NIfTI, PathDB, and controlled-metadata inventory;
- viewer routes for publicly viewable imaging; and
- filtered cohort downloads containing patient-level clinical data plus
  route-specific TCIA Data Retriever manifests.

## Requirements

- Python 3.10 or newer;
- the packages pinned in `requirements.txt`;
- the current `idc_metadata.parquet` series index in this repository; and
- TCIA metadata SQLite caches produced by the sibling `tcia-query-skill`
  checkout.

Create an environment and install the application dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Prepare the TCIA metadata caches:

```bash
cd ../tcia-query-skill
python scripts/tcia_snapshot.py ensure
python scripts/tcia_clinical_metadata.py ensure
python scripts/tcia_nifti_metadata.py ensure
python scripts/tcia_pathology_metadata.py ensure
python scripts/tcia_controlled_access_metadata.py ensure
```

Then start the canonical application:

```bash
cd ../tcia-cohort-builder
streamlit run tcia-cohort-builder.py
```

Set `TCIA_QUERY_SKILL_ROOT` if the skill checkout lives elsewhere. Individual
inputs can be overridden with `TCIA_SNAPSHOT_DB`,
`TCIA_CLINICAL_METADATA_DB`, `TCIA_NIFTI_METADATA_DB`,
`TCIA_PATHOLOGY_METADATA_DB`, `TCIA_CONTROLLED_ACCESS_METADATA_DB`, and
`TCIA_IDC_METADATA_PARQUET`.

## Data refresh

`idc_metadata.parquet` is the only application data artifact retained in this
repository. It contains the public IDC series fields needed by the patient
index, imaging drill-down, viewer routing, and manifest export. Refresh it with:

```bash
python fetch_data.py
```

The refresh writes to a temporary Parquet file and replaces the current index
only after the complete IDC export succeeds. The daily GitHub Actions workflow
uses the same command.

## Tests

```bash
python -m unittest discover -s tests -v
```

The patient count is intentionally not the raw clinical row count. Verified
dataset-specific aliases are collapsed. Related Collection and Analysis Result
memberships are grouped when IDC supplies the same collection identity and
exact PatientID, or when WordPress explicitly identifies one source Collection
and that Collection contains the exact PatientID. Unrelated or ambiguous
dataset memberships remain separate.

## Branding

The interface uses TCIA's published logo and color palette as documented at
[cancerimagingarchive.net/branding](https://www.cancerimagingarchive.net/branding/).
