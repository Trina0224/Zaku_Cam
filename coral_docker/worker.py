#!/usr/bin/env python3
import os, sys, csv, json, time, signal, shutil, traceback
from pathlib import Path
from datetime import datetime
from PIL import Image
from pycoral.adapters import detect, common
from pycoral.utils.dataset import read_label_file
from pycoral.utils.edgetpu import make_interpreter, list_edge_tpus

MODEL_PATH   = os.environ.get("MODEL",  "/app/test_data/model.tflite")
LABELS_PATH  = os.environ.get("LABELS", "/app/test_data/coco_labels.txt")
DATA_ROOT    = Path(os.environ.get("DATA_ROOT", "/data"))
EVENTS_ROOT  = Path(os.environ.get("EVENTS_ROOT", "/events"))
LOGS_ROOT    = Path(os.environ.get("LOGS_ROOT", "/logs"))
THRESHOLD    = float(os.environ.get("THRESHOLD", "0.3"))
SLEEP_SEC    = int(os.environ.get("SLEEP_SEC", "10"))
STABLE_SEC   = int(os.environ.get("STABLE_SEC", "15"))
STATE_PATH   = Path(os.environ.get("STATE_PATH", "/logs/worker_state.json"))
DEBUG        = os.environ.get("DEBUG", "0") == "1"
MIN_IMAGES   = int(os.environ.get("MIN_IMAGES", "1"))   # require at least this many images before scanning

IMG_EXTS = {".jpg", ".jpeg", ".png"}
_running = True

def log(msg):  print(f"[INFO] {msg}", flush=True)
def warn(msg): print(f"[WARN] {msg}", file=sys.stderr, flush=True)
def err(msg):  print(f"[ERROR] {msg}", file=sys.stderr, flush=True)

def _sigterm(_s, _f):
    global _running
    _running = False
signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT, _sigterm)

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)
    return p

def read_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}

def write_state(state: dict):
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as e:
        warn(f"Failed to write state: {e}")

def list_images(folder: Path):
    try:
        imgs = [p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in IMG_EXTS]
    except FileNotFoundError:
        return []
    return sorted(imgs)

def newest_image_mtime(folder: Path) -> float:
    newest = 0.0
    for p in list_images(folder):
        m = p.stat().st_mtime
        if m > newest:
            newest = m
    return newest

def choose_latest_ready_folder(root: Path):
    """
    Among direct subfolders of root, pick the one whose newest image mtime is the most recent,
    provided it has >= MIN_IMAGES images and is stable for >= STABLE_SEC.
    Return (folder_path, newest_mtime, num_images) or (None, 0.0, 0)
    """
    best = (None, 0.0, 0)
    try:
        subs = [p for p in root.iterdir() if p.is_dir()]
    except FileNotFoundError:
        return best

    now = time.time()
    for d in subs:
        imgs = list_images(d)
        n = len(imgs)
        if n < MIN_IMAGES:
            if DEBUG: log(f"Skip {d.name}: only {n} images (<{MIN_IMAGES})")
            continue
        m = newest_image_mtime(d)
        if m == 0.0:
            if DEBUG: log(f"Skip {d.name}: no image mtime")
            continue
        age = now - m
        if age < STABLE_SEC:
            if DEBUG: log(f"Skip {d.name}: not stable yet (age {age:.1f}s < {STABLE_SEC}s)")
            continue
        # pick the most recent (largest m)
        if m > best[1]:
            best = (d, m, n)
    return best

def append_csv(logfile: Path, rows):
    new_file = not logfile.exists()
    with logfile.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["utc_time", "folder", "image", "human", "confidence"])
        w.writerows(rows)

