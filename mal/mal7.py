import subprocess
import csv
import json
import os
import getpass
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from collections import defaultdict
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

def oc_login_and_save_kubeconfig(cluster, username, password):
    os.makedirs(KUBECONFIG_DIR, exist_ok=True)
    print(f"[INFO] Logging into cluster: {cluster}")
    
    login_cmd = f"oc login {cluster} -u {username} -p {password} --insecure-skip-tls-verify=true"
    result = run_command(login_cmd)
    
    if result and "Logged into" in result:
        dest_kubeconfig = os.path.join(KUBECONFIG_DIR, f"kubeconfig_{cluster.replace('https://', '').replace(':', '_').replace('/', '_')}")
        shutil.copyfile(os.path.expanduser("~/.kube/config"), dest_kubeconfig)
        print(f"[INFO] Saved kubeconfig to: {dest_kubeconfig}")
        return dest_kubeconfig
    else:
        print(f"[ERROR] Login failed for cluster: {cluster}")
        return None

def should_ignore_namespace(ns_name):
    return ns_name.startswith(IGNORE_PATTERNS)

def extract_namespace_info(kubeconfig_path):
    data = {}
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig_path

    output = run_command("oc get ns -o json", env=env)
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
        print(f"[ERROR] Failed to parse namespace info for {kubeconfig_path}: {e}")

    return data

def merge_results(results):
    final_data = defaultdict(set)
    for cluster_data in results:
        for tia, emails in cluster_data.items():
            final_data[tia].update(emails)
    return final_data

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

    with open(CLUSTERS_FILE) as f:
        clusters = [line.strip() for line in f if line.strip()]

    kubeconfig_files = []
    for cluster in clusters:
        kubeconfig = oc_login_and_save_kubeconfig(cluster, username, password)
        if kubeconfig:
            kubeconfig_files.append(kubeconfig)

    if not kubeconfig_files:
        print("[ERROR] No kubeconfigs generated. Exiting.")
        return

    results = []
    print(f"[INFO] Starting parallel data extraction from {len(kubeconfig_files)} clusters...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_kube = {
            executor.submit(extract_namespace_info, kc): kc for kc in kubeconfig_files
        }
        for future in as_completed(future_to_kube):
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:
                print(f"[ERROR] Exception during data extraction: {exc}")

    merged = merge_results(results)
    if merged:
        write_csv(merged)
        cleanup_old_csvs()
    else:
        print("[INFO] No TIA or maintainer data found.")

    cleanup_kubeconfigs()

if __name__ == "__main__":
    main()

