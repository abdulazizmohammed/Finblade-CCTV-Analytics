import os
import urllib.request

os.makedirs("models", exist_ok=True)
URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt"
DEST = "models/yolov8n.pt"
print("downloading", URL)
urllib.request.urlretrieve(URL, DEST)
size = os.path.getsize(DEST)
print(f"saved {DEST} ({size} bytes)")
if size < 1_000_000:
    raise SystemExit("weights suspiciously small; download may have failed")
