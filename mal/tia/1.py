import csv
import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import os

# Setup error logging
logging.basicConfig(
    filename='errors.log',
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Allowed roles
FILTER_ROLES = {
    "Technical Executive Contact",
    "Second Level Production Support Contact",
    "Application Manager",
    "Line of Business Primary Contact",
    "Management Support Contact",
    "Application Admin Contact"
}

TIA_CSV = 'tia.csv'
MAINTAINER_CSV = 'tia_maintainers.csv'

# Read TIA list
def read_tias(csv_path):
    with open(csv_path, newline='') as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        return [row[0].strip() for row in reader if row]

# Extract emails from XML
def extract_contact_info(tia, xml_content):
    try:
        root = ET.fromstring(xml_content)
        seen_emails = set()
        emails = []
        for contact in root.findall(".//{*}ContactsResponse"):
            role = contact.find('{*}role')
            email_elem = contact.find('{*}workEmail')
            if role is not None and role.text in FILTER_ROLES and email_elem is not None:
                email = email_elem.text.strip()
                if email and email not in seen_emails:
                    seen_emails.add(email)
                    emails.append(email)
        return tia, emails
    except ET.ParseError as e:
        logging.error(f"TIA {tia} XML parsing error: {str(e)}")
        return tia, []

# API fetch logic with error handling
def fetch_contact_for_tia(tia, headers):
    url = f"https://abc.xyz.com/v1/Applications/{tia}/contacts"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return extract_contact_info(tia, response.content)
        else:
            logging.error(f"TIA {tia} failed with status {response.status_code}: {response.reason}")
            return tia, None  # None = failed
    except requests.exceptions.RequestException as e:
        logging.error(f"TIA {tia} request exception: {str(e)}")
        return tia, None

# Load current maintainer CSV
def load_existing_maintainers(path):
    data = {}
    if os.path.exists(path):
        with open(path, newline='') as csvfile:
            reader = csv.reader(csvfile)
            for row in reader:
                if row and len(row) >= 2:
                    tia = row[0].strip()
                    emails = [e.strip() for e in row[1].split(',') if e.strip()]
                    data[tia] = set(emails)
    return data

# Save updated results
def save_maintainers_csv(data, output_path):
    with open(output_path, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        for tia, emails in sorted(data.items()):
            writer.writerow([tia, ', '.join(sorted(emails))])

# Main
def main():
    token = "abcdefghijklmnop"
    headers = {
        "toolkit-token": token
    }

    tia_list = read_tias(TIA_CSV)
    existing_data = load_existing_maintainers(MAINTAINER_CSV)

    total = len(tia_list)
    success = 0
    no_contacts = 0
    failures = 0

    print(f"🔄 Processing {total} TIAs...")

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_tia = {executor.submit(fetch_contact_for_tia, tia, headers): tia for tia in tia_list}
        for future in as_completed(future_to_tia):
            tia = future_to_tia[future]
            try:
                tia, emails = future.result()
                if emails is None:
                    failures += 1
                elif emails:
                    success += 1
                    if tia not in existing_data:
                        existing_data[tia] = set()
                    existing_data[tia].update(emails)
                else:
                    no_contacts += 1
            except Exception as e:
                logging.error(f"Unexpected error for TIA {tia}: {str(e)}")
                failures += 1

    save_maintainers_csv(existing_data, MAINTAINER_CSV)

    print("\n✅ Done! Summary:")
    print(f"🟢 Success (emails found):     {success}")
    print(f"🟡 No matching contacts:       {no_contacts}")
    print(f"🔴 Failed API responses:       {failures}")
    print(f"📄 Updated file:               {MAINTAINER_CSV}")
    print("📝 See errors.log for details if any failures occurred.")

if __name__ == "__main__":
    main()
