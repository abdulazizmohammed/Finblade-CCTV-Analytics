"""Fetch model weights into models/ (not committed — see .gitignore).

Two models:
  yolov8n.pt              person detection (COCO class 0)
  osnet_x0_25_msmt17.pt   person ReID embeddings for cross-camera identity

Run:  .venv/bin/python scripts/get_weights.py
"""

import os
import urllib.request

os.makedirs("models", exist_ok=True)

# ---- detector -------------------------------------------------------------
URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt"
DEST = "models/yolov8n.pt"
if os.path.exists(DEST):
    print(f"skip {DEST} (exists)")
else:
    print("downloading", URL)
    urllib.request.urlretrieve(URL, DEST)
    size = os.path.getsize(DEST)
    print(f"saved {DEST} ({size} bytes)")
    if size < 1_000_000:
        raise SystemExit("weights suspiciously small; download may have failed")

# ---- ReID embedder --------------------------------------------------------
# osnet_x0_25 trained on MSMT17. Chosen over the market1501 variant because
# MSMT17 is larger and shot across more cameras/lighting, so it generalises
# better to real CCTV than the cleaner Market-1501 benchmark. ~3 MB.
# boxmot resolves the download (Google Drive) and verifies the load.
REID_DEST = "models/osnet_x0_25_msmt17.pt"
if os.path.exists(REID_DEST):
    print(f"skip {REID_DEST} (exists)")
else:
    print("downloading ReID weights via boxmot ->", REID_DEST)
    from pathlib import Path

    from boxmot.reid import ReID

    # Instantiating triggers the fetch; device 'cpu' so this works headless.
    ReID(weights=Path(REID_DEST), device="cpu")
    size = os.path.getsize(REID_DEST)
    print(f"saved {REID_DEST} ({size} bytes)")
    if size < 1_000_000:
        raise SystemExit("ReID weights suspiciously small; download may have failed")

print("done.")
