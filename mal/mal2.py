import subprocess
import csv
import json
import os
import getpass
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime
import glob
import re

CLUSTERS_FILE = "clusters.txt"
CSV_PREFIX = "tia_maintainers_"
CSV_SUFFIX = ".csv"
KEEP_LATEST = 10
MAX_WORKERS = 5

# Patterns to ignore system namespaces
IGNORE_PATTERNS = (
    "openshift-", "kube", "vault", "validate", "open-cluster",
    "cert-", "demo", "lunks-toel", "abclog", "badard", "mit",
    "default", "ab-sandbox", "ab-pmp", "ab-admin", "litmuz"
)

ANNOTATION_KEY = "abc.com/maintainer"

def run_command(command):
    try:
        result = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Command failed: {e.stderr.strip()}")
        return None

def oc_login(cluster, username, password):
    login_cmd = f"oc login {cluster} -u {username} -p {password} --insecure-skip-tls-verify=true"
    print(f"[INFO] Logging in to cluster: {cluster}")
    output = run_command(login_cmd)
    if output:
        print(f"[INFO] Logged in to {cluster}")
    else:
        print(f"[ERROR] Login failed for {cluster}")

def should_ignore_namespace(ns_name):
    return ns_name.startswith(IGNORE_PATTERNS)

def extract_namespace_info(cluster):
    data = {}
    output = run_command("oc get ns -o json")
    if output:
        try:
            namespaces = json.loads(output).get("items", [])
            for ns in namespaces:
                ns_name = ns.get("metadata", {}).get("name", "")
                if should_ignore_namespace(ns_name):
                    continue

                labels = ns.get("metadata", {}).get("labels", {})
                annotations = ns.get("metadata", {}).get("annotations", {})

                tia = labels.get("tia")
                maintainer = annotations.get(ANNOTATION_KEY)

                if tia and maintainer:
                    data.setdefault(tia.strip(), set()).add(maintainer.strip())
        except Exception as e:
            print(f"[ERROR] Failed to parse namespace info for cluster {cluster}: {e}")
    return data

def process_cluster(cluster, username, password):
    oc_login(cluster, username, password)
    return extract_namespace_info(cluster.strip())

def merge_results(results):
    final_data = defaultdict(set)
    for cluster_data in results:
        for tia, emails in cluster_data.items():
            final_data[tia].update(emails)
    return final_data

def write_csv(data, append=False):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"{CSV_PREFIX}{timestamp}{CSV_SUFFIX}" if not append else "tia_maintainers_log.csv"

    mode = 'a' if append else 'w'
    file_exists = os.path.exists(filename)

    with open(filename, mode, newline="") as csvfile:
        writer = csv.writer(csvfile)
        if not append or (append and not file_exists):
            writer.writerow(["Folder Name", "Email Addresses"])
        for tia, emails in data.items():
            writer.writerow([tia, ",".join(sorted(emails))])

    print(f"[INFO] CSV written to: {filename}")
    return filename

def cleanup_old_csvs(prefix, suffix, keep_latest):
    files = sorted(glob.glob(f"{prefix}*{suffix}"), key=os.path.getmtime, reverse=True)
    for old_file in files[keep_latest:]:
        try:
            os.remove(old_file)
            print(f"[CLEANUP] Removed old CSV: {old_file}")
        except Exception as e:
            print(f"[CLEANUP ERROR] Could not remove file {old_file}: {e}")

def main():
    if not os.path.exists(CLUSTERS_FILE):
        print(f"[ERROR] '{CLUSTERS_FILE}' not found.")
        return

    username = input("Enter OpenShift Username: ").strip()
    password = getpass.getpass("Enter OpenShift Password: ").strip()

    with open(CLUSTERS_FILE) as f:
        clusters = [line.strip() for line in f if line.strip()]

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_cluster = {
            executor.submit(process_cluster, cluster, username, password): cluster
            for cluster in clusters
        }
        for future in as_completed(future_to_cluster):
            cluster = future_to_cluster[future]
            try:
                result = future.result()
                results.append(result)
                print(f"[INFO] Completed processing for cluster: {cluster}")
            except Exception as exc:
                print(f"[ERROR] Cluster {cluster} generated an exception: {exc}")

    merged_data = merge_results(results)

    filename = write_csv(merged_data, append=False)
    cleanup_old_csvs(CSV_PREFIX, CSV_SUFFIX, KEEP_LATEST)

if __name__ == "__main__":
    main()

