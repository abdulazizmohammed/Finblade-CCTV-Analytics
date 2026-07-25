import os, sys
import numpy as np, cv2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services.inference.run_cpu import annotate, BGR_CRITICAL, BGR_WARNING, BGR_TRACK
from finblade.zones import Zone

zones = [
    Zone("ZONE-LOBBY", "Concourse", False, 20, 5.0, [[40, 40], [300, 40], [300, 400], [40, 400]]),
    Zone("ZONE-RESTRICTED", "Restricted", True, 2, 6.0, [[320, 40], [460, 40], [460, 400]]),
]
# A: intrusion (feet in restricted); B: loitering in concourse; C: normal in concourse
A = (1, 360, 120, 420, 300)   # foot (390,300) in restricted
B = (2, 90, 120, 150, 300)    # foot (120,300) in concourse -> loiter
C = (3, 180, 120, 240, 300)   # foot (210,300) in concourse -> normal
meta = {
    1: {"dwell": 5.0, "loiter": False},
    2: {"dwell": 42.0, "loiter": True},
    3: {"dwell": 3.0, "loiter": False},
}
frame = np.full((460, 500, 3), 28, np.uint8)
out = annotate(frame.copy(), zones, [A, B, C], {"ZONE-LOBBY": 2, "ZONE-RESTRICTED": 1}, meta)
cv2.imwrite("evidence/annotate_check.jpg", out)

def has(img, color, tol=35):
    b, g, r = color
    m = (abs(img[:, :, 0].astype(int) - b) < tol) & (abs(img[:, :, 1].astype(int) - g) < tol) & (abs(img[:, :, 2].astype(int) - r) < tol)
    return int(m.sum())

print("red (intrusion) px:", has(out, BGR_CRITICAL))
print("amber (loiter)  px:", has(out, BGR_WARNING))
print("teal (normal)   px:", has(out, BGR_TRACK))
print("saved evidence/annotate_check.jpg")
