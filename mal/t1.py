
def oc_login_to_generate_kubeconfig(cluster, username, password, kubeconfig_path):
    os.makedirs(os.path.dirname(kubeconfig_path), exist_ok=True)
    login_cmd = f"oc login {cluster} -u {username} -p {password} --insecure-skip-tls-verify=true --kubeconfig={kubeconfig_path}"
    print(f"[INFO] Attempting login to cluster: {cluster}")

    result = run_command(login_cmd)
    
    if result:
        print(f"[DEBUG] oc login output:\n{result}")
    else:
        print(f"[ERROR] oc login command did not return any output.")

    if "Logged into" in result and os.path.exists(kubeconfig_path):
        # Extra delay to ensure file is fully written (in case of timing issues)
        import time; time.sleep(1)
        print(f"[INFO] Successfully created kubeconfig: {kubeconfig_path}")
        return True
    else:
        print(f"[ERROR] Login failed for cluster: {cluster} → Output: {result}")
        return False

