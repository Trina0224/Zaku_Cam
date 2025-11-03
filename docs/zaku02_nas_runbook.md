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
