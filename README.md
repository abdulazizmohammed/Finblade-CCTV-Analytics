# FinBlade CCTV — Day 1: the inference spine

Goal of this slice: prove **detect → track → assign-to-zone → live occupancy**
on a deterministic recorded loop, viewable in a browser. Everything else
(rules, density thresholds, dashboard, reports) builds outward from this.

This runs on the **Arc box** (Intel Arc Pro A60). It was scaffolded, not run in
the authoring environment, so first execution here is the shake-out.

## Step 0 — get a clip
Record ~2–3 min of people moving through the space at a **30–45° oblique angle**
(not top-down — keeps COCO-trained YOLO reliable). Save it as:

    ./media/clip.mp4

## Step 1 — export the model to OpenVINO (once)
On the Arc box:

    pip install ultralytics openvino
    yolo export model=yolov8n.pt format=openvino
    mkdir -p models && mv yolov8n_openvino_model models/

Sanity-check the GPU is visible to OpenVINO:

    python -c "import openvino; print(openvino.Core().available_devices)"
    # expect something including 'GPU' for the Arc

## Step 2 — bring it up

    docker compose up --build

- MediaMTX serves `rtsp://<host>:8554/cam1` (the looping clip)
- Inference service opens the stream, runs YOLO+ByteTrack, prints occupancy

## Step 3 — watch it
Open **http://<host>:8080** — annotated feed with boxes, track IDs, foot points,
zone polygons, and live occupancy/density labels. The console also prints:

    [14:32:10] tracked=4  Lobby=3  Restricted Bay=1

## Step 4 — set your real zones
On first run the service saves `./media/cam1_frame.jpg`. Open it, read off the
pixel coordinates of your actual zone corners, and update `config/cameras.yaml`
(`polygon` + measured `area_sqm`). Restart. Occupancy should now match a manual
head-count in each zone (that's acceptance test UC-A1).

## What "done" looks like for this slice
- People get stable IDs that survive brief occlusion (UC-V2)
- A person straddling a boundary doesn't flip zones each frame (UC-V3)
- Zone occupancy matches a manual count within ±1 (UC-A1)

Once that's solid, next is Redis + the FastAPI `/events/ingest` + the R-01..R-08
rule engine — the events already have everything they need from this spine.
