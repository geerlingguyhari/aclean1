import csv
import json
import os
import subprocess
import tempfile
import requests
from datetime import datetime
import concurrent.futures

def fetch_tia_emails_from_api():
    url = "https://abc.kit.com/v1/applications"
    token = "zbaxabc"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        tia_email_mapping = {}

        for entry in data.get("applications", []):
            tia = str(entry.get("tia_number", "")).strip()
            if not tia:
                continue

            emails = set()
            for key in ["software_owner_email", "management_contact_email", "support_owner_email"]:
                email = entry.get(key)
                if email:
                    emails.update(map(str.strip, email.split(",")))

            if emails:
                tia_email_mapping[tia] = list(emails)

        return tia_email_mapping

    except Exception as e:
        print(f"Error fetching TIA emails from API: {e}")
        return {}

def merge_tia_emails(output_csv, tia_email_mapping):
    output_rows = []
    existing_tias = set()

    if os.path.exists(output_csv):
        with open(output_csv, "r", newline="") as infile:
            reader = csv.DictReader(infile)
            for row in reader:
                tia_number = row.get("Folder Name", "").strip()
                if tia_number in tia_email_mapping:
                    existing_emails = set(map(str.strip, row.get("Email Address", "").split(",")))
                    new_emails = set(tia_email_mapping.pop(tia_number, []))
                    combined_emails = existing_emails.union(new_emails)
                    row["Email Address"] = ", ".join(sorted(combined_emails))
                output_rows.append(row)
                existing_tias.add(tia_number)

    # Add remaining TIA entries not present in output_csv
    for tia, emails in tia_email_mapping.items():
        output_rows.append({"Folder Name": tia, "Email Address": ", ".join(sorted(emails))})

    # Write back to CSV
    with open(output_csv, "w", newline="") as outfile:
        fieldnames = ["Folder Name", "Email Address"]
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)


def get_namespaces(kubeconfig_path):
    output = subprocess.check_output([
        "oc", "get", "namespaces", "-o", "json", "--kubeconfig", kubeconfig_path
    ], text=True)
    return json.loads(output)["items"]


def should_exclude(ns_name):
    return ns_name.startswith("openshift") or ns_name.startswith("kube") or ns_name in ["default", "kubernetes"]


def extract_tia(labels):
    return labels.get("tia", "").strip()


def extract_maintainer(annotations):
    return annotations.get("abc.com/maintainer", "").strip()


def login_to_cluster(cluster, username, password, kubeconfig_path):
    subprocess.run([
        "oc", "login", cluster, "-u", username, "-p", password, "--kubeconfig", kubeconfig_path
    ], check=True, capture_output=True, text=True)


def read_clusters(file_path):
    with open(file_path, "r") as f:
        return [line.strip() for line in f if line.strip()]


def write_csv(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="") as outfile:
        fieldnames = ["Folder Name", "Email Address"]
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)


def process_clusters(clusters_file, output_csv):
    def process_single_cluster(cluster):
        temp_dir = tempfile.mkdtemp(prefix="kubeconfig_")
        kubeconfig_path = os.path.join(temp_dir, "config")
        rows = []

        try:
            login_to_cluster(cluster, "<USERNAME>", "<PASSWORD>", kubeconfig_path)

            for ns in get_namespaces(kubeconfig_path):
                ns_name = ns.get("metadata", {}).get("name", "")
                if should_exclude(ns_name):
                    continue

                tia = extract_tia(ns.get("metadata", {}).get("labels", {}))
                maintainer = extract_maintainer(ns.get("metadata", {}).get("annotations", {}))

                if tia and maintainer:
                    rows.append({"Folder Name": tia, "Email Address": maintainer})

        except Exception as e:
            print(f"Error processing cluster {cluster}: {e}")

        finally:
            subprocess.run(["oc", "logout", "--kubeconfig", kubeconfig_path], check=False)

        return rows

    all_rows = []
    clusters = read_clusters(clusters_file)

    with concurrent.futures.ProcessPoolExecutor() as executor:
        futures = {executor.submit(process_single_cluster, cluster): cluster for cluster in clusters}
        for future in concurrent.futures.as_completed(futures):
            try:
                all_rows.extend(future.result())
            except Exception as e:
                print(f"Error during cluster processing: {e}")

    write_csv(output_csv, all_rows)


def main():
    clusters_file = "clusters.txt"
    output_csv = "output/output.csv"

    process_clusters(clusters_file, output_csv)

    tia_email_mapping = fetch_tia_emails_from_api()
    merge_tia_emails(output_csv, tia_email_mapping)


if __name__ == "__main__":
    main()
