import csv
import os
import time
import subprocess
import getpass
from datetime import datetime
from kubernetes import client, config
from kubernetes.config import ConfigException

# Configuration
CLUSTERS_FILE = "clusters.txt"
OUTPUT_CSV = "tia_maintainers.csv"
SLEEP_BETWEEN_CLUSTERS = 300  # 5 minutes in seconds

# System namespaces to ignore
SYSTEM_NAMESPACES = {
    'openshift-', 'kube-', 'vault', 'validate', 'open-cluster', 'cert-',
    'demo', 'lunks-toel', 'abclog', 'badard', 'mit', 'default',
    'ab-sandbox', 'ab-pmp', 'ab-admin', 'litmuz'
}

def is_system_namespace(namespace_name):
    """Check if namespace should be ignored"""
    return any(namespace_name.startswith(system_ns) for system_ns in SYSTEM_NAMESPACES)

def login_to_cluster(cluster_url, username, password):
    """Login to OpenShift cluster using oc command"""
    try:
        # Run oc login command
        login_cmd = [
            'oc', 'login', 
            '-u', username, 
            '-p', password, 
            cluster_url, 
            '--insecure-skip-tls-verify=true'
        ]
        process = subprocess.run(
            login_cmd, 
            check=True, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Load kubeconfig after successful login
        config.load_kube_config()
        return client.CoreV1Api()
    except subprocess.CalledProcessError as e:
        print(f"Login failed for cluster {cluster_url}")
        print(f"Error: {e.stderr.strip()}")
        return None

def get_cluster_details(filename):
    """Read cluster URLs from file"""
    clusters = []
    with open(filename, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                clusters.append(line)
    return clusters

def extract_tia_maintainer_info(api):
    """Extract TIA from labels and Maintainer from annotations"""
    tia_data = {}
    
    # Get all namespaces
    namespaces = api.list_namespace().items
    
    for ns in namespaces:
        ns_name = ns.metadata.name
        
        # Skip system namespaces
        if is_system_namespace(ns_name):
            continue
        
        # Get TIA from labels
        labels = ns.metadata.labels or {}
        tia = labels.get('tia')
        
        # Get maintainer from annotations
        annotations = ns.metadata.annotations or {}
        maintainer = annotations.get('abc.com/maintainer')
        
        if tia and maintainer:
            # Clean up the values
            tia = tia.strip().lower()
            maintainer = maintainer.strip().lower()
            
            # Add to our dictionary
            if tia not in tia_data:
                tia_data[tia] = set()
            tia_data[tia].add(maintainer)
    
    return tia_data

def update_csv(output_file, new_data):
    """Update the CSV file with new data, handling duplicates"""
    existing_data = {}
    
    # Read existing data if file exists
    if os.path.exists(output_file):
        with open(output_file, 'r') as f:
            reader = csv.reader(f)
            next(reader, None)  # Skip header
            for row in reader:
                if row:  # Skip empty rows
                    tia = row[0].strip().lower()
                    emails = set(email.strip().lower() for email in row[1:])
                    existing_data[tia] = emails
    
    # Merge new data with existing data
    for tia, emails in new_data.items():
        if tia in existing_data:
            existing_data[tia].update(emails)
        else:
            existing_data[tia] = emails
    
    # Write back to CSV
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Folder Name', 'Email Addresses'])
        for tia in sorted(existing_data.keys()):
            emails = sorted(existing_data[tia])
            writer.writerow([tia] + emails)

def main():
    # Get username and password
    username = input("Enter OpenShift username: ")
    password = getpass.getpass("Enter OpenShift password: ")
    
    clusters = get_cluster_details(CLUSTERS_FILE)
    
    if not clusters:
        print("No clusters found in clusters.txt")
        return
    
    all_tia_data = {}
    
    for i, cluster_url in enumerate(clusters):
        print(f"\nProcessing cluster {i+1}/{len(clusters)}: {cluster_url}")
        print(f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        try:
            # Login to cluster
            api = login_to_cluster(cluster_url, username, password)
            if api is None:
                continue
            
            # Extract TIA and Maintainer info
            cluster_tia_data = extract_tia_maintainer_info(api)
            print(f"Found {len(cluster_tia_data)} TIA entries in this cluster")
            
            # Merge with existing data
            for tia, emails in cluster_tia_data.items():
                if tia not in all_tia_data:
                    all_tia_data[tia] = set()
                all_tia_data[tia].update(emails)
            
            # Update CSV after each cluster
            update_csv(OUTPUT_CSV, cluster_tia_data)
            print(f"CSV file updated with data from this cluster")
            
        except Exception as e:
            print(f"Error processing cluster {cluster_url}: {str(e)}")
        
        # Sleep between clusters except after the last one
        if i < len(clusters) - 1:
            print(f"Waiting {SLEEP_BETWEEN_CLUSTERS//60} minutes before next cluster...")
            time.sleep(SLEEP_BETWEEN_CLUSTERS)
    
    print("\nProcessing complete!")
    print(f"Final data saved to {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
