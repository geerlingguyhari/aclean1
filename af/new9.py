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


# In the send_email_report function, replace the trend calculation part with this:

# Calculate cutoff date (30 days ago from now)
cutoff_date = datetime.now() - timedelta(days=30)

# Process percentage increase with better history comparison
folder_name = row['Main Folder'].strip()
increase_text = row['30-Day Increase'].strip()

# Enhanced history comparison
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
            current_gb = item['gb']
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

# Replace the trend_text calculation in the table row generation with:
trend_text = item['increase']
