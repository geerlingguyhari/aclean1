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

# Configuration - Update these with your email settings
SMTP_SERVER = 'xyz.com'
SMTP_PORT = 25
EMAIL_FROM = 'xyz.com'
EMAIL_TO = 'xyz.com'

# Global variables
written_paths = set()
folder_size_history = {}
repository_name = ""

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

def safe_json_decode(response):
    try:
        return response.json()
    except json.decoder.JSONDecodeError:
        print(f"Warning: Could not decode JSON for URL: {response.url}")
        return None

def calculate_percentage_increase(folder_name, current_size_mb):
    if folder_name not in folder_size_history:
        return "N/A (First run)"
    history = folder_size_history[folder_name]
    if len(history) < 2:
        return "N/A (Need more data)"
    thirty_days_ago = datetime.now() - timedelta(days=30)
    relevant_data = [(date, size) for date, size in history if date >= thirty_days_ago]
    if not relevant_data:
        return "N/A (No recent data)"
    oldest_date, oldest_size = min(relevant_data, key=lambda x: x[0])
    if oldest_size == 0:
        return "N/A (Zero initial size)"
    try:
        percentage = ((current_size_mb - oldest_size) / oldest_size) * 100
        return f"{percentage:.2f}% (since {oldest_date.strftime('%Y-%m-%d')})"
    except ZeroDivisionError:
        return "N/A (Division error)"

