#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, threading, signal, sys, traceback, zipfile, io, shutil, subprocess, shlex
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string, make_response, Response, send_file
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput
import PCA9685

# =============================
# 💡 ハードウェア & 動作パラメータ
# =============================
I2C_ADDR   = 0x40
SERVO_CH   = 0
FREQ_HZ    = 50
CENTER_DEG = 90
SPAN_DEG   = 45
MIN_US, MAX_US = 550, 2350
LEFT_DEG  = CENTER_DEG - SPAN_DEG
RIGHT_DEG = CENTER_DEG + SPAN_DEG

# ---- 保存設定 ----
BASE_DIR    = os.path.dirname(__file__)
IMG_DIR     = os.path.join(BASE_DIR, "webdata")
CAPTURE_DIR = os.path.join(IMG_DIR, "captures")
LATEST_IMG  = "latest.jpg"
os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(CAPTURE_DIR, exist_ok=True)

# ---- 撮影設定（環境変数で上書き可）----
PREVIEW_SIZE  = (640, 360)
PREVIEW_FPS   = int(os.getenv("ZAKU_PREVIEW_FPS", "8"))
STILL_SIZE    = (2304, 1296)  # この環境の capture_file に quality kw は無い

# ---- 連続撮影設定（要件どおり）----
# ↓ ここは環境変数で 60 か 30 に変えるだけで OK
CONT_SESSION_SEC = int(os.getenv("ZAKU_CONT_SEC", "180"))  # 既定 3 分
CONT_SAVE_FPS    = float(os.getenv("ZAKU_CONT_FPS", "3"))  # 毎秒 3 枚

# =============================
# 🔧 PCA9685 サーボ制御
# =============================
def clamp(v, lo, hi): return max(lo, min(hi, v))
def angle_to_us(angle):
    angle = clamp(angle, 0, 180)
    return int(MIN_US + (MAX_US - MIN_US) * (angle / 180.0))

pwm = PCA9685.PCA9685(I2C_ADDR, debug=False)
pwm.setPWMFreq(FREQ_HZ)

def goto_angle(angle):
    pwm.setServoPulse(SERVO_CH, angle_to_us(angle))

# =============================
# 📷 Picamera2 設定
# =============================
picam2 = Picamera2()
video_config = picam2.create_video_configuration(
    main={"size": PREVIEW_SIZE},
    controls={"FrameRate": PREVIEW_FPS}
)
still_config = picam2.create_still_configuration(
    main={"size": STILL_SIZE},
    buffer_count=2
)

class StreamingOutput(io.BufferedIOBase):
    """MJPEG フレームの最新 1 枚を保持し、購読者へ通知する簡易出力。"""
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()
    def write(self, buf: bytes):
        if not buf: return
        with self.condition:
            self.frame = buf
            self.condition.notify_all()

output = StreamingOutput()
cam_lock = threading.Lock()

def start_video_stream():
    """MJPEG プレビュー開始（旧APIでも動く形）"""
    with cam_lock:
        picam2.configure(video_config)
        try:
            picam2.start_recording(MJPEGEncoder(), FileOutput(output))
        except Exception as e:
            # 一部版では start()→start_recording が必要
            print("[stream] start_recording failed once, trying start():", repr(e))
            picam2.start()
            picam2.start_recording(MJPEGEncoder(), FileOutput(output))

def stop_video_stream():
    """録画停止 + カメラ停止（安全側）"""
    try: picam2.stop_recording()
    except Exception: pass
    try: picam2.stop()
    except Exception: pass

# 起動時にライブ開始
start_video_stream()

# =============================
# 🌐 Flask アプリ
# =============================
app = Flask(__name__)

@app.after_request
def add_no_cache_headers(resp):
    # すべて no-store（Safari/Chrome のキャッシュ抑止）
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers.pop("ETag", None)
    return resp

# =============================
# 🔐 環境変数ベースの PIN & 並列制限
# =============================
VIEW_PIN = os.getenv("ZAKU_PIN", "1234")  # set via env or .env file            # 既定 PIN
MAX_CLIENTS = int(os.getenv("ZAKU_MAX_CLIENTS", "5"))
active_clients = 0
clients_lock = threading.Lock()

@app.before_request
def require_pin():
    """PIN を ?pin=XXXX で付与（/ と /stream.mjpg に適用）"""
    if request.path in ("/", "/stream.mjpg"):
        pin = request.args.get("pin")
        if pin != VIEW_PIN:
            return Response("PIN required. Use ?pin=" + VIEW_PIN + " (or set ZAKU_PIN)", status=401)

