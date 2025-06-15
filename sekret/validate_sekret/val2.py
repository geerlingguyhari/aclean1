import subprocess
import base64
import binascii
import json
import csv
import os
from getpass import getpass
from concurrent.futures import ThreadPoolExecutor, as_completed

OUTPUT_DIR = "output"
OUTPUT_FILE = f"{OUTPUT_DIR}/validated.csv"
CLUSTERS_FILE = "clusters.txt"
THREADS = 10  # Adjust number of parallel threads as required

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Prompt for service ID and password
service_id = input("Enter Service ID to validate: ").strip()
password_to_validate = getpass("Enter Password to validate: ").strip()

# Prompt for OpenShift username and password for login
oc_username = input("Enter OpenShift Username: ").strip()
oc_password = getpass("Enter OpenShift Password: ").strip()

# Read clusters from file
with open(CLUSTERS_FILE, "r") as f:
    clusters = [line.strip() for line in f if line.strip()]

def process_namespace(cluster_url, namespace):
    results = []
    try:
        result = subprocess.run(["oc", "get", "secret", "-n", namespace, "-o", "json"], capture_output=True, text=True)
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
                    if password_to_validate == decoded_value.strip():
                        password_match = "Yes"
                    else:
                        password_match = "No"

            except (binascii.Error, UnicodeDecodeError):
                continue

        if service_id_found == "Yes":
            results.append([cluster_url, namespace, secret_name, secret_type, service_id_found, password_match])
    return results

with open(OUTPUT_FILE, "w", newline="") as csvfile:
    writer = csv.writer(csvfile)
    writer.writerow(["Cluster URL", "Namespace", "Secret Name", "Secret Type", "Service ID Found", "Password Match"])

    for cluster in clusters:
        print(f"\n🔗 Logging into cluster: {cluster}")
        try:
            subprocess.run(["oc", "logout"], check=False)
            subprocess.run(["oc", "login", "-u", oc_username, "-p", oc_password, cluster], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to login to cluster {cluster}: {e.stderr.decode()}")
            continue

        # Get all namespaces
        result = subprocess.run(["oc", "get", "ns", "-o", "json"], capture_output=True, text=True)
        namespaces = [ns["metadata"]["name"] for ns in json.loads(result.stdout)["items"]]

        with ThreadPoolExecutor(max_workers=THREADS) as executor:
            future_to_namespace = {executor.submit(process_namespace, cluster, ns): ns for ns in namespaces}

            for future in as_completed(future_to_namespace):
                ns = future_to_namespace[future]
                try:
                    ns_results = future.result()
                    for row in ns_results:
                        writer.writerow(row)
                except Exception as e:
                    print(f"⚠️ Error processing namespace {ns} in cluster {cluster}: {e}")

print(f"\n✅ Validation complete. Results saved to {OUTPUT_FILE}")

