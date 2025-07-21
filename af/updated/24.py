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
import io

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EMAIL_MAPPING_FILE = "/tmp/test_emails.csv"
DEFAULT_EMAIL = "abc3@xyz.com"
HISTORY_FILE_PATH = "/var/opt/automation/af/artifactory_size_history.json"
CLEANUP_MESSAGE = """Clean up images older than 180 days with the following methods:
1. <a href="abc.va.com/gcops">PCP RES Virtual Assistant</a><br>
2. <a href="xyz.api.com">Artifactory-Cleanup API</a><br><br>
For more details refer <a href="old.images.com">clean_up_old_images_from_Artifactory</a><br>. To engage: <a href="123.support.com">ABC Support</a> | <a href="dfg.request.com">My request</a>"""
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
# Update these global variables at the top (keep as comma-separated strings):
DEFAULT_EMAIL = "abc3@xyz.com,ab4@xyz.com,ab5@xyz.com"  # CC emails
EMAIL_TO = "xyz@abc.com,xyz1@abc.com,xyz2@abc.com"  # CC emails for summary reports

# Add this helper function near the top (with other utility functions):
def parse_cc_emails(email_string):
    """Parse CC emails from comma-separated string, skip if empty/invalid"""
    if not email_string or not isinstance(email_string, str):
        return []
    return [email.strip() for email in email_string.split(',') if email.strip()]

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
    # For history file, always use /var/opt/automation/af/
    if filename == "artifactory_size_history.json":
        return "/var/opt/automation/af/artifactory_size_history.json"
    
    # For other files, try /var/opt/automation first, then fall back to /tmp
    automation_path = "/var/opt/automation"
    if os.path.exists(automation_path) and os.access(automation_path, os.W_OK):
        return f"{automation_path}/{filename}"
    tmp_path = f"/tmp/{filename}"
    if os.access("/tmp", os.W_OK):
        return tmp_path
    return os.path.expanduser(f"~/{filename}")

def load_history():
    global folder_size_history
    history_file = get_writable_path("artifactory_size_history.json")
    try:
        os.makedirs(os.path.dirname(history_file), exist_ok=True)
        if os.path.exists(history_file):
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
        os.makedirs(os.path.dirname(history_file), exist_ok=True)
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
    
    # Calculate cutoff date (30 days ago from now)
    cutoff_date = datetime.now() - timedelta(days=30)
    
    # Find the oldest entry within the last 30 days
    recent_history = [(date, size) for date, size in history if date >= cutoff_date]
    
    if not recent_history:
        return "N/A (No data in last 30 days)"
    
    # Get the oldest and newest entries within the 30-day window
    oldest_date, oldest_size = min(recent_history, key=lambda x: x[0])
    newest_date, newest_size = max(recent_history, key=lambda x: x[0])
    
    # If there's only one entry in the last 30 days, compare with current size
    if len(recent_history) == 1:
        newest_size = current_size_mb
        newest_date = datetime.now()
    
    # Calculate changes
    size_change_gb = (newest_size - oldest_size) / 1024
    try:
        percentage = ((newest_size - oldest_size) / oldest_size) * 100
        return f"{size_change_gb:+.2f} GB ({percentage:+.2f}%) since {oldest_date.strftime('%Y-%m-%d')}"
    except ZeroDivisionError:
        return "N/A (Division error)"

def send_email_report(csv_file, folder_choice, report_scope):
    """
    Send email report with:
    - Clean format for summary reports with [Actions Required] subject
    - No cleanup message for summary reports
    - Nice table format for all folder summaries
    """
    # Verify file exists
    if not os.path.exists(csv_file):
        print(f"Error: CSV file not found at {csv_file}")
        return False
        
    try:
        # Read and process CSV data
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
                
            # Sort data by size (GB) in descending order
            data.sort(key=lambda x: x['sort_key'], reverse=True)

        # Prepare HTML content - no cleanup message for summary reports
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Artifactory Storage Report</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 1000px;
            margin: 0 auto;
            padding: 20px;
        }}
        h1 {{
            color: #2c3e50;
            border-bottom: 2px solid #3498db;
            padding-bottom: 10px;
            text-align: center;
        }}
        .report-info {{
            margin: 20px 0;
            padding: 15px;
            background-color: #f8f9fa;
            border-radius: 5px;
        }}
        .report-info p {{
            margin: 8px 0;
            font-size: 15px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            font-size: 14px;
        }}
        th {{
            background-color: #3498db;
            color: white;
            text-align: left;
            padding: 12px;
        }}
        td {{
            padding: 10px;
            border-bottom: 1px solid #ddd;
        }}
        .number {{
            text-align: right;
        }}
        .increase-positive {{
            color: #27ae60;
            font-weight: bold;
        }}
        .increase-negative {{
            color: #e74c3c;
            font-weight: bold;
        }}
        .footer {{
            margin-top: 20px;
            font-size: 12px;
            color: #777;
            text-align: center;
        }}
    </style>
