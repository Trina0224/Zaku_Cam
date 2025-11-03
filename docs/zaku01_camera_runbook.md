# Cam01 Camera System — Stepdown Runbook (English)

**Host:** `cam01` (Raspberry Pi Zero 2 W)  
**OS:** Raspberry Pi OS (previous release — *not* 2025 Oct build, due to known Wi‑Fi issue on Zero 2W)  
**App Path:** `/home/piuser/zaku-camera-system/app/app.py`  
**Purpose:** Capture continuous JPEG frames → pack ZIP every *N* seconds → upload to NAS (nas01) via `rsync` over SSH.  
**Autostart:** `systemd` service `zaku-camera.service`  
**Cron jobs:** **None** (all logic runs inside the app)

---

## 1) One‑time System Prep

### 1.1 Update base system
```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

### 1.2 Install required packages
> Picamera2 pulls in libcamera stack. Pillow/NumPy are common deps; OpenCV is optional but helpful.
```bash
sudo apt install -y   python3-picamera2 python3-libcamera python3-flask python3-pip   python3-pil python3-numpy python3-opencv   python3-rpi.gpio rsync openssh-client
```

### 1.3 Directory layout
```
/home/piuser/zaku-camera-system/app/         # project root
└── app.py                          # main Flask + capture/uploader
└── PCA9685.py                      # (servo driver module, if used)
└── webdata/
    └── captures/                   # per-session JPEGs + ZIP before upload
```

### 1.4 SSH key (for uploading to NAS)
Ensure the **camera host (cam01)** can SSH to NAS as `piuser` without a password:
```bash
# on cam01
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
ssh-copy-id piuser@nas01.local        # or use NAS IP
# test
ssh -o BatchMode=yes piuser@nas01.local 'echo OK'
```

> If your NAS user isn’t `piuser`, adjust the service env vars in §3.2.

---

## 2) Application Configuration

The app reads a few environment variables (sane defaults included):

| Variable | Meaning | Default |
|---|---|---|
| `ZAKU_PIN` | Web UI PIN (`/?pin=...`) to view stream | `0000` |
| `ZAKU_PREVIEW_FPS` | MJPEG preview FPS | `8` |
| `ZAKU_CONT_SEC` | Seconds per ZIP packet | `60` (set in service) |
| `ZAKU_CONT_FPS` | Frame save rate during continuous capture | `3` |
| `ZAKU_NAS_HOST` | NAS hostname or IP | `nas01.local` |
| `ZAKU_NAS_USER` | NAS SSH user | `piuser` |
| `ZAKU_NAS_PATH` | NAS incoming dir for ZIPs | `/mnt/storage/cam_uploads/incoming/` |
| `ZAKU_RSYNC_BIN` | rsync binary | `/usr/bin/rsync` |
| `ZAKU_SSH_KEY` | private key used for SSH | `/home/<user>/.ssh/id_ed25519` |

---

## 3) Autostart via systemd

### 3.1 Create the service unit
Create `/etc/systemd/system/zaku-camera.service`:
```ini
[Unit]
Description=Zaku Web Camera (PCA9685 Controller)
After=network-online.target
Wants=network-online.target

[Service]
User=piuser
Group=piuser
WorkingDirectory=/home/piuser/zaku-camera-system/app
ExecStart=/usr/bin/python3 /home/piuser/zaku-camera-system/app/app.py
Restart=always
RestartSec=5
# Wait a bit for Wi‑Fi on Zero 2W
ExecStartPre=/bin/sleep 10

# Environment (tune as needed)
Environment=ZAKU_CONT_SEC=60
Environment=ZAKU_NAS_HOST=nas01.local
Environment=ZAKU_NAS_USER=piuser
Environment=ZAKU_NAS_PATH=/mnt/storage/cam_uploads/incoming/

# Security: allow access to /home (needed for WorkingDirectory + key)
ProtectSystem=full
ProtectHome=false
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### 3.2 Enable + start
```bash
sudo systemctl daemon-reload
sudo systemctl enable zaku-camera.service
sudo systemctl start  zaku-camera.service
```

