import subprocess
import json
import base64
import os
import csv
import getpass
import binascii
import shutil
import tempfile
import logging
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

OUTPUT_FILE = 'output/validated.csv'
LOG_FILE = 'output/audit.log'
THREADS_PER_CLUSTER = 5

def setup_logger():
    Path('output').mkdir(exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logging.info("🔍 Audit session started")

def mask(value):
    return value[:3] + '****' + value[-2:] if value else 'None'

def run_cmd(args, kubeconfig=None):
    cmd = args + (["--kubeconfig", kubeconfig] if kubeconfig else [])
    return subprocess.run(cmd, text=True, capture_output=True)

def decode_b64(data, nested=False):
    try:
        decoded = base64.b64decode(data).decode('utf-8')
        if nested:
            try:
                inner = base64.b64decode(decoded).decode('utf-8')
                return inner
            except Exception:
                pass
        return decoded
    except (binascii.Error, UnicodeDecodeError):
        return None

def check_auth(auth_str, serviceid, password):
    decoded = decode_b64(auth_str)
    if decoded:
        parts = decoded.strip().split(':', 1)
        if len(parts) == 2:
            uid, pwd = parts
            if uid.strip().lower() == serviceid.lower() and pwd.strip() == password.strip():
                return uid, 'yes'
    return '', 'no'

def match_credentials(decoded_json, serviceid, password):
    if isinstance(decoded_json, dict):
        if 'auth' in decoded_json:
            sid, match = check_auth(decoded_json['auth'], serviceid, password)
            if match == 'yes':
                return sid, match
        if (
            decoded_json.get('username', '').strip().lower() == serviceid.lower()
            and decoded_json.get('password', '').strip() == password.strip()
        ):
            return decoded_json.get('username'), 'yes'
        if isinstance(decoded_json.get('auths'), dict):
            for entry in decoded_json['auths'].values():
                sid, match = match_credentials(entry, serviceid, password)
                if match == 'yes':
                    return sid, match
    return '', 'no'

def process_secret(secret, serviceid, password):
    secret_type = secret.get('type')
    metadata = secret.get('metadata', {})
    data = secret.get('data', {})
    namespace = metadata.get('namespace')
    name = metadata.get('name')

    sid_found, matched = '', 'no'

    for _, b64_val in data.items():
        decoded = decode_b64(b64_val, nested=True)
        if not decoded:
            continue
        try:
            inner_json = json.loads(decoded)
            sid_found, matched = match_credentials(inner_json, serviceid, password)
            if matched == 'yes':
                break
        except json.JSONDecodeError:
            continue

    return namespace, name, secret_type, sid_found, matched

def process_namespace(kubeconfig_path, cluster_url, ns, serviceid, password):
    rows = []
    proc = run_cmd(["oc", "get", "secrets", "-n", ns, "-o", "json"], kubeconfig_path)
    if proc.returncode != 0:
        logging.warning(f"Could not get secrets in namespace {ns} on {cluster_url}")
        return rows

    try:
        secrets = json.loads(proc.stdout).get("items", [])
    except Exception:
        return rows

    for secret in secrets:
        namespace, name, stype, sid, match = process_secret(secret, serviceid, password)
        if sid:
            rows.append([cluster_url, namespace, name, stype, sid, match])
            logging.info(f"✅ Match in secret '{name}' (ns: {namespace}, cluster: {cluster_url})")
    return rows

def process_cluster(cluster_url, username, password, serviceid, target_password):
    temp_dir = tempfile.mkdtemp(prefix="kubeconfig_")
    kubeconfig_path = os.path.join(temp_dir, "config")
    results = []

    try:
        login = subprocess.run(
            ["oc", "login", "-u", username, "-p", password, cluster_url,
             "--insecure-skip-tls-verify", "--kubeconfig", kubeconfig_path],
            capture_output=True, text=True
        )
        if login.returncode != 0:
            logging.error(f"❌ Login failed for cluster {cluster_url}: {login.stderr.strip()}")
            return results

        ns_proc = run_cmd(["oc", "get", "ns", "-o", "json"], kubeconfig_path)
        if ns_proc.returncode != 0:
            logging.error(f"❌ Failed to get namespaces for cluster {cluster_url}")
            return results

        ns_data = json.loads(ns_proc.stdout).get("items", [])
        namespaces = [ns["metadata"]["name"] for ns in ns_data]

        with ThreadPoolExecutor(max_workers=THREADS_PER_CLUSTER) as executor:
            futures = [executor.submit(process_namespace, kubeconfig_path, cluster_url, ns, serviceid, target_password) for ns in namespaces]
            for future in as_completed(futures):
                results.extend(future.result())

    except Exception as e:
        logging.error(f"Unhandled error on cluster {cluster_url}: {e}")
    finally:
        shutil.rmtree(temp_dir)

    return results

# Top-level wrapper for multiprocessing
def cluster_task(args):
    return process_cluster(*args)

def main():
    setup_logger()
    username = input("Enter OpenShift username: ")
    password = getpass.getpass("Enter OpenShift password: ")
    serviceid = input("Enter Service ID to validate: ")
    target_password = getpass.getpass("Enter corresponding password: ")

    logging.info(f"🔧 Starting with user '{mask(username)}' for service ID '{mask(serviceid)}'")

    with open('clusters.txt') as f:
        clusters = [line.strip() for line in f if line.strip()]

    args_list = [(cluster, username, password, serviceid, target_password) for cluster in clusters]
    all_rows = []

    with ProcessPoolExecutor() as executor:
        for cluster_rows in executor.map(cluster_task, args_list):
            all_rows.extend(cluster_rows)

    with open(OUTPUT_FILE, mode='w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Cluster URL', 'Namespace', 'Secret Name', 'Secret Type', 'Service ID Found', 'Password Match'])
        writer.writerows(all_rows)

    logging.info(f"📦 Complete. Validated secrets written to '{OUTPUT_FILE}'")
    print(f"\n✅ Validation finished. Output at {OUTPUT_FILE}")
    print(f"📝 Audit log available at {LOG_FILE}")

if __name__ == '__main__':
    main()

