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
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip(), result.stderr.strip()

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

def check_auth(auth_str, serviceid_to_check, user_password):
    decoded = decode_b64(auth_str)
    if decoded:
        parts = decoded.strip().split(':', 1)
        if len(parts) == 2:
            uid, pwd = parts
            if uid.strip().lower() == serviceid_to_check.strip().lower():
                return uid, 'yes' if pwd.strip() == user_password.strip() else 'no'
    return None, 'no'

def match_credentials(decoded_json, serviceid_to_check, user_password):
    if isinstance(decoded_json, dict):
        if 'auth' in decoded_json:
            sid, match = check_auth(decoded_json['auth'], serviceid_to_check, user_password)
            if sid is not None:
                return sid, match
        if decoded_json.get('username', '').strip().lower() == serviceid_to_check.strip().lower():
            return decoded_json.get('username'), 'yes' if decoded_json.get('password', '').strip() == user_password.strip() else 'no'
        if isinstance(decoded_json.get('auths'), dict):
            for entry in decoded_json['auths'].values():
                sid, match = match_credentials(entry, serviceid_to_check, user_password)
                if sid is not None:
                    return sid, match
    return None, 'no'

def check_credential_pair(key, value, serviceid_to_check, user_password, is_base64=True):
    """Check if a key-value pair contains credentials that match the target"""
    if is_base64:
        decoded_value = decode_b64(value)
        if not decoded_value:
            return None, 'no'
    else:
        decoded_value = value
    
    # Check if this is a direct credential pair (like username:password)
    if ':' in decoded_value:
        parts = decoded_value.strip().split(':', 1)
        if len(parts) == 2:
            uid, pwd = parts
            if uid.strip().lower() == serviceid_to_check.strip().lower():
                return uid, 'yes' if pwd.strip() == user_password.strip() else 'no'
    
    # Check if the key looks like it might contain a username
    key_lower = key.lower()
    if any(term in key_lower for term in ['user', 'id', 'login', 'account', 'auth']):
        if decoded_value.strip().lower() == serviceid_to_check.strip().lower():
            return decoded_value, 'no'  # Username found but password not verified
    
    # Check if the key looks like it might contain a password
    elif any(term in key_lower for term in ['pass', 'pwd', 'secret', 'cred']):
        if decoded_value.strip() == user_password.strip():
            return None, 'no'  # Password found but we need matching username
    
    return None, 'no'

def process_secret(secret, serviceid_to_check, user_password):
    secret_type = secret.get('type')
    metadata = secret.get('metadata', {})
    data = secret.get('data', {})
    string_data = secret.get('stringData', {})
    namespace = metadata.get('namespace')
    name = metadata.get('name')

    sid_found, matched = None, 'no'

    # Special handling for Opaque secrets
    if secret_type == 'Opaque':
        # First pass: look for username matches
        username_matches = []
        
        # Check data (base64 encoded)
        for key, val in data.items():
            sid, match = check_credential_pair(key, val, serviceid_to_check, user_password, is_base64=True)
            if sid is not None:  # We found the username
                username_matches.append((key, sid, match))
        
        # Check stringData (plain text)
        for key, val in string_data.items():
            sid, match = check_credential_pair(key, val, serviceid_to_check, user_password, is_base64=False)
            if sid is not None:  # We found the username
                username_matches.append((key, sid, match))
        
        # If we found any username matches, check for corresponding password
        if username_matches:
            for key, sid, match in username_matches:
                if match == 'yes':
                    return namespace, name, secret_type, sid, 'yes'
            
            # If we got here, we have username matches but no password matches yet
            # Check if password exists in any field
            password_found = False
            for key, val in data.items():
                decoded_val = decode_b64(val)
                if decoded_val and decoded_val.strip() == user_password.strip():
                    password_found = True
                    break
            
            if not password_found:
                for key, val in string_data.items():
                    if val.strip() == user_password.strip():
                        password_found = True
                        break
            
            # Return the first username match with password status
            return namespace, name, secret_type, username_matches[0][1], 'yes' if password_found else 'no'
    else:
        # Original handling for other secret types
        for secret_data in [data, string_data]:
            for _, val in secret_data.items():
                if secret_data is data:  # data is base64 encoded
                    decoded = decode_b64(val, nested=True)
                else:  # stringData is plain text
                    decoded = val
                
                if not decoded:
                    continue
                    
                try:
                    inner_json = json.loads(decoded)
                    sid_found, matched = match_credentials(inner_json, serviceid_to_check, user_password)
                    if sid_found is not None:
                        return namespace, name, secret_type, sid_found, matched
                except json.JSONDecodeError:
                    if ':' in decoded:
                        parts = decoded.strip().split(':', 1)
                        if len(parts) == 2:
                            uid, pwd = parts
                            if uid.strip().lower() == serviceid_to_check.strip().lower():
                                return namespace, name, secret_type, uid, 'yes' if pwd.strip() == user_password.strip() else 'no'

    return namespace, name, secret_type, sid_found, matched

