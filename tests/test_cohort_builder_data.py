import csv
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cohort_builder_data import (
    DataPaths,
    add_idc_imaging_facets,
    aggregate_idc,
    build_patient_index,
    build_filtered_cohort_download,
    build_grouped_patient_index,
    build_manifest_download,
    canonical_imaging_token,
    canonicalize_patient_categories,
    cart_item,
    collapse_clinical_subject_aliases,
    collect_filtered_imaging_routes,
    count_visible_dataset_contexts,
    deduplicate_cart,
    enrich_participants_with_clinical_detail,
    exclude_nlst_clinical_only,
    filter_patient_groups_by_dataset_type,
    filter_patient_groups_by_asset_facets,
    filter_imaging_rows,
    idc_viewer_url,
    load_clinical_subjects,
    load_idc_series,
    load_idc_patient_search_summary,
    load_dataset_aspera_packages,
    load_patient_idc,
    load_patient_idc_scope,
    load_patient_clinical_longitudinal,
    load_patient_nifti_packages,
    load_patient_public_non_dicom,
    load_participant_pathology_facets,
    load_public_non_dicom_image_metadata,
    load_public_non_dicom_locations,
    normalize_dataset_key,
    normalize_subject_key,
    participant_availability_rows,
    resolve_data_paths,
    subject_join_key,
    subject_join_keys,
)
import pandas as pd