</head>
<body>
    <h1>Artifactory Storage Report</h1>
    
    <div class="report-info">
        <p><strong>Repository:</strong> {repository_name}</p>
        <p><strong>Report Scope:</strong> {report_scope}</p>
        <p><strong>Generated:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
        <p><strong>Folders Analyzed:</strong> {len(data)}</p>
    </div>
    
    <table>
        <thead>
            <tr>
                <th>Folder Name</th>
                <th class="number">Size (GB)</th>
                <th class="number">Size (TB)</th>
                <th>Storage Trend (30 Days)</th>
            </tr>
        </thead>
        <tbody>"""

        # Add data rows
        for item in data:
            trend_class = "increase-positive" if "+" in item['increase'] else "increase-negative" if "-" in item['increase'] else ""
            html += f"""
                <tr>
                    <td>{item['folder']}</td>
                    <td class="number">{item['gb']:,.2f}</td>
                    <td class="number">{item['tb']:,.3f}</td>
                    <td class="{trend_class}">{item['increase']}</td>
                </tr>"""

        # Calculate totals
        total_gb = sum(item['gb'] for item in data)
        total_tb = sum(item['tb'] for item in data)

        html += f"""
                <tr style="font-weight: bold; background-color: #f1f1f1;">
                    <td>Total Storage</td>
                    <td class="number">{total_gb:,.2f}</td>
                    <td class="number">{total_tb:,.3f}</td>
                    <td></td>
                </tr>
            </tbody>
        </table>

        <div class="footer">
            <p>This report was automatically generated by the Artifactory Storage Scanner</p>
        </div>
    </body>
