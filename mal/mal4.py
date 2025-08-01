import subprocess
import csv
import json
import os
import getpass
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime
import tempfile
import shutil
import glob

CLUSTERS_FILE = "clusters.txt"
CSV_PREFIX = "tia_maintainers_"
CSV_SUFFIX = ".csv"
KEEP_LATEST = 10
MAX_WORKERS = 5
KUBECONFIG_DIR = "/tmp/abc"

IGNORE_PATTERNS = (
    "openshift-", "kube", "vault", "validate", "open-cluster",
    "cert-", "demo", "lunks-toel", "abclog", "badard", "mit",
    "default", "ab-sandbox", "ab-pmp", "ab-admin", "litmuz"
)

ANNOTATION_KEY = "abc.com/maintainer"

def run_command(command, env=None):
    try:
        result = subprocess.run(command, shell=True, check=True, capture_output=True, text=True, env=env)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Command failed: {e.stderr.strip()}")
        return None

def oc_login(cluster, username, password, kubeconfig_path):
    login_cmd = f"oc login {cluster} -u {username} -p {password} --insecure-skip-tls-verify=true --kubeconfig={kubeconfig_path}"
    print(f"[INFO] Logging in to cluster: {cluster}")
    return run_command(login_cmd)

def should_ignore_namespace(ns_name):
    return ns_name.startswith(IGNORE_PATTERNS)

def extract_namespace_info(cluster, kubeconfig_path):
    data = {}
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig_path

    output = run_command("oc get ns -o json", env=env)
    if not output:
        print(f"[WARN] No namespace output for {cluster}")
        return data

    try:
        namespaces = json.loads(output).get("items", [])
        print(f"[INFO] {len(namespaces)} namespaces fetched from {cluster}")

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
    kubeconfig_path = os.path.join(KUBECONFIG_DIR, f"kubeconfig_{os.path.basename(cluster)}")
    if oc_login(cluster, username, password, kubeconfig_path):
        data = extract_namespace_info(cluster, kubeconfig_path)
    else:
        print(f"[ERROR] Skipping cluster {cluster} due to login failure.")
        data = {}

    return data, kubeconfig_path

def merge_results(results):
    final_data = defaultdict(set)
    for cluster_data, _ in results:
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

def cleanup_kubeconfigs():
    if os.path.exists(KUBECONFIG_DIR):
        shutil.rmtree(KUBECONFIG_DIR)
        print(f"[CLEANUP] Removed kubeconfig directory: {KUBECONFIG_DIR}")

def main():
    if not os.path.exists(CLUSTERS_FILE):
        print(f"[ERROR] '{CLUSTERS_FILE}' not found.")
        return

    username = input("Enter OpenShift Username: ").strip()
    password = getpass.getpass("Enter OpenShift Password: ").strip()

    os.makedirs(KUBECONFIG_DIR, exist_ok=True)

    with open(CLUSTERS_FILE) as f:
        clusters = [line.strip() for line in f if line.strip()]

    results = []
    kubeconfig_paths = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_cluster = {
            executor.submit(process_cluster, cluster, username, password): cluster
            for cluster in clusters
        }
        for future in as_completed(future_to_cluster):
            cluster = future_to_cluster[future]
            try:
                result, kubeconfig_path = future.result()
                results.append((result, kubeconfig_path))
                print(f"[INFO] Completed processing for cluster: {cluster}")
            except Exception as exc:
                print(f"[ERROR] Cluster {cluster} generated an exception: {exc}")

    merged_data = merge_results(results)

    if merged_data:
        write_csv(merged_data, append=False)
        cleanup_old_csvs(CSV_PREFIX, CSV_SUFFIX, KEEP_LATEST)
    else:
        print("[INFO] No data to write to CSV.")

    cleanup_kubeconfigs()

if __name__ == "__main__":
    main()

