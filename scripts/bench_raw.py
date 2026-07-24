import sys, time, torch
from ultralytics import YOLO

imgsz = int(sys.argv[1]) if len(sys.argv) > 1 else 1280
m = YOLO("models/yolov8n.pt").model.eval()
x = torch.rand(1, 3, imgsz, imgsz)

def raw(dev, n=40):
    md = m.to(dev)
    xd = x.to(dev)
    with torch.no_grad():
        for _ in range(8):
            md(xd)
        if dev.startswith("cuda"):
            torch.cuda.synchronize()
        t = time.time()
        for _ in range(n):
            md(xd)
        if dev.startswith("cuda"):
            torch.cuda.synchronize()
        dt = time.time() - t
    print(f"  {dev:8s}: {n/dt:7.1f} FPS raw inference ({dt/n*1000:5.1f} ms/frame)")

print(f"=== raw YOLOv8n forward @ {imgsz} (batch=1) ===")
raw("cpu")
if torch.cuda.is_available():
    raw("cuda:0")
