# Video-generation prompts for cross-camera validation footage

Purpose: produce the two-camera footage that B-4 blocks on. Four clips, 15s each.

## The one thing that decides whether this works

Text-to-video models do **not** maintain character consistency across separate
generations. Two clips generated from the same description will produce two
people who merely look similar-ish — and cross-camera ReID is precisely the thing
that measures "is this the same person". If the model invents a different face,
build or shade of jacket per clip, the footage tests nothing and a low match rate
would be the generator's fault, not the matcher's.

Mitigations, best first:
1. **Generate both cameras of a pair in ONE clip if the tool allows it** — e.g. a
   split-screen or a single wide shot you crop into two views in post. Guarantees
   identity. `scripts/eval_cross_camera.py` already does the cropping trick.
2. **Use a tool with character reference / consistent-character support**
   (image-to-video seeded from the same character stills, or Runway/Kling
   character reference). Generate five character stills FIRST, then use those
   stills as the reference for every clip.
3. **Extend a single clip** rather than generating fresh ones, where supported.
4. If none of the above: accept it, and label the ground truth by what you can
   actually see rather than by what you asked for.

If the people come out inconsistent, tell me — a match rate from inconsistent
footage is worse than no measurement, because it looks like data.

## Constraints that come from the code, not from taste

These map to real gates in `finblade/appearance.py`, so ignoring them will make
crops get discarded and the run look emptier than it should:

- **Full body, never clipped by the frame edge.** `CropQualityGate` rejects any
  box within 8px of the border as truncated.
- **Each person at least a third of the frame height.** Boxes under 96px tall are
  rejected as too small to embed.
- **People mostly separated, not overlapping.** Crops where another box covers
  >25% get dropped as occluded.
- **Locked-off camera.** The whole pipeline assumes a fixed view; any pan/zoom
  breaks zone polygons and the frozen-frame detector.
- **No burned-in timestamp.** Generators render text as garbage, and garbled text
  in-frame invites false detections. Log times in the sheet below instead.

---

## Block 1 — paste IDENTICALLY into all four prompts

```
STYLE: Fixed CCTV security camera footage. Camera mounted high on a wall at 3.5
metres, angled about 30 degrees downward. Completely static locked-off camera —
no camera movement, no pan, no tilt, no zoom, no cuts, one continuous take.
Wide-angle security lens with slight barrel distortion. Slightly soft focus,
mild sensor noise, flat low-contrast desaturated colour typical of surveillance
video. No text, no timestamp, no watermark, no on-screen graphics, no subtitles.

PEOPLE (exactly these five, no one else in shot):
P1 — man, 40s, navy blue business suit, no tie, short dark hair, carrying a brown
     leather briefcase in his right hand.
P2 — man, 30s, dark charcoal business suit, no tie, short dark hair, empty hands.
P3 — woman, 30s, bright red knee-length coat, shoulder-length blonde hair, black
     handbag on left shoulder.
P4 — man, 20s, yellow hi-vis safety vest over a grey hoodie, blue jeans, black
     baseball cap.
P5 — woman, 50s, beige trench coat, dark hair in a bun, pulling a small black
     wheeled suitcase.

FRAMING RULES: the camera is far enough away that each person stands only about
one third of the frame height — roughly 30-40%, never more than half. There is
always clear empty space above every person's head and below their feet; nobody
ever touches or is cut off by the top, bottom or side edge of the frame. The
whole room is visible, not a close-up. People walk at a normal unhurried pace
through the middle of the frame, well separated from each other, never
overlapping or blocking one another.
```

> **Measured on the first attempt** (`media/cam-vid-1.mp4`, 752x416): the
> generator produced people with a **median box height of 370px in a 416px
> frame — 89% of frame height**. They filled the frame, so their boxes touched
> the top and bottom edges and `CropQualityGate` discarded **79% of detections**
> as `truncated_at_edge`, leaving only 54 usable crops out of 336.
>
> "At least one third of the frame height" was read as a floor and massively
> overshot. The wording above gives a *range* with an explicit ceiling and
> demands headroom, which is what real CCTV at 3.5m actually looks like. If a
> regenerated clip still fills the frame, add: *"the people are small in frame,
> shot from far across a large room, the ceiling and the far wall are both
> visible."*

**P1 and P2 are the hard case on purpose** — two men in near-identical dark suits.
That pair is what proves the runner-up margin rule earns its place. If your
footage only has visually distinct people, the test is too easy and will flatter
the system exactly like the synthetic evaluation did.

---

## Pair A — OVERLAPPING cameras (same room, two corners)

Both cameras see the same floor at the same moment. This validates the
double-counting fix: five people must resolve to five identities, not ten.

