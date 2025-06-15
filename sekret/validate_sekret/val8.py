import subprocess
import base64
import binascii
import json
import csv
import os
import tempfile
import shutil
import re
from getpass import getpass
from concurrent.futures import ThreadPoolExecutor, as_completed

OUTPUT_DIR = "output"
OUTPUT_FILE = f"{OUTPUT_DIR}/validated.csv"
DEBUG_FILE = os.path.abspath(f"{OUTPUT_DIR}/debug_mismatches.log")
CLUSTERS_FILE = "clusters.txt"
THREADS_PER_CLUSTER = 10
DEBUG = True

os.makedirs(OUTPUT_DIR, exist_ok=True)
if os.path.exists(DEBUG_FILE):
    os.remove(DEBUG_FILE)

service_id = input("Enter Service ID to validate: ").strip()
password_to_validate = getpass("Enter Password to validate: ").strip()
oc_username = input("Enter OpenShift Username: ").strip()
oc_password = getpass("Enter OpenShift Password: ").strip()

provided_bytes = password_to_validate.encode('utf-8')

def aggressive_normalize(b):
    return re.sub(rb'[\x00-\x20]', b'', b).strip()

normalized_provided_bytes = aggressive_normalize(provided_bytes)

with open(CLUSTERS_FILE, "r") as f:
    clusters = [line.strip() for line in f if line.strip()]

def process_namespace(kubeconfig, cluster_url, namespace):
    results = []
    try:
        result = subprocess.run(
            ["oc", "--kubeconfig", kubeconfig, "get", "secret", "-n", namespace, "-o", "json"],
            capture_output=True, text=True, check=True
        )
        secrets = json.loads(result.stdout)["items"]
    except Exception:
        return results

    for secret in secrets:
        secret_name = secret["metadata"]["name"]
        secret_type = secret.get("type", "N/A")
        data = secret.get("data", {})

        service_id_found = "No"
        password_match = "Not Found"

        for key, encoded_value in data.items():
            try:
                decoded_bytes = base64.b64decode(encoded_value)
            except (binascii.Error, UnicodeDecodeError):
                continue

            try:
                decoded_text = decoded_bytes.decode('utf-8', errors='ignore')
                if service_id.lower() in decoded_text.lower():
                    service_id_found = "Yes"

                    normalized_secret_bytes = aggressive_normalize(decoded_bytes)

                    print(f"\n[DEBUG] Cluster: {cluster_url} | Namespace: {namespace} | Secret: {secret_name}")
                    print(f"Provided (hex): {normalized_provided_bytes.hex()}")
                    print(f"Secret   (hex): {normalized_secret_bytes.hex()}")

                    if normalized_provided_bytes == normalized_secret_bytes:
                        password_match = "Yes"
                    else:
                        password_match = "No"
                        with open(DEBUG_FILE, "a") as dbg:
                            dbg.write(f"Cluster: {cluster_url} | Namespace: {namespace} | Secret: {secret_name}\n")
                            dbg.write(f"Provided (hex): {normalized_provided_bytes.hex()}\n")
                            dbg.write(f"Secret   (hex): {normalized_secret_bytes.hex()}\n\n")

            except Exception:
                continue

        if service_id_found == "Yes":
            results.append([cluster_url, namespace, secret_name, secret_type, service_id_found, password_match])

    return results

def process_cluster(cluster_url):
    temp_dir = tempfile.mkdtemp(prefix="kubeconfig_")
    kubeconfig_path = os.path.join(temp_dir, "config")
    rows = []

    try:
        subprocess.run(["oc", "login", "-u", oc_username, "-p", oc_password, cluster_url, "--kubeconfig", kubeconfig_path], check=True, capture_output=True)
        result = subprocess.run(["oc", "--kubeconfig", kubeconfig_path, "get", "ns", "-o", "json"], capture_output=True, text=True, check=True)
        namespaces = [ns["metadata"]["name"] for ns in json.loads(result.stdout)["items"]]

        with ThreadPoolExecutor(max_workers=THREADS_PER_CLUSTER) as executor:
            futures = [executor.submit(process_namespace, kubeconfig_path, cluster_url, ns) for ns in namespaces]
            for future in as_completed(futures):
                ns_results = future.result()
                rows.extend(ns_results)

    except subprocess.CalledProcessError as e:
        print(f"❌ Failed on cluster {cluster_url}: {e.stderr.decode()}")
    finally:
        shutil.rmtree(temp_dir)
    return rows

if __name__ == "__main__":
    with open(OUTPUT_FILE, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Cluster URL", "Namespace", "Secret Name", "Secret Type", "Service ID Found", "Password Match"])

        with ThreadPoolExecutor() as executor:  # ⚠ ThreadPoolExecutor → print works
            futures = [executor.submit(process_cluster, cluster) for cluster in clusters]
            for future in as_completed(futures):
                rows = future.result()
                for row in rows:
                    writer.writerow(row)

    print(f"\n✅ Validation complete. Results saved to {OUTPUT_FILE}")
    print(f"📄 Debug mismatches (if any) saved to {DEBUG_FILE}")

