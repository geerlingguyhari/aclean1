import os
import base64
import json
import csv
from getpass import getpass
import subprocess
import logging
from base64 import b64decode

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def oc_login(cluster_url, username, password):
    """Login to OpenShift cluster using oc command."""
    try:
        cmd = f"oc login -u {username} -p {password} {cluster_url} --insecure-skip-tls-verify=true"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if "Login successful" in result.stdout:
            return True
        else:
            logging.error(f"Failed to login to {cluster_url}: {result.stderr}")
            return False
    except subprocess.CalledProcessError as e:
        logging.error(f"Error logging in to {cluster_url}: {e}")
        return False

def get_all_secrets():
    """Get all secrets from all namespaces."""
    try:
        cmd = "oc get secrets --all-namespaces -o json"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        logging.error(f"Error getting secrets: {e}")
        return None
    except json.JSONDecodeError as e:
        logging.error(f"Error parsing secrets JSON: {e}")
        return None

def decode_base64(data):
    """Safely decode base64 data."""
    try:
        return b64decode(data).decode('utf-8')
    except (base64.binascii.Error, UnicodeDecodeError):
        return None

def check_auth_string(auth_str, serviceid, password):
    """Check if auth string matches serviceid and password."""
    try:
        decoded_auth = decode_base64(auth_str)
        if not decoded_auth:
            return False
        parts = decoded_auth.split(':')
        if len(parts) != 2:
            return False
        return parts[0] == serviceid and parts[1] == password
    except Exception:
        return False

def check_docker_config(secret_data, serviceid, password, secret_type):
    """Check docker config secrets for matching credentials."""
    try:
        if secret_type in ['kubernetes.io/dockerconfigjson', 'kubernetes.io/dockercfg']:
            if secret_type == 'kubernetes.io/dockerconfigjson':
                decoded_data = decode_base64(secret_data['.dockerconfigjson'])
                if not decoded_data:
                    return False
                config = json.loads(decoded_data)
            else:  # dockercfg
                decoded_data = decode_base64(secret_data['.dockercfg'])
                if not decoded_data:
                    return False
                config = json.loads(decoded_data)

            # Check auth in all entries
            for registry, auth_info in config.get('auths', {}).items():
                if 'auth' in auth_info and check_auth_string(auth_info['auth'], serviceid, password):
                    return True
                if auth_info.get('username') == serviceid and auth_info.get('password') == password:
                    return True
        return False
    except (json.JSONDecodeError, KeyError, AttributeError):
        return False

def check_opaque_secret(secret_data, serviceid, password):
    """Check opaque secrets for docker config patterns."""
    try:
        for key, value in secret_data.items():
            decoded_value = decode_base64(value)
            if not decoded_value:
                continue
            
            # Check if it's a JSON that might contain docker config
            try:
                json_data = json.loads(decoded_value)
                if isinstance(json_data, dict):
                    # Check for dockerconfigjson pattern
                    if 'auths' in json_data:
                        for registry, auth_info in json_data['auths'].items():
                            if 'auth' in auth_info and check_auth_string(auth_info['auth'], serviceid, password):
                                return True
                            if auth_info.get('username') == serviceid and auth_info.get('password') == password:
                                return True
            except json.JSONDecodeError:
                # Not JSON, check for raw auth string
                if check_auth_string(value, serviceid, password):
                    return True
        return False
    except Exception:
        return False

def validate_secrets(serviceid, password, clusters):
    """Validate secrets across all clusters."""
    results = []
    
    for cluster_url in clusters:
        logging.info(f"Processing cluster: {cluster_url}")
        
        if not oc_login(cluster_url, serviceid, password):
            logging.warning(f"Failed to login to {cluster_url} with provided credentials")
            continue
        
        secrets_data = get_all_secrets()
        if not secrets_data:
            continue
        
        for item in secrets_data.get('items', []):
            namespace = item['metadata']['namespace']
            secret_name = item['metadata']['name']
            secret_type = item.get('type', 'opaque')
            data = item.get('data', {})
            
            password_match = "no"
            
            if secret_type in ['kubernetes.io/dockerconfigjson', 'kubernetes.io/dockercfg']:
                if check_docker_config(data, serviceid, password, secret_type):
                    password_match = "yes"
            elif secret_type == 'opaque':
                if check_opaque_secret(data, serviceid, password):
                    password_match = "yes"
            
            results.append({
                'cluster_url': cluster_url,
                'namespace': namespace,
                'secret_name': secret_name,
                'secret_type': secret_type,
                'service_id_found': serviceid if password_match == "yes" else "",
                'password_match': password_match
            })
    
    return results

def read_clusters_from_file(filename):
    """Read cluster URLs from file."""
    with open(filename, 'r') as f:
        return [line.strip() for line in f if line.strip()]

def write_results_to_csv(results, filename):
    """Write validation results to CSV."""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, 'w', newline='') as csvfile:
        fieldnames = ['cluster_url', 'namespace', 'secret_name', 'secret_type', 'service_id_found', 'password_match']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

def main():
    serviceid = input("Enter service ID: ")
    password = getpass("Enter password: ")
    
    clusters_file = "clusters.txt"
    output_file = "output/validated.csv"
    
    if not os.path.exists(clusters_file):
        logging.error(f"Cluster file {clusters_file} not found")
        return
    
    clusters = read_clusters_from_file(clusters_file)
    if not clusters:
        logging.error("No clusters found in clusters.txt")
        return
    
    results = validate_secrets(serviceid, password, clusters)
    write_results_to_csv(results, output_file)
    logging.info(f"Validation completed. Results saved to {output_file}")

if __name__ == "__main__":
    main()