### Clip A1 — `CAM-1`
```
[paste Block 1]

SCENE: A large modern office building lobby with a polished grey floor, a
reception desk against the far wall on the right, and glass entrance doors on the
left. Camera is high in the NORTH-EAST corner, looking down and across the lobby
toward the entrance doors. Bright even indoor lighting, cool white.

ACTION over 15 seconds: P3 in the red coat walks from the entrance doors on the
left across the open floor toward the reception desk and stops there. P1 in the
navy suit with the briefcase walks from the bottom of frame diagonally toward the
upper left. P2 in the charcoal suit follows a few seconds later on a similar path
but stays further right. P4 in the hi-vis vest crosses the background from right
to left. P5 with the wheeled suitcase enters from the left and walks slowly
toward the reception desk.
```

### Clip A2 — `CAM-2`
```
[paste Block 1]

SCENE: The SAME large modern office building lobby — same polished grey floor,
same reception desk, same glass entrance doors. Camera is high in the opposite
SOUTH-WEST corner, looking back across the lobby from the other side, so the
reception desk is now on the left and the entrance doors are on the right.
Slightly warmer indoor lighting and a dimmer exposure than the other camera.

ACTION over 15 seconds: exactly the same five people performing exactly the same
movements at the same times, seen from the opposite corner — so everyone's
direction of travel appears mirrored. P3 in the red coat crosses toward the
reception desk on the left. P1 with the briefcase moves diagonally away from
camera. P2 in the charcoal suit follows on a similar path. P4 in the hi-vis vest
crosses the background left to right. P5 with the wheeled suitcase walks toward
the desk.
```

> The deliberate lighting difference between A1 and A2 matters — identical
> lighting makes the match trivially easy and the measurement worthless. Real
> cameras never match exposure.

---

## Pair B — NON-OVERLAPPING cameras (a walk between them)

Separate spaces with a corridor between. This validates the transit gate: a
person must be absent for a plausible walk time before reappearing.

### Clip B1 — `CAM-3`
```
[paste Block 1]

SCENE: The exit end of an office lobby, with a wide doorway leading into a
corridor at the top of frame. Polished grey floor, plain walls. Camera is high on
the wall opposite the doorway, looking down at the doorway. Bright cool white
lighting.

ACTION over 15 seconds: P1 in the navy suit with the briefcase walks from the
bottom of frame up to the doorway and exits through it at about 4 seconds. P3 in
the red coat follows and exits through the same doorway at about 8 seconds. P4 in
the hi-vis vest walks up to the doorway and exits at about 12 seconds. P2 in the
charcoal suit stays in the lobby, walking slowly left to right, and never exits.
```

### Clip B2 — `CAM-4`
```
[paste Block 1]

SCENE: A long narrow office corridor with a beige carpet, plain walls and evenly
spaced ceiling lights, and a doorway at the far end of the corridor. Camera is
high on the wall at the far end, looking back down the length of the corridor
toward that doorway. Dimmer, warmer, more yellow lighting than the lobby.

ACTION over 15 seconds: the corridor is empty for the first 6 seconds. Then P1 in
the navy suit with the briefcase enters through the doorway at the far end and
walks down the corridor toward camera. P3 in the red coat enters the same way at
about 10 seconds and walks toward camera. P4 in the hi-vis vest enters at about
14 seconds. Each person is fully visible head to feet and walks down the middle
of the corridor.
```

**The point of B2:** P2 in the charcoal suit never appears here. He looks almost
identical to P1 and stayed behind in the lobby. If the matcher links corridor-P1
to lobby-P2, that is a false merge and exactly the failure worth catching.

---

## Ground truth I need back

Generate the clips, watch them once, and fill this in from what you actually see
— not from what the prompt asked for. Save as `media/ground_truth.md`.

```
CAM-1 (clip A1, overlapping with CAM-2)
  P1 navy suit      visible 0:00-0:11
  P2 charcoal suit  visible 0:03-0:15
  P3 red coat       visible 0:00-0:15
  P4 hi-vis         visible 0:02-0:07
  P5 trench coat    visible 0:05-0:15

CAM-2 (clip A2, overlapping with CAM-1)
  P1 ...

CAM-3 (clip B1, non-overlapping with CAM-4)
  P1 navy suit      visible 0:00-0:04, EXITS to corridor
  ...

CAM-4 (clip B2)
  P1 navy suit      ENTERS 0:06 from lobby, visible 0:06-0:15
  ...

TRANSIT: measured walk time CAM-3 doorway -> CAM-4 doorway = ___ seconds
NOTES: any person the generator rendered inconsistently between clips
```

That last line is the important one. Also record the real transit seconds — that
number goes straight into `config/topology.yaml` and closes B-5.

## Then

```
# save clips as media/CAM-1_xcam.mp4 ... media/CAM-4_xcam.mp4
```

Tell me they are in place and I will wire the four-camera configs, set the
topology from your measured transit time, and give you a real precision/recall
figure to replace the synthetic proxy in
`evidence/cross_camera_eval_dense.json`.
