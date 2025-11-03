#!/usr/bin/env python3
# please put the code under /usr/local/bin/receive_cam_zip.py
import os
import time
import zipfile
import logging
import tempfile
import shutil

BASE_DIR = "/mnt/nvme0/cam_uploads"
INCOMING = f"{BASE_DIR}/incoming"
PROCESSED = f"{BASE_DIR}/processed"
LOGFILE   = f"{BASE_DIR}/logs/receiver.log"

os.makedirs(INCOMING, exist_ok=True)
os.makedirs(PROCESSED, exist_ok=True)
os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)

logging.basicConfig(
    filename=LOGFILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

MTIME_STABLE_SEC = 15   # 最終更新から15秒以上経過したファイルのみ処理する
SLEEP_INTERVAL   = 10   # 各スキャンの間隔（秒）

def safe_extract(zipf: zipfile.ZipFile, dest_dir: str) -> None:
    """zip-slip防止：すべてのファイルをdest_dir以下に解凍することを保証"""
    for member in zipf.infolist():
        # 絶対パスや上位ディレクトリへの脱出を拒否する
        target_path = os.path.realpath(os.path.join(dest_dir, member.filename))
        if not target_path.startswith(os.path.realpath(dest_dir) + os.sep) and \
           target_path != os.path.realpath(dest_dir):
            raise RuntimeError(f"Unsafe path in zip: {member.filename}")
    zipf.extractall(dest_dir)

def process_one_zip(zip_path: str) -> None:
    base_name = os.path.basename(zip_path).rsplit(".zip", 1)[0]
    final_dir = os.path.join(PROCESSED, base_name)

    # すでに処理済みであればzipを削除（重複防止）
    if os.path.isdir(final_dir):
        os.remove(zip_path)
        logging.info(f"Already processed, removed duplicate zip: {zip_path}")
        return

    # 一時ディレクトリに解凍し、その後アトミックに最終位置へ移動する
    with tempfile.TemporaryDirectory(dir=PROCESSED) as tmpdir:
        tmp_target = os.path.join(tmpdir, "extract")
        os.makedirs(tmp_target, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            # 任意：zipファイルが有効か簡易チェック（zipfile.openで例外を投げても良い）
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f"Corrupted entry in zip ({bad})")

            safe_extract(zf, tmp_target)

        # 最終ディレクトリへ移動（中途半端な状態で他プロセスに拾われないようにする）
        shutil.move(tmp_target, final_dir)

    os.remove(zip_path)
    logging.info(f"Extracted {os.path.basename(zip_path)} -> {final_dir}")

def main():
    logging.info("Receiver started")
    while True:
        now = time.time()
        try:
            # scandirを使用するとlistdirより効率的で、statも直接取得可能
            with os.scandir(INCOMING) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if not name.endswith(".zip"):
                        continue

                    zip_path = entry.path

                    # mtime安定確認：rsyncなどによるリネーム完了を保証
                    try:
                        mtime = entry.stat().st_mtime
                    except FileNotFoundError:
                        # 同時に削除や移動された場合（競合）、無視して次へ
                        continue

                    if now - mtime < MTIME_STABLE_SEC:
                        continue

                    try:
                        process_one_zip(zip_path)
                    except zipfile.BadZipFile as e:
                        logging.error(f"Bad zip file {zip_path}: {e}")
                        # 任意：隔離(quarantine)ディレクトリへ移動することも可能
                        # 現状では手動確認のため元ファイルを保持
                    except Exception as e:
                        logging.error(f"Failed to process {zip_path}: {e}")

        except Exception as e:
            logging.error(f"Scan loop error: {e}")

        time.sleep(SLEEP_INTERVAL)

if __name__ == "__main__":
    main()
