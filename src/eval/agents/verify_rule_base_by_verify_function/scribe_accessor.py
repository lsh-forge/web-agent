import json
import os
import sys
import time
from typing import Any, Dict, List
import requests

BASE_URL = "https://scribe-api.scribehow.com/api/workspace/"

TOKEN = os.environ.get("SCRIBE_TOKEN", "")
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Authorization": f"Bearer {TOKEN}"      
}
session = requests.Session()
session.headers.update(headers)

def download_html_and_requests_from_scribe_id(scribe_file_name):
    mc_path = "/opt/homebrew/bin/mc"  # Homebrew安装
    command = f"{mc_path} get oss/mingjing/fty_work/webworld_htmls/{scribe_file_name}.json ./recorded_requests"
    print(command)
    exit_code = os.system(command)

    if exit_code == 0:
        print("Command executed successfully!")
    else:
        print(f"Error Command:{command}")
        print(f"Command failed with exit code: {exit_code}")


def download_actions_and_title_from_scribe_id(scribe_id):
    documents = scrape_all_pages(
            records_per_page=30,
            max_pages=4,  # Adjust as needed
            type="scribe",
            category="scribes",
            sort_by="recently_created",
            global_permission="team"
        )
    
    with open("scribe_documents_list.json", 'w', encoding='utf-8') as f:
            json.dump(documents, f, ensure_ascii=False, indent=2)

    with open("scribe_documents_list.json", "r") as f:
        documents = json.load(f)
    
    for doc in documents:
        if doc["id"] == scribe_id:
            document_id = {
                'id': doc['id'],
                'name': doc['name'],
                'author': doc['author'],
                'description': doc['description'],
                'app_tags': doc['app_tags']
            }
            break
    
    url = f"https://scribe-api.scribehow.com/api/scribe_documents/{document_id.get('id')}/actions"
    params = {
        "skip": 0,
        "limit": 200
    }
    response = session.get(url, params=params)

    if response.status_code != 200:
            raise Exception(f"API request failed with status code {response.status_code}: {response.text}")

    details =  response.json()
    details['author'] = doc.get('author')
    details['title'] = doc.get('name')
    details['description'] = doc.get('description')
    details['app_tags'] = doc.get('app_tags')

    action_file_name = f"osworld_scribes/{scribe_id}.json"
    with open(action_file_name, 'w', encoding='utf-8') as f:
        json.dump(details, f, ensure_ascii=False, indent=2)

def scrape_all_pages(
                    records_per_page: int = 20,
                    max_pages: int = 10,
                    **kwargs) -> List[Dict[str, Any]]:
    all_records = []

    for page in range(max_pages):
        skip = page * records_per_page

        print(f"Scraping page {page + 1} (skip={skip})...")

        response_data = get_scribes(
            get_first=records_per_page,
            skip=skip,
            **kwargs
        )

        # Extract records from the response
        # Adjust this based on the actual structure of the API response
        if 'documents' in response_data:
            records = response_data['documents']
        else:
            records = response_data  # Assuming the response is a list of records

        if not records:
            print("No more records found. Stopping.")
            break

        all_records.extend(records)

        # Be nice to the server
        time.sleep(1)

    return all_records

def get_scribes(get_first: int = 20,
                skip: int = 0,
                type: str = "scribe",
                category: str = "scribes",
                sort_by: str = "recently_created",
                global_permission: str = "team") -> Dict[str, Any]:

    params = {
        "get_first": get_first,
        "skip": skip,
        "type": type,
        "category": category,
        "sort_by": sort_by,
        "global_permission": global_permission
    }

    response = session.get(BASE_URL, params=params)

    if response.status_code != 200:
        raise Exception(f"API request failed with status code {response.status_code}: {response.text}")

    return response.json()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python filter_requests.py <input.json>")
        sys.exit(1)

    scribe_id = sys.argv[1]

    #download_html_and_requests_from_scribe_id(scribe_id)
    download_actions_and_title_from_scribe_id(scribe_id)