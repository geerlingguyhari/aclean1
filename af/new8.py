import requests
import csv
import os
import json
import fcntl
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from getpass import getpass
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import urllib3
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EMAIL_MAPPING_FILE = "/tmp/test_emails.csv"
DEFAULT_EMAIL = "abc3@xyz.com"
CLEANUP_MESSAGE = "Please cleanup images older than 180 days using API: https://api.xyz.com/cleanup"
CLEANUP_DAYS = 180
MAX_EMAIL_SIZE = 25 * 1024 * 1024  # 25 MB email size limit

# Set up logging
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/tmp/artifactory_scanner.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration - Update these with your email settings
SMTP_SERVER = 'xyz.com'
SMTP_PORT = 25
EMAIL_FROM = 'xyz.com'
EMAIL_TO = 'xyz.com'

# Global variables
written_paths = set()
folder_size_history = {}
repository_name = ""
old_images_data = {}

# Configure retry strategy for requests
retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[403, 500, 502, 503, 504]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
http = requests.Session()
http.mount("https://", adapter)
http.mount("http://", adapter)

def get_writable_path(filename):
    tmp_path = f"/tmp/{filename}"
    if os.access("/tmp", os.W_OK):
        return tmp_path
    return os.path.expanduser(f"~/{filename}")

