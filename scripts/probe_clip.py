import sys, cv2
p = sys.argv[1] if len(sys.argv) > 1 else "media/scenario_demo.mp4"
cap = cv2.VideoCapture(p)
if not cap.isOpened():
    print("CANNOT_OPEN", p); sys.exit(1)
w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
fps = cap.get(cv2.CAP_PROP_FPS)
n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
print(f"WIDTH={w} HEIGHT={h} FPS={fps:.2f} FRAMES={n} DUR_S={n/fps if fps else 0:.1f}")
# also save the very first frame so we can measure zones against the real image
ok, frame = cap.read()
if ok:
    cv2.imwrite("evidence/scenario_first_frame.jpg", frame)
    print("saved evidence/scenario_first_frame.jpg")
cap.release()