def process_namespace(kubeconfig, cluster_url, ns, serviceid_to_check, user_password):
    logging.info(f"🔎 Scanning namespace '{ns}' on cluster '{cluster_url}'")
    rows = []
    # Get all secrets including imagePullSecrets
    secret_out, _ = run_cmd(["oc", "get", "secrets", "-n", ns, "-o", "json"], kubeconfig)
    try:
        secrets = json.loads(secret_out).get('items', [])
    except Exception:
        return rows

    for secret in secrets:
        namespace, name, stype, sid, match = process_secret(secret, serviceid_to_check, user_password)
        if sid is not None:  # Only include secrets where we found the username (even if password doesn't match)
            rows.append([cluster_url, namespace, name, stype, sid, match])
            if match == 'yes':
                logging.info(f"✅ Match in secret '{name}' (ns: {namespace}, type: {stype})")
            else:
                logging.info(f"⚠️ Username found but password mismatch in secret '{name}' (ns: {namespace}, type: {stype})")

    return rows

def process_cluster(cluster_url, oc_username, oc_password, serviceid_to_check, user_password):
    temp_dir = tempfile.mkdtemp(prefix="kubeconfig_")
    kubeconfig_path = os.path.join(temp_dir, "config")
    rows = []

    try:
        subprocess.run(
            ["oc", "login", "-u", oc_username, "-p", oc_password, cluster_url, "--insecure-skip-tls-verify", "--kubeconfig", kubeconfig_path],
            check=True, capture_output=True
        )
        logging.info(f"🔐 Logged in to cluster '{cluster_url}'")

        result = subprocess.run(
            ["oc", "--kubeconfig", kubeconfig_path, "get", "ns", "-o", "json"],
            capture_output=True, text=True, check=True
        )
        namespaces = [ns["metadata"]["name"] for ns in json.loads(result.stdout).get("items", [])]

        with ThreadPoolExecutor(max_workers=THREADS_PER_CLUSTER) as executor:
            futures = [
                executor.submit(process_namespace, kubeconfig_path, cluster_url, ns, serviceid_to_check, user_password)
                for ns in namespaces
            ]
            for future in as_completed(futures):
                rows.extend(future.result())

    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Failed on cluster {cluster_url}: {e.stderr}")
    finally:
        shutil.rmtree(temp_dir)

    return rows

def cluster_task(args):
    return process_cluster(*args)

def main():
    setup_logger()

    oc_username = input("Enter OpenShift username: ")
    oc_password = getpass.getpass("Enter OpenShift password: ")
    serviceid_to_check = input("Enter Service ID to validate: ")
    user_password = getpass.getpass("Enter corresponding password: ")

    logging.info(f"🔧 Running with user '{mask(oc_username)}' for service ID '{mask(serviceid_to_check)}'")

    with open('clusters.txt') as f:
        clusters = [line.strip() for line in f if line.strip()]

    args_list = [(cluster, oc_username, oc_password, serviceid_to_check, user_password) for cluster in clusters]
    all_rows = []

    with ProcessPoolExecutor() as executor:
        results = executor.map(cluster_task, args_list)
        for cluster_rows in results:
            all_rows.extend(cluster_rows)

    with open(OUTPUT_FILE, mode='w', newline='') as outcsv:
        writer = csv.writer(outcsv)
        writer.writerow(['Cluster URL', 'Namespace', 'Secret Name', 'Secret Type', 'Service ID Found', 'Password Match'])
        writer.writerows(all_rows)

    logging.info(f"📦 Audit complete. Results saved to '{OUTPUT_FILE}'")
    print(f"\n✅ Validation completed. Output saved to {OUTPUT_FILE}")
    print(f"📝 Audit log saved to {LOG_FILE}")

if __name__ == '__main__':
    main()
