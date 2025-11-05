# Nas01 NAS Node — Stepdown Runbook (English)

**Host:** `nas01` (Raspberry Pi 5 or equivalent NAS node)  
**OS:** Raspberry Pi OS Trixie 2025 Oct build (stable on Pi5)  
**Purpose:** Receive ZIP archives from Cam01 cameras, extract them into structured folders, and maintain short-term retention on local NVMe storage.  
**Storage:** `/mnt/storage` (ext4, ~870 GB, no RAID, no backup)

---

## 1) One-time Setup

### 1.1 Mount and verify storage
NVMe SSD mounted persistently at boot via `/etc/fstab`:
```bash
# Example line in /etc/fstab
UUID=<your-nvme-uuid> /mnt/storage ext4 defaults,noatime 0 2
```
Verify after boot:
```bash
lsblk -f
ls -ld /mnt/storage
```

### 1.2 Directory layout
```
/mnt/storage/
└── cam_uploads/
    ├── incoming/      # ZIP files arrive here via rsync from cameras
    ├── processed/     # Extracted folders
    └── logs/          # Receiver logs
```

---

## 2) Python Receiver Script

Location: `/usr/local/bin/receive_cam_zip.py`

### 2.1 Purpose
Continuously watches `incoming/` for new ZIP files.  
When a `.zip` file appears and remains unmodified for ≥15 s, it extracts to `processed/` and deletes the original.  

### 2.2 Source Code
```python
#!/usr/bin/env python3
import os, time, zipfile, logging

BASE_DIR = "/mnt/storage/cam_uploads"
INCOMING = f"{BASE_DIR}/incoming"
PROCESSED = f"{BASE_DIR}/processed"
LOGFILE = f"{BASE_DIR}/logs/receiver.log"

os.makedirs(INCOMING, exist_ok=True)
os.makedirs(PROCESSED, exist_ok=True)
os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)

logging.basicConfig(filename=LOGFILE, level=logging.INFO,
                    format="%(asctime)s %(message)s")

while True:
    now = time.time()
    for f in os.listdir(INCOMING):
        if not f.endswith(".zip"):
            continue
        full = os.path.join(INCOMING, f)
        # Skip files still being written
        if now - os.path.getmtime(full) < 15:
            continue
        try:
            target = os.path.join(PROCESSED, f.replace(".zip", ""))
            os.makedirs(target, exist_ok=True)
            with zipfile.ZipFile(full, "r") as z:
                z.extractall(target)
            os.remove(full)
            logging.info(f"Extracted {f} → {target}")
        except Exception as e:
            logging.error(f"Failed to extract {f}: {e}")
    time.sleep(10)
```

Permissions:
```bash
sudo chmod +x /usr/local/bin/receive_cam_zip.py
```

---

## 3) Systemd Service

Path: `/etc/systemd/system/cam-receiver.service`

```ini
[Unit]
Description=Camera ZIP Receiver
After=network.target local-fs.target

[Service]
User=piuser
Group=piuser
ExecStart=/usr/bin/python3 /usr/local/bin/receive_cam_zip.py
Restart=always
RestartSec=5

# storage and logs
WorkingDirectory=/mnt/storage/cam_uploads

[Install]
WantedBy=multi-user.target
```

### Enable + start
```bash
sudo systemctl daemon-reload
sudo systemctl enable cam-receiver.service
sudo systemctl start cam-receiver.service
systemctl status cam-receiver.service
```

### Logs
```bash
sudo journalctl -u cam-receiver.service -f
tail -f /mnt/storage/cam_uploads/logs/receiver.log
```

Expected entries:
```
Extracted 20251102-142330.zip → /mnt/storage/cam_uploads/processed/20251102-142330
```

---

## 4) Rsync Uploads from Cameras

### 4. What we did on Zaku02 (Date: YYYY-MM-DD)

1. Created a **Dockerfile** using Python 3.9 and installed the Coral USB Accelerator runtime (`libedgetpu1-std`), `python3-pycoral`, and Pillow.  
2. Developed `worker.py` (monitor loop) and control script `coral_worker.sh` to:  
   - Detect humans in images using the EdgeTPU  
   - Move entire timestamp folders from `/processed` → `/events` if a human is found  
   - Record per-image results (human/no, confidence) into daily CSV logs in `/logs`.  
3. Set up a **cron job** to clean `/mnt/nvme0/cam_uploads/processed` folders older than 7 days.  
4. Verified system health:  
   - Coral USB LED blinking = TPU recognized  
   - Worker picks up newest stable folder and moves correctly when human appears  
   - Logs appear under `/logs/events_YYYYMMDD.csv`.  

#### Prerequisites / Host Setup Notes  
- Docker must be installed and the user has privileges to run containers.  
- Host directories must be mounted into the container:  
/mnt/nvme0/cam_uploads/processed → /data
/mnt/nvme0/cam_uploads/events → /events
/mnt/nvme0/cam_uploads/logs → /logs

- USB bus must be passed into container with: `--privileged -v /dev/bus/usb:/dev/bus/usb` so the Coral USB device is accessible.  
- Ensure directories exist on host and correct owner/permissions are set (container runs as root by default).  

#### Control Script Usage  
```bash
./coral_worker.sh start   # start the worker container  
./coral_worker.sh logs    # tail live logs  
./coral_worker.sh stop    # stop and remove the container  
./coral_worker.sh status  # display container status  
Notes for Operation

If no new folder appears, the service waits. It uses a “stable” threshold (default 15 seconds) to ensure the folder is no longer being written to before detection.

If the Coral USB LED is not blinking, then the device may not be recognized by the container — check lsusb inside container and confirm permissions.

The threshold for detection (default 0.30) may be adjusted via the THRESHOLD environment variable in coral_worker.sh.

The cron cleanup runs daily at 02:30 and deletes folders in /processed older than 7 days:
30 2 * * * find /mnt/nvme0/cam_uploads/processed -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +


### 4.1 Expected Source
Each camera (cam01, zaku03, …) performs an rsync push:
```
rsync -av --remove-source-files -e "ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes"     <zipfile> piuser@nas01.local:/mnt/storage/cam_uploads/incoming/
```
Receiver doesn’t run rsync server; it only needs SSH listening (`sshd` from default OS).

### 4.2 Confirm connectivity
```bash
sudo systemctl status ssh
```

---

## 5) Maintenance and Retention

### 5.1 Daily cleanup (optional)
If desired, remove processed data older than N days:
```bash
sudo nano /etc/cron.daily/cleanup_cam_processed
```
```bash
#!/bin/bash
find /mnt/storage/cam_uploads/processed/ -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
```
```bash
sudo chmod +x /etc/cron.daily/cleanup_cam_processed
```

### 5.2 Manual cleanup
```bash
sudo rm -rf /mnt/storage/cam_uploads/processed/*/*
```

---

## 6) Troubleshooting

| Symptom | Likely Cause | Fix |
|----------|--------------|-----|
| Service fails with `Permission denied` on NVMe | `/mnt/storage` not owned by user | `sudo chown -R piuser:piuser /mnt/storage/cam_uploads` |
| ZIPs remain unprocessed | Files still being written or corrupted | Wait >15 s; inspect `receiver.log` |
| No new data | Check SSH connectivity from camera side | `ssh piuser@nas01.local` |
| Disk full | No auto-cleanup | Enable cron cleanup or manual deletion |

---

## 7) Change Log

- **2025‑11‑02** – Initial deployment of `receive_cam_zip.py` and `cam-receiver.service`  
- **2025‑11‑02** – Verified upload from Cam01 successful, ZIP auto‑extracted, and service persists across reboot.  
