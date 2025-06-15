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

def aggressive_normalize(b):
    return re.sub(rb'[\x00-\x20]', b'', b).strip()

os.makedirs(OUTPUT_DIR, exist_ok=True)
if os.path.exists(DEBUG_FILE):
    os.remove(DEBUG_FILE)

service_id = input("Enter Service ID to validate: ").strip()
password_to_validate = getpass("Enter Password to validate: ").strip()
oc_username = input("Enter OpenShift Username: ").strip()
oc_password = getpass("Enter OpenShift Password: ").strip()

provided_password_bytes = password_to_validate.encode('utf-8')
normalized_provided_password = aggressive_normalize(provided_password_bytes)
normalized_provided_serviceid = service_id.lower()

with open(CLUSTERS_FILE, "r") as f:
    clusters = [line.strip() for line in f if line.strip()]

def decode_and_check_credentials(details, cluster_url, namespace, secret_name):
    username_match, password_match = False, False
    username = details.get("username", "").strip()
    password = details.get("password", "").strip()

    if username:
        username_match = (username.lower() == normalized_provided_serviceid)
    if password:
        normalized_secret_password = aggressive_normalize(password.encode('utf-8'))
        password_match = (normalized_secret_password == normalized_provided_password)

    auth_field_b64 = details.get("auth", "")
    if auth_field_b64:
        try:
            decoded_auth = base64.b64decode(auth_field_b64).decode('utf-8', errors='ignore')
            if ':' in decoded_auth:
                auth_username, auth_password = decoded_auth.split(':', 1)
                if auth_username.strip().lower() == normalized_provided_serviceid:
                    username_match = True
                normalized_secret_auth_password = aggressive_normalize(auth_password.encode('utf-8'))
                if normalized_secret_auth_password == normalized_provided_password:
                    password_match = True
        except Exception:
            pass

    if username_match and not password_match:
        with open(DEBUG_FILE, "a") as dbg:
            dbg.write(f"[Mismatch] Cluster: {cluster_url} | Namespace: {namespace} | Secret: {secret_name}\n")
            dbg.write(f"Username Match ✅ | Password mismatch ❌\n\n")

    return username_match, password_match

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

        def handle_dockerconfig(json_obj):
            nonlocal service_id_found, password_match
            auths = json_obj.get("auths", json_obj)
            for reg, details in auths.items():
                username_match, pwd_match = decode_and_check_credentials(details, cluster_url, namespace, secret_name)
                if username_match:
                    service_id_found = "Yes"
                    password_match = "Yes" if pwd_match else "No"

        if secret_type == "kubernetes.io/dockerconfigjson":
            try:
                dockerconfig_json = json.loads(base64.b64decode(data.get(".dockerconfigjson", "")).decode('utf-8'))
                handle_dockerconfig(dockerconfig_json)
            except Exception:
                pass

        elif secret_type == "kubernetes.io/dockercfg":
            try:
                dockercfg_json = json.loads(base64.b64decode(data.get(".dockercfg", "")).decode('utf-8'))
                handle_dockerconfig(dockercfg_json)
            except Exception:
                pass

        else:  # Opaque and other types
            docker_keys = [".dockerconfigjson", ".dockercfg"]
            for docker_key in docker_keys:
                if docker_key in data:
                    try:
                        dockerconfig_json = json.loads(base64.b64decode(data[docker_key]).decode('utf-8'))
                        handle_dockerconfig(dockerconfig_json)
                    except Exception:
                        continue

            for key, encoded_value in data.items():
                try:
                    decoded_bytes = base64.b64decode(encoded_value)
                except (binascii.Error, UnicodeDecodeError):
                    continue

                decoded_text = decoded_bytes.decode('utf-8', errors='ignore')
                normalized_secret_bytes = aggressive_normalize(decoded_bytes)

                if normalized_provided_serviceid in decoded_text.lower():
                    service_id_found = "Yes"
                    password_match = "Yes" if normalized_secret_bytes == normalized_provided_password else "No"
                    if password_match == "No":
                        with open(DEBUG_FILE, "a") as dbg:
                            dbg.write(f"[Mismatch] Cluster: {cluster_url} | Namespace: {namespace} | Secret: {secret_name}\n")
                            dbg.write(f"Provided (hex): {normalized_provided_password.hex()}\n")
                            dbg.write(f"Secret   (hex): {normalized_secret_bytes.hex()}\n\n")

                if key.lower() == "auth":
                    try:
                        decoded_auth = base64.b64decode(decoded_bytes).decode('utf-8', errors='ignore')
                        if ':' in decoded_auth:
                            auth_username, auth_password = decoded_auth.split(':', 1)
                            if auth_username.strip().lower() == normalized_provided_serviceid:
                                service_id_found = "Yes"
                            normalized_secret_auth_password = aggressive_normalize(auth_password.encode('utf-8'))
                            if normalized_secret_auth_password == normalized_provided_password:
                                password_match = "Yes"
                    except Exception:
                        continue

                if key.lower() == "username":
                    if decoded_text.strip().lower() == normalized_provided_serviceid:
                        service_id_found = "Yes"
                elif key.lower() == "password":
                    if normalized_secret_bytes == normalized_provided_password:
                        password_match = "Yes"

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
                rows.extend(future.result())

    except subprocess.CalledProcessError as e:
        print(f"❌ Failed on cluster {cluster_url}: {e.stderr.decode()}")
    finally:
        shutil.rmtree(temp_dir)

    return rows

if __name__ == "__main__":
    with open(OUTPUT_FILE, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Cluster URL", "Namespace", "Secret Name", "Secret Type", "Service ID Found", "Password Match"])

        with ThreadPoolExecutor() as executor:
            futures = [executor.submit(process_cluster, cluster) for cluster in clusters]
            for future in as_completed(futures):
                for row in future.result():
                    writer.writerow(row)

    print(f"\n✅ Validation complete. Results saved to {OUTPUT_FILE}")
    print(f"📄 Debug mismatches (if any) saved to {DEBUG_FILE}")