# =============================
# 📦 NAS への ZIP アップローダ（Option 1）
# =============================
NAS_USER = os.getenv("ZAKU_NAS_USER", "piuser")
NAS_HOST = os.getenv("ZAKU_NAS_HOST", "nas01.local")  # もしくは IP
NAS_PATH = os.getenv("ZAKU_NAS_PATH", "/mnt/storage/cam_uploads/incoming/")
RSYNC_BIN = os.getenv("ZAKU_RSYNC_BIN", "/usr/bin/rsync")
SSH_KEY   = os.getenv("ZAKU_SSH_KEY", "/home/piuser/.ssh/id_ed25519")
SSH_OPTS  = f"-o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i {SSH_KEY}"

def upload_to_nas(zip_path, retries=1):
    """ZIP を NAS へ送る。成功なら True"""
    if not os.path.isfile(zip_path):
        print(f"[upload] missing: {zip_path}")
        return False
    cmd = [
        RSYNC_BIN, "-av", "--remove-source-files",
        "-e", f"ssh {SSH_OPTS}",
        zip_path, f"{NAS_USER}@{NAS_HOST}:{NAS_PATH}"
    ]
    print("[upload] cmd:", " ".join(shlex.quote(c) for c in cmd))
    for attempt in range(retries + 1):
        try:
            res = subprocess.run(cmd, check=True, capture_output=True, text=True)
            if res.stdout: print("[upload][stdout]\n" + res.stdout.strip())
            if res.stderr: print("[upload][stderr]\n" + res.stderr.strip())
            print(f"[upload] OK -> {NAS_HOST}")
            return True
        except subprocess.CalledProcessError as e:
            print(f"[upload] failed try {attempt+1}: rc={e.returncode}")
            if e.stderr: print("[upload][stderr]\n" + e.stderr.strip())
            time.sleep(2)
    return False

# =============================
# 📦 連続キャプチャとZIP保存（勾選で開始/取消で停止）
# =============================
CONT_RUNNING = threading.Event()

def _save_session_from_mjpeg(session_dir, duration_sec, save_fps):
    """指定 duration_sec の間、MJPEG の最新フレームを save_fps で JPEG 保存。"""
    os.makedirs(session_dir, exist_ok=True)
    end_time = time.time() + duration_sec
    next_save = 0.0
    saved = 0
    while time.time() < end_time and CONT_RUNNING.is_set():
        with output.condition:
            output.condition.wait(timeout=0.5)
            frame = output.frame
        if frame is None:
            continue
        now = time.time()
        if now < next_save:
            continue
        next_save = now + (1.0 / max(0.5, float(save_fps)))
        fn = os.path.join(session_dir, f"{int(time.time()*1000)}.jpg")
        try:
            with open(fn, "wb") as f:
                f.write(frame)
            saved += 1
        except Exception as e:
            print("[cont] write failed:", e)
    return saved

def _zip_and_cleanup(session_dir, zip_path):
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for fn in sorted(os.listdir(session_dir)):
            zf.write(os.path.join(session_dir, fn), arcname=fn)
    try:
        shutil.rmtree(session_dir)
    except Exception:
        pass

def cont_worker():
    """ON の間、パックをループ生成。OFF 指示で終了。"""
    print("[cont] started")
    try:
        while CONT_RUNNING.is_set():
            label = datetime.now().strftime("%Y%m%d-%H%M%S")
            session_dir = os.path.join(CAPTURE_DIR, f"session-{label}")
            zip_path    = os.path.join(CAPTURE_DIR, f"{label}.zip")
            print(f"[cont] capture {label} ...")
            saved = _save_session_from_mjpeg(session_dir, CONT_SESSION_SEC, CONT_SAVE_FPS)
            _zip_and_cleanup(session_dir, zip_path)
            print(f"[cont] saved {saved} frames -> {zip_path}")
            ok = upload_to_nas(zip_path)
            print(f"[cont] {'uploaded' if ok else 'upload failed'} -> {zip_path}")
    finally:
        print("[cont] stopped")