</html>"""

        # Create email message
        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        
        # TO email comes from first in EMAIL_TO
        to_email = EMAIL_TO.split(',')[0].strip()
        
        # CC emails come from ALL remaining in EMAIL_TO
        cc_emails = [e.strip() for e in EMAIL_TO.split(',')[1:]] if ',' in EMAIL_TO else []
        if cc_emails:
            msg['Cc'] = ", ".join(cc_emails)
        
        # Set subject - always use [Actions Required] for summary reports
        msg['Subject'] = f"[Actions Required]: Artifactory Storage Cleanup Report - {report_scope} | {datetime.now().strftime('%b %d, %Y')}"

        # Attach HTML content
        msg.attach(MIMEText(html, 'html'))

        # Add CSV attachment
        with open(csv_file, 'rb') as f:
            attachment = MIMEText(f.read().decode('utf-8'), 'plain')
            attachment.add_header('Content-Disposition', 'attachment',
                               filename=f"storage_report_{datetime.now().strftime('%Y%m%d')}.csv")
            msg.attach(attachment)

        # Send email to all recipients (To + Cc)
        all_recipients = [to_email] + cc_emails
        # if cc_emails:
        #     all_recipients.extend(cc_emails)
            
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
            server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())
        
        cc_log = f", CC: {msg['Cc']}" if cc_emails else ""
        print(f"Successfully sent {report_scope} report to {msg['To']}{cc_log}")
        # print(f"Successfully sent {report_scope} report with {len(data)} folders")
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
                                    'size': file_data.get('size', 0)
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
                        # Split emails by comma, strip whitespace, and filter out empty strings
                        emails = []
                        for email_part in row[1:]:
                            emails.extend([email.strip() for email in email_part.split(',') if email.strip()])
                        mappings[folder] = emails
                        logger.debug(f"Loaded email mapping for {folder}: {emails}")
    except Exception as e:
        logger.error(f"Error loading email mappings: {e}")
    return mappings

def send_individual_folder_email(folder_data, recipients, custom_body=None, reminder_text=""):
    """Send email for individual folder report with all requested updates:
    - New subject format
    - Updated cleanup recommendation with links
    - Bold image count
    - Link styling
    - Sorted old images by date (oldest first)
    - Size converted to MB
    """
    max_attempts = 2  # Initial attempt + one retry without attachments if needed
    attempt = 0
    include_attachments = True
    
    # Ensure recipients is always a list (keep original TO behavior)
    if not isinstance(recipients, list):
        recipients = [recipients] if recipients else []
        recipients = [email.strip() for email in recipients if email.strip()]
        
    # Parse default CC emails
    cc_emails = []
    if DEFAULT_EMAIL:
        cc_emails = [e.strip() for e in DEFAULT_EMAIL.split(',') if e.strip()]

    
    
    # Keep original TO recipient behavior
    to_email = recipients[0] if recipients else EMAIL_FROM  # Fallback to EMAIL_FROM if no recipients
    
    # Log the recipients before sending
    logger.info(f"Preparing to send email for folder {folder_data['folder']}")
    logger.info(f"TO recipient: {to_email}")
    if cc_emails:
        logger.info(f"CC recipients: {cc_emails}")    
    
    while attempt < max_attempts:
        try:
            msg = MIMEMultipart()
            msg['From'] = EMAIL_FROM
            msg['To'] = to_email
            if cc_emails:
                msg['Cc'] = ", ".join(cc_emails)
                
            # Updated email subject
            msg['Subject'] = f"{reminder_text}[Actions Required]: Request for Artifactory Storage Cleanup for TIA: {folder_data['folder']}"

            # Get old images for this folder and sort by created date (oldest first)
            old_images = old_images_data.get(folder_data['folder'], [])
            old_images_sorted = sorted(old_images, key=lambda x: x.get('created', ''))
            
            # Parse and trend information
            trend_text = folder_data['increase']
            trend_arrow = ""
            trend_class = ""
            
            if '(+' in trend_text:
                trend_arrow = "⬆️"
                trend_class = "increase-positive"
            elif '(-)' in trend_text:
                trend_arrow = "⬇️"
                trend_class = "increase-negative"
            
            # Updated cleanup recommendation with clickable links
            cleanup_html = f"""
<div class="cleanup-notice">
    <strong>⚠️ Cleanup Recommendation:</strong><br><br>
    Clean up images older than {CLEANUP_DAYS} days with the following methods:<br>
    1. <a href="abc.va.com/gcops">PCP RES Virtual Assistant</a><br>
    2. <a href="xyz.api.com">Artifactory-Cleanup API</a><br><br>
    For more details refer <a href="old.images.com">clean_up_old_images_from_Artifactory</a><br>. To engage: <a href="123.support.com">ABC Support</a> | <a href="dfg.request.com">My request</a><br><br>
    <strong>Found {len(old_images)} images older than {CLEANUP_DAYS} days</strong> {'' if include_attachments else '(details not included due to email size limits)'}