def send_email_report(csv_file, folder_choice, report_scope):
    """
    Final refined email report with:
    - Clean left-aligned info section
    - Removed all background lines
    - Added banner header
    - Simplified storage trend display
    """

    # Verify file exists
    if not os.path.exists(csv_file):
        print(f"Error: CSV file not found at {csv_file}")
        return False
    try:
        # Load historical data for comparison
        history_file = get_writable_path("artifactory_size_history.json")
        history_data = {}
        if os.path.exists(history_file):
            with open(history_file, 'r') as f:
                history_data = json.load(f)
                
        # Read and process CSV data
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            data = []
            for row in reader:
                try:
                    # Convert sizes
                    mb = float(row['Size (MB)'])
                    gb = float(row['Size (GB)'])
                    tb = float(row['Size (TB)'])

                    # Round near-zero values
                    mb_rounded = round(mb, 3) if mb < 0.001 else round(mb, 2)
                    gb_rounded = round(gb, 3) if gb < 0.001 else round(gb, 2)
                    tb_rounded = round(tb, 4) if tb < 0.001 else round(tb, 3)
                   
                    # Process percentage increase with better history comparison
                    folder_name = row['Main Folder'].strip()
                    increase_text = row['30-Day Increase'].strip()

                    # Enhanced history comparison
                    if folder_name in history_data:
                        history_entries = history_data[folder_name]
                        valid_entries = [float(size) for date, size in history_entries if float(size) > 0]
                        if valid_entries:
                            oldest_valid = min(valid_entries)
                            current_gb = gb
                            if oldest_valid > 0:
                                pct_change = ((current_gb - oldest_valid) / oldest_valid) * 100
                                arrow = "↑" if pct_change >= 0 else "↓"
                                increase_text = f"{arrow} {abs(pct_change):.2f}% (since first valid record)"
                    data.append({
                        'folder': folder_name,
                        'mb': mb_rounded,
                        'gb': gb_rounded,
                        'tb': tb_rounded,
                        'increase': increase_text,
                        'sort_key': gb
                    })                    
                except (ValueError, KeyError) as e:
                    print(f"Skipping malformed row: {row}. Error: {e}")
                    continue

            if not data:
                print("Error: No valid data found in CSV file")
                return False
            # Sort data by size (GB) in descending order
            data.sort(key=lambda x: x['sort_key'], reverse=True)

        # Prepare HTML content with updated styling
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Artifactory Storage Report</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&family=Source+Code+Pro&display=swap');

        body {{
            font-family: 'Roboto', sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 1000px;
            margin: 0 auto;
            padding: 0;
            background-color: #f9f9f9;
        }}

        .email-container {{
            background-color: white;
            border-radius: 0;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
            border: 1px solid #d1d5db;
        }}

        .banner {{
            background: linear-gradient(135deg, #3498db, #2c3e50);
            color: white;
            padding: 30px 25px;
            text-align: left;
            border-bottom: 4px solid #2980b9;
        }}

        .banner h1 {{
            margin: 0;
            font-weight: 500;
            font-size: 32px;
            letter-spacing: 0.5px;
        }}

        .info-section {{
            padding: 20px 25px;
            text-align: left;
            background-color: white;
            border-bottom: 1px solid #e2e8f0;
        }}

        .info-line {{
            margin-bottom: 8px;
            display: flex;
        }}

        .info-label {{
            font-weight: 500;
            color: #4a5568;
            font-size: 14px;
            min-width: 120px;
        }}

        .info-value {{
            font-weight: 600;
            font-size: 14px;
            color: #2d3748;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 0;
            font-size: 14px;
        }}

        th {{
            background-color: #f8fafc;
            color: #2c3e50;
            text-align: left;
            padding: 12px 15px;
            font-weight: 600;
            border-bottom: 2px solid #e2e8f0;
        }}

        td {{
            padding: 12px 15px;
            border-bottom: 1px solid #e2e8f0;
        }}

        .folder-name {{
            background-color: #f8fafc;
            font-weight: 500;
        }}

        .size-gb {{
            background-color: #f0fff4;
        }}

        .size-tb {{
            background-color: #fff0f0;
        }}

        .trend-cell {{
            background-color: #fffaf0;
        }}

        .number {{
            font-family: 'Source Code Pro', monospace;
            text-align: right;
        }}

        .increase-positive {{
            color: #27ae60;
            font-weight: 500;
        }}

        .increase-negative {{
            color: #e74c3c;
            font-weight: 500;
        }}

        .highlight {{
            background-color: #e3f2fd !important;
            font-weight: 600;
        }}

        .footer {{
            padding: 15px;
            text-align: center;
            font-size: 12px;
            color: #718096;
            background-color: #f8fafc;
        }}
    </style>
</head>
<body>
    <div class="email-container">
        <!-- Banner Header -->
        <div class="banner">
            <h1>Artifactory Storage Report</h1>
        </div>

        <!-- Left-aligned Info Section -->
        <div class="info-section">
            <div class="info-line">
                <div class="info-label">Repository:</div>
                <div class="info-value">{repository_name}</div>
            </div>
            
            <div class="info-line">
                <div class="info-label">Report Scope:</div>
                <div class="info-value">{report_scope}</div>
            </div>
            
            <div class="info-line">
                <div class="info-label">Generated:</div>
                <div class="info-value">{datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
            </div>

            <div class="info-line">
                <div class="info-label">Folders Analyzed:</div>
                <div class="info-value">{len(data)}</div>
            </div>
        </div>

        <!-- Main Data Table -->
        <table>
            <thead>
                <tr>
                    <th>#</th>
                    <th>Folder Name</th>
                    <th class="number">Size (GB)</th>
                    <th class="number">Size (TB)</th>
                    <th>Storage Trend</th>
                </tr>
            </thead>
            <tbody>"""

        # Add data rows with simplified trend display
        for rank, item in enumerate(data, start=1):
            # Calculate absolute size change
            trend_text = item['increase']
            if folder_name in history_data and history_data[folder_name]:
                valid_entries = [float(size) for date, size in history_data[folder_name] if float(size) > 0]
                if valid_entries:
                    oldest_gb = min(valid_entries)
                    current_gb = item['gb']
                    change_gb = current_gb - oldest_gb
                    if change_gb > 0:
                        trend_text = f"↑ {abs(change_gb):.2f}GB"
                    elif change_gb < 0:
                        trend_text = f"↓ {abs(change_gb):.2f}GB"
                    else:
                        trend_text = "No change"

            html += f"""
                <tr>
                    <td class="number">{rank}</td>
                    <td class="folder-name">{item['folder']}</td>
                    <td class="number size-gb">{item['gb']:,.2f}</td>
                    <td class="number size-tb">{item['tb']:,.3f}</td>
                    <td class="trend-cell">
                        <span class="{{ 'increase-positive' if '↑' in trend_text else 'increase-negative' if '↓' in trend_text else '' }}">
                            {trend_text}
                        </span>
                    </td>
                </tr>"""

        # Calculate totals
        total_gb = sum(item['gb'] for item in data)
        total_tb = sum(item['tb'] for item in data)

        html += f"""
                <tr class="highlight">
                    <td colspan="2"><strong>Total Storage</strong></td>
                    <td class="number"><strong>{total_gb:,.2f}</strong></td>
                    <td class="number"><strong>{total_tb:,.3f}</strong></td>
                    <td></td>
                </tr>
            </tbody>
        </table>

        <div class="footer">
            <p>This report was automatically generated by the Artifactory Storage Scanner</p>
        </div>
    </div>
</body>
</html>"""

        # Create and send email
        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        msg['To'] = EMAIL_TO
        msg['Subject'] = f"📊 Artifactory Storage Report: {report_scope} | {datetime.now().strftime('%b %d, %Y')}"

        # Attach HTML content
        msg.attach(MIMEText(html, 'html'))

        # Add CSV as attachment
        with open(csv_file, 'rb') as f:
            attachment = MIMEText(f.read().decode('utf-8'), 'plain')
            attachment.add_header('Content-Disposition', 'attachment',
                               filename=f"storage_report_{datetime.now().strftime('%Y%m%d')}.csv")
            msg.attach(attachment)

        # Send email
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"🎉 Successfully sent colorful email report with {len(data)} folders")
        return True

    except Exception as e:
        print(f"❌ Error generating/sending report: {str(e)}")
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

def process_main_folder(base_url, folder_name, username, password, output_writer, total_size_writer):
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
                next(reader) # Skip header
                for row in reader:
                    if len(row) >= 2:
                        folder = row[0].strip()
                        emails = [e.strip() for e in row[1].split(',') if e.strip()]
                        mappings[folder] = emails
    except Exception as e:
        print(f"Error loading email mappings: {e}")
    return mappings

def send_individual_folder_email(folder_data, recipients):
    """Send email for individual folder report"""
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        msg['To'] = ", ".join(recipients)
        msg['Subject'] = f"Artifactory Folder Report: {folder_data['folder']}"

        # Create HTML content
        html = f"""<html>
<head>
    <meta charset="UTF-8">
    <title>Artifactory Folder Report</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
        }}
        .header {{
            background-color: #2c3e50;
            color: white;
            padding: 20px;
            text-align: center;
            margin-bottom: 20px;
        }}
        .content {{
            padding: 20px;
            background-color: #f9f9f9;
            border: 1px solid #ddd;
        }}
        .footer {{
            margin-top: 20px;
            text-align: center;
            font-size: 12px;
            color: #777;
        }}
        .info-label {{
            font-weight: bold;
            width: 150px;
            display: inline-block;
        }}
        .info-value {{
            margin-left: 10px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Artifactory Folder Storage Report</h1>
    </div>
    
    <div class="content">
        <p><span class="info-label">Folder Name:</span> <span class="info-value">{folder_data['folder']}</span></p>
        <p><span class="info-label">Repository:</span> <span class="info-value">{repository_name}</span></p>
        <p><span class="info-label">Current Size (GB):</span> <span class="info-value">{folder_data['gb']:,.2f}</span></p>
        <p><span class="info-label">Current Size (TB):</span> <span class="info-value">{folder_data['tb']:,.3f}</span></p>
        <p><span class="info-label">Storage Trend:</span> <span class="info-value">{folder_data['increase']}</span></p>
    </div>
    
    <div class="footer">
        <p>This report was automatically generated by the Artifactory Storage Scanner</p>
    </div>
</body>
</html>"""

        msg.attach(MIMEText(html, 'html'))
        
        # Add CSV attachment with just this folder's data
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
        
        # Create CSV attachment in memory
        import io
        csv_buffer = io.StringIO()
        csv_writer = csv.writer(csv_buffer)
        csv_writer.writerows(csv_data)
        
        attachment = MIMEText(csv_buffer.getvalue(), 'plain')
        attachment.add_header('Content-Disposition', 'attachment',
                           filename=f"{folder_data['folder']}_storage_report_{datetime.now().strftime('%Y%m%d')}.csv")
        msg.attach(attachment)

        # Send email
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
            server.sendmail(EMAIL_FROM, recipients, msg.as_string())
        print(f"Sent individual report for {folder_data['folder']} to {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"Error sending individual email for {folder_data['folder']}: {e}")
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
                
                # Check size filter
                if size_filter == "500gb" and size_gb < 500:
                    continue
                if size_filter == "1tb" and size_gb < 1024:
                    continue
                
                # Get recipients
                recipients = email_mappings.get(folder_name, [DEFAULT_EMAIL])
                if not recipients:
                    recipients = [DEFAULT_EMAIL]
                
                # Prepare folder data
                folder_data = {
                    'folder': folder_name,
                    'mb': float(row['Size (MB)']),
                    'gb': size_gb,
                    'tb': float(row['Size (TB)']),
                    'increase': row['30-Day Increase']
                }
                
                # Send email
                if send_individual_folder_email(folder_data, recipients):
                    sent_count += 1
                    
            except (ValueError, KeyError) as e:
                print(f"Skipping malformed row for folder {folder_name}: {e}")
                continue
    
    return sent_count

def main():
    global repository_name
    artifactory_url = "https://registry-xyz.com"
    repository_name = "registry-local-docker-nonprod"

    # Get credentials
    username = input("Enter Artifactory username: ")
    password = getpass("Enter Artifactory password: ")
    repo_base_url = f"{artifactory_url}/artifactory/api/storage/{repository_name}/"
    
    # Load existing history data
    load_history()

    # Get user input for processing
    folder_choice = input("Enter a main folder number to process, or 'all' to process all main folders: ")

    # Create timestamp for output files
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if folder_choice.lower() == "all":
        # Get size filter preference
        print("\nSelect which folders to include in email report:")
        print("1. All folders (default)")
        print("2. Folders above 500GB")
        print("3. Folders above 1TB")
        size_filter = input("Enter your choice (1-3): ").strip() or "1"

        # Get individual email preference
        print("\nSelect individual email options:")
        print("1. Don't send individual emails (default)")
        print("2. Send individual emails for all folders")
        print("3. Send individual emails for folders above 500GB")
        print("4. Send individual emails for folders above 1TB")
        email_option = input("Enter your choice (1-4): ").strip() or "1"

        # Process all folders
        output_file = get_writable_path(f"artifactory_data_{timestamp}.csv")
        total_size_file = get_writable_path(f"artifactory_total_size_{timestamp}.csv")
        lock_file = "/tmp/artifactory_script.lock"

        with open(lock_file, "w") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)

                # Get repository contents
                response = make_retry_request(repo_base_url, (username, password))
                if not response or response.status_code != 200:
                    print(f"Error: Could not access URL after retries: {repo_base_url}")
                    return
                repo_content = safe_json_decode(response)
                if not repo_content:
                    return

                # Get all main folders
                main_folders = [folder['uri'].strip('/') for folder in repo_content['children'] if folder['folder']]

                if not main_folders:
                    print("No folders found to process.")
                    return

                # Open output files
                with open(output_file, 'w', newline='') as output_csv, \
                     open(total_size_file, 'w', newline='') as total_csv:
                    output_writer = csv.writer(output_csv)
                    total_size_writer = csv.writer(total_csv)
                    
                    # Write headers
                    output_writer.writerow(["Repository", "Main Folder", "Image Path", "Created", "Last Used", "Size (MB)"])
                    total_size_writer.writerow(["Repository", "Main Folder", "Size (MB)", "Size (GB)", "Size (TB)", "30-Day Increase"])
 
                    # Process folders in parallel
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
                            future.result() # Wait for all to complete

                # Create filtered version if needed
                filtered_file = None
                if size_filter in ("2", "3"):
                    threshold_gb = 500 if size_filter == "2" else 1024
                    filtered_file = get_writable_path(f"artifactory_filtered_{threshold_gb}GB_{timestamp}.csv")
                    with open(total_size_file, 'r') as infile, open(filtered_file, 'w', newline='') as outfile:
                        reader = csv.reader(infile)
                        writer = csv.writer(outfile)

                        # Copy header
                        writer.writerow(next(reader))
                        # Filter rows
                        for row in reader:
                            try:
                                size_gb = float(row[3]) # GB column
                                if size_gb >= threshold_gb:
                                    writer.writerow(row)
                            except (ValueError, IndexError):
                                continue

                # Send appropriate email report
                if os.path.exists(total_size_file) and os.path.getsize(total_size_file) > 0:
                    if size_filter == "1":
                        send_email_report(total_size_file, folder_choice, "All Folders")
                    elif filtered_file and os.path.exists(filtered_file):
                        send_email_report(
                            filtered_file,
                            folder_choice,
                            f"Folders above {'1TB' if size_filter == '3' else '500GB'}"
                        )
                    
                    # Handle individual emails if requested
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
        # Process single folder (original code remains unchanged)
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
                send_email_report(total_size_file, folder_choice, f"Single Folder: {folder_choice}")
            else:
                print("Error: Total size file not created properly, skipping email")
        except Exception as e:
            print(f"Error processing folder {folder_choice}: {e}")
            return

    # Save updated history data
    save_history()
    print(f"\nProcessing complete. Results saved to:\n- Details: {output_file}\n- Summary: {total_size_file}")

if __name__ == "__main__":
    main()