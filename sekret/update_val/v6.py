import subprocess
import json
import base64
import os
import csv
import getpass
import binascii
from pathlib import Path

OUTPUT_FILE = 'output/validated.csv'

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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

def check_auth(auth_str, serviceid, password):
    decoded = decode_b64(auth_str)
    if decoded:
        parts = decoded.strip().split(':', 1)
        if len(parts) == 2:
            uid, pwd = parts
            return uid.strip() == serviceid.strip() and pwd.strip() == password.strip()
    return False

def match_credentials(decoded_json, serviceid, password):
    if isinstance(decoded_json, dict):
        # Check direct fields
        if 'auth' in decoded_json and check_auth(decoded_json['auth'], serviceid, password):
            return serviceid, 'yes'
        if decoded_json.get('username', '').strip() == serviceid.strip() and decoded_json.get('password', '').strip() == password.strip():
            return serviceid, 'yes'
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

    for key, b64_val in data.items():
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

def main():
    Path('output').mkdir(exist_ok=True)
    username = input("Enter OpenShift username: ")
    password = getpass.getpass("Enter OpenShift password: ")
    serviceid = input("Enter Service ID to validate: ")
    user_password = getpass.getpass("Enter corresponding password: ")

    with open(OUTPUT_FILE, mode='w', newline='') as outcsv:
        writer = csv.writer(outcsv)
        writer.writerow(['Cluster URL', 'Namespace', 'Secret Name', 'Secret Type', 'Service ID Found', 'Password Match'])

        with open('clusters.txt') as f:
            clusters = f.read().splitlines()

        for cluster in clusters:
            print(f"\nLogging into cluster: {cluster}")
            out, err = run_cmd(f'oc login -u {username} -p {password} {cluster} --insecure-skip-tls-verify')
            if 'error' in err.lower():
                print(f"Failed to login to cluster {cluster}: {err}")
                continue

            print("Fetching all namespaces...")
            ns_out, _ = run_cmd("oc get ns -o json")
            try:
                namespaces = json.loads(ns_out)['items']
            except Exception as e:
                print(f"Failed to parse namespaces on {cluster}: {e}")
                continue

            for ns in namespaces:
                ns_name = ns['metadata']['name']
                secret_out, _ = run_cmd(f"oc get secrets -n {ns_name} -o json")
                try:
                    secrets = json.loads(secret_out)['items']
                except Exception:
                    continue

                for secret in secrets:
                    namespace, name, stype, sid, match = process_secret(secret, serviceid, user_password)
                    if sid:
                        writer.writerow([cluster, namespace, name, stype, sid, match])
                        print(f"Validated secret: {name} in namespace: {namespace}")

if __name__ == '__main__':
    main()

