import os, sys
import numpy as np, cv2
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services.inference.run_cpu import annotate, BGR_CRITICAL, BGR_TRACK
from finblade.zones import Zone
from finblade.geometry import foot_point
from finblade.zones import zone_of

zones = [
    Zone("ZONE-LOBBY","Concourse",False,20,5.0,[[40,240],[268,210],[285,398],[215,600],[40,560]]),
    Zone("ZONE-RESTRICTED","Restricted Area",True,2,6.0,[[270,408],[440,250],[464,262],[464,500]]),
]
# A: feet inside restricted; B: feet in concourse
A=(1,370,250,430,360); B=(2,90,300,150,410)
for t in (A,B):
    print("track",t[0],"foot",foot_point(*t[1:]),"->zone",zone_of(foot_point(*t[1:]),zones))

frame=np.full((688,464,3),28,np.uint8)
out=annotate(frame.copy(),zones,[A,B],{"ZONE-LOBBY":1,"ZONE-RESTRICTED":1})
cv2.imwrite("evidence/redbox_check.jpg",out)

# programmatic proof: does a red-ish pixel appear on A's box border, teal on B's?
def has_color(img,color,tol=40):
    b,g,r=color
    m=(abs(img[:,:,0].astype(int)-b)<tol)&(abs(img[:,:,1].astype(int)-g)<tol)&(abs(img[:,:,2].astype(int)-r)<tol)
    return int(m.sum())
print("red(critical) pixels:",has_color(out,BGR_CRITICAL))
print("teal(track) pixels:",has_color(out,BGR_TRACK))
print("saved evidence/redbox_check.jpg")
