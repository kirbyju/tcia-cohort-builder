import pandas as pd
import requests
from idc_index import IDCClient
import os
import json
import urllib.request

def fetch_wp_metadata():
    print("Fetching WordPress metadata...")
    def fetch_v2(endpoint):
        base_url = f"https://cancerimagingarchive.net/api/v2/{endpoint}?per_page=100&v=1"
        all_results = []
        try:
            r = requests.get(base_url, timeout=60)
            r.raise_for_status()
            data = r.json()
            all_results.extend(data.get('results', []))
            total_pages = int(data.get('total_pages', 1))
            if total_pages > 1:
                for p in range(2, total_pages + 1):
                    print(f"Fetching {endpoint} page {p} of {total_pages}...")
                    r = requests.get(f"{base_url}&page={p}", timeout=60)
                    r.raise_for_status()
                    all_results.extend(r.json().get('results', []))
        except Exception as e:
            print(f"Error fetching {endpoint}: {e}")
        return all_results

    c_v2 = fetch_v2('collections')
    a_v2 = fetch_v2('analysis-results')

    wp_data = []

    def process_v2(results, is_collection=True):
        short_title_key = 'collection_short_title' if is_collection else 'result_short_title'
        doi_key = 'collection_doi' if is_collection else 'result_doi'
        downloads_key = 'collection_downloads' if is_collection else 'result_downloads'

        for item in results:
            short_title = str(item.get(short_title_key, ""))
            if not short_title or short_title == "nan":
                continue

            downloads = item.get(downloads_key, [])
            is_controlled = False
            licenses = []
            if isinstance(downloads, list):
                for d in downloads:
                    if isinstance(d, dict):
                        l_info = d.get('license', {})
                        l_label = str(l_info.get('label', '') if isinstance(l_info, dict) else l_info).lower()
                        licenses.append(l_label)
                        if any(term in l_label for term in ['controlled', 'restricted', 'limited', 'usage agreement', 'dbgap']):
                            is_controlled = True

            if not licenses:
                access = str(item.get('collection_page_accessibility', '')).lower()
                if any(term in access for term in ['controlled', 'restricted', 'limited']):
                    is_controlled = True

            wp_data.append({
                'short_title': short_title,
                'is_controlled': is_controlled,
                'link': item.get('url', ''),
                'doi': item.get(doi_key, ''),
                'species': str(item.get('species', '')),
                'cancer_types': str(item.get('cancer_types', '')),
                'licenses': "; ".join(licenses)
            })

    process_v2(c_v2, True)
    process_v2(a_v2, False)

    df = pd.DataFrame(wp_data)
    df.to_parquet('wp_metadata.parquet')
    print(f"Saved {len(df)} WP records.")

def fetch_idc_metadata():
    print("Fetching IDC metadata...")
    # Load clinical data to get projects
    df_clin = pd.read_excel("https://github.com/kirbyju/tcia-cohort-builder/raw/refs/heads/main/crdc-clinical.xlsx")
    projects = df_clin['Project Short Name'].unique()
    idc_collections = [p.lower().replace('-', '_') for p in projects]

    client = IDCClient()
    query = f"""
    SELECT
        collection_id,
        PatientID,
        StudyInstanceUID,
        StudyDate,
        StudyDescription,
        BodyPartExamined,
        SeriesInstanceUID,
        Modality,
        SeriesDescription,
        instanceCount,
        series_size_MB
    FROM index
    WHERE collection_id IN ({','.join([repr(c) for c in idc_collections])})
    """
    df = client.sql_query(query)
    if df is not None and not df.empty:
        df.to_parquet('idc_metadata.parquet')
        print(f"Saved {len(df)} IDC records.")
    else:
        print("No IDC records found.")

def fetch_gc_metadata():
    print("Fetching General Commons metadata...")
    endpoint = "https://general.datacommons.cancer.gov/v1/graphql/"
    phs = "phs004225"
    query = """
    query TCIAStudies($phs: [String], $first: Int) {
      studies(phs_accessions: $phs, first: $first) {
        study_acronym
      }
    }
    """
    payload = json.dumps({"query": query, "variables": {"phs": ["phs004225"], "first": 10000}}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "tcia-query-skill/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
            studies = result.get("data", {}).get("studies", [])
            df = pd.DataFrame(studies)
            if not df.empty:
                df.to_parquet('gc_metadata.parquet')
                print(f"Saved {len(df)} GC study acronyms.")
            else:
                print("No GC studies found.")
    except Exception as e:
        print(f"Error fetching GC metadata: {e}")

if __name__ == "__main__":
    fetch_wp_metadata()
    fetch_idc_metadata()
    fetch_gc_metadata()