</div>
"""
            
            # Create HTML content with enhanced styling
            html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Artifactory Folder Report</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&family=Source+Code+Pro&display=swap');
        
        body {{
            font-family: 'Roboto', sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 0;
            background-color: #f9f9f9;
        }}
        
        /* NEW LINK STYLING */
        a {{
            color: #3498db;
            text-decoration: none;
        }}
        
        a:hover {{
            text-decoration: underline;
        }}

        .email-container {{
            background-color: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
            border: 1px solid #e2e8f0;
        }}
        
        .header {{
            background: linear-gradient(135deg, #2c3e50, #3498db);
            color: black;
            padding: 30px;
            text-align: center;
            margin-bottom: 0;
        }}
        
        .header h1 {{
            margin: 0;
            font-size: 28px;
            font-weight: 600;
            letter-spacing: 0.5px;
            text-shadow: 1px 1px 3px rgba(0, 0, 0, 0.3);
        }}
        
        .content {{
            padding: 25px;
            background-color: white;
        }}
        
        .info-section {{
            margin-bottom: 20px;
        }}
        
        .info-card {{
            margin-bottom: 15px;
            padding: 15px;
            border-radius: 6px;
            background-color: #f8fafc;
            border-left: 4px solid #3498db;
        }}
        
        .info-card h2 {{
            margin: 0 0 10px 0;
            font-size: 16px;
            color: #2c3e50;
            font-weight: 600;
        }}
        
        .info-line {{
            margin-bottom: 8px;
            display: flex;
        }}
        
        .info-label {{
            font-weight: 500;
            color: #4a5568;
            min-width: 150px;
        }}
        
        .info-value {{
            font-weight: 600;
            color: #2d3748;
        }}
        
        .size-value {{
            font-family: 'Source+Code+Pro', monospace;
        }}
        
        .trend-value {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }}
        
        .increase-positive {{
            color: #27ae60;
            font-weight: 500;
        }}
        
        .increase-negative {{
            color: #e74c3c;
            font-weight: 500;
        }}
        
        .cleanup-notice {{
            margin-top: 20px;
            padding: 15px;
            background-color: #fffaf0;
            border-left: 4px solid #f6ad55;
            font-size: 14px;
            border-radius: 6px;
            font-weight: 500;
        }}
        
        .footer {{
            margin-top: 20px;
            padding: 15px;
            text-align: center;
            font-size: 12px;
            color: #718096;
            background-color: #f8fafc;
            border-radius: 6px;
        }}
        
        pre {{
            font-family: 'Roboto', sans-serif;
            white-space: pre-wrap;
            margin: 0;
        }}
    </style>
</head>
<body>
    <div class="email-container">
        <!-- Header -->
        <div class="header">
            <h1>Artifactory Storage Report</h1>
        </div>
        
        <!-- Main Content -->
        <div class="content">
            <!-- Custom Body Text -->
            <div class="info-card" style="margin-bottom: 20px; background-color: #f0f7ff; border-left: 4px solid #3498db;">
                <pre>{custom_body if custom_body else ''}</pre>
            </div>
            
            <div class="info-section">
                <div class="info-card">
                    <h2>📁 Folder Information</h2>
                    <div class="info-line">
                        <div class="info-label">Folder Name:</div>
                        <div class="info-value">{folder_data['folder']}</div>
                    </div>
                    <div class="info-line">
                        <div class="info-label">Repository:</div>
                        <div class="info-value">{repository_name}</div>
                    </div>
                </div>
                
                <div class="info-card" style="border-left-color: #27ae60;">
                    <h2>📊 Size Information</h2>
                    <div class="info-line">
                        <div class="info-label">Current Size (GB):</div>
                        <div class="info-value size-value">{folder_data['gb']:,.2f}</div>
                    </div>
                    <div class="info-line">
                        <div class="info-label">Current Size (TB):</div>
                        <div class="info-value size-value">{folder_data['tb']:,.3f}</div>
                    </div>
                </div>
                
                <div class="info-card" style="border-left-color: #f39c12;">
                    <h2>📈 Storage Trend</h2>
                    <div class="info-line">
                        <div class="info-label">Change:</div>
                        <div class="info-value">
                            <span class="trend-value {trend_class}">
                                {trend_arrow} {folder_data['increase']}
                            </span>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="cleanup-notice">
                {cleanup_html}
            </div>
            
            <div class="footer">
                <p>This report was automatically generated by the Artifactory Storage Scanner</p>
            </div>
        </div>
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
            
            # Create CSV attachment in memory
            import io
            csv_buffer = io.StringIO()
            csv_writer = csv.writer(csv_buffer)
            csv_writer.writerows(csv_data)
            
            attachment = MIMEText(csv_buffer.getvalue(), 'plain')
            attachment.add_header('Content-Disposition', 'attachment',
                               filename=f"{folder_data['folder']}_storage_summary_{datetime.now().strftime('%Y%m%d')}.csv")
            msg.attach(attachment)
            
            # Add old images CSV if any and if we're including attachments
            if old_images_sorted and include_attachments:
                old_images_csv = [
                    ["Image Path", "Created Date", "Size (MB)"]
                ]
                for img in old_images_sorted:
                    old_images_csv.append([
                        img['path'],
                        img['created'],
                        f"{int(img['size']) / (1024 * 1024):.2f}"  # Convert bytes to MB
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
            all_recipients = [to_email] + cc_emails
            # if cc_emails:
            #     all_recipients.extend(cc_emails)
                
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())
            
            cc_log = f", CC: {msg['Cc']}" if cc_emails else ""    
            logger.info(f"Successfully sent individual report for {folder_data['folder']} to {msg['To']}{cc_log}")
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
                
                # Check size filter
                if size_filter == "500gb" and size_gb < 500:
                    continue
                if size_filter == "1tb" and size_gb < 1024:
                    continue
                
                # Get recipients
                recipients = email_mappings.get(folder_name, [DEFAULT_EMAIL])
                
                # Prepare folder data
                folder_data = {
                    'folder': folder_name,
                    'mb': float(row['Size (MB)']),
                    'gb': size_gb,
                    'tb': float(row['Size (TB)']),
                    'increase': row['30-Day Increase']
                }
                
                # Prepare the custom email body
                custom_body = f"""Hello Team,

