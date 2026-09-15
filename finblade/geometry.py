"""Geometry primitives: foot point, point-in-polygon, and item->person association.

Pure Python (no cv2, no numpy) so it is testable headless. Semantics match
OpenCV's ``pointPolygonTest(..., measureDist=False) >= 0``: a point exactly on
an edge or vertex counts as INSIDE. That matters for zone assignment at
boundaries.
"""

from typing import Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Polygon = Sequence[Point]
BBox = Tuple[float, float, float, float]      # x1, y1, x2, y2

_EPS = 1e-9


def foot_point(x1: float, y1: float, x2: float, y2: float) -> Point:
    """Ground-contact point = bottom-centre of the bbox.

    Zones are assigned on the feet, not the centroid, so a tall person leaning
    over a boundary line is placed by where they actually stand.
    """
    return ((x1 + x2) / 2.0, float(max(y1, y2)))


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    """True if point p lies on the closed segment a-b (collinear + in bounds)."""
    px, py = p
    ax, ay = a
    bx, by = b
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > _EPS:
        return False
    if px < min(ax, bx) - _EPS or px > max(ax, bx) + _EPS:
        return False
    if py < min(ay, by) - _EPS or py > max(ay, by) + _EPS:
        return False
    return True


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Ray-casting point-in-polygon with on-edge treated as inside.

    Works for any simple polygon (convex or concave), vertices in either winding.
    """
    n = len(polygon)
    if n < 3:
        return False

    # On-edge / on-vertex counts as inside (matches cv2 >= 0 semantics).
    for i in range(n):
        if _on_segment(point, polygon[i], polygon[(i + 1) % n]):
            return True

    x, y = point
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Does the horizontal ray at y cross edge (i, j)?
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


# --------------------------------------------------------------------------
# Item -> person association
# --------------------------------------------------------------------------
#
# WHY NOT PLAIN IoU AGAINST THE WHOLE PERSON BOX. A hardhat is a few percent of
# a person's area at the very top; IoU between them is near zero however
# perfectly it sits on their head, so IoU would reject every correct pairing.
# Worse, IoU is symmetric and blind to WHERE the item sits: a hardhat lying on
# a bench overlapping someone's shins would score the same as one on their
# head. So the test is instead "does this item sit where this item BELONGS on a
# body", which is what makes wrong-body and wrong-place associations fail.
#
# The regions are fractions of the person box's height, measured from its top.
# They are generous rather than tight: a person bbox is produced by a detector
# and wobbles, people bend and lean, and cameras look down from a height, all of
# which move a hat away from the exact top of the box. Being too tight fails
# silently — the item is dropped and the person looks like they are not wearing
# it, which for PPE is the dangerous direction.
#
# GUESSES, and labelled as such: these bands are reasoned from anatomy, not
# measured on this site's footage. They should be checked against real CCTV
# before anyone treats an R-11 alert as calibrated.
ANATOMY = {
    # Head and a little shoulder. A hardhat can sit above the detector's box
    # when someone tilts their head back, hence the small negative top.
    "hardhat":     (-0.08, 0.40),
    # Biased higher than the hat band: a mask is on the face, and allowing it
    # down to the chest invites a chest logo or a hi-vis collar to match.
    "mask":        (-0.05, 0.32),
    # Torso. Starts below the head so a white hardhat cannot satisfy a vest
    # requirement, ends above the knees.
    "safety_vest": (0.15, 0.70),

    # --- medical / laboratory profile --------------------------------------
    # REASONED / NOT YET SITE VALIDATED. Every band below is derived from where
    # the garment sits on a body, not from measurement on lab footage. None has
    # been checked against a real camera angle, and a ceiling camera looking
    # steeply down compresses all of them.
    #
    # Tighter than the hardhat band: a surgical cap is fabric ON the skull
    # rather than a shell sitting above it, so it does not overshoot the box
    # the way a hard hat does when someone looks up.
    "surgical_cap":   (-0.05, 0.30),
    # Same reasoning as the industrial mask, and the same band: it is the same
    # part of the same face. Kept as a separate entry rather than aliased so
    # the two can diverge when one of them is measured.
    "surgical_mask":  (-0.05, 0.32),
    # Above the mask band. Goggles are on the eyes, and letting this reach the
    # chin would let a mask satisfy a goggles requirement.
    "goggles":        (-0.05, 0.25),
    # Head plus upper chest: a face shield hangs from a headband and its lower
    # edge reaches the sternum, so it is genuinely taller than mask or goggles.
    "face_shield":    (-0.08, 0.45),
    # A surgical gown is LONG — mid-calf on many people — so this runs further
    # down the body than a hi-vis vest. Starting at 0.10 keeps a cap or shield
    # from satisfying it.
    "surgical_gown":  (0.10, 0.85),
    # A lab coat is knee-length and open at the front — the same silhouette as
    # a gown from a ceiling camera, so it takes the gown's band. A separate
    # entry rather than an alias for the same reason mask/surgical_mask are:
    # either can be measured and moved without dragging the other.
    "lab_coat":       (0.10, 0.85),
    # Scrubs are a torso garment like a vest, and share its band for the same
    # reason: below the head, above the knees.
    "surgical_scrubs": (0.15, 0.70),
    # A coverall is nearly the whole person. Deliberately NOT the full body —
    # leaving a margin at each end means a detection covering the entire box
    # (which is what a mis-fired whole-person detection looks like) does not
    # automatically satisfy it.
    "coverall":       (0.05, 0.95),
    # Feet. Overshoots the bottom because a person box is routinely clipped at
    # the ankle by the frame edge or by the detector, and shoe covers are then
    # partly outside it.
    "shoe_covers":    (0.85, 1.05),

    # NOTE: "surgical_gloves" is deliberately ABSENT from this table. Hands
    # have no fixed vertical position — waist when idle, chest when pipetting,
    # above the head when reaching — so any band would be wrong most of the
    # time. Gloves therefore fall to _DEFAULT_BAND below, which is the honest
    # behaviour, and finblade.ppe.PPE_STATUS marks them EXPERIMENTAL because of
    # it. Adding a band here to make gloves "work" would make them worse: a
    # wrong band silently drops correct detections.
}
# Applied when the item type is not in ANATOMY: the whole body, which reduces
# the check to "inside this person" and is the honest fallback for an item
# whose anatomy nobody has declared.
_DEFAULT_BAND = (0.0, 1.0)


def _area(b: BBox) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _intersection(a: BBox, b: BBox) -> float:
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    return iw * ih if iw > 0 and ih > 0 else 0.0


def containment(item: BBox, region: BBox) -> float:
    """Fraction of ``item`` that lies inside ``region``.

    Containment, not IoU, and the asymmetry is the point: a small item can be
    100% inside a large region, which is exactly the relationship "this hat is
    on that head" has. IoU would score the same pair near zero purely because
    the boxes differ in size.
    """
    a = _area(item)
    return (_intersection(item, region) / a) if a > 0 else 0.0


def anatomical_region(person: BBox, item_type: str) -> BBox:
    """The slice of a person box where ``item_type`` should appear."""
    x1, y1, x2, y2 = person
    h = y2 - y1
    top_f, bot_f = ANATOMY.get(item_type, _DEFAULT_BAND)
    return (x1, y1 + top_f * h, x2, y1 + bot_f * h)


def associate_item(item: BBox, item_type: str, people: Dict[object, BBox],
                   min_containment: float = 0.5,
                   min_margin: float = 0.15) -> Tuple[Optional[object], float]:
    """Which person is wearing/carrying this item? Returns (key, score).

    ``people`` maps any hashable key (a track id) to that person's bbox.
    Returns ``(None, score)`` when no association is confident enough.

    TWO REFUSALS, both deliberate:

      min_containment  the item must actually sit in the right region. Below
                       this it is not associated at all - a hat on a shelf is
                       nobody's hat, and inventing an owner for it would put a
                       compliance verdict on a person who was never involved.

      min_margin       the best candidate must beat the runner-up by this much.
                       Two workers standing shoulder to shoulder produce
                       overlapping head regions, and one hat cannot be
                       arbitrated between them by geometry alone. Refusing is
                       correct: the alternative is a coin toss that accuses
                       whichever person sorted first.

    This mirrors the threshold-plus-margin shape the identity matcher already
    uses, and for the same reason - "I don't know" beats a confident guess.
    """
    scored: List[Tuple[float, object]] = []
    for key, pbox in (people or {}).items():
        score = containment(item, anatomical_region(pbox, item_type))
        if score > 0:
            scored.append((score, key))
    if not scored:
        return None, 0.0
    # Sort by score, then by key for determinism: an arbitrary tie-break that
    # varies between runs would make the tests flaky and the behaviour
    # unreproducible on the same footage.
    scored.sort(key=lambda sk: (-sk[0], str(sk[1])))
    best_score, best_key = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < min_containment:
        return None, best_score
    if (best_score - runner_up) < min_margin:
        return None, best_score
    return best_key, best_score


def associate_items(items: Sequence[Tuple[BBox, str]], people: Dict[object, BBox],
                    min_containment: float = 0.5,
                    min_margin: float = 0.15) -> List[Tuple[int, Optional[object], float]]:
    """Associate several items at once.

    Returns one (item_index, person_key_or_None, score) per input item. Items
    are associated INDEPENDENTLY: two people can each be wearing a hardhat, and
    one person can own a hat and a vest. What is prevented is the opposite
    error - one item being credited to several people - which cannot happen
    because each item resolves to at most one key.
    """
    return [(i, *associate_item(box, kind, people, min_containment, min_margin))
            for i, (box, kind) in enumerate(items or ())]