@app.get("/api/cont/enable")
def cont_enable():
    """on=1 で開始、on=0 で停止。開始時は無限に ZIP を量産。"""
    on = request.args.get("on", "0") in ("1", "true", "True", "yes")
    if on and not CONT_RUNNING.is_set():
        CONT_RUNNING.set()
        threading.Thread(target=cont_worker, daemon=True).start()
        return jsonify(ok=True, message="Continuous capture started")
    elif not on and CONT_RUNNING.is_set():
        CONT_RUNNING.clear()
        return jsonify(ok=True, message="Continuous capture stopping...")
    else:
        return jsonify(ok=True, message="No change")

@app.get("/api/cont/status")
def cont_status():
    return jsonify(ok=True, running=CONT_RUNNING.is_set())

# =============================
# 🌀 自動巡航モード（ゆっくり左右往復）
# =============================
CRUISE_ONE_WAY_SEC = 10.0
CRUISE_END_PAUSE   = 0.5
SWEEP_RUNNING = threading.Event()

def sweep_worker():
    """左→右 10秒、停止、右→左 10秒、停止…を繰り返す。"""
    print("[sweep] started")
    try:
        while SWEEP_RUNNING.is_set():
            # 左→右
            t0 = time.time()
            while SWEEP_RUNNING.is_set():
                el = time.time() - t0
                if el >= CRUISE_ONE_WAY_SEC: break
                ratio = el / CRUISE_ONE_WAY_SEC
                angle = LEFT_DEG + (RIGHT_DEG - LEFT_DEG) * ratio
                goto_angle(angle)
                time.sleep(0.05)
            goto_angle(RIGHT_DEG); time.sleep(CRUISE_END_PAUSE)
            # 右→左
            t0 = time.time()
            while SWEEP_RUNNING.is_set():
                el = time.time() - t0
                if el >= CRUISE_ONE_WAY_SEC: break
                ratio = el / CRUISE_ONE_WAY_SEC
                angle = RIGHT_DEG - (RIGHT_DEG - LEFT_DEG) * ratio
                goto_angle(angle)
                time.sleep(0.05)
            goto_angle(LEFT_DEG); time.sleep(CRUISE_END_PAUSE)
    finally:
        print("[sweep] stopped")

@app.get("/api/sweep/toggle")
def sweep_toggle():
    if SWEEP_RUNNING.is_set():
        SWEEP_RUNNING.clear()
        return jsonify(ok=True, message="Cruise stopped")
    else:
        SWEEP_RUNNING.set()
        threading.Thread(target=sweep_worker, daemon=True).start()
        return jsonify(ok=True, message="Cruise started")

