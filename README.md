# TCIA Cohort Builder

The patient-centric application is now the primary entry point at
`tcia-cohort-builder.py`. The identical `tcia-cohort-builder-v2.py` file is
retained temporarily as a compatibility/reference entry point.

## Patient-centric cohort builder

`tcia-cohort-builder.py` provides one-row-per-patient filtering and drill-down
across:

- the TCIA WordPress/Collection Manager provenance and access snapshot;
- patient-level clinical metadata with source priority, inference flags, and
  conflict preservation;
- public DICOM series exported from IDC/idc-index;
- IDC-derived series represented under their TCIA Analysis Result as well as
  their source Collection;
- public NIfTI file metadata;
- PathDB slide metadata and pathology Aspera package summaries; and
- public metadata for controlled-access files.

The default setup expects `tcia-query-skill` to be a sibling of this
repository. Prepare its metadata caches before starting the app:

```bash
cd ../tcia-query-skill
python scripts/tcia_snapshot.py ensure
python scripts/tcia_clinical_metadata.py ensure
python scripts/tcia_nifti_metadata.py ensure
python scripts/tcia_pathology_metadata.py ensure
python scripts/tcia_controlled_access_metadata.py ensure

cd ../tcia-cohort-builder
streamlit run tcia-cohort-builder.py
```

Set `TCIA_QUERY_SKILL_ROOT` when the skill checkout lives elsewhere. Individual
paths can be overridden with `TCIA_SNAPSHOT_DB`,
`TCIA_CLINICAL_METADATA_DB`, `TCIA_NIFTI_METADATA_DB`,
`TCIA_PATHOLOGY_METADATA_DB`, `TCIA_CONTROLLED_ACCESS_METADATA_DB`, and
`TCIA_IDC_METADATA_PARQUET`.

The v2 shopping cart creates TCIA Data Retriever-compatible CSV files with one
route column per manifest: `SeriesInstanceUID` for public DICOM, `imageUrl` for
public PathDB files, or `drs_uri` for controlled files. A mixed cart downloads
as a ZIP containing separate CSV manifests.

The patient count is intentionally not the raw row count of
`clinical_subjects`. V2 collapses verified dataset-specific identifier aliases
to one patient (including scan-level CBIS-DDSM IDs), and excludes only NLST
clinical-only subjects because those records extend beyond TCIA's published
imaging cohort. Non-NLST records that still lack a patient-level imaging link
remain visible with `Needs artifact linkage review` so artifact extraction and
crosswalk gaps are auditable.

`fetch_data.py` exports the complete current IDC series index into
`idc_metadata.parquet`. The v2 application then intersects that cache with the
visible TCIA snapshot. This is intentional: filtering the IDC export through
the legacy clinical workbook omits imaging-only collections such as 4D-Lung
and C4KC-KiTS.
