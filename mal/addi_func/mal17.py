import subprocess
import json
import csv
import os
import re
import time
from getpass import getpass

# Configuration
CLUSTERS_FILE = 'clusters.txt'
XYZABC_FILE = 'xyzabc.csv'
OUTPUT_CSV = 'output.csv'
EXCLUDE_PREFIXES = (
    'openshift-', 'kube', 'vault', 'validate', 'open-cluster', 'cert-', 'demo',
    'lunks-toel', 'abclog', 'badard', 'mit', 'default', 'ab-sandbox',
    'ab-pmp', 'ab-admin', 'litmuz'
)
MAINTAINER_ANNOTATION = 'abc.com/maintainer'

def run_cmd(command):
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {command}\n{result.stderr}")
    return result.stdout

def get_namespaces():
    output = run_cmd("oc get ns -ojson")
    return json.loads(output).get('items', [])

def should_exclude(ns_name):
    return any(ns_name.startswith(prefix) for prefix in EXCLUDE_PREFIXES)

def extract_tia(labels):
    return labels.get('tia')

def extract_maintainer(annotations):
    return annotations.get(MAINTAINER_ANNOTATION)

def login_to_cluster(cluster, username, password):
    print(f"Logging into cluster: {cluster}")
    run_cmd(f"oc logout || true")  # ignore errors if already logged out
    run_cmd(f"oc login -u {username} -p {password} {cluster}")

def read_clusters(file_path):
    with open(file_path) as f:
        return [line.strip() for line in f if line.strip()]

def load_xyzabc_csv(filepath):
    tia_to_app_manager = {}
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                tia_number = row.get('TIA_NUMBER')
                app_manager = row.get('APP_MANAGER')
                if tia_number and app_manager:
                    if tia_number not in tia_to_app_manager:
                        tia_to_app_manager[tia_number] = set()
                    tia_to_app_manager[tia_number].add(app_manager.strip())
    return tia_to_app_manager

def write_csv(filepath, data):
    with open(filepath, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Folder Name', 'Email Addresses'])
        for tia, emails in data.items():
            writer.writerow([tia, ','.join(sorted(emails))])

def main():
    username = input("OpenShift Username: ")
    password = getpass("OpenShift Password: ")

    clusters = read_clusters(CLUSTERS_FILE)
    tia_to_emails = {}

    # Process OpenShift clusters first
    for cluster in clusters:
        try:
            login_to_cluster(cluster, username, password)
            namespaces = get_namespaces()

            for ns in namespaces:
                ns_name = ns.get('metadata', {}).get('name', '')
                if should_exclude(ns_name):
                    continue

                labels = ns.get('metadata', {}).get('labels', {})
                annotations = ns.get('metadata', {}).get('annotations', {})

                tia = extract_tia(labels)
                maintainer = extract_maintainer(annotations)

                if tia and maintainer:
                    if tia not in tia_to_emails:
                        tia_to_emails[tia] = set()
                    tia_to_emails[tia].add(maintainer)

        except Exception as e:
            print(f"Error processing cluster {cluster}: {e}")

        print(f"Waiting 5 minutes before next cluster...")
        time.sleep(300)  # wait 5 minutes before next cluster

    # Process xyzabc.csv
    xyzabc_tias = load_xyzabc_csv(XYZABC_FILE)

    # Merge xyzabc emails into tia_to_emails
    for tia, app_managers in xyzabc_tias.items():
        if tia not in tia_to_emails:
            tia_to_emails[tia] = set()
        tia_to_emails[tia].update(app_managers)

    # Write final output
    write_csv(OUTPUT_CSV, tia_to_emails)
    print(f"CSV saved at: {OUTPUT_CSV}")

if __name__ == "__main__":
    main()

