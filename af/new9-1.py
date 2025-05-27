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
    Final refined email report with:
    - Clean left-aligned info section
    - Removed all background lines
    - Added banner header
    - Simplified storage trend display
    - Consistent 30-day trend calculations
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
                   
                    # Process percentage increase with 30-day window
                    folder_name = row['Main Folder'].strip()
                    increase_text = row['30-Day Increase'].strip()

                    # Calculate cutoff date (30 days ago from now)
                    cutoff_date = datetime.now() - timedelta(days=30)

                    # Enhanced history comparison with 30-day window
                    if folder_name in history_data:
                        history_entries = history_data[folder_name]
                        
                        # Convert string dates to datetime objects and filter for last 30 days
                        valid_entries = []
                        for date_str, size_str in history_entries:
                            try:
                                date = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                                if date >= cutoff_date:
                                    valid_entries.append((date, float(size_str)))
                            except (ValueError, TypeError):
                                continue
                        
                        if valid_entries:
                            # Get oldest and newest entries in last 30 days
                            oldest_entry = min(valid_entries, key=lambda x: x[0])
                            newest_entry = max(valid_entries, key=lambda x: x[0])
                            
                            # If only one entry in last 30 days, compare with current size
                            if len(valid_entries) == 1:
                                oldest_gb = oldest_entry[1] / 1024
                                current_gb = gb
                                change_gb = current_gb - oldest_gb
                            else:
                                oldest_gb = oldest_entry[1] / 1024
                                current_gb = newest_entry[1] / 1024
                                change_gb = current_gb - oldest_gb
                            
                            if oldest_gb > 0:
                                pct_change = (change_gb / oldest_gb) * 100
                                arrow = "↑" if pct_change >= 0 else "↓"
                                increase_text = f"{arrow} {abs(pct_change):.2f}% ({abs(change_gb):.2f}GB)"
                            else:
                                increase_text = "N/A (Zero base)"

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
            background: linear-gradient(135deg, #2c3e50, #2c3e50);
            color: black;
            padding: 30px 25px;
            text-align: center;
            border-bottom: 4px solid #2980b9;
        }}

        .banner h1 {{
            margin: 0;
            font-weight: 600;
            font-size: 32px;
            letter-spacing: 0.5px;
            text-shadow: 1px 1px 3px rgba(0, 0, 0, 0.3);
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
            font-family: 'Source+Code+Pro', monospace;
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
                    <th>Storage Trend (30 Days)</th>
                </tr>
            </thead>
            <tbody>"""

        # Add data rows with simplified trend display
        for rank, item in enumerate(data, start=1):
            trend_text = item['increase']
            
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
