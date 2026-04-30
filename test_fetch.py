import pandas as pd
from tcia_utils import wordpress
from idc_index import IDCClient

def test():
    # 1. Test WordPress fetching
    print("Fetching WordPress collections...")
    collections = wordpress.getCollections(format="df")
    print(f"Found {len(collections)} collections.")
    license_cols = ['collection_short_title', 'collection_page_accessibility', 'hide_from_browse_table']
    # Check if license fields exist as described in skill
    # Skill says: license_status, licenses, controlled_access, etc.
    # Let's see what's actually there.
    print("Available columns in collections:", collections.columns.tolist())

    # 2. Test IDC fetching for a few patients
    print("\nFetching IDC metadata...")
    client = IDCClient()
    # Let's try some known TCGA patients if possible, or just a generic query
    query = "SELECT collection_id, PatientID, StudyInstanceUID, StudyDate, SeriesInstanceUID, Modality FROM index WHERE collection_id = 'tcga_brca' LIMIT 5"
    res = client.sql_query(query)
    print("IDC Query Result:")
    print(res)

if __name__ == "__main__":
    test()
