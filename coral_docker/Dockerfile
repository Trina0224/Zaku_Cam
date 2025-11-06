FROM debian:bullseye-slim
ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Base deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    curl ca-certificates gnupg unzip udev xxd \
 && rm -rf /var/lib/apt/lists/*

# Coral APT repo + runtime + PyCoral
RUN install -d /usr/share/keyrings /etc/apt/sources.list.d && \
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --dearmor -o /usr/share/keyrings/coral-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/coral-archive-keyring.gpg] https://packages.cloud.google.com/apt coral-edgetpu-stable main" \
      > /etc/apt/sources.list.d/coral-edgetpu.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends libedgetpu1-std python3-pycoral && \
    rm -rf /var/lib/apt/lists/*

# Pillow for image IO
RUN pip3 install --no-cache-dir pillow

# Fetch labels + sample image + model (with retries) and verify magic appears early in file
RUN set -eux; \
    mkdir -p /app/test_data; \
    curl --retry 4 -fL -o /app/test_data/coco_labels.txt \
      https://raw.githubusercontent.com/google-coral/test_data/master/coco_labels.txt; \
    curl --retry 4 -fL -o /app/test_data/face.jpg \
      https://raw.githubusercontent.com/google-coral/test_data/master/face.jpg; \
    # Try primary SSD MobileNet v2 model (EdgeTPU-compiled), then fallback to test_data EfficientDet
    ( curl --retry 4 -fL -o /app/test_data/model.tflite \
        https://storage.googleapis.com/cloud-iot-edge-pretrained-models/vision/ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite \
      || \
      curl --retry 4 -fL -o /app/test_data/model.tflite \
        https://raw.githubusercontent.com/google-coral/test_data/master/efficientdet_lite0_320_ptq_edgetpu.tflite ); \
    # Verify that the magic 'TFL3' occurs within the first 16 bytes (some files start with an offset)
    python3 - <<'PY' || (echo "Model file is invalid or truncated"; exit 1)
import pathlib
p = pathlib.Path("/app/test_data/model.tflite")
h = p.read_bytes()[:16]
assert b"TFL3" in h, f"'TFL3' not found in first 16 bytes: {h!r}"
print("Verified TFLite magic found within first 16 bytes.")
PY

# App
COPY detect_person.py /app/detect_person.py
COPY batch_scan.py /app/batch_scan.py
COPY worker.py /app/worker.py

# Add Google Drive upload helper
COPY zaku02_upload_drive.sh /usr/local/bin/zaku02_upload_drive.sh
RUN chmod +x /usr/local/bin/zaku02_upload_drive.sh


# Default:
CMD ["python3", "/app/worker.py"]
#CMD ["python3", "/app/batch_scan.py"]


# Self-test on bundled image
#CMD ["python3","/app/detect_person.py", \
#     "--model","/app/test_data/model.tflite", \
#     "--labels","/app/test_data/coco_labels.txt", \
#     #"--image","/app/test_data/face.jpg", \
#     "--threshold","0.3"]


### Rebuild this docker command: " docker build -t coral-py39 . "
