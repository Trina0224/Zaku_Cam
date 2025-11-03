# Zaku Camera System (Sanitized)

A tiny Flask + Picamera2 app that serves an MJPEG preview, pans a servo via PCA9685, captures high‑res snapshots, 
and runs a **continuous capture → ZIP → rsync to NAS** pipeline.

## Quick start (Cam node)

```bash
sudo apt update && sudo apt install -y python3-picamera2 python3-libcamera python3-flask rsync openssh-client
git clone https://github.com/<yourname>/zaku-camera-system.git
cd zaku-camera-system/app
python3 app.py
# open http://<cam-host>:8080/?pin=1234
```

Environment variables (or use `.env`):
- `ZAKU_PIN` (default `1234`)
- `ZAKU_PREVIEW_FPS` (default `8`)
- `ZAKU_CONT_SEC` (ZIP duration seconds, default `180`)
- `ZAKU_CONT_FPS` (frame save rate, default `3`)
- `ZAKU_NAS_HOST` (default `nas01.local`)
- `ZAKU_NAS_USER` (default `piuser`)
- `ZAKU_NAS_PATH` (default `/mnt/storage/cam_uploads/incoming/`)
- `ZAKU_SSH_KEY` (default `/home/piuser/.ssh/id_ed25519`)

## Systemd

See `systemd/zaku-camera.service` and `systemd/cam-receiver.service` for optional auto‑start on boot.

## Docs

- `docs/zaku01_camera_runbook.md` — camera node setup
- `docs/zaku02_nas_runbook.md` — NAS receiver setup

> **Security note:** Do **not** commit any private keys. The defaults here are placeholders.
