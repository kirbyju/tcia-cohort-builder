# TCIA Participant Explorer

The TCIA Participant Explorer is a patient-level Streamlit interface for
finding TCIA data, reviewing source-aware clinical metadata, inspecting imaging
and supporting files, and preparing retrieval manifests.

## Capabilities

- one row per dataset-scoped patient;
- dataset-scoped Collection and Analysis Result participants kept distinct;
- basic and advanced cohort filters with a source-aware patient detail panel;
- V2 Participant Inventory search and summaries for public DICOM, public
  non-DICOM, controlled-access, and clinical availability;
- separate IDC series and Aspera-only public DICOM holdings;
- on-demand IDC, public non-DICOM, controlled-access, and clinical detail;
- opt-in clinical cohort filters, longitudinal imaging context, and schema-7
  public image metadata;
- public WordPress Aspera package links shown separately from participant assets
  and TCIA Data Retriever routes;
- explicit missing-crosswalk and participant-link coverage states;
- logical asset counts that do not multiply alternate delivery locations;
- viewer routes for publicly viewable imaging; and
- filtered cohort downloads containing patient-level clinical data plus
  route-specific TCIA Data Retriever manifests.

## Requirements

- Python 3.10 or newer;
- the packages pinned in `requirements.txt`;
- the current `idc_metadata.parquet` series index in this repository; and
- an official local installation of the published
  `tcia-metadata-v2-latest` release.

Create an environment and install the application dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Start the canonical application:

```bash
cd ../tcia-cohort-builder
streamlit run tcia-cohort-builder.py
```

Install the stable V2 research profile from the adjacent
`tcia-query-skill` checkout before starting Streamlit:

```bash
cd ../tcia-query-skill
python3 scripts/tcia_v2_bundle.py install \
  --tag tcia-metadata-v2-latest \
  --profile research_core
```

The app reads the bundle manifest and official install receipt from
`../tcia-query-skill/cache/tcia-metadata-v2-latest/`. It supports the stable V2
`full` and `streamlined` contracts (bundle schema 2), Participant Inventory
schema 6, and Snapshot schema 7. Unsupported or incomplete installations fail
with an explicit install command. Downloads, hashing, SQLite integrity checks,
and atomic replacement remain the responsibility of the official bundle
installer instead of running during a Streamlit rerun.

Public non-DICOM, controlled-access, and full clinical details are in the
optional `research_detail` profile. Install them outside the app when needed:

```bash
cd ../tcia-query-skill
python3 scripts/tcia_v2_bundle.py install \
  --tag tcia-metadata-v2-latest \
  --profile research_detail
```

The consumer requires public non-DICOM schema 7, controlled-access schema 2,
and clinical schema 17. It does not read preview caches or
`tcia-snapshot-latest` compatibility caches during V2 integration.

The compact Participant Inventory reports clinical availability but does not
embed accepted clinical fact rows. Loading the clinical research-detail
component enables diagnosis, site, sex-at-birth, and vital-status filters,
age-at-treatment summaries, and longitudinal imaging context. Loading public
imaging detail exposes selected file-grain acquisition and sequence metadata,
direct viewer/access locations, coverage summaries, and review notes. Verbose
evidence remains in optional audit companions and is not read by the public
app.

Set `TCIA_V2_INSTALL_DIR` to share one official bundle installation with the
MCP and REST services. `TCIA_METADATA_V2_CACHE` is the Streamlit-specific
override when it needs a different installation. Set
`TCIA_METADATA_V2_RELEASE_TAG` to test a different compatible stable tag, and
`TCIA_IDC_METADATA_PARQUET` to override the local IDC detail index.

## Data refresh

`idc_metadata.parquet` is retained in this repository for public DICOM
drill-down, viewer routing, and manifest export. It is not used to build the
participant search index. Refresh it with:

```bash
python fetch_data.py
```

The refresh writes to a temporary Parquet file and replaces the current index
only after the complete IDC export succeeds. The daily GitHub Actions workflow
uses the same command.

## Alpha server update

After revised code and matching SQLite release assets have been published, the
Ubuntu deployment can be updated in place with:

```bash
cd /home/exouser/tcia-cohort-builder
./scripts/update_server.sh
```

The server procedure must install the same stable bundle profiles used here and
validate the Streamlit, MCP, and REST V2 surfaces after restart. Review
`scripts/update_server.sh` against the locally tested release contract before
running it; the deployment-specific upgrade is intentionally a separate step.

## Tests

```bash
python -m unittest discover -s tests -v
```

The participant count follows the V2 canonical participant search contract
after application filters. Identity remains dataset-scoped by default. Missing participant
crosswalks and link issues remain visible as coverage states and never prove
that a dataset lacks the corresponding data.

## Branding

The interface uses TCIA's published logo and color palette as documented at
[cancerimagingarchive.net/branding](https://www.cancerimagingarchive.net/branding/).