def load_history():
    global folder_size_history
    history_file = get_writable_path("artifactory_size_history.json")
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r') as f:
                loaded_history = json.load(f)
                for folder in loaded_history:
                    folder_size_history[folder] = [
                        (datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S"), float(size))
                        for date_str, size in loaded_history[folder]
                    ]
        except Exception as e:
            print(f"Warning: Could not load history file: {e}")

def save_history():
    history_file = get_writable_path("artifactory_size_history.json")
    try:
        save_data = {}
        for folder in folder_size_history:
            save_data[folder] = [
                (date.strftime("%Y-%m-%d %H:%M:%S"), str(size))
                for date, size in folder_size_history[folder]
            ]
        with open(history_file, 'w') as f:
            json.dump(save_data, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save history file: {e}")

def calculate_percentage_increase(folder_name, current_size_mb):
    if folder_name not in folder_size_history:
        return "N/A (First run)"
    
    history = folder_size_history[folder_name]
    if len(history) < 2:
        return "N/A (Need more data)"
    
    # Find the oldest entry within the last 30 days
    thirty_days_ago = datetime.now() - timedelta(days=30)
    recent_history = [(date, size) for date, size in history if date >= thirty_days_ago]
    
    if not recent_history:
        return "N/A (No recent data)"
    
    # Get the oldest and newest entries within the last 30 days
    oldest_date, oldest_size = min(recent_history, key=lambda x: x[0])
    newest_date, newest_size = max(recent_history, key=lambda x: x[0])
    
    # Calculate changes
    size_change_gb = (newest_size - oldest_size) / 1024
    try:
        percentage = ((newest_size - oldest_size) / oldest_size) * 100
        return f"{size_change_gb:+.2f} GB ({percentage:+.2f}%) since {oldest_date.strftime('%Y-%m-%d')}"
    except ZeroDivisionError:
        return "N/A (Division error)"

def safe_json_decode(response):
    try:
        return response.json()
    except json.decoder.JSONDecodeError:
        print(f"Warning: Could not decode JSON for URL: {response.url}")
        return None

def send_email_report(csv_file, folder_choice, report_scope):
    """Send email report with storage data"""
    if not os.path.exists(csv_file):
        print(f"Error: CSV file not found at {csv_file}")
        return False
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            data = []
            for row in reader:
                try:
                    data.append({
                        'folder': row['Main Folder'].strip(),
                        'mb': float(row['Size (MB)']),
                        'gb': float(row['Size (GB)']),
                        'tb': float(row['Size (TB)']),
                        'increase': row['30-Day Increase'].strip(),
                        'sort_key': float(row['Size (GB)'])
                    })
                except (ValueError, KeyError) as e:
                    print(f"Skipping malformed row: {row}. Error: {e}")
                    continue

            if not data:
                print("Error: No valid data found in CSV file")
                return False
            data.sort(key=lambda x: x['sort_key'], reverse=True)

        html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Artifactory Storage Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f2f2f2; }
        .number { text-align: right; }
    </style>
</head>
<body>
    <h2>Artifactory Storage Report</h2>
    <p><strong>Repository:</strong> {repo}</p>
    <p><strong>Scope:</strong> {scope}</p>
    <p><strong>Generated:</strong> {date}</p>
    <table>
        <tr>
            <th>Folder</th>
            <th>Size (GB)</th>
            <th>Size (TB)</th>
            <th>30-Day Change</th>
        </tr>
        {rows}
    </table>
</body>
</html>""".format(
            repo=repository_name,
            scope=report_scope,
            date=datetime.now().strftime('%Y-%m-%d %H:%M'),
            rows='\n'.join([
                f"<tr><td>{item['folder']}</td><td class='number'>{item['gb']:,.2f}</td>"
                f"<td class='number'>{item['tb']:,.3f}</td><td>{item['increase']}</td></tr>"
                for item in data
            ])
        )

        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        msg['To'] = EMAIL_TO
        msg['Subject'] = f"Artifactory Storage Report: {report_scope}"

        msg.attach(MIMEText(html, 'html'))

        with open(csv_file, 'rb') as f:
            attachment = MIMEText(f.read().decode('utf-8'), 'plain')
            attachment.add_header('Content-Disposition', 'attachment',
                               filename=f"storage_report_{datetime.now().strftime('%Y%m%d')}.csv")
            msg.attach(attachment)

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"Successfully sent email report with {len(data)} folders")
        return True

    except Exception as e:
        print(f"Error generating/sending report: {str(e)}")
        return False

def make_retry_request(url, auth, max_retries=3, retry_delay=1):
    """Make HTTP request with retry logic"""
    for attempt in range(max_retries):
        try:
            response = http.get(url, auth=auth, verify=False)
            if response.status_code == 200:
                return response
            elif response.status_code == 403 and attempt < max_retries - 1:
                print(f"Attempt {attempt + 1} of {max_retries}: 403 Forbidden for {url}")
                time.sleep(retry_delay)
        except Exception as e:
            print(f"Attempt {attempt + 1} of {max_retries}: Error accessing {url}: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
    return None

def collect_artifactory_data(base_url, path, auth, main_folder, writer, version_path=None):
    url = f"{base_url}{path}"
    response = make_retry_request(url, auth)
    if not response or response.status_code != 200:
        print(f"Error: Could not access URL after retries: {url}")
        return 0
    content = safe_json_decode(response)
    if not content:
        return 0
    total_size = 0

    if 'children' in content:
        for item in content['children']:
            item_path = f"{path}{item['uri']}"
            if item['folder']:
                total_size += collect_artifactory_data(base_url, item_path, auth, main_folder, writer, version_path=item_path)
            else:
                total_size += process_image(base_url, version_path, auth, main_folder, writer)
    return total_size

def process_image(base_url, version_path, auth, main_folder, writer):
    if version_path in written_paths:
        print(f"Skipping duplicate entry for {version_path}")
        return 0
    total_size_in_bytes = calculate_total_size(base_url, version_path, auth)
    size_in_mb = f"{total_size_in_bytes / (1024 * 1024):.2f}" if total_size_in_bytes > 0 else 'N/A'
    creation_time, last_used_time = get_image_time_info(base_url, version_path, auth)
    writer.writerow([repository_name, main_folder, version_path, creation_time, last_used_time, size_in_mb])
    written_paths.add(version_path)
    return total_size_in_bytes

def calculate_total_size(base_url, path, auth):
    total_size = 0
    url = f"{base_url}{path}"
    response = make_retry_request(url, auth)
    if not response or response.status_code != 200:
        print(f"Error: Could not access URL after retries: {url}")
        return total_size
    content = safe_json_decode(response)
    if not content:
        return total_size
    if 'children' in content:
        for item in content['children']:
            if not item['folder']:
                item_url = f"{base_url}{path}{item['uri']}"
                item_response = make_retry_request(item_url, auth)
                if not item_response or item_response.status_code != 200:
                    continue
                item_data = safe_json_decode(item_response)
                if item_data:
                    total_size += int(item_data.get('size', 0))
    return total_size

def get_image_time_info(base_url, path, auth):
    creation_time, last_used_time = 'N/A', 'N/A'
    url = f"{base_url}{path}"
    response = make_retry_request(url, auth)
    if not response or response.status_code != 200:
        return creation_time, last_used_time
    content = safe_json_decode(response)
    if not content:
        return creation_time, last_used_time
    if 'children' in content:
        for item in content['children']:
            if not item['folder']:
                item_url = f"{base_url}{path}{item['uri']}"
                item_response = make_retry_request(item_url, auth)
                if not item_response or item_response.status_code != 200:
                    continue
                item_data = safe_json_decode(item_response)
                if item_data:
                    creation_time = item_data.get('created', 'N/A')
                    last_used_time = item_data.get('lastDownloaded', item_data.get('lastModified', 'N/A'))
                    break
    return creation_time, last_used_time

def find_old_images(base_url, path, auth, folder_name):
    """Find images older than CLEANUP_DAYS days"""
    cutoff_date = datetime.now() - timedelta(days=CLEANUP_DAYS)
    old_images = []
    
    def scan_directory(current_path):
        url = f"{base_url}{current_path}"
        response = make_retry_request(url, auth)
        if not response or response.status_code != 200:
            return
        
        content = safe_json_decode(response)
        if not content or 'children' not in content:
            return
            
        for item in content['children']:
            item_path = f"{current_path}{item['uri']}"
            if item['folder']:
                scan_directory(item_path)
            else:
                # Get file details
                file_url = f"{base_url}{item_path}"
                file_response = make_retry_request(file_url, auth)
                if not file_response or file_response.status_code != 200:
                    continue
                    
                file_data = safe_json_decode(file_response)
                if file_data:
                    created_str = file_data.get('created', '')
                    if created_str:
                        try:
                            created_date = datetime.strptime(created_str.split('.')[0], "%Y-%m-%dT%H:%M:%S")
                            if created_date < cutoff_date:
                                old_images.append({
                                    'path': item_path,
                                    'created': created_str,
                                    'size_bytes': file_data.get('size', 0),
                                    'size_mb': round(file_data.get('size', 0) / (1024 * 1024), 2)
                                })
                        except ValueError:
                            continue
    scan_directory(path)
    old_images_data[folder_name] = old_images
    return old_images

def process_main_folder(base_url, folder_name, username, password, output_writer, total_size_writer):
    # First find old images for this folder
    find_old_images(base_url, f"{folder_name}", (username, password), folder_name)
    
    # Then collect regular data
    total_size_in_bytes = collect_artifactory_data(base_url, f"{folder_name}", (username, password), folder_name, output_writer)
    total_size_mb = total_size_in_bytes / (1024 * 1024)
    total_size_gb = total_size_in_bytes / (1024 ** 3)
    total_size_tb = total_size_in_bytes / (1024 ** 4)
    current_date = datetime.now()
    if folder_name not in folder_size_history:
        folder_size_history[folder_name] = []
    folder_size_history[folder_name].append((current_date, total_size_mb))
    folder_size_history[folder_name] = [
        (date, size) for date, size in folder_size_history[folder_name]
        if date > current_date - timedelta(days=90)
    ]
    percentage_increase = calculate_percentage_increase(folder_name, total_size_mb)
    total_size_writer.writerow([
        repository_name,
        folder_name,
        f"{total_size_mb:.2f}",
        f"{total_size_gb:.2f}",
        f"{total_size_tb:.3f}",
        percentage_increase
    ])

def load_email_mappings():
    """Load folder to email mappings from CSV"""
    mappings = {}
    try:
        if os.path.exists(EMAIL_MAPPING_FILE):
            with open(EMAIL_MAPPING_FILE, 'r') as f:
                reader = csv.reader(f)
                next(reader)  # Skip header
                for row in reader:
                    if len(row) >= 2:
                        folder = row[0].strip()
                        emails = [email.strip() for email in row[1:] if email.strip()]
                        mappings[folder] = emails
                        logger.debug(f"Loaded email mapping for {folder}: {emails}")
    except Exception as e:
        logger.error(f"Error loading email mappings: {e}")
    return mappings

def send_individual_folder_email(folder_data, recipients, custom_body=None):
    """Send email for individual folder report"""
    max_attempts = 2  # Initial attempt + one retry without attachments if needed
    attempt = 0
    include_attachments = True
    
    if not isinstance(recipients, list):
        recipients = [recipients] if recipients else []
        recipients = [email.strip() for email in recipients if email.strip()]
        
    primary_recipients = [email for email in recipients if email != DEFAULT_EMAIL]
    cc_recipients = [DEFAULT_EMAIL] if DEFAULT_EMAIL not in primary_recipients and DEFAULT_EMAIL else []

    logger.info(f"Preparing to send email for folder {folder_data['folder']}")
    logger.info(f"Primary recipients: {primary_recipients}")
    logger.info(f"CC recipients: {cc_recipients}")    
    
    while attempt < max_attempts:
        try:
            msg = MIMEMultipart()
            msg['From'] = EMAIL_FROM
            
            msg['To'] = ", ".join(primary_recipients) if primary_recipients else DEFAULT_EMAIL
            if cc_recipients:
                msg['Cc'] = ", ".join(cc_recipients)
                
            msg['Subject'] = f"Artifactory Folder Report: {folder_data['folder']}"

            old_images = old_images_data.get(folder_data['folder'], [])
            
            trend_text = folder_data['increase']
            trend_arrow = ""
            trend_class = ""
            
            if '(+' in trend_text:
                trend_arrow = "⬆️"
                trend_class = "increase-positive"
            elif '(-)' in trend_text:
                trend_arrow = "⬇️"
                trend_class = "increase-negative"
            
            html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Artifactory Folder Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; }}
        .info-card {{ background: #f8f9fa; padding: 15px; margin-bottom: 15px; border-left: 4px solid #6c757d; }}
        .info-label {{ font-weight: bold; min-width: 150px; display: inline-block; }}
        .increase-positive {{ color: green; }}
        .increase-negative {{ color: red; }}
    </style>
</head>
<body>
    <h2>Artifactory Storage Report</h2>
    
    <div class="info-card">
        <h3>📁 Folder Information</h3>
        <p><span class="info-label">Folder Name:</span> {folder_data['folder']}</p>
        <p><span class="info-label">Repository:</span> {repository_name}</p>
    </div>
    
    <div class="info-card">
        <h3>📊 Size Information</h3>
        <p><span class="info-label">Current Size (GB):</span> {folder_data['gb']:,.2f}</p>
        <p><span class="info-label">Current Size (TB):</span> {folder_data['tb']:,.3f}</p>
    </div>
    
    <div class="info-card">
        <h3>📈 Storage Trend</h3>
        <p><span class="info-label">Change:</span> 
            <span class="{trend_class}">{trend_arrow} {folder_data['increase']}</span>
        </p>
    </div>
    
    <div class="info-card">
        <h3>⚠️ Cleanup Recommendation</h3>
        <p>{CLEANUP_MESSAGE}</p>
        <p>Found {len(old_images)} images older than {CLEANUP_DAYS} days {'' if include_attachments else '(details not included due to email size limits)'}</p>
    </div>
</body>
</html>"""

            msg.attach(MIMEText(html, 'html'))
            
            # Add CSV attachment with folder summary
            csv_data = [
                ["Repository", "Main Folder", "Size (MB)", "Size (GB)", "Size (TB)", "30-Day Increase"],
                [
                    repository_name,
                    folder_data['folder'],
                    f"{folder_data['mb']:.2f}",
                    f"{folder_data['gb']:.2f}",
                    f"{folder_data['tb']:.3f}",
                    folder_data['increase']
                ]
            ]
            
            import io
            csv_buffer = io.StringIO()
            csv_writer = csv.writer(csv_buffer)
            csv_writer.writerows(csv_data)
            
            attachment = MIMEText(csv_buffer.getvalue(), 'plain')
            attachment.add_header('Content-Disposition', 'attachment',
                               filename=f"{folder_data['folder']}_storage_summary_{datetime.now().strftime('%Y%m%d')}.csv")
            msg.attach(attachment)
            
            # Add old images CSV if any and if we're including attachments
            if old_images and include_attachments:
                old_images_csv = [
                    ["Image Path", "Created Date", "Size (MB)"]
                ]
                for img in sorted(old_images, key=lambda x: x['created']):  # Sort by creation date
                    old_images_csv.append([
                        img['path'],
                        img['created'],
                        f"{img['size_mb']:.2f}"
                    ])
                
                csv_buffer = io.StringIO()
                csv_writer = csv.writer(csv_buffer)
                csv_writer.writerows(old_images_csv)
                
                attachment = MIMEText(csv_buffer.getvalue(), 'plain')
                attachment.add_header('Content-Disposition', 'attachment',
                                   filename=f"{folder_data['folder']}_old_images_{datetime.now().strftime('%Y%m%d')}.csv")
                msg.attach(attachment)

            # Check message size
            msg_size = len(msg.as_bytes())
            if msg_size > MAX_EMAIL_SIZE and include_attachments:
                logger.warning(f"Email too large ({msg_size/1024:.1f}KB), retrying without attachments...")
                include_attachments = False
                attempt += 1
                continue

            # Send email to all recipients (To + Cc)
            all_recipients = primary_recipients + cc_recipients
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())
                
            logger.info(f"Successfully sent individual report for {folder_data['folder']} to {msg['To']}" + (f", CC: {msg['Cc']}" if cc_recipients else ""))
            return True

        except smtplib.SMTPDataError as e:
            if "exceeds size limit" in str(e) and include_attachments:
                logger.warning(f"Email too large, retrying without attachments...")
                include_attachments = False
                attempt += 1
                continue
            logger.error(f"Error sending individual email for {folder_data['folder']}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error sending individual email for {folder_data['folder']}: {e}")
            return False
    
    logger.error(f"Failed to send email for {folder_data['folder']} after {max_attempts} attempts")
    return False

