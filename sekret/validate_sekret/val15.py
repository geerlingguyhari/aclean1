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

os.makedirs(OUTPUT_DIR, exist_ok=True)
if os.path.exists(DEBUG_FILE):
    os.remove(DEBUG_FILE)

service_id = input("Enter Service ID to validate: ").strip()
password_to_validate = getpass("Enter Password to validate: ").strip()
oc_username = input("Enter OpenShift Username: ").strip()
oc_password = getpass("Enter OpenShift Password: ").strip()

provided_password_bytes = password_to_validate.encode("utf-8")
normalized_provided_password = re.sub(rb"[\x00-\x20]", b"", provided_password_bytes).strip()
normalized_provided_serviceid = service_id.lower()

with open(CLUSTERS_FILE, "r") as f:
    clusters = [line.strip() for line in f if line.strip()]

def aggressive_normalize(b):
    return re.sub(rb"[\x00-\x20]", b"", b).strip()

def safe_b64decode(value):
    try:
        return base64.b64decode(value)
    except (binascii.Error, UnicodeDecodeError):
        return b""

def decode_auth_field(auth_field):
    try:
        decoded_auth = base64.b64decode(auth_field).decode("utf-8", errors="ignore")
        if ":" in decoded_auth:
            return decoded_auth.split(":", 1)
    except Exception:
        pass
    return None, None

def recursive_decode(data):
    try:
        decoded = safe_b64decode(data)
        if decoded == b"" or decoded == data:
            return decoded
        return recursive_decode(decoded)
    except Exception:
        return data

def extract_dockerconfig_from_bytes(decoded_bytes):
    try:
        json_obj = json.loads(decoded_bytes.decode("utf-8", errors="ignore"))
        auths = json_obj.get("auths", json_obj)
        return auths
    except Exception:
        return {}

def match_credentials(auths):
    for _, details in auths.items():
        username = details.get("username", "").strip()
        password = details.get("password", "").strip()
        auth_b64 = details.get("auth", "")

        if username.lower() == normalized_provided_serviceid:
            pwd_match = "Yes" if aggressive_normalize(password.encode("utf-8")) == normalized_provided_password else "No"
            return "Yes", pwd_match

        if auth_b64:
            u, p = decode_auth_field(auth_b64)
            if u and u.strip().lower() == normalized_provided_serviceid:
                pwd_match = "Yes" if aggressive_normalize(p.encode("utf-8")) == normalized_provided_password else "No"
                return "Yes", pwd_match

    return "No", "Not Found"

def try_parse_json(decoded_bytes):
    try:
        return json.loads(decoded_bytes.decode("utf-8", errors="ignore"))
    except Exception:
        return None

def process_secret_data(data, cluster_url, namespace, secret_name):
    service_id_found, password_match = "No", "Not Found"

    for key, encoded_value in data.items():
        decoded_bytes = recursive_decode(encoded_value)
        decoded_text = decoded_bytes.decode("utf-8", errors="ignore")

        json_obj = try_parse_json(decoded_bytes)

        # Check if it's a dockerconfigjson or dockercfg embedded in Opaque secret
        if json_obj and ("auths" in json_obj or ".dockercfg" in json_obj):
            auths = extract_dockerconfig_from_bytes(decoded_bytes)
            sid, pwd = match_credentials(auths)
            if sid == "Yes":
                return sid, pwd

        # Fallback to regular search in text
        if normalized_provided_serviceid in decoded_text.lower():
            normalized_secret_bytes = aggressive_normalize(decoded_bytes)
            service_id_found = "Yes"
            password_match = "Yes" if normalized_secret_bytes == normalized_provided_password else "No"

            if password_match == "No":
                with open(DEBUG_FILE, "a") as dbg:
                    dbg.write(f"[Mismatch] {cluster_url} | {namespace} | {secret_name}\n")
                    dbg.write(f"Provided(hex): {normalized_provided_password.hex()}\n")
                    dbg.write(f"Secret  (hex): {normalized_secret_bytes.hex()}\n\n")

        if key.lower() == "auth":
            auth_username, auth_password = decode_auth_field(decoded_bytes)
            if auth_username and auth_username.strip().lower() == normalized_provided_serviceid:
                service_id_found = "Yes"
            if auth_password:
                normalized_secret_auth_password = aggressive_normalize(auth_password.encode("utf-8"))
                if normalized_secret_auth_password == normalized_provided_password:
                    password_match = "Yes"

    return service_id_found, password_match

def process_namespace(kubeconfig, cluster_url, namespace):
    results = []
    try:
        result = subprocess.run(
            ["oc", "--kubeconfig", kubeconfig, "get", "secret", "-n", namespace, "-o", "json"],
            capture_output=True, text=True, check=True
        )
        secrets = json.loads(result.stdout).get("items", [])
    except Exception:
        return results

    for secret in secrets:
        secret_name = secret["metadata"]["name"]
        secret_type = secret.get("type", "N/A")
        data = secret.get("data", {})

        service_id_found, password_match = process_secret_data(data, cluster_url, namespace, secret_name)

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
        namespaces = [ns["metadata"]["name"] for ns in json.loads(result.stdout).get("items", [])]

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

