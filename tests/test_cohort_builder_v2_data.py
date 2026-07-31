import csv
import io
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from cohort_builder_v2_data import (
    DataPaths,
    aggregate_idc,
    build_manifest_download,
    canonical_imaging_token,
    canonicalize_patient_categories,
    cart_item,
    collapse_clinical_subject_aliases,
    deduplicate_cart,
    exclude_nlst_clinical_only,
    idc_viewer_url,
    load_clinical_subjects,
    load_idc_series,
    load_patient_idc,
    normalize_dataset_key,
    normalize_subject_key,
    subject_join_key,
    subject_join_keys,
)
import pandas as pd


class CohortBuilderV2DataTests(unittest.TestCase):
    def test_dataset_keys_match_idc_style_names(self):
        self.assertEqual(
            normalize_dataset_key("NSCLC-Radiomics"), normalize_dataset_key("nsclc_radiomics")
        )

    def test_subject_keys_reconcile_case_but_preserve_punctuation(self):
        self.assertEqual(
            normalize_subject_key("100_hm10395"),
            normalize_subject_key("100_HM10395"),
        )
        self.assertNotEqual(
            normalize_subject_key("CASE-01"),
            normalize_subject_key("CASE_01"),
        )

    def test_dataset_scoped_subject_join_keys(self):
        self.assertEqual(
            subject_join_key("ISPY2", "100899"),
            subject_join_key("ISPY2", "ISPY2-100899"),
        )
        self.assertEqual(
            subject_join_key("Lung-PET-CT-Dx", "A0001"),
            subject_join_key("Lung-PET-CT-Dx", "Lung_Dx-A0001"),
        )
        self.assertEqual(
            subject_join_key("CBIS-DDSM", "P_00038"),
            subject_join_key("CBIS-DDSM", "Calc-Test_P_00038_LEFT_CC_1"),
        )
        self.assertEqual(
            subject_join_key("CFB-GBM", "1"),
            subject_join_key("CFB-GBM", "001"),
        )
        self.assertEqual(
            subject_join_key("Spinal-Multiple-Myeloma-SEG", "Myel_012_a"),
            subject_join_key("Spinal-Multiple-Myeloma-SEG", "Myel_012"),
        )
        self.assertNotEqual(
            subject_join_key("UNRELATED", "CASE-01"),
            subject_join_key("UNRELATED", "CASE_01"),
        )

    def test_vectorized_subject_join_keys_match_scalar_rules(self):
        frame = pd.DataFrame(
            [
                {"short_title": "CBIS-DDSM", "subject_id": "Calc-Test_P_00038_LEFT_CC_1"},
                {"short_title": "ISPY2", "subject_id": "ISPY2-100899"},
                {"short_title": "CFB-GBM", "subject_id": "001"},
                {"short_title": "Spinal-Multiple-Myeloma-SEG", "subject_id": "Myel_012_b"},
                {"short_title": "UNRELATED", "subject_id": "CASE-01"},
            ]
        )
        expected = frame.apply(
            lambda row: subject_join_key(row["short_title"], row["subject_id"]),
            axis=1,
        )
        self.assertEqual(subject_join_keys(frame).tolist(), expected.tolist())

    def test_idc_aggregation_rolls_scan_ids_up_to_patient(self):
        frame = pd.DataFrame(
            [
                {
                    "short_title": "CBIS-DDSM",
                    "subject_id": "Calc-Test_P_00038_LEFT_CC_1",
                    "collection_id": "cbis_ddsm",
                    "PatientID": "Calc-Test_P_00038_LEFT_CC_1",
                    "SeriesInstanceUID": "1",
                    "StudyInstanceUID": "10",
                    "Modality": "MG",
                    "BodyPartExamined": "BREAST",
                    "StudyDate": "20200101",
                    "series_size_MB": 1.0,
                },
                {
                    "short_title": "CBIS-DDSM",
                    "subject_id": "Calc-Test_P_00038_RIGHT_CC_1",
                    "collection_id": "cbis_ddsm",
                    "PatientID": "Calc-Test_P_00038_RIGHT_CC_1",
                    "SeriesInstanceUID": "2",
                    "StudyInstanceUID": "11",
                    "Modality": "MG",
                    "BodyPartExamined": "BREAST",
                    "StudyDate": "20200202",
                    "series_size_MB": 2.0,
                },
            ]
        )
        result = aggregate_idc(frame)
        self.assertEqual(len(result), 1)
        self.assertEqual(int(result.iloc[0]["dicom_series"]), 2)
        self.assertEqual(float(result.iloc[0]["dicom_size_mb"]), 3.0)

    def test_idc_analysis_results_are_separate_dataset_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            parquet_path = Path(directory) / "idc.parquet"
            pd.DataFrame(
                [
                    {
                        "collection_id": "source_collection",
                        "analysis_result_id": None,
                        "PatientID": "P1",
                        "SeriesInstanceUID": "ORIGINAL",
                        "StudyInstanceUID": "STUDY-1",
                        "Modality": "CT",
                        "BodyPartExamined": "CHEST",
                        "StudyDate": "20200101",
                        "series_size_MB": 1.0,
                    },
                    {
                        "collection_id": "source_collection",
                        "analysis_result_id": "Derived-Result",
                        "PatientID": "P1",
                        "SeriesInstanceUID": "DERIVED",
                        "StudyInstanceUID": "STUDY-1",
                        "Modality": "SEG",
                        "BodyPartExamined": "CHEST",
                        "StudyDate": "20200101",
                        "series_size_MB": 0.1,
                    },
                ]
            ).to_parquet(parquet_path, index=False)
            catalog = pd.DataFrame(
                [
                    {
                        "short_title": "Source-Collection",
                        "dataset_key": normalize_dataset_key("Source-Collection"),
                    },
                    {
                        "short_title": "Derived-Result",
                        "dataset_key": normalize_dataset_key("Derived-Result"),
                    },
                ]
            )

            expanded = load_idc_series(parquet_path, catalog)
            self.assertEqual(
                set(expanded["short_title"]),
                {"Source-Collection", "Derived-Result"},
            )
            self.assertEqual(
                set(expanded.loc[
                    expanded["short_title"] == "Derived-Result",
                    "SeriesInstanceUID",
                ]),
                {"DERIVED"},
            )

            aggregated = aggregate_idc(expanded)
            derived = aggregated[
                aggregated["short_title"] == "Derived-Result"
            ].iloc[0]
            self.assertEqual(derived["idc_analysis_result_id"], "Derived-Result")
            self.assertEqual(int(derived["dicom_series"]), 1)

            paths = DataPaths(
                snapshot_db=Path(directory) / "snapshot.sqlite",
                clinical_db=Path(directory) / "clinical.sqlite",
                nifti_db=Path(directory) / "nifti.sqlite",
                pathology_db=Path(directory) / "pathology.sqlite",
                controlled_db=Path(directory) / "controlled.sqlite",
                idc_parquet=parquet_path,
            )
            detail = load_patient_idc(
                paths,
                catalog,
                "Derived-Result",
                "P1",
                collection_id="source_collection",
                analysis_result_id="Derived-Result",
            )
            self.assertEqual(set(detail["SeriesInstanceUID"]), {"DERIVED"})

    def test_clinical_loader_uses_complete_subject_view(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clinical.sqlite"
            with sqlite3.connect(path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE clinical_subjects (
                      short_title TEXT,
                      subject_id TEXT,
                      has_imaging INTEGER,
                      age_at_diagnosis TEXT
                    );
                    INSERT INTO clinical_subjects VALUES
                      ('TEST', 'P1', 1, '50'),
                      ('TEST', 'P2', 0, '60');
                    CREATE VIEW agent_clinical_subjects AS
                      SELECT * FROM clinical_subjects WHERE has_imaging = 1;
                    CREATE VIEW agent_clinical_all_subjects AS
                      SELECT * FROM clinical_subjects;
                    """
                )
            result = load_clinical_subjects(path)
            self.assertEqual(set(result["subject_id"]), {"P1", "P2"})

    def test_clinical_aliases_collapse_and_preserve_source_ids(self):
        frame = pd.DataFrame(
            [
                {
                    "short_title": "ISPY2",
                    "subject_id": "100899",
                    "has_imaging": 0,
                    "source_count": 1,
                    "conflict_count": 0,
                    "source_kinds": '["idc_clinical"]',
                    "sex_at_birth": "Female",
                },
                {
                    "short_title": "ISPY2",
                    "subject_id": "ISPY2-100899",
                    "has_imaging": 1,
                    "source_count": 2,
                    "conflict_count": 1,
                    "source_kinds": '["dicom","wordpress_dataset_inference"]',
                    "sex_at_birth": None,
                },
            ]
        )
        result = collapse_clinical_subject_aliases(frame)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["subject_id"], "100899")
        self.assertEqual(result.iloc[0]["clinical_subject_ids"], "100899; ISPY2-100899")
        self.assertEqual(int(result.iloc[0]["has_imaging"]), 1)
        self.assertEqual(int(result.iloc[0]["source_count"]), 3)
        self.assertEqual(result.iloc[0]["sex_at_birth"], "Female")

    def test_only_clinical_only_nlst_patients_are_excluded(self):
        frame = pd.DataFrame(
            [
                {"short_title": "NLST", "subject_id": "N1", "has_any_imaging": False},
                {"short_title": "NLST", "subject_id": "N2", "has_any_imaging": True},
                {"short_title": "TEST", "subject_id": "T1", "has_any_imaging": False},
            ]
        )
        result = exclude_nlst_clinical_only(frame)
        self.assertEqual(set(result["subject_id"]), {"N2", "T1"})

    def test_idc_viewer_is_public_independent_of_dataset_access(self):
        self.assertIn(
            "initialSeriesInstanceUID=1.2.3.4",
            idc_viewer_url("1.2.3", "1.2.3.4", "CT", "controlled"),
        )

    def test_radiology_and_sm_viewers(self):
        radiology_url = idc_viewer_url("1.2.3", "1.2.3.4", "CT", "open")
        self.assertEqual(
            radiology_url,
            "https://viewer.imaging.datacommons.cancer.gov/v3/viewer/"
            "?StudyInstanceUIDs=1.2.3&initialSeriesInstanceUID=1.2.3.4",
        )
        self.assertNotIn("SeriesInstanceUIDs=", radiology_url)
        self.assertIn(
            "/slim/studies/1.2.3/series/1.2.3.4",
            idc_viewer_url("1.2.3", "1.2.3.4", "SM", "open"),
        )

    def test_cart_deduplicates_by_route_and_value(self):
        item = cart_item(
            "dicom",
            "1.2.3",
            short_title="TEST",
            subject_id="P1",
            label="CT",
            source="IDC",
            access_level="open",
        )
        self.assertEqual(len(deduplicate_cart([item, item])), 1)

    def test_filter_categories_and_pathdb_modality_are_canonical(self):
        frame = pd.DataFrame(
            {
                "sex_at_birth": ["F", "female", "Female", "M", "male"],
                "primary_site": ["breast", "Breast", "Breast", "lung", "Lung"],
            }
        )
        normalized = canonicalize_patient_categories(frame)
        self.assertEqual(
            normalized["sex_at_birth"].tolist(),
            ["Female", "Female", "Female", "Male", "Male"],
        )
        self.assertEqual(set(normalized["primary_site"]), {"Breast", "Lung"})
        self.assertEqual(
            canonical_imaging_token("Whole slide image"), "Whole Slide Image"
        )

    def test_single_route_is_one_column_csv(self):
        item = cart_item(
            "dicom",
            "1.2.3",
            short_title="TEST",
            subject_id="P1",
            label="CT",
            source="IDC",
            access_level="open",
        )
        payload, filename, mime, counts = build_manifest_download([item])
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8"))))
        self.assertEqual(rows, [["SeriesInstanceUID"], ["1.2.3"]])
        self.assertEqual(filename, "tcia_dicom_series.csv")
        self.assertEqual(mime, "text/csv")
        self.assertEqual(counts, {"SeriesInstanceUID": 1})

    def test_mixed_routes_are_separate_files(self):
        items = [
            cart_item(
                "dicom",
                "1.2.3",
                short_title="TEST",
                subject_id="P1",
                label="CT",
                source="IDC",
                access_level="open",
            ),
            cart_item(
                "pathdb",
                "https://example.org/slide.svs",
                short_title="TEST",
                subject_id="P1",
                label="Slide",
                source="PathDB",
                access_level="open",
            ),
            cart_item(
                "drs",
                "drs://example/file",
                short_title="TEST",
                subject_id="P1",
                label="Controlled",
                source="CTDC",
                access_level="controlled",
            ),
        ]
        payload, filename, mime, counts = build_manifest_download(items)
        self.assertEqual(filename, "tcia_data_retriever_manifests.zip")
        self.assertEqual(mime, "application/zip")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
            self.assertIn("tcia_dicom_series.csv", names)
            self.assertIn("tcia_pathdb_files.csv", names)
            self.assertIn("tcia_controlled_drs.csv", names)
            for name in names - {"README.txt"}:
                header = archive.read(name).decode("utf-8").splitlines()[0]
                self.assertNotIn(",", header)
        self.assertEqual(sum(counts.values()), 3)


if __name__ == "__main__":
    unittest.main()
