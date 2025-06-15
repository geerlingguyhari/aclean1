import subprocess
import base64
import binascii
import json
import csv
import os
import tempfile
import shutil
from getpass import getpass
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

OUTPUT_DIR = "output"
OUTPUT_FILE = f"{OUTPUT_DIR}/validated.csv"
CLUSTERS_FILE = "clusters.txt"
THREADS_PER_CLUSTER = 10

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Prompt for service ID and password
service_id = input("Enter Service ID to validate: ").strip()
password_to_validate = getpass("Enter Password to validate: ").strip()
oc_username = input("Enter OpenShift Username: ").strip()
oc_password = getpass("Enter OpenShift Password: ").strip()

# Read clusters from file
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
                decoded_value = decoded_bytes.decode('utf-8')

                if service_id.lower() in decoded_value.lower():
                    service_id_found = "Yes"

                    # Normalize for matching: remove leading/trailing whitespace, carriage returns, newlines
                    normalized_provided = password_to_validate.strip()
                    normalized_secret = decoded_value.strip().replace('\r', '').replace('\n', '')

                    if normalized_provided == normalized_secret:
                        password_match = "Yes"
                    else:
                        password_match = "No"

            except (binascii.Error, UnicodeDecodeError):
                continue

        if service_id_found == "Yes":
            results.append([cluster_url, namespace, secret_name, secret_type, service_id_found, password_match])

    return results

def process_cluster(cluster_url):
    temp_dir = tempfile.mkdtemp(prefix="kubeconfig_")
    kubeconfig_path = os.path.join(temp_dir, "config")

    try:
        subprocess.run(["oc", "login", "-u", oc_username, "-p", oc_password, cluster_url, "--kubeconfig", kubeconfig_path], check=True, capture_output=True)
        result = subprocess.run(["oc", "--kubeconfig", kubeconfig_path, "get", "ns", "-o", "json"], capture_output=True, text=True, check=True)
        namespaces = [ns["metadata"]["name"] for ns in json.loads(result.stdout)["items"]]

        rows = []
        with ThreadPoolExecutor(max_workers=THREADS_PER_CLUSTER) as executor:
            futures = [executor.submit(process_namespace, kubeconfig_path, cluster_url, ns) for ns in namespaces]
            for future in as_completed(futures):
                ns_results = future.result()
                rows.extend(ns_results)

        return rows

    except subprocess.CalledProcessError as e:
        print(f"❌ Failed on cluster {cluster_url}: {e.stderr.decode()}")
        return []
    finally:
        shutil.rmtree(temp_dir)

if __name__ == "__main__":
    with open(OUTPUT_FILE, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Cluster URL", "Namespace", "Secret Name", "Secret Type", "Service ID Found", "Password Match"])

        with ProcessPoolExecutor() as executor:
            futures = [executor.submit(process_cluster, cluster) for cluster in clusters]
            for future in as_completed(futures):
                rows = future.result()
                for row in rows:
                    writer.writerow(row)

    print(f"\n✅ Validation complete. Results saved to {OUTPUT_FILE}")

