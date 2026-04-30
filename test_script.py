import pandas as pd
from tcia_utils import wordpress
from idc_index import IDCClient
import requests

def test_data():
    print("Testing data fetching...")
    try:
        # WP
        url = "https://cancerimagingarchive.net/api/v2/collections?per_page=1&v=1"
        r = requests.get(url)
        r.raise_for_status()
        print("WP V2 Success")

        # IDC
        client = IDCClient()
        df = client.sql_query("SELECT collection_id FROM index LIMIT 1")
        print("IDC Success")

        # Clinical
        df_clin = pd.read_excel("https://github.com/kirbyju/tcia-cohort-builder/raw/refs/heads/main/crdc-clinical.xlsx")
        print("Clinical Load Success")
    except Exception as e:
        print(f"Test failed: {e}")

if __name__ == "__main__":
    test_data()
