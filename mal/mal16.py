import subprocess
import json
import csv
import os
import re
import time
from getpass import getpass

# Configuration
CLUSTERS_FILE = 'clusters.txt'
OUTPUT_CSV = 'tia_maintainers.csv'
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

def load_existing_csv(filepath):
    data = {}
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if len(row) >= 2:
                    tia = row[0]
                    emails = set(row[1].split(','))
                    data[tia] = emails
    return data

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
    result = load_existing_csv(OUTPUT_CSV)

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
                    if tia not in result:
                        result[tia] = set()
                    result[tia].add(maintainer)

        except Exception as e:
            print(f"Error processing cluster {cluster}: {e}")

        print(f"Waiting 5 minutes before next cluster...")
        time.sleep(300)  # wait for 5 minutes before next cluster

    write_csv(OUTPUT_CSV, result)
    print(f"CSV saved at: {OUTPUT_CSV}")

if __name__ == "__main__":
    main()

