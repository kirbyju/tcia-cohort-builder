# TCIA Participant Explorer

The TCIA Participant Explorer is a patient-level Streamlit interface for
finding TCIA data, reviewing source-aware clinical metadata, inspecting imaging
and supporting files, and preparing retrieval manifests.

## Capabilities

- one row per dataset-scoped patient;
- dataset-scoped Collection and Analysis Result participants kept distinct;
- basic and advanced cohort filters with a source-aware patient detail panel;
- separate data-category, data-type, and file-format facets aligned with the
  TCIA WordPress label hierarchy, with access kept independent;
- V2 Participant Inventory search and summaries for public DICOM, public
  non-DICOM, controlled-access, and clinical availability;
- separate IDC series and Aspera-only public DICOM holdings;
- on-demand IDC, public non-DICOM, controlled-access, and clinical detail;
- opt-in clinical cohort filters, longitudinal imaging context, and schema-7/8
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
- a local `tcia-query-skill` checkout from which the app can install the
  published `tcia-metadata-v2-latest` release.

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

The app automatically installs the stable `research_detail` profile from the
adjacent `tcia-query-skill` checkout during its first execution in each server
process. That profile includes `research_core`, so one installer call prepares
participant search plus clinical, controlled-access, and public non-DICOM
detail. End users do not need to trigger downloads from the interface.

To prepare the same installation before starting Streamlit manually, run:

```bash
cd ../tcia-query-skill
python3 scripts/tcia_v2_bundle.py install \
  --tag tcia-metadata-v2-latest \
  --profile research_detail
```

The app reads the bundle manifest and official install receipt from
`../tcia-query-skill/cache/tcia-metadata-v2-latest/`. It supports the stable V2
`full` and `streamlined` contracts (bundle schema 2), Participant Inventory
schemas 6 through 8, and Snapshot schema 7. Unsupported or incomplete installations
fail with an explicit operator error. Downloads, hashing, SQLite integrity
checks, and atomic replacement remain the responsibility of the official
query-skill bundle installer. The startup call is cached once per Streamlit
server process rather than repeated on widget reruns or for each browser
session.

The consumer accepts public non-DICOM schemas 7 and 8, controlled-access schema
2, and clinical schemas 17-18. It does not read preview caches or
`tcia-snapshot-latest` compatibility caches during V2 integration.

The compact Participant Inventory reports clinical availability but does not
embed accepted clinical fact rows. Loading the clinical research-detail
component enables diagnosis, site, sex-at-birth, and vital-status filters,
age-at-treatment summaries, and longitudinal imaging context. Loading public
imaging detail exposes selected file-grain acquisition and sequence metadata,
direct viewer/access locations, coverage summaries, and review notes. Verbose
evidence remains in optional audit companions and is not read by the public
app.

Participant Inventory schemas 7 and 8 provide geometry status summaries. IDC DICOM
statuses come from idc-index's separate volume geometry index; eligible
non-IDC DICOM and single-file volume assets remain `not_checked` until an
external batch result is imported. The app does not infer geometric coherence
from file format or modality.

Data category, data type, file format, and image geometry qualify participants
through same-asset facet matching. The `Imaging & download contents` control
then chooses whether Imaging & Files and cohort manifests contain all linked
imaging for those participants or only series/files matching the imaging
facets. Dataset-package-only rows remain in the unrouted inventory and include
the published package URL; they are not represented as individually routable
TCIA Data Retriever files.

Set `TCIA_V2_INSTALL_DIR` to share one official bundle installation with the
MCP and REST services. `TCIA_METADATA_V2_CACHE` is the Streamlit-specific
override when it needs a different installation. Set `TCIA_QUERY_SKILL_ROOT`
when the query-skill checkout is not adjacent to this repository. Set
`TCIA_METADATA_V2_RELEASE_TAG` to test a different compatible stable tag, and
`TCIA_IDC_METADATA_PARQUET` to override the local IDC detail index.

## Data refresh

`idc_metadata.parquet` is retained in this repository for public DICOM
drill-down, viewer routing, geometry filtering, and manifest export. The
refresh joins IDC's `volume_geometry_index` to the series index by
`SeriesInstanceUID`. It does not replace Participant Inventory as the identity
or participant-search authority. Refresh it with:

```bash
python fetch_data.py
```

The refresh writes to a temporary Parquet file and replaces the current index
only after the complete IDC export succeeds. The daily GitHub Actions workflow
uses the same command.

## Alpha server update

After revised code and matching SQLite release assets have been published,
fast-forward both server checkouts and install the stable `research_detail`
profile into a new, versioned directory with the query-skill bundle installer.
Point Streamlit, MCP, and REST at that same installation, restart all three
services, and validate their V2 surfaces before removing the previous bundle.

The exact service units, environment files, reverse-proxy configuration, and
storage paths are deployment-specific. Follow the query-skill
[deployment guide](https://github.com/kirbyju/tcia-query-skill/blob/main/mcp_server/DEPLOYMENT.md)
for the supported variables, ports, installer, and smoke tests rather than an
unchecked host-specific update script.

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
