import os
import csv
import subprocess
import json
import base64
from datetime import datetime
from getpass import getpass

clusters_file = "clusters.txt"
output_dir = "outputs"
os.makedirs(output_dir, exist_ok=True)

# Prompt for username and password once at the start
username = input("Enter OpenShift username: ")
password = getpass("Enter OpenShift password: ")

output_csv = os.path.join(output_dir, f"secrets_with_service_id_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

with open(output_csv, mode='w', newline='') as file:
    writer = csv.writer(file)
    writer.writerow(['Cluster URL', 'Namespace', 'Secret Name', 'Secret Type', 'Matching Key', 'Matching Value'])

    with open(clusters_file, 'r') as f:
        clusters = [line.strip() for line in f if line.strip()]

    for cluster_url in clusters:
        print(f"\nLogging into cluster: {cluster_url}")
        login_cmd = ["oc", "login", cluster_url, "-u", username, "-p", password]
        result = subprocess.run(login_cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"Failed to login to {cluster_url}: {result.stderr}")
            continue

        # Get all namespaces
        ns_cmd = ["oc", "get", "ns", "-o", "json"]
        ns_result = subprocess.run(ns_cmd, capture_output=True, text=True)

        if ns_result.returncode != 0:
            print(f"Failed to get namespaces for {cluster_url}")
            continue

        namespaces = json.loads(ns_result.stdout)["items"]

        for ns in namespaces:
            namespace = ns["metadata"]["name"]

            # Skip system namespaces
            if namespace in ('kube-system', 'openshift-system', 'openshift-config', 'openshift-monitoring'):
                continue

            print(f"  Searching in namespace: {namespace}")

            # Get all secrets in the namespace
            sec_cmd = ["oc", "get", "secret", "-n", namespace, "-o", "json"]
            sec_result = subprocess.run(sec_cmd, capture_output=True, text=True)

            if sec_result.returncode != 0:
                print(f"  Failed to get secrets in namespace {namespace}")
                continue

            secrets = json.loads(sec_result.stdout).get("items", [])

            for secret in secrets:
                secret_name = secret['metadata']['name']
                secret_type = secret.get('type', 'Unknown')
                data = secret.get('data', {})

                for key, value in data.items():
                    try:
                        decoded_bytes = base64.b64decode(value)
                        decoded_value = decoded_bytes.decode('utf-8')
                    except (base64.binascii.Error, UnicodeDecodeError):
                        decoded_value = "<unable to decode>"

                    if "service" in key.lower() or "service" in decoded_value.lower():
                        writer.writerow([cluster_url, namespace, secret_name, secret_type, key, decoded_value])

print(f"\nOutput saved to {output_csv}")