### 3.3 Basic ops
```bash
systemctl status zaku-camera.service
sudo journalctl -u zaku-camera.service -n 100 -f

# control
sudo systemctl stop zaku-camera.service
sudo systemctl restart zaku-camera.service
sudo systemctl disable zaku-camera.service
```

---

## 4) Web Interface & API

### 4.1 Open the UI
```
http://cam01.local:8080/?pin=0000
```
(Use IP if mDNS isn’t available.)

UI features:
- Live MJPEG stream
- Left/Center/Right buttons
- Angle **slider**
- **Snapshot** button (disabled during continuous mode)
- **Continuous** toggle (ZIP every N seconds)
- **Cruise** toggle (slow left↔right sweep)

### 4.2 Health endpoint
```
GET /health
```
Returns JSON: current clients, preview/still sizes, whether continuous/cruise are running, etc.

---

## 5) How to Modify the App

1. **Stop** the service (avoids file-in-use glitches):
   ```bash
   sudo systemctl stop zaku-camera.service
   ```
2. **Edit** the file:
   ```bash
   nano /home/piuser/zaku-camera-system/app/app.py
   ```
3. **Start** (or restart) and watch logs:
   ```bash
   sudo systemctl start zaku-camera.service
   sudo journalctl -u zaku-camera.service -n 80 -f
   ```

> Quick one-off test without systemd:  
> `cd /home/piuser/zaku-camera-system/app && python3 app.py`  
> (Hit `Ctrl+C` to quit; then return to systemd: `sudo systemctl restart zaku-camera.service`.)

---

## 6) Upload Flow (cam01 → nas01)

- App creates `YYYYMMDD-HHMMSS.zip` in `webdata/captures/` at the end of each session.  
- Immediately calls `rsync` to push to NAS:
  - SSH options: non‑interactive (`BatchMode`), skip host‑key prompt for robustness, short timeout, specific key.
  - On success, the local ZIP is **removed** (flag `--remove-source-files`).  
- On the NAS, a separate receiver service extracts ZIPs and files them (documented in the nas01 runbook).

---

## 7) Troubleshooting

**Service fails with `status=200/CHDIR`:**  
- Likely `ProtectHome=true` blocked `/home`. Set `ProtectHome=false` (as in §3.1).  
- Verify path and case: `/home/piuser/zaku-camera-system/app`

**Web UI 401 / needs PIN:**  
- Append `?pin=0000` (or your custom PIN).  
- Change the PIN via env `ZAKU_PIN` if desired.

**No ZIP uploads but manual rsync works:**  
- Check service user (`User=piuser`) matches SSH key ownership.
- Confirm key path in logs; ensure file exists and permissions are `600`.
- Tail logs: `sudo journalctl -u zaku-camera.service -f` and look for `[upload]` lines.

**Preview/snapshot conflicts:**  
- Snapshot temporarily stops the preview stream; the code restarts stream afterward. If it fails, reload the page or restart service.

**Wi‑Fi not up at boot:**  
- Increase `ExecStartPre=/bin/sleep 20` to 20s (or more).

---

## 8) What’s *not* here (by design)

- **No cron jobs** on cam01. All cadence (continuous capture, ZIP cadence, upload) is part of the running app.  
- **No global write under `/`**; app writes only in its project dir.  
- **No public network exposure**; access is within home LAN.

---

## 9) Quick Reference — Most used commands

```bash
# logs
sudo journalctl -u zaku-camera.service -n 100 -f

# restart after changing app.py
sudo systemctl restart zaku-camera.service

# stop service (to run app manually for debugging)
sudo systemctl stop zaku-camera.service

# run manually (foreground)
cd /home/piuser/zaku-camera-system/app && python3 app.py
```

---

## 10) Change Log (operator notes)

- **2025‑11‑02**: Installed service `zaku-camera.service`; `ProtectHome=false` to allow /home WorkingDirectory.  
- **2025‑11‑02**: Set `ZAKU_CONT_SEC=60` for ~1‑minute ZIPs.  
- **2025‑11‑02**: Confirmed uploads to `nas01.local:/mnt/storage/cam_uploads/incoming/`.
