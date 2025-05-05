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
        return None, result.stderr.strip()
    return json.loads(result.stdout), None

def patch_secret(namespace, secret_name, updated_data):
    patch_data = {"data": updated_data}
    patch_command = f"oc patch secret {secret_name} -n {namespace} --type=merge -p '{json.dumps(patch_data)}'"
    result = subprocess.run(patch_command, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, None

def update_dockerconfigjson(secret_data, new_username, new_password):
    """
    If new_username is provided (not None), update both username and password.
    Otherwise, update only the password and leave username unchanged.
    """
    decoded_data = base64.b64decode(secret_data['data']['.dockerconfigjson']).decode()
    dockerconfigjson = json.loads(decoded_data)
    registries = dockerconfigjson.get('auths', dockerconfigjson)

    for registry, credentials in registries.items():
        # Determine the username to use:
        if new_username is not None:
            credentials['username'] = new_username
            updated_username = new_username
        else:
            # Attempt to use existing username.
            if 'username' in credentials:
                updated_username = credentials['username']
            elif 'auth' in credentials:
                # Decode the auth field if username is not set explicitly.
                try:
                    current_auth = base64.b64decode(credentials['auth']).decode()
                    updated_username = current_auth.split(':')[0]
                except Exception:
                    updated_username = ""
            else:
                updated_username = ""
        # Always update the password.
        credentials['password'] = new_password

        # Update the auth field accordingly.
        if 'auth' in credentials:
            updated_auth = f"{updated_username}:{new_password}"
            credentials['auth'] = base64.b64encode(updated_auth.encode()).decode()
    dockerconfigjson['auths'] = registries
    return {".dockerconfigjson": base64.b64encode(json.dumps(dockerconfigjson).encode()).decode()}

def update_dockercfg(secret_data, new_username, new_password):
    """
    If new_username is provided, update both username and password.
    Otherwise, update only the password.
    """
    decoded_data = base64.b64decode(secret_data['data']['.dockercfg']).decode()
    dockercfg = json.loads(decoded_data)

    for registry, credentials in dockercfg.items():
        if new_username is not None:
            credentials['username'] = new_username
            updated_username = new_username
        else:
            if 'username' in credentials:
                updated_username = credentials['username']
            elif 'auth' in credentials:
                try:
                    current_auth = base64.b64decode(credentials['auth']).decode()
                    updated_username = current_auth.split(':')[0]
                except Exception:
                    updated_username = ""
            else:
                updated_username = ""
        credentials['password'] = new_password

        if 'auth' in credentials:
            updated_auth = f"{updated_username}:{new_password}"
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
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    updated_csv = f"updated_secrets_{timestamp_str}.csv"
    failed_csv = f"failed_secrets_{timestamp_str}.csv"
    dry_run_csv = f"dry_run_results_{timestamp_str}.csv"

    clusters = read_csv(file_path)

    print("Enter your OpenShift credentials:")
    username = input("Username: ")
    password = getpass("Password: ")

    # Prompt for update mode.
    print("\nChoose update mode:")
    print("1. Update both username and password")
    print("2. Update only password")
    choice = input("Enter 1 or 2: ").strip()
    if choice == "1":
        new_username = input("Enter the NEW username for the Docker secret: ").strip()
        new_password = getpass("Enter the NEW password for the Docker secret: ")
    elif choice == "2":
        new_username = None
        new_password = getpass("Enter the NEW password for the Docker secret: ")
    else:
        print("Invalid choice. Exiting.")
        return

    # Prompt for dry-run.
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

        # Use appropriate function based on secret type.
        if secret_type == "kubernetes.io/dockerconfigjson":
            updated_data = update_dockerconfigjson(secret, new_username, new_password)
        elif secret_type == "kubernetes.io/dockercfg":
            updated_data = update_dockercfg(secret, new_username, new_password)
        else:
            reason = f"Unsupported secret type: {secret_type}"
            append_to_csv(failed_csv, cluster, "failed", reason)
            continue

        if is_dry_run:
            print(f"Dry-run: Would update secret {secret_name} in namespace {namespace}")
            append_to_csv(dry_run_csv, cluster, "simulated", f"Would update secret of type {secret_type}")
        else:
            success, error = patch_secret(namespace, secret_name, updated_data)
            if success:
                append_to_csv(updated_csv, cluster, "updated", f"Secret type: {secret_type}")
                print(f"Secret {secret_name} updated successfully.")
            else:
                append_to_csv(failed_csv, cluster, "failed", f"Patch error: {error}")
                print(f"Failed to patch secret {secret_name}: {error}")

    print(f"\nProcess completed. Output saved to:\n Updated: {updated_csv}\n Failed: {failed_csv}")
    if is_dry_run:
        print(f"(Dry-run results saved to {dry_run_csv})")

if __name__ == "__main__":
    main()
