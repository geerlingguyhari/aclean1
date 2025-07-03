import subprocess
import json
import base64
import os
import csv
import tempfile
import shutil
from getpass import getpass
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

OUTPUT_FILE = "output/validated.csv"
DEBUG_FILE = "output/debug_mismatches.log"
os.makedirs("output", exist_ok=True)

THREADS_PER_CLUSTER = 10

def run_oc_command(command, kubeconfig):
    env = os.environ.copy()
    env['KUBECONFIG'] = kubeconfig
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"Error running command: {' '.join(command)}\n{result.stderr.strip()}")
    return result.stdout.strip()

def decode_base64_data(data):
    try:
        return base64.b64decode(data).decode("utf-8")
    except (base64.binascii.Error, UnicodeDecodeError):
        return None

def extract_credentials_from_json(decoded_str):
    try:
        return json.loads(decoded_str)
    except json.JSONDecodeError:
        return {}

def recursive_decode_auth(encoded_auth):
    for _ in range(3):  # Limit recursion depth
        decoded = decode_base64_data(encoded_auth)
        if decoded and ":" in decoded:
            return decoded
        elif decoded:
            encoded_auth = decoded
        else:
            break
    return None

def find_username_in_credentials(decoded_json, service_id, debug_log, cluster_url, namespace, secret_name):
    found = False
    for registry, creds in decoded_json.items():
        auth = creds.get("auth")
        if auth:
            decoded_auth = recursive_decode_auth(auth)
            if decoded_auth:
                try:
                    username, _ = decoded_auth.split(":", 1)
                    if username.lower() == service_id.lower():
                        debug_log.append(f"[MATCH FOUND] {cluster_url}::{namespace}::{secret_name} -> decoded auth: {decoded_auth}")
                        found = True
                except ValueError:
                    pass
        # Also check "username" field
        if creds.get("username", "").lower() == service_id.lower():
            debug_log.append(f"[MATCH FOUND] {cluster_url}::{namespace}::{secret_name} -> username field match")
            found = True
    return found

def process_secret(secret, service_id, cluster_url, namespace, debug_log):
    name = secret["metadata"]["name"]
    secret_type = secret.get("type", "Opaque")
    data = secret.get("data", {})
    string_data = secret.get("stringData", {})
    found = False

    # Process both data (base64 encoded) and stringData (plain text)
    for key, value in {**data, **string_data}.items():
        # For data fields (base64 encoded), decode first
        if key in data:
            decoded_data = decode_base64_data(value)
            if not decoded_data:
                continue
        else:  # stringData fields are already in plain text
            decoded_data = value

        # Check for embedded JSON credentials
        embedded_json = extract_credentials_from_json(decoded_data)
        if isinstance(embedded_json, dict) and find_username_in_credentials(embedded_json, service_id, debug_log, cluster_url, namespace, name):
            found = True
            break
        else:
            # Try recursive decoding for embedded 'auth' strings directly in opaque data
            possible_decoded = recursive_decode_auth(decoded_data)
            if possible_decoded:
                try:
                    username, _ = possible_decoded.split(":", 1)
                    if username.lower() == service_id.lower():
                        debug_log.append(f"[MATCH FOUND] {cluster_url}::{namespace}::{name} -> recursive decoded auth: {possible_decoded}")
                        found = True
                        break
                except ValueError:
                    pass

    # Special handling for docker-registry secrets
    if not found and secret_type == "kubernetes.io/dockerconfigjson":
        try:
            docker_config = json.loads(decode_base64_data(data.get(".dockerconfigjson", "")))
            if docker_config and "auths" in docker_config:
                for registry, auth_info in docker_config["auths"].items():
                    if auth_info.get("auth"):
                        decoded_auth = recursive_decode_auth(auth_info["auth"])
                        if decoded_auth:
                            username, _ = decoded_auth.split(":", 1)
                            if username.lower() == service_id.lower():
                                debug_log.append(f"[MATCH FOUND] {cluster_url}::{namespace}::{name} -> dockerconfigjson auth: {decoded_auth}")
                                found = True
                                break
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass

    if found:
        return {
            "cluster_url": cluster_url,
            "namespace": namespace,
            "secret_name": name,
            "secret_type": secret_type,
            "match": "Yes"
        }
    return None

def process_namespace(kubeconfig, cluster_url, ns_name, service_id, debug_log):
    results = []
    try:
        secrets_json = run_oc_command(["oc", "get", "secrets", "-n", ns_name, "-o", "json"], kubeconfig)
        secrets = json.loads(secrets_json).get("items", [])
        for secret in secrets:
            result = process_secret(secret, service_id, cluster_url, ns_name, debug_log)
            if result:
                results.append(result)
    except Exception as e:
        debug_log.append(f"[namespace error] {cluster_url}::{ns_name} -> {e}")
    return results

def process_cluster(cluster_url, oc_username, oc_password, service_id):
    validated_results = []
    debug_log = []
    temp_dir = tempfile.mkdtemp(prefix="kubeconfig_")
    kubeconfig_path = os.path.join(temp_dir, "config")

    try:
        login = subprocess.run(
            ["oc", "login", "-u", oc_username, "-p", oc_password, cluster_url, "--kubeconfig", kubeconfig_path],
            capture_output=True, text=True
        )
        if login.returncode != 0:
            print(f"❌ Failed to login to {cluster_url}: {login.stderr.strip()}")
            return [], []

        ns_output = run_oc_command(["oc", "--kubeconfig", kubeconfig_path, "get", "namespaces", "-o", "json"], kubeconfig_path)
        ns_list = json.loads(ns_output).get("items", [])
        ns_names = [ns["metadata"]["name"] for ns in ns_list]

        with ThreadPoolExecutor(max_workers=THREADS_PER_CLUSTER) as executor:
            futures = [
                executor.submit(process_namespace, kubeconfig_path, cluster_url, ns, service_id, debug_log)
                for ns in ns_names
            ]
            for future in as_completed(futures):
                validated_results.extend(future.result())

    except Exception as e:
        print(f"❌ Error processing cluster {cluster_url}: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return validated_results, debug_log

def main():
    clusters_file = "clusters.txt"
    oc_username = input("Enter OpenShift Username: ")
    oc_password = getpass("Enter OpenShift Password: ")
    service_id = input("Enter Service ID to search for: ")

    with open(clusters_file, "r") as f:
        clusters = [line.strip() for line in f if line.strip()]

    all_results = []
    all_debug_logs = []

    with ProcessPoolExecutor(max_workers=min(5, len(clusters))) as executor:
        futures = [
            executor.submit(process_cluster, cluster, oc_username, oc_password, service_id)
            for cluster in clusters
        ]
        for future in as_completed(futures):
            validated, debug = future.result()
            all_results.extend(validated)
            all_debug_logs.extend(debug)

    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["cluster_url", "namespace", "secret_name", "secret_type", "match"])
        writer.writeheader()
        for row in all_results:
            writer.writerow(row)

    with open(DEBUG_FILE, "w") as f:
        for line in all_debug_logs:
            f.write(line + "\n")

    print(f"✅ Search completed. Matching results saved to {OUTPUT_FILE}")
    print(f"🪵 Matching debug logs saved to {DEBUG_FILE}")

if __name__ == "__main__":
    main()