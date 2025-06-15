import os
import csv
import subprocess
import json
import base64
from datetime import datetime
from getpass import getpass
from concurrent.futures import ThreadPoolExecutor, as_completed, ProcessPoolExecutor

clusters_file = "clusters.txt"
output_dir = "outputs"
os.makedirs(output_dir, exist_ok=True)

# Prompt for username, password, and service ID once at the start
username = input("Enter OpenShift username: ")
password = getpass("Enter OpenShift password: ")
service_id = input("Enter the Service ID to search for: ")

output_csv = os.path.join(output_dir, f"secrets_with_service_id_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

def process_namespace(cluster_url, namespace):
    rows = []

    # Get all secrets in the namespace
    sec_cmd = ["oc", "get", "secret", "-n", namespace, "-o", "json"]
    sec_result = subprocess.run(sec_cmd, capture_output=True, text=True)

    if sec_result.returncode != 0:
        print(f"  Failed to get secrets in namespace {namespace}")
        return rows

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

            if (service_id.lower() in key.lower() or service_id.lower() in decoded_value.lower()
                or service_id.upper() in key or service_id.upper() in decoded_value):
                rows.append([cluster_url, namespace, secret_name, secret_type, key, decoded_value])

    return rows

def process_cluster(cluster_url):
    results = []

    kubeconfig_path = f"/tmp/kubeconfig_{cluster_url.replace('https://', '').replace(':', '_')}.yaml"

    print(f"\nLogging into cluster: {cluster_url}")
    login_cmd = ["oc", "login", cluster_url, "-u", username, "-p", password, f"--kubeconfig={kubeconfig_path}"]
    result = subprocess.run(login_cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Failed to login to {cluster_url}: {result.stderr}")
        return results

    # Get all namespaces
    ns_cmd = ["oc", "get", "ns", "-o", "json", f"--kubeconfig={kubeconfig_path}"]
    ns_result = subprocess.run(ns_cmd, capture_output=True, text=True)

    if ns_result.returncode != 0:
        print(f"Failed to get namespaces for {cluster_url}")
        return results

    namespaces = json.loads(ns_result.stdout)["items"]
    namespaces_to_process = [ns["metadata"]["name"] for ns in namespaces if ns["metadata"]["name"] not in (
        'kube-system', 'openshift-system', 'openshift-config', 'openshift-monitoring'
    )]

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ns = {
            executor.submit(process_namespace_with_kubeconfig, cluster_url, ns, kubeconfig_path): ns for ns in namespaces_to_process
        }

        for future in as_completed(future_to_ns):
            ns = future_to_ns[future]
            try:
                results.extend(future.result())
            except Exception as exc:
                print(f"  Namespace {ns} generated an exception: {exc}")

    os.remove(kubeconfig_path)
    return results

def process_namespace_with_kubeconfig(cluster_url, namespace, kubeconfig_path):
    rows = []

    sec_cmd = ["oc", "get", "secret", "-n", namespace, "-o", "json", f"--kubeconfig={kubeconfig_path}"]
    sec_result = subprocess.run(sec_cmd, capture_output=True, text=True)

    if sec_result.returncode != 0:
        print(f"  Failed to get secrets in namespace {namespace}")
        return rows

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

            if (service_id.lower() in key.lower() or service_id.lower() in decoded_value.lower()
                or service_id.upper() in key or service_id.upper() in decoded_value):
                rows.append([cluster_url, namespace, secret_name, secret_type, key, decoded_value])

    return rows

with open(clusters_file, 'r') as f:
    clusters = [line.strip() for line in f if line.strip()]

with open(output_csv, mode='w', newline='') as file:
    writer = csv.writer(file)
    writer.writerow(['Cluster URL', 'Namespace', 'Secret Name', 'Secret Type', 'Matching Key', 'Matching Value'])

    with ProcessPoolExecutor() as executor:
        future_to_cluster = {executor.submit(process_cluster, cluster): cluster for cluster in clusters}

        for future in as_completed(future_to_cluster):
            cluster_url = future_to_cluster[future]
            try:
                rows = future.result()
                writer.writerows(rows)
            except Exception as exc:
                print(f"{cluster_url} generated an exception: {exc}")

print(f"\nOutput saved to {output_csv}")