# =============================
# 🧭 HTML インターフェース（スライダー + Snapshot小図 + 状態表示）
# =============================
INDEX_HTML = """
<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Zaku Cam</title>
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, 'Noto Sans JP','Noto Sans TC',sans-serif; margin: 16px; }
  .toolbar { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; align-items:center; }
  button { padding:10px 14px; border:0; border-radius:8px; background:#444; color:#fff; cursor:pointer; }
  button:hover { filter:brightness(1.1); }
  #live { max-width:100%; border-radius:8px; border:1px solid #ddd; display:block; }
  #still { max-width:50%; border-radius:8px; border:1px solid #ccc; margin-top:8px; }
  .slider-wrap { max-width:640px; margin: 6px 0 12px; }
  .status { margin-top:8px; color:#444; font-size:14px; }
  .pill { display:inline-block; padding:2px 8px; border-radius:999px; background:#eee; margin-left:6px;}
  .pill.on { background:#c8f7c5; }
  .pill.off{ background:#ffd6d6; }
</style></head><body>
<h2>🟢 Zaku Web GUI</h2>

<div class="toolbar">
  <button onclick="move('left')">← Left</button>
  <button onclick="move('center')">● Center</button>
  <button onclick="move('right')">Right →</button>

  <button id="snapBtn" onclick="snap()">📸 Snapshot（高画質）</button>

  <label><input type="checkbox" id="contChk" onchange="toggleCont(this)"> Enable Continuous capture</label>
  <button id="cruiseBtn" onclick="toggleSweep()">🧭 Cruise: OFF</button>
</div>

<!-- 🎚 サーボ角度スライダー -->
<div class="slider-wrap">
  <label for="servoSlider">Pan position: <span id="servoLabel">50%</span></label>
  <input id="servoSlider" type="range" min="0" max="100" value="50" step="1"
         style="width:100%;" oninput="onSliderChange(this.value)">
</div>

<img id="live" src="/stream.mjpg?pin={{pin|e}}" alt="live">

<h4>Last Snapshot</h4>
<img id="still" src="/image/latest.jpg?ts={{ts}}" alt="latest still">

<div class="status" id="status">Ready.</div>

<!-- 🩺 ライブ状態表示 -->
<div class="status">
  Clients: <span id="clNow">-</span>/<span id="clMax">-</span>
  <span id="pillCont" class="pill off">Cont</span>
  <span id="pillCruise" class="pill off">Cruise</span>
</div>

<script>
const statusEl   = document.getElementById('status');
const stillEl    = document.getElementById('still');
const slider     = document.getElementById('servoSlider');
const sliderLb   = document.getElementById('servoLabel');
const snapBtn    = document.getElementById('snapBtn');
const contChk    = document.getElementById('contChk');
const cruiseBtn  = document.getElementById('cruiseBtn');
const clNow      = document.getElementById('clNow');
const clMax      = document.getElementById('clMax');
const pillCont   = document.getElementById('pillCont');
const pillCruise = document.getElementById('pillCruise');

function refreshStill(){
  const u=new URL(stillEl.src, window.location.origin);
  u.searchParams.set('ts', Date.now());
  stillEl.src=u.toString();
}

function syncSliderByDir(dir){
  const map={left:0, center:50, right:100};
  const val=map[dir] ?? 50;
  slider.value=val; sliderLb.textContent=val+'%';
}

async function move(dir){
  statusEl.textContent='Moving '+dir+'...';
  try{
    const r=await fetch('/api/move?dir='+encodeURIComponent(dir));
    const j=await r.json();
    statusEl.textContent=j.message||'OK';
    syncSliderByDir(dir); // ← ボタン操作でもスライダー同期
  }catch(e){ statusEl.textContent='Move failed'; }
}

let sliderBusy=false;
async function onSliderChange(val){
  sliderLb.textContent=val+'%';
  if(sliderBusy) return; sliderBusy=true;
  try{
    const r=await fetch('/api/angle?percent='+val);
    const j=await r.json();
    statusEl.textContent=j.message || ('Angle: '+j.angle.toFixed(1)+'°');
  }catch(e){ statusEl.textContent='Slider move failed'; }
  finally{ sliderBusy=false; }
}

async function snap(){
  if(contChk.checked){ statusEl.textContent='Disabled during continuous mode'; return; }
  statusEl.textContent='Capturing (HQ)...';
  try{
    const r=await fetch('/api/snapshot');
    const j=await r.json();
    statusEl.textContent=j.message||'Captured';
    if(j.ok!==False) refreshStill();
  }catch(e){ statusEl.textContent='Capture failed'; }
}

async function toggleCont(cb){
  const on = cb.checked ? 1 : 0;
  snapBtn.disabled = !!on;        // 連続撮影ON中は Snapshot 無効
  statusEl.textContent = on ? 'Continuous capture ON' : 'Continuous capture OFF';
  try{ await fetch('/api/cont/enable?on='+on); }catch(e){}
  // 即時 UI 更新
  pillCont.classList.toggle('on', !!on);
  pillCont.classList.toggle('off', !on);
}

async function toggleSweep(){
  try{
    const r=await fetch('/api/sweep/toggle');
    const j=await r.json();
    const on = j.message.includes('started');
    cruiseBtn.textContent = on ? '🧭 Cruise: ON' : '🧭 Cruise: OFF';
    pillCruise.classList.toggle('on', on);
    pillCruise.classList.toggle('off', !on);
    statusEl.textContent  = j.message;
  }catch(e){ statusEl.textContent='Cruise toggle failed'; }
}

// /health を定期ポーリングして簡易状態を更新
async function pollHealth(){
  try{
    const r=await fetch('/health');
    const j=await r.json();
    clNow.textContent = j.active_clients ?? '-';
    clMax.textContent = j.max_clients ?? '-';
    const cont = !!j.cont_running;
    const crui = !!j.sweep_running;
    pillCont.classList.toggle('on', cont);
    pillCont.classList.toggle('off', !cont);
    pillCruise.classList.toggle('on', crui);
    pillCruise.classList.toggle('off', !crui);
    contChk.checked = cont;
    snapBtn.disabled = cont;
  }catch(e){}
  setTimeout(pollHealth, 5000);
}
pollHealth();
</script>
</body></html>
"""

@app.route("/")
def index():
    # index は PIN 必須なので、テンプレに渡し直して img のクエリへ再付与
    pin = request.args.get("pin", "")
    return render_template_string(INDEX_HTML, ts=int(time.time()), pin=pin)