As part of our storage optimization efforts and upcoming quota enforcement, we request your support in cleaning up unused images older than 180 days.

Your team is currently using {folder_data['gb']:.2f}GB of storage. Attached is a list of images older than 180 days—please review and remove those no longer needed.
Further details are provided below. Thank you for your cooperation.
"""
                # Send email with custom body
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
                            future.result()  # Wait for all to complete

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
                                size_gb = float(row[3])  # GB column
                                if size_gb >= threshold_gb:
                                    writer.writerow(row)
                            except (ValueError, IndexError):
                                continue

                # Send appropriate email report
                if os.path.exists(total_size_file) and os.path.getsize(total_size_file) > 0:
                    if size_filter == "1":
                        send_email_report(total_size_file, folder_choice, "All Folders")  # Clean summary report
                    elif filtered_file and os.path.exists(filtered_file):
                        scope = f"Folders above {'1TB' if size_filter == '3' else '500GB'}"
                        send_email_report(filtered_file, folder_choice, scope)  # Action-oriented report
                    
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
        # Process single folder with email sending capability
        output_file = get_writable_path(f"{folder_choice}_output_{timestamp}.csv")
        total_size_file = get_writable_path(f"{folder_choice}_total_size_{timestamp}.csv")

        # Get reminder option right after folder selection
        print("\nSelect reminder option for email subject:")
        print("1. No reminder (default)")
        print("2. Reminder 1")
        print("3. Reminder 2")
        print("4. Reminder 3")
        reminder_option = input("Enter your choice (1-4): ").strip() or "1"
        
        reminder_text = ""
        if reminder_option == "2":
            reminder_text = "Reminder 1: "
        elif reminder_option == "3":
            reminder_text = "Reminder 2: "
        elif reminder_option == "4":
            reminder_text = "Reminder 3: "            

        try:
            with open(output_file, 'w', newline='') as output_csv, \
                 open(total_size_file, 'w', newline='') as total_csv:
                output_writer = csv.writer(output_csv)
                total_size_writer = csv.writer(total_csv)

                output_writer.writerow(["Repository", "Main Folder", "Image Path", "Created", "Last Used", "Size (MB)"])
                total_size_writer.writerow(["Repository", "Main Folder", "Size (MB)", "Size (GB)", "Size (TB)", "30-Day Increase"])

                process_main_folder(repo_base_url, folder_choice, username, password, output_writer, total_size_writer)

            if os.path.exists(total_size_file) and os.path.getsize(total_size_file) > 0:
                # Read the folder data
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
                    # Load email mappings
                    email_mappings = load_email_mappings()
                    recipients = email_mappings.get(folder_choice, [])
                    
                    # default_emails = [e.strip() for e in DEFAULT_EMAIL.split(',')] if DEFAULT_EMAIL else []
                    # TO email comes from email.csv or falls back to EMAIL_FROM
                    to_email = recipients[0] if recipients else EMAIL_FROM
                    
                    # CC emails come from ALL DEFAULT_EMAIL addresses
                    cc_emails = []
                    if DEFAULT_EMAIL:
                        cc_emails = [e.strip() for e in DEFAULT_EMAIL.split(',') if e.strip()]
                        
                    # Create and send email
                    msg = MIMEMultipart()
                    msg['From'] = EMAIL_FROM
                    if cc_emails:
                        msg['Cc'] = ", ".join(cc_emails)    
                    
                    # primary_recipients = [email for email in recipients if email != DEFAULT_EMAIL]
                    # cc_recipients = [DEFAULT_EMAIL] if DEFAULT_EMAIL not in primary_recipients and DEFAULT_EMAIL else []
                    
                    
                    # Get old images for this folder and sort by created date (oldest first)
                    old_images = old_images_data.get(folder_choice, [])
                    old_images_sorted = sorted(old_images, key=lambda x: x.get('created', ''))
                    
                    # Prepare the custom email body
                    custom_body = f"""Hello Team,

