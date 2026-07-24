import os, sys, time
import numpy as np, cv2
from ultralytics import YOLO

# a representative frame (fall back to noise if none saved)
frame = cv2.imread("media/CAM-SYN-01_frame.jpg")
if frame is None:
    frame = cv2.imread("evidence/scenario_first_frame.jpg")
if frame is None:
    frame = np.random.randint(0, 255, (688, 464, 3), np.uint8)
print("frame:", frame.shape)

imgsz = int(sys.argv[1]) if len(sys.argv) > 1 else 1280
model = YOLO("models/yolov8n.pt", task="detect")

def bench(dev, n=25):
    try:
        for _ in range(3):  # warmup (kernel autotune / H2D)
            model.predict(frame, imgsz=imgsz, device=dev, verbose=False)
        t = time.time()
        for _ in range(n):
            model.predict(frame, imgsz=imgsz, device=dev, verbose=False)
        dt = time.time() - t
        print(f"  {dev:8s}: {n/dt:6.1f} FPS   ({dt/n*1000:5.0f} ms/frame)")
    except Exception as e:
        print(f"  {dev:8s}: ERROR {type(e).__name__}: {e}")

import torch
print("torch", torch.__version__, "cuda_available", torch.cuda.is_available())
print(f"=== YOLOv8n @ imgsz {imgsz} ===")
bench("cpu")
if torch.cuda.is_available():
    torch.cuda.init()
    try:
        print("  gpu:", torch.cuda.get_device_name(0))
    except Exception as e:
        print("  gpu name unavailable:", e)
    bench("cuda:0")
