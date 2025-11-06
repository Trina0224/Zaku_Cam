#!/bin/bash

CONTAINER_NAME="coral-worker"
IMAGE_NAME="coral-py39"

DATA_ROOT="/mnt/nvme0/cam_uploads/processed"
EVENTS_ROOT="/mnt/nvme0/cam_uploads/events"
LOGS_ROOT="/mnt/nvme0/cam_uploads/logs"

# Default env values
THRESHOLD="0.30"
SLEEP_SEC="10"
STABLE_SEC="15"

show_menu() {
    echo ""
    echo "Usage: $0 {start|stop|logs|status}"
    echo ""
    echo "Commands:"
    echo "  start   - Start the coral worker container"
    echo "  stop    - Stop and remove the coral worker container"
    echo "  logs    - Tail live logs (Ctrl+C to exit)"
    echo "  status  - Show container status"
    echo ""
    echo "Example:"
    echo "  $0 start"
    echo ""
}

case "$1" in
    start)
        echo "[INFO] Starting $CONTAINER_NAME ..."
        docker run -d \
	  --env-file ./.env.zaku02 \
          --name "$CONTAINER_NAME" \
          --restart unless-stopped \
          --privileged \
          -v /dev/bus/usb:/dev/bus/usb \
          -v "$DATA_ROOT":/data \
          -v "$EVENTS_ROOT":/events \
          -v "$LOGS_ROOT":/logs \
          -e THRESHOLD="$THRESHOLD" \
          -e SLEEP_SEC="$SLEEP_SEC" \
          -e STABLE_SEC="$STABLE_SEC" \
          "$IMAGE_NAME"
        ;;
    stop)
        echo "[INFO] Stopping and removing $CONTAINER_NAME ..."
        docker stop "$CONTAINER_NAME" >/dev/null 2>&1
        docker rm "$CONTAINER_NAME" >/dev/null 2>&1
        ;;
    logs)
        echo "[INFO] Tailing logs (Ctrl+C to exit) ..."
        docker logs -f "$CONTAINER_NAME"
        ;;
    status)
        docker ps -a | grep "$CONTAINER_NAME"
        ;;
    ""|help|-h|--help)
        show_menu
        ;;
    *)
        echo "[ERROR] Unknown command: $1"
        show_menu
        exit 1
        ;;
esac