class CohortBuilderDataTests(unittest.TestCase):
    def test_shared_v2_install_dir_is_used_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            install_dir = Path(directory) / "v2"
            with mock.patch.dict(
                "os.environ",
                {"TCIA_V2_INSTALL_DIR": str(install_dir)},
                clear=True,
            ):
                paths = resolve_data_paths(
                    app_dir=Path(directory) / "app",
                    skill_root=Path(directory) / "skill",
                )

            self.assertEqual(
                paths.participant_db,
                (install_dir / "participant_inventory.sqlite").resolve(),
            )
            self.assertEqual(
                paths.bundle_manifest,
                (install_dir / "tcia_metadata_v2_bundle_manifest.json").resolve(),
            )

    def test_clinical_detail_enrichment_is_opt_in_and_case_equivalent(self):
        with tempfile.TemporaryDirectory() as directory:
            clinical_db = Path(directory) / "clinical.sqlite"
            with sqlite3.connect(clinical_db) as connection:
                connection.execute(
                    "CREATE TABLE agent_clinical_all_subjects ("
                    "short_title TEXT, subject_id TEXT, primary_diagnosis TEXT, "
                    "primary_diagnosis_is_inferred INTEGER, "
                    "primary_site_is_inferred INTEGER, "
                    "has_imaging INTEGER, source_count INTEGER, conflict_count INTEGER, "
                    "source_kinds TEXT)"
                )
                connection.execute(
                    "INSERT INTO agent_clinical_all_subjects VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "TEST", "p-01", "Resolved diagnosis", 1, 0,
                        1, 1, 0, '[\"clinical\"]',
                    ),
                )
            participants = pd.DataFrame(
                [{"short_title": "TEST", "subject_id": "P-01"}]
            )

            missing = enrich_participants_with_clinical_detail(
                participants, Path(directory) / "missing.sqlite"
            )
            self.assertTrue(pd.isna(missing.iloc[0]["primary_diagnosis"]))

            enriched = enrich_participants_with_clinical_detail(
                participants, clinical_db
            )
            self.assertEqual(
                enriched.iloc[0]["primary_diagnosis"], "Resolved diagnosis"
            )
            self.assertEqual(
                int(enriched.iloc[0]["primary_diagnosis_is_inferred"]), 1
            )

    def test_idc_search_summary_is_aggregated_by_dataset_scoped_participant(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "idc.parquet"
            pd.DataFrame(
                [
                    {
                        "collection_id": "test_collection",
                        "analysis_result_id": "",
                        "PatientID": "P1",
                        "SeriesInstanceUID": "1",
                        "StudyDate": "20200101",
                        "Modality": "CT",
                        "BodyPartExamined": "CHEST",
                    },
                    {
                        "collection_id": "test_collection",
                        "analysis_result_id": "",
                        "PatientID": "P1",
                        "SeriesInstanceUID": "2",
                        "StudyDate": "20210101",
                        "Modality": "SEG",
                        "BodyPartExamined": "ABDOMEN",
                    },
                    {
                        "collection_id": "test_collection",
                        "analysis_result_id": "",
                        "PatientID": "P2",
                        "SeriesInstanceUID": "3",
                        "StudyDate": "",
                        "Modality": "MR",
                        "BodyPartExamined": None,
                    },
                ]
            ).to_parquet(path, index=False)
            catalog = pd.DataFrame(
                [{"short_title": "TEST", "dataset_key": "testcollection"}]
            )

            result = load_idc_patient_search_summary(path, catalog)

            self.assertEqual(len(result), 2)
            p1 = result[result["subject_join_key"] == "p1"].iloc[0]
            self.assertEqual(p1["short_title"], "TEST")
            self.assertEqual(int(p1["dicom_series_idc"]), 2)
            self.assertEqual(int(p1["dicom_timepoints_idc"]), 2)
            self.assertEqual(p1["dicom_modalities"], "CT; SEG")
            self.assertEqual(p1["body_parts"], "ABDOMEN; CHEST")

    def test_participant_pathology_facets_are_standardized_and_json_encoded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE agent_public_non_dicom_image_metadata ("
                    "short_title TEXT, subject_id TEXT, pathology_protocol TEXT, "
                    "magnification TEXT)"
                )
                connection.executemany(
                    "INSERT INTO agent_public_non_dicom_image_metadata VALUES (?,?,?,?)",
                    [
                        ("TEST", "P1", "Hematoxylin and eosin", "40X"),
                        ("TEST", "p1", "H&E", "40x"),
                        ("TEST", "P1", "Multiplex assay, panel A", "20 x"),
                        ("TEST", "", "H&E", "10x"),
                    ],
                )

            result = load_participant_pathology_facets(path)

            self.assertEqual(len(result), 1)
            row = result.iloc[0]
            self.assertEqual(row["subject_join_key"], "p1")
            self.assertEqual(
                json.loads(row["pathology_protocols"]),
                ["H&E", "Multiplex assay, panel A"],
            )
            self.assertEqual(
                json.loads(row["pathology_magnifications"]),
                ["20x", "40x"],
            )

    def test_dataset_aspera_packages_are_public_wordpress_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE agent_current_downloads (short_title TEXT, hidden INTEGER, "
                    "download_id TEXT, download_title TEXT, download_url TEXT, download_size TEXT, "
                    "download_size_unit TEXT, license_label TEXT, access_level TEXT, controlled_access INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO agent_current_downloads VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        ("Pedi-Cranial-CT-Healthy", 0, "1", "Images", "https://faspex.cancerimagingarchive.net/?context=public-package", "3.2", "gb", "CC BY 4.0", "open", 0),
                        ("Pedi-Cranial-CT-Healthy", 0, "2", "Controlled", "https://faspex.cancerimagingarchive.net/?context=controlled-package", "1", "gb", "Restricted", "controlled", 1),
                        ("Pedi-Cranial-CT-Healthy", 0, "3", "Untrusted", "https://example.org/?context=other", "1", "gb", "CC BY 4.0", "open", 0),
                    ],
                )

            packages = load_dataset_aspera_packages(
                path, "Pedi-Cranial-CT-Healthy"
            )

            self.assertEqual(packages["download_id"].tolist(), ["1"])

    def test_filtered_mha_export_does_not_require_controlled_drs_column(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_db = root / "public.sqlite"
            with sqlite3.connect(public_db) as connection:
                connection.execute(
                    "CREATE TABLE agent_public_non_dicom_asset_participants ("
                    "short_title TEXT, subject_id TEXT, file_name TEXT, package_path TEXT, "
                    "asset_id TEXT, file_format TEXT, media_kind TEXT, "
                    "imaging_domain TEXT, modality TEXT, source_url TEXT)"
                )
                connection.execute(
                    "INSERT INTO agent_public_non_dicom_asset_participants "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        "Pedi-Cranial-CT-Healthy", "107", "00107_CT.mha",
                        "package/00107_CT.mha", "asset1", "MHA", "image",
                        "radiology", "CT", "https://faspex.example/package",
                    ),
                )
            missing = root / "missing.sqlite"
            paths = DataPaths(
                missing, missing, missing, missing, missing, missing,
                public_non_dicom_db=public_db,
            )
            patients = pd.DataFrame(
                [{"short_title": "Pedi-Cranial-CT-Healthy", "subject_id": "107"}]
            )

            routes, unrouted = collect_filtered_imaging_routes(
                paths,
                pd.DataFrame(),
                patients,
                imaging_sources=["MHA volumes", "Controlled access"],
            )

            self.assertEqual(routes, {})
            self.assertEqual(len(unrouted), 1)
            self.assertEqual(unrouted.iloc[0]["source"], "Public non-DICOM")
            self.assertIn("dataset-level package", unrouted.iloc[0]["reason"])
            self.assertEqual(
                unrouted.iloc[0]["package_url"],
                "https://faspex.example/package",
            )

    def test_asset_facets_require_same_row_geometry_and_data_type(self):
        patients = pd.DataFrame(
            [
                {"patient_group_key": "G1", "subject_id": "P1"},
                {"patient_group_key": "G2", "subject_id": "P2"},
            ]
        )
        memberships = pd.DataFrame(
            [
                {"participant_key": "K1", "patient_group_key": "G1"},
                {"participant_key": "K2", "patient_group_key": "G2"},
            ]
        )
        assets = pd.DataFrame(
            [
                {
                    "participant_key": "K1",
                    "data_type": "SEG",
                    "file_format": "DICOM",
                    "geometry_status": "checked_not_regular",
                },
                {
                    "participant_key": "K1",
                    "data_type": "CT",
                    "file_format": "DICOM",
                    "geometry_status": "checked_regular",
                },
                {
                    "participant_key": "K2",
                    "data_type": "SEG",
                    "file_format": "DICOM",
                    "geometry_status": "checked_regular",
                },
            ]
        )

        matched = filter_patient_groups_by_asset_facets(
            patients,
            memberships,
            assets,
            data_types=["SEG"],
            file_formats=["DICOM"],
            geometry="Regular",
        )

        self.assertEqual(matched["patient_group_key"].tolist(), ["G2"])

    def test_idc_geometry_status_filters_series_rows(self):
        frame = pd.DataFrame(
            [
                {
                    "SeriesInstanceUID": "regular",
                    "Modality": "CT",
                    "volume_geometry_indexed": True,
                    "regularly_spaced_3d_volume": True,
                },
                {
                    "SeriesInstanceUID": "irregular",
                    "Modality": "CT",
                    "volume_geometry_indexed": True,
                    "regularly_spaced_3d_volume": False,
                },
                {
                    "SeriesInstanceUID": "outside",
                    "Modality": "SEG",
                    "volume_geometry_indexed": False,
                    "regularly_spaced_3d_volume": pd.NA,
                },
            ]
        )
        enriched = add_idc_imaging_facets(frame)

        self.assertEqual(
            filter_imaging_rows(enriched, geometry="Regular")[
                "SeriesInstanceUID"
            ].tolist(),
            ["regular"],
        )
        self.assertEqual(
            filter_imaging_rows(enriched, geometry="Irregular")[
                "SeriesInstanceUID"
            ].tolist(),
            ["irregular"],
        )

    def test_participant_availability_distinguishes_unlinked_from_absent(self):
        rows = participant_availability_rows(
            {
                "has_public_dicom": True,
                "has_public_non_dicom": False,
                "has_clinical": True,
                "has_controlled_metadata": False,
                "dataset_unlinked_asset_groups": 2,
            }
        )
        by_data = {row["Data"]: row["Coverage"] for row in rows}
        self.assertEqual(by_data["Public DICOM"], "Participant linked")
        self.assertEqual(
            by_data["Public non-DICOM"],
            "Dataset-level only; participant crosswalk unavailable",
        )
        self.assertNotIn("absent", by_data["Public non-DICOM"].casefold())

    def test_v2_participant_inventory_is_primary_search_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            participant_db = root / "participant.sqlite"
            with sqlite3.connect(participant_db) as connection:
                connection.execute(
                    "CREATE TABLE participants (participant_key TEXT, dataset_type TEXT, "
                    "short_title TEXT, display_participant_id TEXT, identity_scope TEXT, "
                    "within_dataset_identity_status TEXT, identity_resolution_method TEXT, "
                    "cross_dataset_identity_status TEXT)"
                )
                connection.execute(
                    "INSERT INTO participants VALUES (?,?,?,?,?,?,?,?)",
                    (
                        "pk1", "Collection", "TEST", "P1", "dataset_scoped",
                        "resolved", "casefolded_identifier_same_tcia_dataset",
                        "not_asserted",
                    ),
                )
                connection.execute(
                    "CREATE VIEW agent_participant_search AS SELECT *, 2 AS source_namespace_count, "
                    "'tcia_dataset:TEST,crdc_idc:TEST' AS source_namespaces, 1 AS inventory_rows, "
                    "1 AS has_open_data, 1 AS has_controlled_data, 1 AS has_public_dicom, "
                    "1 AS has_public_non_dicom, 1 AS has_clinical, 'radiology,clinical' AS data_domains, "
                    "'CT' AS modalities, 'DICOM,MHA' AS file_formats, "
                    "'crdc_idc,tcia_wordpress' AS managed_systems FROM participants"
                )
                connection.execute(
                    "CREATE TABLE agent_participant_assets (participant_asset_id TEXT, participant_key TEXT, "
                    "managed_system TEXT, source_artifact TEXT, access_level TEXT, data_domain TEXT, "
                    "media_kind TEXT, modality TEXT, file_format TEXT, object_role TEXT, study_count INTEGER, "
                    "series_count INTEGER, file_count INTEGER, known_size_bytes INTEGER, "
                    "has_file_level_metadata INTEGER, detail_pointer TEXT, access_route TEXT, "
                    "inventory_status TEXT, source_version TEXT, provenance_json TEXT)"
                )
                connection.executemany(
                    "INSERT INTO agent_participant_assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        ("a1", "pk1", "crdc_idc", "idc", "open", "radiology", "image_series", "CT", "DICOM", "source_image", 1, 2, 20, 100, 1, "idc", "view", "known", "v1", "{}"),
                        ("a2", "pk1", "tcia_wordpress", "public_non_dicom_metadata", "open", "radiology", "image_volume", "CT", "MHA", "source_image,segmentation", 1, 0, 1, 50, 1, "pnd", "download", "known", "v3", "{}"),
                        ("a3", "pk1", "crdc_gc", "controlled_access_metadata", "controlled", "radiology", "image_series", "CT", "DICOM", "submitted_original", 1, 1, 5, 80, 1, "controlled", "request", "known", "v2", "{}"),
                        ("a4", "pk1", "tcia_aspera", "public_non_dicom_metadata", "open", "radiology", "participant_modality", "MR", "DICOM", "submitted_original", 0, 0, 400, 0, 0, "pnd", "download", "known", "v6", "{}"),
                    ],
                )
                connection.execute(
                    "CREATE TABLE agent_participant_clinical_values (participant_clinical_value_id TEXT, "
                    "participant_key TEXT, concept TEXT, raw_field_name TEXT, raw_value TEXT, standardized_value TEXT, "
                    "value_role TEXT, normalization_method TEXT, managed_system TEXT, source_artifact TEXT, "
                    "source_url TEXT, confidence TEXT, review_status TEXT, provenance_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO agent_participant_clinical_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("c1", "pk1", "primary_diagnosis", "diagnosis", "raw dx", "Resolved diagnosis", "resolved", "review", "tcia_wordpress", "clinical_metadata", "https://example.org", "source_supported", "accepted", "{}"),
                )
                connection.execute(
                    "CREATE TABLE agent_dataset_assets_without_participant_crosswalk (short_title TEXT)"
                )
                connection.execute("INSERT INTO agent_dataset_assets_without_participant_crosswalk VALUES ('TEST')")
                connection.execute(
                    "CREATE TABLE agent_participant_link_issues (short_title TEXT, raw_identifier TEXT)"
                )

            clinical_db = root / "clinical.sqlite"
            with sqlite3.connect(clinical_db) as connection:
                connection.execute(
                    "CREATE TABLE agent_clinical_all_subjects ("
                    "short_title TEXT, subject_id TEXT, primary_diagnosis TEXT, "
                    "primary_diagnosis_is_inferred INTEGER, "
                    "primary_site_is_inferred INTEGER)"
                )
                connection.execute(
                    "INSERT INTO agent_clinical_all_subjects VALUES (?,?,?,?,?)",
                    ("TEST", "P1", "Dataset diagnosis", 1, 0),
                )

            empty = root / "missing.sqlite"
            paths = DataPaths(
                empty,
                clinical_db,
                empty,
                empty,
                empty,
                empty,
                participant_db=participant_db,
            )
            result = build_patient_index(paths)

            self.assertEqual(len(result), 1)
            row = result.iloc[0]
            self.assertEqual(row["patient_key"], "pk1")
            self.assertEqual(int(row["ct_series"]), 2)
            self.assertEqual(int(row["dicom_series"]), 2)
            self.assertEqual(int(row["public_dicom_files_outside_idc"]), 400)
            self.assertEqual(int(row["public_non_dicom_files"]), 1)
            self.assertEqual(int(row["controlled_files"]), 5)
            self.assertEqual(int(row["mha_volumes"]), 1)
            self.assertEqual(row["resolved_access_level"], "mixed")
            self.assertEqual(row["primary_diagnosis"], "Dataset diagnosis")
            self.assertTrue(bool(row["primary_diagnosis_is_inferred"]))
            self.assertTrue(bool(row["has_annotations"]))
            self.assertEqual(
                row["data_categories"],
                "Annotations/Segmentations; Radiology",
            )
            self.assertIn("Annotation/Segmentation", row["data_types"])
            self.assertIn("CT", row["data_types"])
            self.assertIn("MHA", row["file_formats"])
            self.assertEqual(
                row["identity_resolution_method"],
                "casefolded_identifier_same_tcia_dataset",
            )
            self.assertEqual(row["imaging_linkage_status"], "Partial coverage or linkage review")

    def test_public_non_dicom_locations_do_not_multiply_logical_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE public_non_dicom_assets (asset_id TEXT, dataset_type TEXT, short_title TEXT, "
                    "asset_name TEXT, file_name TEXT, package_path TEXT, file_format TEXT, media_kind TEXT, "
                    "imaging_domain TEXT, modality TEXT, object_role TEXT, represented_file_count INTEGER, size_bytes INTEGER, "
                    "participant_link_status TEXT, representation_provenance_class TEXT, source_system TEXT, "
                    "source_url TEXT, quality_flag_json TEXT)"
                )
                connection.executemany(
                    "INSERT INTO public_non_dicom_assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        ("asset1", "Collection", "TEST", "scan", "scan.mha", "", "MHA", "image_volume", "radiology", "CT", "source_image", 1, 10, "reviewed_source_crosswalk", "submitted_original", "tcia_aspera", "https://example.org/source", "{}"),
                        ("asset2", "Collection", "TEST", "dicom", "", "dicom/", "DICOM", "participant_modality", "radiology", "MR", "submitted_original", 400, 0, "reviewed_source_crosswalk", "submitted_original", "tcia_aspera", "https://example.org/source", "{}"),
                    ],
                )
                connection.execute(
                    "CREATE TABLE public_non_dicom_asset_participants (asset_participant_id TEXT, asset_id TEXT, "
                    "subject_id TEXT, raw_subject_id TEXT, subject_id_namespace TEXT, link_status TEXT)"
                )
                connection.executemany(
                    "INSERT INTO public_non_dicom_asset_participants VALUES (?,?,?,?,?,?)",
                    [
                        ("ap1", "asset1", "P1", "raw-P1", "tcia_dataset:TEST", "reviewed_source_crosswalk"),
                        ("ap2", "asset2", "P1", "raw-P1", "tcia_dataset:TEST", "reviewed_source_crosswalk"),
                    ],
                )
                connection.execute(
                    "CREATE TABLE public_non_dicom_locations (location_id TEXT, asset_id TEXT, "
                    "representation_provenance_class TEXT)"
                )
                connection.executemany(
                    "INSERT INTO public_non_dicom_locations VALUES (?,?,?)",
                    [("l1", "asset1", "submitted_original"), ("l2", "asset1", "standardized_representation")],
                )
                connection.execute(
                    "CREATE VIEW agent_public_non_dicom_asset_participants AS "
                    "SELECT * FROM public_non_dicom_asset_participants"
                )

            result = load_patient_public_non_dicom(path, "TEST", "P1")
            self.assertEqual(len(result), 1)
            self.assertEqual(int(result.iloc[0]["location_count"]), 2)
            dicom = load_patient_public_non_dicom(
                path, "TEST", "P1", include_dicom=True
            )
            self.assertEqual(len(dicom), 1)
            self.assertEqual(int(dicom.iloc[0]["represented_file_count"]), 400)

    def test_public_non_dicom_image_metadata_projects_research_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE agent_public_non_dicom_image_metadata ("
                    "asset_id TEXT, modality TEXT, body_part_examined TEXT, "
                    "study_description TEXT, series_description TEXT, manufacturer TEXT, "
                    "manufacturer_model_name TEXT, magnetic_field_strength_t REAL, "
                    "study_datetime TEXT, acquisition_dimensionality TEXT, "
                    "scanner_site TEXT, sequence_class TEXT, sequence_tags TEXT, "
                    "slice_thickness_mm REAL, spacing_between_slices_mm REAL, "
                    "repetition_time_ms REAL, echo_time_ms REAL, inversion_time_ms REAL, "
                    "pre_included INTEGER, post_included INTEGER, t2_included INTEGER, "
                    "flair_included INTEGER, sequences_present TEXT, "
                    "rows INTEGER, columns INTEGER, number_of_slices INTEGER, "
                    "pixel_spacing_mm TEXT, pathology_protocol TEXT, magnification TEXT, "
                    "field_source_ids_json TEXT, populated_field_count INTEGER, "
                    "conflict_field_count INTEGER)"
                )
                connection.execute(
                    "INSERT INTO agent_public_non_dicom_image_metadata "
                    "(asset_id, modality, body_part_examined, study_description, "
                    "series_description, manufacturer, manufacturer_model_name, "
                    "magnetic_field_strength_t, study_datetime, "
                    "acquisition_dimensionality, sequence_class, sequence_tags, "
                    "slice_thickness_mm, rows, columns, number_of_slices, "
                    "pixel_spacing_mm, pathology_protocol, magnification, "
                    "field_source_ids_json, populated_field_count, conflict_field_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "asset1", "CT", "HEAD", "Study", "Series", "Vendor",
                        "Model", None, "2020-01-01", "3D", "T2", "T2; FLAIR",
                        1.25, 512, 512, 80, "[0.5,0.5]", "", "",
                        '{"modality":["source-1"]}', 7, 0,
                    ),
                )

            result = load_public_non_dicom_image_metadata(path, ["asset1"])

            self.assertEqual(result.iloc[0]["metadata_modality"], "CT")
            self.assertEqual(result.iloc[0]["body_part_examined"], "HEAD")
            self.assertEqual(int(result.iloc[0]["number_of_slices"]), 80)
            self.assertEqual(result.iloc[0]["sequence_class"], "T2")

    def test_public_non_dicom_locations_batch_and_route_pathdb_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_db = root / "public.sqlite"
            with sqlite3.connect(public_db) as connection:
                connection.execute(
                    "CREATE TABLE agent_public_non_dicom_asset_participants ("
                    "short_title TEXT, subject_id TEXT, file_name TEXT, package_path TEXT, "
                    "asset_id TEXT, file_format TEXT, media_kind TEXT, "
                    "imaging_domain TEXT, modality TEXT)"
                )
                connection.executemany(
                    "INSERT INTO agent_public_non_dicom_asset_participants "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        ("PATH", "P1", "slide1.svs", "slides/slide1.svs", "a1", "SVS", "image", "pathology", "SM"),
                        ("PATH", "P1", "slide2.svs", "slides/slide2.svs", "a2", "SVS", "image", "pathology", "SM"),
                    ],
                )
                connection.execute(
                    "CREATE TABLE agent_public_non_dicom_locations ("
                    "location_id TEXT, asset_id TEXT, managed_system TEXT, "
                    "access_url TEXT, file_name TEXT)"
                )
                connection.executemany(
                    "INSERT INTO agent_public_non_dicom_locations VALUES (?,?,?,?,?)",
                    [
                        ("l1", "a1", "tcia_pathdb", "https://pathdb.example/a1", "slide1.svs"),
                        ("l2", "a2", "aspera", "", "slide2.svs"),
                    ],
                )

            locations = load_public_non_dicom_locations(public_db, ["a1", "a2"])
            self.assertEqual(set(locations["asset_id"]), {"a1", "a2"})

            missing = root / "missing.sqlite"
            paths = DataPaths(
                missing, missing, missing, missing, missing, missing,
                public_non_dicom_db=public_db,
            )
            patients = pd.DataFrame([{"short_title": "PATH", "subject_id": "P1"}])
            routes, unrouted = collect_filtered_imaging_routes(
                paths,
                pd.DataFrame(),
                patients,
                imaging_sources=["Pathology images"],
            )

            self.assertEqual(routes["imageUrl"], ["https://pathdb.example/a1"])
            self.assertEqual(unrouted["item"].tolist(), ["slide2.svs"])

    def test_clinical_longitudinal_observations_remain_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clinical.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE agent_clinical_longitudinal_observations ("
                    "short_title TEXT, subject_id TEXT, observation_type TEXT, "
                    "study_datetime TEXT, file_name TEXT, age_at_imaging_years REAL, "
                    "manufacturer TEXT, manufacturer_model_name TEXT, "
                    "magnetic_field_strength_t REAL, acquisition_dimensionality TEXT, "
                    "scanner_site TEXT, sequence_class TEXT, sequence_tags TEXT, "
                    "slice_thickness_mm REAL, spacing_between_slices_mm REAL, "
                    "repetition_time_ms REAL, echo_time_ms REAL, inversion_time_ms REAL)"
                )
                connection.executemany(
                    "INSERT INTO agent_clinical_longitudinal_observations "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        ("TEST", "P1", "scan", "2020-01-01", "scan1.nii.gz", 50, "A", "M1", 3, "3D", "site1", "T1", "PRE", 1, 1, 5, 2, 900),
                        ("TEST", "p1", "scan", "2021-01-01", "scan2.nii.gz", 51, "B", "M2", 3, "3D", "site2", "T2", "POST", 1, 1, 6, 3, 950),
                    ],
                )

            result = load_patient_clinical_longitudinal(path, "TEST", ["P1", "p1"])

            self.assertEqual(result["file_name"].tolist(), ["scan1.nii.gz", "scan2.nii.gz"])
            self.assertEqual(result["sequence_class"].tolist(), ["T1", "T2"])

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

            search_summary = load_idc_patient_search_summary(parquet_path, catalog)
            source_summary = search_summary[
                search_summary["short_title"] == "Source-Collection"
            ].iloc[0]
            derived_summary = search_summary[
                search_summary["short_title"] == "Derived-Result"
            ].iloc[0]
            self.assertEqual(source_summary["idc_subject_id"], "P1")
            self.assertEqual(source_summary["idc_collection_id"], "source_collection")
            self.assertEqual(source_summary["idc_analysis_result_id"], "")
            self.assertEqual(derived_summary["idc_subject_id"], "P1")
            self.assertEqual(derived_summary["idc_collection_id"], "source_collection")
            self.assertEqual(
                derived_summary["idc_analysis_result_id"], "Derived-Result"
            )

            participant_rows = pd.DataFrame(
                [
                    {
                        "patient_key": "source|p1",
                        "dataset_type": "Collection",
                        "short_title": "Source-Collection",
                        "subject_id": "P1",
                    },
                    {
                        "patient_key": "derived|p1",
                        "dataset_type": "Analysis Result",
                        "short_title": "Derived-Result",
                        "subject_id": "P1",
                    },
                ]
            )
            participant_rows["subject_join_key"] = subject_join_keys(
                participant_rows
            )
            participant_rows = participant_rows.merge(
                search_summary,
                on=["short_title", "subject_join_key"],
                how="left",
            )
            grouped, memberships = build_grouped_patient_index(participant_rows)
            self.assertEqual(len(grouped), 1)
            self.assertEqual(len(memberships), 2)
            self.assertEqual(int(grouped.iloc[0]["dataset_count"]), 2)

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

            all_detail = load_patient_idc_scope(
                paths,
                catalog,
                memberships,
                include_all_related=True,
            )
            self.assertEqual(
                set(all_detail["SeriesInstanceUID"]), {"ORIGINAL", "DERIVED"}
            )
            collection_detail = load_patient_idc_scope(
                paths,
                catalog,
                memberships[memberships["dataset_type"] == "Collection"],
            )
            self.assertEqual(
                set(collection_detail["SeriesInstanceUID"]), {"ORIGINAL"}
            )
            analysis_detail = load_patient_idc_scope(
                paths,
                catalog,
                memberships[memberships["dataset_type"] == "Analysis Result"],
            )
            self.assertEqual(
                set(analysis_detail["SeriesInstanceUID"]), {"DERIVED"}
            )

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

    def test_dataset_type_filter_uses_source_memberships(self):
        patients = pd.DataFrame(
            [
                {"patient_group_key": "grouped", "subject_id": "P1"},
                {"patient_group_key": "collection-only", "subject_id": "P2"},
            ]
        )
        memberships = pd.DataFrame(
            [
                {"patient_group_key": "grouped", "dataset_type": "Collection", "short_title": "SOURCE"},
                {"patient_group_key": "grouped", "dataset_type": "Analysis Result", "short_title": "RESULT"},
                {"patient_group_key": "collection-only", "dataset_type": "Collection", "short_title": "OTHER"},
            ]
        )

        analysis_patients, analysis_memberships = (
            filter_patient_groups_by_dataset_type(
                patients, memberships, "Analysis Result"
            )
        )

        self.assertEqual(analysis_patients["patient_group_key"].tolist(), ["grouped"])
        self.assertEqual(analysis_memberships["short_title"].tolist(), ["RESULT"])
        all_patients, all_memberships = filter_patient_groups_by_dataset_type(
            patients, memberships, "All"
        )
        self.assertIs(all_patients, patients)
        self.assertIs(all_memberships, memberships)

    def test_visible_dataset_count_respects_explicit_dataset_selection(self):
        patients = pd.DataFrame(
            [{"patient_group_key": "grouped", "subject_id": "P1"}]
        )
        memberships = pd.DataFrame(
            [
                {"patient_group_key": "grouped", "short_title": "LIDC-IDRI"},
                {
                    "patient_group_key": "grouped",
                    "short_title": "DICOM-LIDC-IDRI-Nodules",
                },
                {"patient_group_key": "grouped", "short_title": "Other-Result-A"},
                {"patient_group_key": "grouped", "short_title": "Other-Result-B"},
            ]
        )

        self.assertEqual(
            count_visible_dataset_contexts(patients, memberships),
            4,
        )
        self.assertEqual(
            count_visible_dataset_contexts(
                patients,
                memberships,
                ["LIDC-IDRI", "DICOM-LIDC-IDRI-Nodules"],
            ),
            2,
        )

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
                    "package_url": "https://faspex.example/package",
                    "reason": "No supported route.",
                }
            ]
        )

        payload, filename, mime, counts = build_filtered_cohort_download(
            patients,
            routes,
            unrouted,
            selection={
                "image_geometry": "Regular",
                "imaging_contents": "Only imaging matching filters",
            },
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
                    "cohort_selection.json",
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
            inventory_csv = archive.read(
                "tcia_unrouted_imaging_inventory.csv"
            ).decode("utf-8")
            self.assertIn("package_url", inventory_csv.splitlines()[0])
            self.assertIn("https://faspex.example/package", inventory_csv)
            selection = json.loads(archive.read("cohort_selection.json"))
            self.assertEqual(selection["image_geometry"], "Regular")


if __name__ == "__main__":
    unittest.main()
