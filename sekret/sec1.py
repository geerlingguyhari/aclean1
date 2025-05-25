import csv
import json
import base64
import subprocess
from getpass import getpass
import os
from datetime import datetime

def read_csv(file_path):
    clusters = []
    with open(file_path, mode='r') as csvfile:
        csvreader = csv.DictReader(csvfile)
        for row in csvreader:
            clusters.append({
                'cluster_url': row['Clusterurl'],
                'namespace': row['namespace'],
                'secret_name': row['secretname']
            })
    return clusters

def login_to_cluster(url, username, password):
    login_command = f"oc login {url} -u {username} -p {password}"
    result = subprocess.run(login_command, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Failed to login to the cluster: {result.stderr}")
        return False
    return True

def get_secret(namespace, secret_name):
    get_secret_command = f"oc get secret {secret_name} -n {namespace} -o json"
    result = subprocess.run(get_secret_command, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        return None, result.stderr
    return json.loads(result.stdout), None

def patch_secret(namespace, secret_name, updated_data):
    patch_data = {"data": updated_data}
    patch_command = f"oc patch secret {secret_name} -n {namespace} --type=merge -p '{json.dumps(patch_data)}'"
    result = subprocess.run(patch_command, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr
    return True, None

def update_dockerconfigjson(secret_data, new_username, new_password):
    decoded_data = base64.b64decode(secret_data['data']['.dockerconfigjson']).decode()
    dockerconfigjson = json.loads(decoded_data)
    registries = dockerconfigjson.get('auths', dockerconfigjson)
    for registry, credentials in registries.items():
        credentials['username'] = new_username
        credentials['password'] = new_password
        if 'auth' in credentials:
            updated_auth = f"{new_username}:{new_password}"
            credentials['auth'] = base64.b64encode(updated_auth.encode()).decode()
    dockerconfigjson['auths'] = registries
    return {".dockerconfigjson": base64.b64encode(json.dumps(dockerconfigjson).encode()).decode()}

def update_dockercfg(secret_data, new_username, new_password):
    decoded_data = base64.b64decode(secret_data['data']['.dockercfg']).decode()
    dockercfg = json.loads(decoded_data)
    for registry, credentials in dockercfg.items():
        credentials['username'] = new_username
        credentials['password'] = new_password
        if 'auth' in credentials:
            updated_auth = f"{new_username}:{new_password}"
            credentials['auth'] = base64.b64encode(updated_auth.encode()).decode()
    return {".dockercfg": base64.b64encode(json.dumps(dockercfg).encode()).decode()}

def append_to_csv(file_path, cluster, status, reason=""):
    file_exists = os.path.isfile(file_path)
    with open(file_path, mode='a', newline='') as csvfile:
        fieldnames = ['Timestamp', 'Clusterurl', 'namespace', 'secretname', 'status', 'reason']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'Clusterurl': cluster['cluster_url'],
            'namespace': cluster['namespace'],
            'secretname': cluster['secret_name'],
            'status': status,
            'reason': reason
        })

def main():
    file_path = "output.csv"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    updated_csv = f"updated_secrets_{timestamp}.csv"
    failed_csv = f"failed_secrets_{timestamp}.csv"
    dry_run_csv = f"dry_run_results_{timestamp}.csv"

    clusters = read_csv(file_path)

    username = input("Enter your OpenShift login username: ")
    password = getpass("Enter your OpenShift login password: ")
    new_username = input("Enter the NEW username for the Docker secret (press Enter to skip): ").strip()
    new_password = getpass("Enter the NEW password for the Docker secret: ")

    dry_run_input = input("Do you want to perform a dry-run (no actual changes)? yes/no: ").strip().lower()
    is_dry_run = dry_run_input == "yes"

    for cluster in clusters:
        url = cluster['cluster_url']
        namespace = cluster['namespace']
        secret_name = cluster['secret_name']
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Logging into cluster: {url}")

        if not login_to_cluster(url, username, password):
            append_to_csv(failed_csv, cluster, "failed", "Login failed")
            continue

        secret, error = get_secret(namespace, secret_name)
        if not secret:
            append_to_csv(failed_csv, cluster, "failed", f"Get secret error: {error}")
            continue

        secret_type = secret.get('type', 'unknown')
        print(f"Found secret type: {secret_type}")

        if secret_type == "kubernetes.io/dockerconfigjson":
            updated_data = update_dockerconfigjson(secret, new_username or 'USERNAME_UNCHANGED', new_password)
        elif secret_type == "kubernetes.io/dockercfg":
            updated_data = update_dockercfg(secret, new_username or 'USERNAME_UNCHANGED', new_password)
        else:
            reason = f"Unsupported secret type: {secret_type}"
            append_to_csv(failed_csv, cluster, "failed", reason)
            continue

        if is_dry_run:
            print(f"Dry-run: would update secret {secret_name} in namespace {namespace}")
            append_to_csv(dry_run_csv, cluster, "simulated", f"Would update secret of type {secret_type}")
        else:
            success, error = patch_secret(namespace, secret_name, updated_data)
            if success:
                append_to_csv(updated_csv, cluster, "updated", f"Secret type: {secret_type}")
                print(f"Secret {secret_name} updated successfully.")
            else:
                append_to_csv(failed_csv, cluster, "failed", f"Patch error: {error}")
                print(f"Failed to patch secret {secret_name}: {error}")

    print(f"\nProcess completed. Output saved to:\n{updated_csv}\n{failed_csv}")
    if is_dry_run:
        print(f"(Dry-run results saved to {dry_run_csv})")

if __name__ == "__main__":
    main()
