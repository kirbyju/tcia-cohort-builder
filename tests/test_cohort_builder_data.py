import csv
import io
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from cohort_builder_data import (
    DataPaths,
    aggregate_idc,
    build_filtered_cohort_download,
    build_grouped_patient_index,
    build_manifest_download,
    canonical_imaging_token,
    canonicalize_patient_categories,
    cart_item,
    collapse_clinical_subject_aliases,
    collect_filtered_imaging_routes,
    deduplicate_cart,
    exclude_nlst_clinical_only,
    idc_viewer_url,
    load_clinical_subjects,
    load_idc_series,
    load_patient_idc,
    load_patient_nifti_packages,
    normalize_dataset_key,
    normalize_subject_key,
    subject_join_key,
    subject_join_keys,
)
import pandas as pd


class CohortBuilderDataTests(unittest.TestCase):
    def test_patient_nifti_packages_are_subject_scoped_and_public(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "nifti.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    CREATE TABLE radiology_series (
                        short_title TEXT,
                        subject_id TEXT,
                        download_ids TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO radiology_series VALUES (?, ?, ?)",
                    [
                        ("Result-A", "P1", '["10"]'),
                        ("Result-A", "p1", '["10"]'),
                        ("Result-A", "P2", '["20"]'),
                        ("BraTS-PEDs", "BraTS-PED-00001", '["53630"]'),
                    ],
                )
                connection.execute(
                    """
                    CREATE TABLE nifti_downloads (
                        short_title TEXT,
                        download_id TEXT,
                        download_label TEXT,
                        download_title TEXT,
                        download_url TEXT,
                        download_size TEXT,
                        download_size_unit TEXT,
                        access_level TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO nifti_downloads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "Result-A",
                            "10",
                            "P1 package",
                            "",
                            "https://faspex.cancerimagingarchive.net/aspera/faspex/public/package?context=p1",
                            "1",
                            "GB",
                            "open",
                        ),
                        (
                            "Result-A",
                            "20",
                            "P2 package",
                            "",
                            "https://faspex.cancerimagingarchive.net/aspera/faspex/public/package?context=p2",
                            "2",
                            "GB",
                            "open",
                        ),
                        (
                            "Result-A",
                            "10",
                            "Untrusted route",
                            "",
                            "https://example.org/package?id=10",
                            "1",
                            "GB",
                            "open",
                        ),
                        (
                            "BraTS-PEDs",
                            "53630",
                            "Images (Training and Validation)",
                            "",
                            "https://faspex.cancerimagingarchive.net/?context=brats-peds",
                            "32.7",
                            "GB",
                            "open",
                        ),
                    ],
                )

            packages = load_patient_nifti_packages(path, "result-a", "p1")

            self.assertEqual(len(packages), 1)
            self.assertEqual(packages.iloc[0]["download_id"], "10")
            self.assertEqual(packages.iloc[0]["download_label"], "P1 package")

            brats_packages = load_patient_nifti_packages(
                path, "BraTS-PEDs", "BraTS-PED-00001"
            )

            self.assertEqual(len(brats_packages), 1)
            self.assertEqual(brats_packages.iloc[0]["download_id"], "53630")
            self.assertEqual(
                brats_packages.iloc[0]["download_label"],
                "Images (Training and Validation)",
            )

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

            source_detail = load_patient_idc(
                paths,
                catalog,
                "Source-Collection",
                "P1",
                collection_id="source_collection",
                direct_collection_only=True,
            )
            self.assertEqual(set(source_detail["SeriesInstanceUID"]), {"ORIGINAL"})

            route_patient = pd.DataFrame(
                [
                    {
                        "short_title": "Source-Collection",
                        "dataset_type": "Collection",
                        "subject_id": "P1",
                        "idc_subject_id": "P1",
                        "idc_collection_id": "source_collection",
                        "idc_analysis_result_id": "",
                    }
                ]
            )
            routes, _ = collect_filtered_imaging_routes(
                paths,
                catalog,
                route_patient,
                imaging_sources=["IDC DICOM"],
                direct_collection_titles=["Source-Collection"],
            )
            self.assertEqual(routes["SeriesInstanceUID"], ["ORIGINAL"])

    def test_verified_collection_and_analysis_memberships_group_for_display(self):
        patients = pd.DataFrame(
            [
                {
                    "patient_key": "Source-Collection|P1",
                    "short_title": "Source-Collection",
                    "dataset_type": "Collection",
                    "subject_id": "P1",
                    "idc_subject_id": "P1",
                    "idc_collection_id": "source_collection",
                    "idc_analysis_result_id": "",
                    "has_clinical": True,
                    "primary_diagnosis": "Diagnosis",
                    "dicom_series": 2,
                    "modalities": "CT; SEG",
                    "resolved_access_level": "open",
                },
                {
                    "patient_key": "Derived-Result|P1",
                    "short_title": "Derived-Result",
                    "dataset_type": "Analysis Result",
                    "subject_id": "P1",
                    "idc_subject_id": "P1",
                    "idc_collection_id": "source_collection",
                    "idc_analysis_result_id": "derived_result",
                    "has_clinical": False,
                    "primary_diagnosis": None,
                    "dicom_series": 1,
                    "modalities": "SEG",
                    "resolved_access_level": "open",
                },
                {
                    "patient_key": "Unrelated|P1",
                    "short_title": "Unrelated",
                    "dataset_type": "Collection",
                    "subject_id": "P1",
                    "idc_subject_id": "P1",
                    "idc_collection_id": "unrelated",
                    "idc_analysis_result_id": "",
                    "has_clinical": False,
                    "primary_diagnosis": None,
                    "dicom_series": 4,
                    "modalities": "MR",
                    "resolved_access_level": "open",
                },
            ]
        )

        grouped, memberships = build_grouped_patient_index(patients)

        self.assertEqual(len(grouped), 2)
        self.assertEqual(len(memberships), 3)
        combined = grouped[grouped["dataset_count"] == 2].iloc[0]
        self.assertEqual(combined["primary_diagnosis"], "Diagnosis")
        self.assertEqual(int(combined["dicom_series"]), 2)
        self.assertIn("Source-Collection [Collection]", combined["dataset_memberships"])
        self.assertIn("Derived-Result [Analysis Result]", combined["dataset_memberships"])
        grouped_members = memberships[
            memberships["patient_group_key"] == combined["patient_group_key"]
        ]
        self.assertEqual(set(grouped_members["short_title"]), {"Source-Collection", "Derived-Result"})

    def test_explicit_source_collection_groups_non_idc_analysis_result(self):
        patients = pd.DataFrame(
            [
                {
                    "patient_key": "TCGA-LGG|TCGA-CS-4944",
                    "short_title": "TCGA-LGG",
                    "dataset_type": "Collection",
                    "subject_id": "TCGA-CS-4944",
                    "source_collections": "",
                    "has_clinical": True,
                    "has_controlled_metadata": True,
                    "controlled_files": 11,
                    "resolved_access_level": "controlled",
                },
                {
                    "patient_key": "BraTS-TCGA-LGG|TCGA-CS-4944",
                    "short_title": "BraTS-TCGA-LGG",
                    "dataset_type": "Analysis Result",
                    "subject_id": "tcga-cs-4944",
                    "source_collections": (
                        "Corresponding Original Images from TCGA-LGG (DICOM)."
                    ),
                    "has_clinical": False,
                    "has_nifti": True,
                    "nifti_files": 6,
                    "resolved_access_level": "open",
                },
                {
                    "patient_key": "Unrelated|TCGA-CS-4944",
                    "short_title": "Unrelated",
                    "dataset_type": "Collection",
                    "subject_id": "TCGA-CS-4944",
                    "source_collections": "",
                    "has_clinical": False,
                    "resolved_access_level": "open",
                },
            ]
        )

        grouped, memberships = build_grouped_patient_index(patients)

        self.assertEqual(len(grouped), 2)
        combined = grouped[grouped["dataset_count"] == 2].iloc[0]
        self.assertEqual(combined["short_title"], "TCGA-LGG")
        self.assertEqual(combined["resolved_access_level"], "mixed")
        self.assertEqual(int(combined["controlled_files"]), 11)
        self.assertEqual(int(combined["nifti_files"]), 6)
        self.assertIn("TCGA-LGG [Collection]", combined["dataset_memberships"])
        self.assertIn(
            "BraTS-TCGA-LGG [Analysis Result]",
            combined["dataset_memberships"],
        )
        grouped_members = memberships[
            memberships["patient_group_key"] == combined["patient_group_key"]
        ]
        self.assertEqual(
            set(grouped_members["short_title"]),
            {"TCGA-LGG", "BraTS-TCGA-LGG"},
        )

    def test_explicit_relationship_requires_exact_subject_and_one_source(self):
        patients = pd.DataFrame(
            [
                {
                    "patient_key": "Source-A|P1",
                    "short_title": "Source-A",
                    "dataset_type": "Collection",
                    "subject_id": "P1",
                },
                {
                    "patient_key": "Source-B|P1",
                    "short_title": "Source-B",
                    "dataset_type": "Collection",
                    "subject_id": "P1",
                },
                {
                    "patient_key": "Ambiguous-Result|P1",
                    "short_title": "Ambiguous-Result",
                    "dataset_type": "Analysis Result",
                    "subject_id": "P1",
                    "source_collections": "Source-A; Source-B",
                },
                {
                    "patient_key": "Source-A-Result|P2",
                    "short_title": "Source-A-Result",
                    "dataset_type": "Analysis Result",
                    "subject_id": "P2",
                    "source_collections": "Source-A",
                },
            ]
        )

        grouped, memberships = build_grouped_patient_index(patients)

        self.assertEqual(len(grouped), 4)
        self.assertTrue((grouped["dataset_count"] == 1).all())
        self.assertTrue(
            (memberships["patient_group_key"] == memberships["patient_key"]).all()
        )

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

    def test_filtered_cohort_download_includes_patients_and_separate_routes(self):
        patients = pd.DataFrame(
            [
                {
                    "short_title": "TEST",
                    "dataset_type": "Collection",
                    "subject_id": "P1",
                    "primary_diagnosis": "Example diagnosis",
                    "primary_site": "Example site",
                },
                {
                    "short_title": "TEST",
                    "dataset_type": "Collection",
                    "subject_id": "P2",
                    "primary_diagnosis": "Example diagnosis",
                    "primary_site": "Example site",
                },
            ]
        )
        routes = {
            "SeriesInstanceUID": ["1.2.3", "1.2.3", "1.2.4"],
            "imageUrl": ["https://example.org/slide.svs"],
        }
        unrouted = pd.DataFrame(
            [
                {
                    "source": "NIfTI",
                    "short_title": "TEST",
                    "subject_id": "P2",
                    "item": "scan.nii.gz",
                    "reason": "No supported route.",
                }
            ]
        )

        payload, filename, mime, counts = build_filtered_cohort_download(
            patients, routes, unrouted
        )

        self.assertEqual(filename, "tcia_filtered_cohort_export.zip")
        self.assertEqual(mime, "application/zip")
        self.assertEqual(counts["patients"], 2)
        self.assertEqual(counts["SeriesInstanceUID"], 2)
        self.assertEqual(counts["imageUrl"], 1)
        self.assertEqual(counts["unrouted_imaging"], 1)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "README.txt",
                    "tcia_filtered_patients.csv",
                    "tcia_dicom_series.csv",
                    "tcia_pathdb_files.csv",
                    "tcia_unrouted_imaging_inventory.csv",
                },
            )
            patients_csv = archive.read("tcia_filtered_patients.csv").decode("utf-8")
            self.assertIn("primary_diagnosis", patients_csv.splitlines()[0])
            self.assertEqual(len(patients_csv.splitlines()), 3)
            dicom_csv = archive.read("tcia_dicom_series.csv").decode("utf-8")
            self.assertEqual(
                dicom_csv.splitlines(),
                ["SeriesInstanceUID", "1.2.3", "1.2.4"],
            )


if __name__ == "__main__":
    unittest.main()
