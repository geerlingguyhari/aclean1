import subprocess
import csv
import json
import os
import getpass
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime
import shutil
import glob

CLUSTERS_FILE = "clusters.txt"
KUBECONFIG_DIR = "/tmp/abc"
CSV_PREFIX = "tia_maintainers_"
CSV_SUFFIX = ".csv"
KEEP_LATEST = 10
MAX_WORKERS = 5

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

def oc_login_to_generate_kubeconfig(cluster, username, password, kubeconfig_path):
    login_cmd = f"oc login {cluster} -u {username} -p {password} --insecure-skip-tls-verify=true --kubeconfig={kubeconfig_path}"
    print(f"[INFO] Generating kubeconfig for cluster: {cluster}")
    result = run_command(login_cmd)
    if os.path.exists(kubeconfig_path):
        return True
    else:
        print(f"[ERROR] Failed to create kubeconfig for cluster: {cluster}")
        return False

def should_ignore_namespace(ns_name):
    return ns_name.startswith(IGNORE_PATTERNS)

def extract_namespace_info(kubeconfig_path):
    data = {}
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig_path

    output = run_command("oc get ns -o json", env=env)
    if not output:
        print(f"[WARN] No namespace output for kubeconfig: {kubeconfig_path}")
        return data

    try:
        namespaces = json.loads(output).get("items", [])
        print(f"[INFO] {len(namespaces)} namespaces fetched from {os.path.basename(kubeconfig_path)}")

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
        print(f"[ERROR] Failed to parse namespace info for kubeconfig {kubeconfig_path}: {e}")

    return data

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

def cleanup_kubeconfigs():
    if os.path.exists(KUBECONFIG_DIR):
        shutil.rmtree(KUBECONFIG_DIR)
        print(f"[CLEANUP] Removed kubeconfig directory: {KUBECONFIG_DIR}")

def generate_all_kubeconfigs(clusters, username, password):
    os.makedirs(KUBECONFIG_DIR, exist_ok=True)
    kubeconfigs = []
    for cluster in clusters:
        kubeconfig_path = os.path.join(KUBECONFIG_DIR, f"kubeconfig_{os.path.basename(cluster)}")
        success = oc_login_to_generate_kubeconfig(cluster, username, password, kubeconfig_path)
        if success:
            kubeconfigs.append(kubeconfig_path)
    return kubeconfigs

def main():
    if not os.path.exists(CLUSTERS_FILE):
        print(f"[ERROR] '{CLUSTERS_FILE}' not found.")
        return

    username = input("Enter OpenShift Username: ").strip()
    password = getpass.getpass("Enter OpenShift Password: ").strip()

    with open(CLUSTERS_FILE) as f:
        clusters = [line.strip() for line in f if line.strip()]

    print(f"[INFO] Generating kubeconfig files for {len(clusters)} clusters...")
    kubeconfig_files = generate_all_kubeconfigs(clusters, username, password)

    if not kubeconfig_files:
        print("[ERROR] No kubeconfig files were generated successfully. Exiting.")
        return

    print(f"[INFO] Starting parallel data extraction from {len(kubeconfig_files)} clusters...")
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_config = {
            executor.submit(extract_namespace_info, kubeconfig_path): kubeconfig_path for kubeconfig_path in kubeconfig_files
        }
        for future in as_completed(future_to_config):
            kubeconfig_path = future_to_config[future]
            try:
                result = future.result()
                results.append(result)
                print(f"[INFO] Completed processing for kubeconfig: {os.path.basename(kubeconfig_path)}")
            except Exception as exc:
                print(f"[ERROR] Kubeconfig {kubeconfig_path} generated an exception: {exc}")

    merged_data = merge_results(results)

    if merged_data:
        write_csv(merged_data, append=False)
        cleanup_old_csvs(CSV_PREFIX, CSV_SUFFIX, KEEP_LATEST)
    else:
        print("[INFO] No data to write to CSV.")

    cleanup_kubeconfigs()

if __name__ == "__main__":
    main()