# =============================
# 📡 Stream & API
# =============================
@app.get("/stream.mjpg")
def stream_mjpg():
    global active_clients
    with clients_lock:
        if active_clients >= MAX_CLIENTS:
            return Response("Too many clients", status=503)
        active_clients += 1
    print(f"[stream] client connected ({active_clients}/{MAX_CLIENTS})")

    def gen():
        global active_clients  # ← modify global safely
        try:
            while True:
                with output.condition:
                    output.condition.wait(timeout=1.0)
                    frame = output.frame
                if frame is None: continue
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        except (BrokenPipeError, ConnectionResetError, GeneratorExit):
            # client closed connection — normal
            pass
        except Exception as e:
            print("[stream] client error:", e)
        finally:
            with clients_lock:
                active_clients = max(0, active_clients - 1)
                print(f"[stream] client disconnected ({active_clients}/{MAX_CLIENTS})")

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.get("/api/move")
def api_move():
    DIR = request.args.get("dir", "center")
    try:
        if DIR == "left":   goto_angle(LEFT_DEG)
        elif DIR == "right":goto_angle(RIGHT_DEG)
        else:               goto_angle(CENTER_DEG)
        return jsonify(ok=True, where=DIR, message=f"Moved to {DIR}.")
    except Exception as e:
        traceback.print_exc(); return jsonify(ok=False, error=str(e)),500

@app.get("/api/angle")
def api_angle():
    """スライダー（0～100%）を角度にマップ：0%=LEFT, 50%=CENTER, 100%=RIGHT"""
    try:
        pct = float(request.args.get("percent", 50))
        pct = clamp(pct, 0.0, 100.0)
        angle = LEFT_DEG + (RIGHT_DEG - LEFT_DEG) * (pct / 100.0)
        goto_angle(angle)
        return jsonify(ok=True, percent=pct, angle=angle,
                       message=f"Moved to {pct:.0f}% ({angle:.1f}°)")
    except Exception as e:
        traceback.print_exc(); return jsonify(ok=False, error=str(e)), 500

@app.get("/api/snapshot")
def api_snapshot():
    # 連続撮影ON中は不可
    if CONT_RUNNING.is_set():
        return jsonify(ok=False, message="Disabled in continuous mode"), 400
    path = os.path.join(IMG_DIR, LATEST_IMG)
    try:
        with cam_lock:
            stop_video_stream()
            picam2.configure(still_config)
            picam2.start()
            time.sleep(0.08)
            tmp = f"{path}.tmp_{int(time.time()*1000)}.jpg"  # .jpg 必須
            picam2.capture_file(tmp)                         # quality kw 非対応
            os.replace(tmp, path)
            picam2.stop()
            start_video_stream()
        return jsonify(ok=True, message="Snapshot saved (HQ).")
    except Exception as e:
        traceback.print_exc()
        try: start_video_stream()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500

@app.get("/image/<path:name>")
def image(name):
    p = os.path.join(IMG_DIR, name)
    if not os.path.isfile(p): return jsonify(ok=False, error="not found"), 404
    return make_response(send_file(p, mimetype="image/jpeg", conditional=False))

@app.get("/health")
def health():
    return jsonify(ok=True,
                   active_clients=active_clients,
                   max_clients=MAX_CLIENTS,
                   cont_running=CONT_RUNNING.is_set(),
                   sweep_running=SWEEP_RUNNING.is_set(),
                   time=time.strftime("%H:%M:%S"),
                   preview_size=PREVIEW_SIZE, preview_fps=PREVIEW_FPS,
                   still_size=STILL_SIZE,
                   cont_sec=CONT_SESSION_SEC, cont_fps=CONT_SAVE_FPS)

# =============================
# 🚪 Graceful exit
# =============================
def _graceful_exit(signum=None, frame=None):
    try:
        CONT_RUNNING.clear()
        SWEEP_RUNNING.clear()
        stop_video_stream()
    finally:
        sys.exit(0)

signal.signal(signal.SIGTERM, _graceful_exit)
signal.signal(signal.SIGINT,  _graceful_exit)

if __name__ == "__main__":
    try:
        goto_angle(CENTER_DEG)
        print(f"Serving on http://0.0.0.0:8080/?pin={VIEW_PIN}")
        app.run(host="0.0.0.0", port=8080, threaded=True)
    finally:
        _graceful_exit()
