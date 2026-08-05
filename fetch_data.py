"""Refresh the public IDC series index used by the Participant Explorer."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from idc_index import IDCClient


IDC_COLUMNS = [
    "collection_id",
    "analysis_result_id",
    "PatientID",
    "PatientAge",
    "PatientSex",
    "StudyInstanceUID",
    "StudyDate",
    "StudyDescription",
    "BodyPartExamined",
    "SeriesInstanceUID",
    "SeriesDate",
    "Modality",
    "SeriesDescription",
    "instanceCount",
    "series_size_MB",
    "license_short_name",
    "source_DOI",
]
STRING_COLUMNS = [
    column for column in IDC_COLUMNS if column not in {"instanceCount", "series_size_MB"}
]


def refresh_idc_metadata(output_path: Path, batch_size: int = 20) -> None:
    """Atomically export the complete current IDC series index to Parquet."""
    client = IDCClient.client()
    print(f"Using idc-index {client.get_idc_version()}.")

    collections = client.sql_query(
        "SELECT DISTINCT collection_id FROM index "
        "WHERE COALESCE(collection_id, '') <> '' ORDER BY collection_id"
    )
    collection_ids = collections["collection_id"].astype(str).tolist()
    if not collection_ids:
        raise RuntimeError("The current IDC index returned no collections.")

    stats = client.sql_query(
        """
        SELECT COUNT(DISTINCT collection_id) AS collections,
               COUNT(DISTINCT PatientID) AS patients,
               COUNT(*) AS series,
               SUM(instanceCount) AS instances,
               SUM(series_size_MB) / 1000000 AS size_TB
        FROM index
        """
    ).iloc[0]
    print(
        "IDC scope: "
        f"{int(stats['collections']):,} collections, "
        f"{int(stats['patients']):,} patients, "
        f"{int(stats['series']):,} series, "
        f"{float(stats['size_TB']):,.3f} TB."
    )

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    writer: pq.ParquetWriter | None = None
    total_rows = 0
    try:
        for start in range(0, len(collection_ids), batch_size):
            batch = collection_ids[start : start + batch_size]
            literals = ",".join(
                "'" + value.replace("'", "''") + "'" for value in batch
            )
            frame = client.sql_query(
                f"SELECT {', '.join(IDC_COLUMNS)} FROM index "
                f"WHERE collection_id IN ({literals})"
            )
            if frame is None or frame.empty:
                continue
            for column in STRING_COLUMNS:
                frame[column] = frame[column].astype("string")
            frame["instanceCount"] = pd.to_numeric(
                frame["instanceCount"], errors="coerce"
            ).astype("Int64")
            frame["series_size_MB"] = pd.to_numeric(
                frame["series_size_MB"], errors="coerce"
            ).astype("float64")

            table = pa.Table.from_pandas(frame[IDC_COLUMNS], preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(
                    temporary_path,
                    table.schema,
                    compression="zstd",
                    use_dictionary=True,
                )
            writer.write_table(table)
            total_rows += len(frame)
            print(
                f"Exported {total_rows:,} series "
                f"({min(start + batch_size, len(collection_ids))}/"
                f"{len(collection_ids)} collections)."
            )

        if writer is None:
            raise RuntimeError("The current IDC index returned no series rows.")
        writer.close()
        writer = None
        os.replace(temporary_path, output_path)
        print(
            f"Saved {total_rows:,} IDC records to {output_path} "
            f"({output_path.stat().st_size / 1_000_000:.1f} MB)."
        )
    finally:
        if writer is not None:
            writer.close()
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "idc_metadata.parquet",
        help="Destination Parquet path (default: repository idc_metadata.parquet)",
    )
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    refresh_idc_metadata(args.output, args.batch_size)


if __name__ == "__main__":
    main()