def send_individual_emails(total_size_file, size_filter="all"):
    """Send individual emails based on the size filter"""
    email_mappings = load_email_mappings()
    sent_count = 0
    
    with open(total_size_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                folder_name = row['Main Folder'].strip()
                size_gb = float(row['Size (GB)'])
                
                if size_filter == "500gb" and size_gb < 500:
                    continue
                if size_filter == "1tb" and size_gb < 1024:
                    continue
                
                recipients = email_mappings.get(folder_name, [DEFAULT_EMAIL])
                
                folder_data = {
                    'folder': folder_name,
                    'mb': float(row['Size (MB)']),
                    'gb': size_gb,
                    'tb': float(row['Size (TB)']),
                    'increase': row['30-Day Increase']
                }
                
                custom_body = f"""Hello Team,

As part of our storage optimization efforts and upcoming quota enforcement, we request your support in cleaning up unused images older than 180 days.

Your team is currently using {folder_data['gb']:.2f}GB of storage. Attached is a list of images older than 180 days—please review and remove those no longer needed.
Further details are provided below. Thank you for your cooperation.
"""
                if send_individual_folder_email(folder_data, recipients, custom_body):
                    sent_count += 1
                    
            except (ValueError, KeyError) as e:
                print(f"Skipping malformed row for folder {folder_name}: {e}")
                continue
    
    return sent_count

def main():
    global repository_name
    artifactory_url = "https://registry-xyz.com"
    repository_name = "registry-local-docker-nonprod"

    username = input("Enter Artifactory username: ")
    password = getpass("Enter Artifactory password: ")
    repo_base_url = f"{artifactory_url}/artifactory/api/storage/{repository_name}/"
    
    load_history()

    folder_choice = input("Enter a main folder number to process, or 'all' to process all main folders: ")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if folder_choice.lower() == "all":
        print("\nSelect which folders to include in email report:")
        print("1. All folders (default)")
        print("2. Folders above 500GB")
        print("3. Folders above 1TB")
        size_filter = input("Enter your choice (1-3): ").strip() or "1"

        print("\nSelect individual email options:")
        print("1. Don't send individual emails (default)")
        print("2. Send individual emails for all folders")
        print("3. Send individual emails for folders above 500GB")
        print("4. Send individual emails for folders above 1TB")
        email_option = input("Enter your choice (1-4): ").strip() or "1"

        output_file = get_writable_path(f"artifactory_data_{timestamp}.csv")
        total_size_file = get_writable_path(f"artifactory_total_size_{timestamp}.csv")
        lock_file = "/tmp/artifactory_script.lock"

        with open(lock_file, "w") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)

                response = make_retry_request(repo_base_url, (username, password))
                if not response or response.status_code != 200:
                    print(f"Error: Could not access URL after retries: {repo_base_url}")
                    return
                repo_content = safe_json_decode(response)
                if not repo_content:
                    return

                main_folders = [folder['uri'].strip('/') for folder in repo_content['children'] if folder['folder']]

                if not main_folders:
                    print("No folders found to process.")
                    return

                with open(output_file, 'w', newline='') as output_csv, \
                     open(total_size_file, 'w', newline='') as total_csv:
                    output_writer = csv.writer(output_csv)
                    total_size_writer = csv.writer(total_csv)
                    
                    output_writer.writerow(["Repository", "Main Folder", "Image Path", "Created", "Last Used", "Size (MB)"])
                    total_size_writer.writerow(["Repository", "Main Folder", "Size (MB)", "Size (GB)", "Size (TB)", "30-Day Increase"])
 
                    with ThreadPoolExecutor(max_workers=5) as executor:
                        futures = [
                            executor.submit(
                                process_main_folder,
                                repo_base_url,
                                folder,
                                username,
                                password,
                                output_writer,
                                total_size_writer
                            ) for folder in main_folders
                        ]
                        for future in futures:
                            future.result()

                filtered_file = None
                if size_filter in ("2", "3"):
                    threshold_gb = 500 if size_filter == "2" else 1024
                    filtered_file = get_writable_path(f"artifactory_filtered_{threshold_gb}GB_{timestamp}.csv")
                    with open(total_size_file, 'r') as infile, open(filtered_file, 'w', newline='') as outfile:
                        reader = csv.reader(infile)
                        writer = csv.writer(outfile)

                        writer.writerow(next(reader))
                        for row in reader:
                            try:
                                size_gb = float(row[3])
                                if size_gb >= threshold_gb:
                                    writer.writerow(row)
                            except (ValueError, IndexError):
                                continue

                if os.path.exists(total_size_file) and os.path.getsize(total_size_file) > 0:
                    if size_filter == "1":
                        send_email_report(total_size_file, folder_choice, "All Folders")
                    elif filtered_file and os.path.exists(filtered_file):
                        send_email_report(
                            filtered_file,
                            folder_choice,
                            f"Folders above {'1TB' if size_filter == '3' else '500GB'}"
                        )
                    
                    if email_option in ("2", "3", "4"):
                        size_filter_map = {
                            "2": "all",
                            "3": "500gb",
                            "4": "1tb"
                        }
                        filter_type = size_filter_map[email_option]
                        print(f"\nSending individual emails for folders ({filter_type})...")
                        sent_count = send_individual_emails(total_size_file, filter_type)
                        print(f"Sent {sent_count} individual email reports")
                        
                else:
                    print("Error: Total size file not created properly, skipping email")

            except BlockingIOError:
                print("Script is already running. Exiting.")
                return
            except Exception as e:
                print(f"Error processing all folders: {e}")
                return
            finally:
                try:
                    os.remove(lock_file)
                except:
                    pass
    else:
        output_file = get_writable_path(f"{folder_choice}_output_{timestamp}.csv")
        total_size_file = get_writable_path(f"{folder_choice}_total_size_{timestamp}.csv")

        try:
            with open(output_file, 'w', newline='') as output_csv, \
                 open(total_size_file, 'w', newline='') as total_csv:
                output_writer = csv.writer(output_csv)
                total_size_writer = csv.writer(total_csv)

                output_writer.writerow(["Repository", "Main Folder", "Image Path", "Created", "Last Used", "Size (MB)"])
                total_size_writer.writerow(["Repository", "Main Folder", "Size (MB)", "Size (GB)", "Size (TB)", "30-Day Increase"])

                process_main_folder(repo_base_url, folder_choice, username, password, output_writer, total_size_writer)

            if os.path.exists(total_size_file) and os.path.getsize(total_size_file) > 0:
                with open(total_size_file, 'r') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row['Main Folder'].strip() == folder_choice:
                            folder_data = {
                                'folder': folder_choice,
                                'mb': float(row['Size (MB)']),
                                'gb': float(row['Size (GB)']),
                                'tb': float(row['Size (TB)']),
                                'increase': row['30-Day Increase']
                            }
                            break
                    else:
                        folder_data = None
                
                if folder_data:
                    email_mappings = load_email_mappings()
                    recipients = email_mappings.get(folder_choice, [DEFAULT_EMAIL])
                    
                    custom_body = f"""Hello Team,

As part of our storage optimization efforts and upcoming quota enforcement, we request your support in cleaning up unused images older than 180 days.

Your team is currently using {folder_data['gb']:.2f}GB of storage. Attached is a list of images older than 180 days—please review and remove those no longer needed.
Further details are provided below. Thank you for your cooperation.
"""
                    send_individual_folder_email(folder_data, recipients, custom_body)
            else:
                print("Error: Total size file not created properly, skipping email")
        except Exception as e:
            print(f"Error processing folder {folder_choice}: {e}")
            return

    save_history()
    print(f"\nProcessing complete. Results saved to:\n- Details: {output_file}\n- Summary: {total_size_file}")