def detect_person_score(interpreter, labels, image_path: Path, threshold: float):
    img = Image.open(image_path).convert("RGB")
    _, scale = common.set_resized_input(
        interpreter, img.size, lambda size: img.resize(size, Image.LANCZOS))
    interpreter.invoke()
    objs = detect.get_objects(interpreter, score_threshold=threshold, image_scale=scale)
    found, max_score = False, 0.0
    for o in objs:
        if labels.get(o.id, str(o.id)).lower() == "person":
            found = True
            if o.score > max_score:
                max_score = o.score
    return found, max_score

def move_folder(src: Path, dst_parent: Path) -> Path:
    ensure_dir(dst_parent)
    target = dst_parent / src.name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = dst_parent / f"{src.name}__moved_{stamp}"
    shutil.move(str(src), str(target))
    return target

def main():
    # Mounts & model
    if not DATA_ROOT.exists():
        err(f"DATA_ROOT not found: {DATA_ROOT}")
        sys.exit(2)
    ensure_dir(EVENTS_ROOT)
    ensure_dir(LOGS_ROOT)
    ensure_dir(STATE_PATH.parent)

    # TPU presence check
    tpus = list_edge_tpus()
    if not tpus:
        warn("No EdgeTPU devices found by pycoral (LED may not blink). Check USB mapping.")
    else:
        log(f"EdgeTPUs found: {tpus}")

    labels = read_label_file(LABELS_PATH)
    interpreter = make_interpreter(MODEL_PATH)
    interpreter.allocate_tensors()
    log("Interpreter ready.")

    state = read_state()
    log(f"Worker watching {DATA_ROOT} | thr={THRESHOLD} stable={STABLE_SEC}s sleep={SLEEP_SEC}s min_imgs={MIN_IMAGES}")
    if state: log(f"Loaded state: {state}")

    global _running
    while _running:
        try:
            folder, newest_m, count = choose_latest_ready_folder(DATA_ROOT)
            if not folder:
                if DEBUG: log("No ready folder to process (either empty/unstable/too few images).")
                time.sleep(SLEEP_SEC); continue

            # Skip if already processed with same newest_mtime
            last = state.get("last")
            last_m = state.get("last_mtime")
            if last == folder.name and last_m == newest_m:
                if DEBUG: log(f"Skipping {folder.name}: already processed at mtime {newest_m}.")
                time.sleep(SLEEP_SEC); continue

            images = list_images(folder)
            log(f"Scanning {folder.name}: {len(images)} images (ready; newest age {(time.time()-newest_m):.1f}s)")
            rows, folder_has_person = [], False

            for i, img in enumerate(images, 1):
                try:
                    human, score = detect_person_score(interpreter, labels, img, THRESHOLD)
                    folder_has_person |= human
                    rows.append([
                        datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        folder.name, img.name,
                        "YES" if human else "NO",
                        f"{score:.4f}",
                    ])
                    if DEBUG and (i % 20 == 0 or i == len(images)):
                        log(f"...processed {i}/{len(images)}")
                except Exception as e:
                    rows.append([
                        datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        folder.name, img.name, "ERROR", ""
                    ])
                    warn(f"Failed on {img}: {e}")
                    if DEBUG: traceback.print_exc()

            # write log lines
            log_file = LOGS_ROOT / f"events_{datetime.utcnow().strftime('%Y%m%d')}.csv"
            append_csv(log_file, rows)
            log(f"Wrote {len(rows)} rows to {log_file.name}")

            # move or leave
            if folder_has_person:
                dst = move_folder(folder, EVENTS_ROOT)
                log(f"PERSON found → moved folder to: {dst}")
                state.update({"last": folder.name, "last_mtime": newest_m, "result": "moved"})
            else:
                log(f"No person in {folder.name} → left in place")
                state.update({"last": folder.name, "last_mtime": newest_m, "result": "no_person"})

            write_state(state)
            time.sleep(SLEEP_SEC)

        except Exception as e:
            err(f"Loop error: {e}")
            if DEBUG: traceback.print_exc()
            time.sleep(SLEEP_SEC)

    log("Worker exiting.")

if __name__ == "__main__":
    main()
