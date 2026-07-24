# evidence/ — what is and isn't here

## Present (SYNTHETIC — core replay, NOT detection)
- `events.jsonl` / `alerts.jsonl` — real schema, produced by the real
  rule engine over a scripted scenario. Proves amber->red->intrusion->
  offline fire correctly and that every event validates.
- `metrics.json` — scenario summary. `detection_ran: false`.

## MISSING (blocked on real detection — BLOCKERS.md B-1)
- `frames/frame_*.jpg` annotated frames
- `contact_sheet.jpg` (the single most useful human artifact)
- real avg detections/frame, FPS, track-ID stability

## To produce the real bundle (morning)
1. Place `models/yolov8n.pt`; install CPU stack (BLOCKERS.md B-1).
2. `python services/inference/run_cpu.py --config config/cameras.dev.yaml --seconds 60 --no-serve`
3. Open `evidence/contact_sheet.jpg` — check boxes are on people and
   zone polygons sit on the floor.
