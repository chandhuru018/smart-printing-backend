import time
import requests
import os
import webbrowser

# 1. Your RENDER URL (Copy exactly from your browser)
BASE_URL = "https://smart-printing-backend-vvlt.onrender.com"

def check_for_paid_jobs():
    try:
        # This calls a hidden 'admin' check to see if jobs are ready
        # In a real app, you'd create a specific API for this
        print("Checking cloud for paid print jobs...")
        response = requests.get(f"{BASE_URL}/admin/api/pending-jobs")
        
        if response.status_code == 200:
            jobs = response.json()
            for job in jobs:
                print(f"FOUND JOB: {job['filename']}. Triggering Print...")
                trigger_physical_print(job)
    except Exception as e:
        print(f"Connection Error: {e}")

def trigger_physical_print(job_data):
    # This tells your laptop's OS to open the file and print it
    # We use the direct file URL from your GridFS/MongoDB
    file_url = f"{BASE_URL}/download/{job_data['file_id']}"
    
    print(f"Printing: {file_url}")
    
    # On Windows, this opens the print dialog automatically
    os.startfile(file_url, "print")
    
    # Tell the cloud the job is done so it doesn't print twice!
    requests.post(f"{BASE_URL}/admin/api/mark-printed/{job_data['id']}")

print("--- KIOSK PRINT AGENT STARTED ---")
while True:
    check_for_paid_jobs()
    time.sleep(10) # Checks every 10 seconds