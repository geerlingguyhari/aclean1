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
MAX_EMAIL_SIZE = 25 * 1024 * 1024  # 25MB email size limit

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

# [Previous functions remain unchanged until send_individual_folder_email]

def send_individual_folder_email(folder_data, recipients):
    """Send email for individual folder report with enhanced styling and old images attachment"""
    max_attempts = 2  # Initial attempt + one retry without attachments if needed
    attempt = 0
    include_attachments = True
    
    while attempt < max_attempts:
        try:
            msg = MIMEMultipart()
            msg['From'] = EMAIL_FROM
            msg['To'] = ", ".join(recipients)
            msg['Subject'] = f"Artifactory Folder Report: {folder_data['folder']}"

            # Get old images for this folder
            old_images = old_images_data.get(folder_data['folder'], [])
            
            # Determine trend arrow
            trend_arrow = ""
            if '↑' in folder_data['increase']:
                trend_arrow = "⬆️"
            elif '↓' in folder_data['increase']:
                trend_arrow = "⬇️"
            
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
        
        .email-container {{
            background-color: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
            border: 1px solid #e2e8f0;
        }}
        
        .header {{
            background: linear-gradient(135deg, #2c3e50, #3498db);
            color: white;
            padding: 30px;
            text-align: center;
            margin-bottom: 0;
        }}
        
        .header h1 {{
            margin: 0;
            font-size: 28px;
            font-weight: 600;
            letter-spacing: 0.5px;
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
                            <span class="trend-value {{ 'increase-positive' if '↑' in folder_data['increase'] else 'increase-negative' if '↓' in folder_data['increase'] else '' }}">
                                {trend_arrow} {folder_data['increase']}
                            </span>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="cleanup-notice">
                <strong>⚠️ Cleanup Recommendation:</strong> {CLEANUP_MESSAGE}
                <br><br>
                Found {len(old_images)} images older than {CLEANUP_DAYS} days {'' if include_attachments else '(details not included due to email size limits)'}
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
            if old_images and include_attachments:
                old_images_csv = [
                    ["Image Path", "Created Date", "Size (Bytes)"]
                ]
                for img in old_images:
                    old_images_csv.append([
                        img['path'],
                        img['created'],
                        img['size']
                    ])
                
                csv_buffer = io.StringIO()
                csv_writer = csv.writer(csv_buffer)
                csv_writer.writerows(old_images_csv)
                
                attachment = MIMEText(csv_buffer.getvalue(), 'plain')
                attachment.add_header('Content-Disposition', 'attachment',
                                   filename=f"{folder_data['folder']}_old_images_{datetime.now().strftime('%Y%m%d')}.csv")
                msg.attach(attachment)

            # Check message size
            msg_size = len(msg.as_string())
            if msg_size > MAX_EMAIL_SIZE and include_attachments:
                print(f"Email too large ({msg_size/1024:.1f}KB), retrying without attachments...")
                include_attachments = False
                attempt += 1
                continue

            # Send email
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                server.sendmail(EMAIL_FROM, recipients, msg.as_string())
            print(f"Sent individual report for {folder_data['folder']} to {', '.join(recipients)}")
            return True

        except smtplib.SMTPDataError as e:
            if "exceeds size limit" in str(e) and include_attachments:
                print(f"Email too large, retrying without attachments...")
                include_attachments = False
                attempt += 1
                continue
            print(f"Error sending individual email for {folder_data['folder']}: {e}")
            return False
        except Exception as e:
            print(f"Error sending individual email for {folder_data['folder']}: {e}")
            return False
    
    return False

# [Rest of the script remains unchanged]