if __name__ == "__main__":
    main()
    
    
"""=======
=========================================
============================================
ACTUAL CHANGES MADE TO THE ORIGINAL SCRIPT
"""
# In the find_old_images function, modify the old_images.append part to include size in MB:
old_images.append({
    'path': item_path,
    'created': created_str,
    'size_bytes': file_data.get('size', 0),
    'size_mb': round(file_data.get('size', 0) / (1024 * 1024), 2)
})

# In the send_individual_folder_email function, update the old_images_csv creation:
old_images_csv = [
    ["Image Path", "Created Date", "Size (MB)"]
]
for img in sorted(old_images, key=lambda x: x['created']):  # Sort by creation date
    old_images_csv.append([
        img['path'],
        img['created'],
        f"{img['size_mb']:.2f}"
    ])

# Update the calculate_percentage_increase function:
def calculate_percentage_increase(folder_name, current_size_mb):
    if folder_name not in folder_size_history:
        return "N/A (First run)"
    
    history = folder_size_history[folder_name]
    if len(history) < 2:
        return "N/A (Need more data)"
    
    # Find the oldest entry within the last 30 days
    thirty_days_ago = datetime.now() - timedelta(days=30)
    recent_history = [(date, size) for date, size in history if date >= thirty_days_ago]
    
    if not recent_history:
        return "N/A (No recent data)"
    
    # Get the oldest and newest entries within the last 30 days
    oldest_date, oldest_size = min(recent_history, key=lambda x: x[0])
    newest_date, newest_size = max(recent_history, key=lambda x: x[0])
    
    # Calculate changes
    size_change_gb = (newest_size - oldest_size) / 1024
    try:
        percentage = ((newest_size - oldest_size) / oldest_size) * 100
        return f"{size_change_gb:+.2f} GB ({percentage:+.2f}%) since {oldest_date.strftime('%Y-%m-%d')}"
    except ZeroDivisionError:
        return "N/A (Division error)"
