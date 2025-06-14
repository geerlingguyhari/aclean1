import subprocess
import csv
import json
import os
import getpass
from datetime import datetime
import glob
from collections import defaultdict

CLUSTERS_FILE = "clusters.txt"
CSV_PREFIX = "tia_maintainers_"
CSV_SUFFIX = ".csv"
KEEP_LATEST = 10
LOGIN_TIMEOUT = 300  # 5 minutes for oc login
COLLECT_TIMEOUT = 300  # 5 minutes for data collection
MAX_RETRIES = 3

IGNORE_PATTERNS = (
    "openshift-", "kube", "vault", "validate", "open-cluster",
    "cert-", "demo", "lunks-toel", "abclog", "badard", "mit",
    "default", "ab-sandbox", "ab-pmp", "ab-admin", "litmuz"
)

ANNOTATION_KEY = "abc.com/maintainer"


def run_command(command, timeout=None):
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            print(f"[ERROR] Command failed: {result.stderr.strip()}")
            return None
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print(f"[ERROR] Command timed out after {timeout} seconds: {command}")
        return None
    except Exception as e:
        print(f"[ERROR] Exception running command '{command}': {e}")
        return None


def should_ignore_namespace(ns_name):
    return ns_name.startswith(IGNORE_PATTERNS)


def login_to_cluster(cluster, username, password):
    print(f"[INFO] Logging into: {cluster}")
    login_cmd = f"oc login {cluster} -u {username} -p {password}"
    try:
        result = subprocess.run(login_cmd, shell=True, capture_output=True, text=True, timeout=LOGIN_TIMEOUT)
        print(result.stdout)
        print(result.stderr)
        if result.returncode == 0 and "Logged into" in result.stdout:
            print(f"[INFO] Successfully logged in to: {cluster}")
            return True
        else:
            print(f"[ERROR] Login failed for cluster: {cluster}\n{result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        print(f"[ERROR] Login timed out after {LOGIN_TIMEOUT // 60} minutes for cluster: {cluster}")
        return False
    except Exception as e:
        print(f"[ERROR] Exception during login to cluster {cluster}: {e}")
        return False


def login_to_cluster_with_retry(cluster, username, password):
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"[INFO] Attempt {attempt} to login to: {cluster}")
        if login_to_cluster(cluster, username, password):
            return True
        print(f"[WARN] Attempt {attempt} failed for: {cluster}")
    print(f"[ERROR] All login attempts failed for: {cluster}")
    return False


def collect_tia_and_maintainer():
    data = {}
    output = run_command("oc get ns -o json", timeout=COLLECT_TIMEOUT)
    if not output:
        return data

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
        print(f"[ERROR] Failed to parse namespace info: {e}")

    return data


def merge_results(existing, new_data):
    for tia, emails in new_data.items():
        existing[tia].update(emails)


def write_csv(data):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"{CSV_PREFIX}{timestamp}{CSV_SUFFIX}"

    with open(filename, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Folder Name", "Email Addresses"])
        for tia, emails in data.items():
            writer.writerow([tia, ",".join(sorted(emails))])

    print(f"[INFO] CSV written to: {filename}")
    return filename


def cleanup_old_csvs():
    files = sorted(glob.glob(f"{CSV_PREFIX}*{CSV_SUFFIX}"), key=os.path.getmtime, reverse=True)
    for old_file in files[KEEP_LATEST:]:
        try:
            os.remove(old_file)
            print(f"[CLEANUP] Removed old CSV: {old_file}")
        except Exception as e:
            print(f"[ERROR] Could not remove file {old_file}: {e}")


def main():
    if not os.path.exists(CLUSTERS_FILE):
        print(f"[ERROR] '{CLUSTERS_FILE}' not found.")
        return

    username = input("Enter OpenShift Username: ").strip()
    password = getpass.getpass("Enter OpenShift Password: ").strip()

    with open(CLUSTERS_FILE) as f:
        clusters = [line.strip() for line in f if line.strip()]

    combined_results = defaultdict(set)

    for cluster in clusters:
        if login_to_cluster_with_retry(cluster, username, password):
            print(f"[INFO] Collecting namespaces for cluster: {cluster} (Timeout: {COLLECT_TIMEOUT//60} min)")
            tia_data = collect_tia_and_maintainer()
            merge_results(combined_results, tia_data)
        else:
            print(f"[WARN] Skipping collection for cluster {cluster} due to login failure.")

    if combined_results:
        write_csv(combined_results)
        cleanup_old_csvs()
    else:
        print("[INFO] No TIA or maintainer data found in any clusters.")

if __name__ == "__main__":
    main()