As part of our storage optimization efforts and upcoming quota enforcement, we request your support in cleaning up unused images older than 180 days.

Your team is currently using {folder_data['gb']:.2f}GB of storage. Attached is a list of images older than 180 days—please review and remove those no longer needed.
Further details are provided below. Thank you for your cooperation.
"""
                    # Create the email message with retry logic for size limits
                    max_attempts = 2
                    attempt = 0
                    include_attachments = True
                    
                    while attempt < max_attempts:
                        try:
                            msg = MIMEMultipart()
                            msg['From'] = EMAIL_FROM
                            msg['To'] = to_email
                            if cc_emails:
                                msg['Cc'] = ", ".join(cc_emails)
                            msg['Subject'] = f"{reminder_text}[Actions Required]: Request for Artifactory Storage Cleanup for TIA: {folder_choice}"

                            # Create HTML content with enhanced styling
                            html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Artifactory Folder Report</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&family=Source+Code+Pro&display=swap');
        
        body {{
            font-family: 'Roboto', sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 0;
            background-color: #f9f9f9;
        }}
        
        a {{
            color: #3498db;
            text-decoration: none;
        }}
        
        a:hover {{
            text-decoration: underline;
        }}

        .email-container {{
            background-color: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
            border: 1px solid #e2e8f0;
        }}
        
        .header {{
            background: linear-gradient(135deg, #2c3e50, #3498db);
            color: black;
            padding: 30px;
            text-align: center;
            margin-bottom: 0;
        }}
        
        .header h1 {{
            margin: 0;
            font-size: 28px;
            font-weight: 600;
            letter-spacing: 0.5px;
            text-shadow: 1px 1px 3px rgba(0, 0, 0, 0.3);
        }}
        
        .content {{
            padding: 25px;
            background-color: white;
        }}
        
        .info-section {{
            margin-bottom: 20px;
        }}
        
        .info-card {{
            margin-bottom: 15px;
            padding: 15px;
            border-radius: 6px;
            background-color: #f8fafc;
            border-left: 4px solid #3498db;
        }}
        
        .info-card h2 {{
            margin: 0 0 10px 0;
            font-size: 16px;
            color: #2c3e50;
            font-weight: 600;
        }}
        
        .info-line {{
            margin-bottom: 8px;
            display: flex;
        }}
        
        .info-label {{
            font-weight: 500;
            color: #4a5568;
            min-width: 150px;
        }}
        
        .info-value {{
            font-weight: 600;
            color: #2d3748;
        }}
        
        .size-value {{
            font-family: 'Source Code Pro', monospace;
        }}
        
        .trend-value {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }}
        
        .increase-positive {{
            color: #27ae60;
            font-weight: 500;
        }}
        
        .increase-negative {{
            color: #e74c3c;
            font-weight: 500;
        }}
        
        .cleanup-notice {{
            margin-top: 20px;
            padding: 15px;
            background-color: #fffaf0;
            border-left: 4px solid #f6ad55;
            font-size: 14px;
            border-radius: 6px;
            font-weight: 500;
        }}
        
        .footer {{
            margin-top: 20px;
            padding: 15px;
            text-align: center;
            font-size: 12px;
            color: #718096;
            background-color: #f8fafc;
            border-radius: 6px;
        }}
        
        pre {{
            font-family: 'Roboto', sans-serif;
            white-space: pre-wrap;
            margin: 0;
        }}
    </style>
</head>
<body>
    <div class="email-container">
        <!-- Header -->
        <div class="header">
            <h1>Artifactory Storage Report</h1>
        </div>
        
        <!-- Main Content -->
        <div class="content">
            <!-- Custom Body Text -->
            <div class="info-card" style="margin-bottom: 20px; background-color: #f0f7ff; border-left: 4px solid #3498db;">
                <pre>{custom_body}</pre>
            </div>
            
            <div class="info-section">
                <div class="info-card">
                    <h2>📁 Folder Information</h2>
                    <div class="info-line">
                        <div class="info-label">Folder Name:</div>
                        <div class="info-value">{folder_data['folder']}</div>
                    </div>
                    <div class="info-line">
                        <div class="info-label">Repository:</div>
                        <div class="info-value">{repository_name}</div>
                    </div>
                </div>
                
                <div class="info-card" style="border-left-color: #27ae60;">
                    <h2>📊 Size Information</h2>
                    <div class="info-line">
                        <div class="info-label">Current Size (GB):</div>
                        <div class="info-value size-value">{folder_data['gb']:,.2f}</div>
                    </div>
                    <div class="info-line">
                        <div class="info-label">Current Size (TB):</div>
                        <div class="info-value size-value">{folder_data['tb']:,.3f}</div>
                    </div>
                </div>
                
                <div class="info-card" style="border-left-color: #f39c12;">
                    <h2>📈 Storage Trend</h2>
                    <div class="info-line">
                        <div class="info-label">Change:</div>
                        <div class="info-value">
                            <span class="trend-value">
                                {folder_data['increase']}
                            </span>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="cleanup-notice">
                <strong>⚠️ Cleanup Recommendation:</strong><br><br>
                Clean up images older than {CLEANUP_DAYS} days with the following methods:<br>
                1. <a href="abc.va.com/gcops">PCP RES Virtual Assistant</a><br>
                2. <a href="xyz.api.com">Artifactory-Cleanup API</a><br><br>
                For more details refer <a href="old.images.com">clean_up_old_images_from_Artifactory</a><br>. To engage: <a href="123.support.com">ABC Support</a> | <a href="dfg.request.com">My request</a><br><br>
                <strong>Found {len(old_images)} images older than {CLEANUP_DAYS} days</strong> {'' if include_attachments else '(details not included due to email size limits)'}
            </div>
            
            <div class="footer">
                <p>This report was automatically generated by the Artifactory Storage Scanner</p>
            </div>
        </div>
    </div>
</body>
</html>"""

                            msg.attach(MIMEText(html, 'html'))
                            
                            # Attach folder summary CSV
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
                            
                            csv_buffer = io.StringIO()
                            csv_writer = csv.writer(csv_buffer)
                            csv_writer.writerows(csv_data)
                            
                            attachment = MIMEText(csv_buffer.getvalue(), 'plain')
                            attachment.add_header('Content-Disposition', 'attachment',
                                               filename=f"{folder_choice}_storage_summary_{datetime.now().strftime('%Y%m%d')}.csv")
                            msg.attach(attachment)
                            
                            # Add old images CSV if any and if we're including attachments
                            if old_images_sorted and include_attachments:
                                old_images_csv = [
                                    ["Image Path", "Created Date", "Size (MB)"]
                                ]
                                for img in old_images_sorted:
                                    old_images_csv.append([
                                        img['path'],
                                        img['created'],
                                        f"{int(img['size']) / (1024 * 1024):.2f}"  # Convert bytes to MB
                                    ])
                                
                                csv_buffer = io.StringIO()
                                csv_writer = csv.writer(csv_buffer)
                                csv_writer.writerows(old_images_csv)
                                
                                attachment = MIMEText(csv_buffer.getvalue(), 'plain')
                                attachment.add_header('Content-Disposition', 'attachment',
                                                   filename=f"{folder_choice}_old_images_{datetime.now().strftime('%Y%m%d')}.csv")
                                msg.attach(attachment)

                            # Check message size
                            msg_size = len(msg.as_bytes())
                            if msg_size > MAX_EMAIL_SIZE and include_attachments:
                                logger.warning(f"Email too large ({msg_size/1024:.1f}KB), retrying without attachments...")
                                include_attachments = False
                                attempt += 1
                                continue

                            # Send email (To + Cc)
                            all_recipients = [to_email] + cc_emails
                            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                                server.sendmail(EMAIL_FROM, recipients, msg.as_string())
                            cc_log = f", CC: {msg['Cc']}" if cc_emails else ""    
                            logger.info(f"Successfully sent email to: {msg['To']}{cc_log}") 
                            
                            break

                        except smtplib.SMTPDataError as e:
                            if "exceeds size limit" in str(e) and include_attachments:
                                logger.warning(f"Email too large, retrying without attachments...")
                                include_attachments = False
                                attempt += 1
                                continue
                            logger.error(f"Error sending email for {folder_choice}: {e}")
                            break
                        except Exception as e:
                            logger.error(f"Error sending email for {folder_choice}: {e}")
                            break
                    
                    if attempt >= max_attempts:
                        logger.error(f"Failed to send email for {folder_choice} after {max_attempts} attempts")
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
