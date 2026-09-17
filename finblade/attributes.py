"""Appearance attributes: what a tracked person is wearing and carrying.

"The person in the blue shirt with a black cap" — a DESCRIPTION, never an
identity. Tags are drawn from a fixed, editable vocabulary of clothing
colours, headwear, mask, bag and outer garment. Nothing here infers gender,
age or ethnicity, and nothing here can be extended to do so without adding a
new attribute to the vocabulary on purpose, in config, where a reviewer sees
it (DECISIONS.md D-41).

HOW A TAG IS EARNED. A track is scored on a few well-spaced crops — not every
frame — after it has been stable for a moment, and the per-attribute answer
is the MAJORITY across those samples with the mean probability of the winner
as its confidence. Below `min_confidence` the attribute is "unknown". One bad
frame (a turn, an occlusion, a shadow) therefore never sticks, and the model
saying "sort of blue-ish" is reported as not knowing rather than as blue.

This module is pure: vocabulary, prompts, sampling policy, voting. The CLIP
scorer that turns a crop into probabilities lives in
services/inference/attr_client.py, behind a one-method interface, so this
file — and its tests — run without torch.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

BBox = Tuple[float, float, float, float]

# The default vocabulary. Every value is a label a search can ask for; the
# prompt is what CLIP is asked to compare the crop against. A label's prompt
# should name the visible thing, not a person category. "none" means the
# absence is itself a class the model chooses between, so a missing hat is a
# decision and not a gap.
DEFAULT_VOCABULARY: Dict[str, Dict[str, str]] = {
    "upper_colour": {c: f"a photo of a person wearing a {c} top" for c in
                     ("black", "white", "grey", "blue", "red", "green", "yellow",
                      "brown", "beige", "pink", "purple", "orange")},
    "lower_colour": {c: f"a photo of a person wearing {c} trousers or a {c} skirt" for c in
                     ("black", "white", "grey", "blue", "red", "green", "yellow",
                      "brown", "beige", "pink", "purple", "orange")},
    "headwear": {
        "none": "a photo of a person with nothing on their head",
        "cap": "a photo of a person wearing a baseball cap",
        "hat": "a photo of a person wearing a hat",
        "headscarf": "a photo of a person wearing a headscarf",
        "helmet": "a photo of a person wearing a hard hat or helmet",
    },
    # A label may carry SEVERAL prompts (an ensemble; their embeddings are
    # averaged). "no" needs it: with a single "mouth visible" prompt, a person
    # seen from behind had no mouth visible and "mask" won by default — every
    # back-of-head crop came back "mask: yes" on the evaluation sheet. A third
    # "face not visible" class was tried and swallowed frontal faces too at
    # CCTV resolution, so the absence is folded into "no" instead.
    "mask": {
        "no": ["a close-up photo of a person's face with the nose and mouth visible",
               "a photo of the back of a person's head, face turned away",
               "a photo of the side of a person's head"],
        "yes": ["a close-up photo of a person wearing a surgical face mask covering the nose and mouth",
                "a photo of a person wearing a white or blue medical face mask"],
    },
    "bag": {
        "none": ["a photo of a person carrying nothing, hands empty",
                 "a photo of a person standing or walking with no bag"],
        "backpack": "a photo of a person wearing a backpack on their back",
        "handbag": "a photo of a person carrying a handbag in their hand",
        "shoulder bag": "a photo of a person with a bag strap across the shoulder",
        "box or case": "a photo of a person carrying a box, a crate or a case with both hands",
    },
    "outerwear": {
        "none": "a photo of a person in ordinary clothes",
        "lab coat": "a photo of a person wearing a white lab coat",
        "gown": "a photo of a person wearing a blue or green medical gown or scrubs",
        "jacket": "a photo of a person wearing a jacket",
        "abaya": "a photo of a person wearing a long black abaya",
        "vest": "a photo of a person wearing a high-visibility vest",
    },
}

# WHICH PART OF THE PERSON EACH ATTRIBUTE IS JUDGED ON. Scoring everything on
# the whole crop let the dominant colour win every question: trousers came
# back the colour of the shirt, and a helmet was "seen" on a bare head
# because the shirt was hi-vis. Fractions of the box height from the top.
DEFAULT_REGIONS: Dict[str, Tuple[float, float]] = {
    "upper_colour": (0.12, 0.60),      # shoulders to waist, below the head
    "lower_colour": (0.50, 1.00),      # waist to feet
    "headwear":     (0.00, 0.28),
    "mask":         (0.00, 0.28),
    "bag":          (0.10, 0.90),
    "outerwear":    (0.10, 0.75),
}
FULL_REGION = (0.0, 1.0)

# Attributes that must never appear, whatever a config file says. The
# vocabulary is editable so a site can add "clipboard" or drop "abaya"; it is
# not editable into a profiling tool.
FORBIDDEN_ATTRIBUTES = ("gender", "sex", "age", "ethnicity", "race", "religion",
                        "skin", "nationality", "identity", "name")

# Colours CLIP confuses on a CCTV crop, so a search for one also returns the
# other, ranked behind the exact hits and marked "near". The first real
# deployment found it: a white shirt under indoor lighting was stored as
# "grey" at 0.55, and an exact search for "white" returned nothing. Pixels do
# not carry the colour a witness remembers; the search has to allow for it.
NEAR_COLOURS: Dict[str, Tuple[str, ...]] = {
    "white":  ("grey", "beige"),
    "grey":   ("white", "black"),
    "black":  ("grey", "blue"),        # navy reads as black and back
    "blue":   ("black", "purple"),
    "red":    ("pink", "orange"),
    "pink":   ("red", "purple"),
    "orange": ("red", "yellow", "brown"),
    "yellow": ("orange", "beige"),
    "green":  ("grey", "blue"),
    "brown":  ("beige", "black", "orange"),
    "beige":  ("white", "brown", "yellow"),
    "purple": ("blue", "pink"),
}
COLOUR_ATTRIBUTES = ("upper_colour", "lower_colour")


def near_labels(attr: str, label: str) -> Tuple[str, ...]:
    """The labels a search for `label` on `attr` should also accept, exact
    first. Non-colour attributes have no neighbours: a cap is not a helmet."""
    if attr in COLOUR_ATTRIBUTES:
        return (label,) + NEAR_COLOURS.get(label, ())
    return (label,)


DEFAULT_MIN_CONFIDENCE = 0.45
DEFAULT_SAMPLES = 3
DEFAULT_SAMPLE_INTERVAL_S = 3.0
DEFAULT_STABLE_AGE_S = 2.0
DEFAULT_MIN_CROP_HEIGHT = 96.0
DEFAULT_MIN_CROP_CONFIDENCE = 0.5


class Vocabulary:
    def __init__(self, spec: Optional[Dict[str, Dict[str, str]]] = None,
                 regions: Optional[Dict[str, Tuple[float, float]]] = None):
        spec = spec or DEFAULT_VOCABULARY
        bad = [a for a in spec if any(f in a.lower() for f in FORBIDDEN_ATTRIBUTES)]
        if bad:
            raise ValueError(f"attributes {bad} are not allowed: descriptions only, never who someone is")
        self.attrs: Dict[str, Dict[str, List[str]]] = {}
        for attr, labels in spec.items():
            if not isinstance(labels, dict) or len(labels) < 2:
                raise ValueError(f"attribute {attr!r} needs at least two labels with prompts")
            self.attrs[str(attr)] = {
                str(k): ([str(p) for p in v] if isinstance(v, (list, tuple)) else [str(v)])
                for k, v in labels.items()}
        self.regions: Dict[str, Tuple[float, float]] = {}
        for attr in self.attrs:
            r = (regions or {}).get(attr) or DEFAULT_REGIONS.get(attr) or FULL_REGION
            top, bottom = float(r[0]), float(r[1])
            if not (0.0 <= top < bottom <= 1.0):
                raise ValueError(f"region for {attr!r} must be 0 <= top < bottom <= 1")
            self.regions[attr] = (top, bottom)

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "Vocabulary":
        """`vocabulary:` in the camera config replaces the default wholesale;
        absent means default. A partial override would be a silent surprise.
        `regions:` may override the crop region per attribute. `disable:`
        lists attributes not to judge at all — the row keeps the column, as
        "unknown". For an attribute the model gets confidently wrong on a
        site's footage (mask on the factory clip: 36/36 false at >= 0.91),
        not tagging is better than tagging."""
        spec = None
        if cfg and isinstance(cfg.get("vocabulary"), dict) and cfg["vocabulary"]:
            spec = cfg["vocabulary"]
        regions = (cfg or {}).get("regions") if isinstance((cfg or {}).get("regions"), dict) else None
        disable = (cfg or {}).get("disable") or ()
        if isinstance(disable, str):
            disable = [disable]
        if disable:
            spec = {a: dict(v) for a, v in (spec or DEFAULT_VOCABULARY).items()
                    if a not in {str(d) for d in disable}}
            if not spec:
                raise ValueError("attributes.disable removed every attribute; set enabled: false instead")
        return cls(spec, regions)

    def region(self, attr: str) -> Tuple[float, float]:
        return self.regions.get(attr, FULL_REGION)

    def names(self) -> List[str]:
        return list(self.attrs)

    def labels(self, attr: str) -> List[str]:
        return list(self.attrs[attr])

    def prompts(self, attr: str) -> List[List[str]]:
        """One prompt LIST per label, in label order (an ensemble each)."""
        return list(self.attrs[attr].values())

    def valid(self, attr: str, label: str) -> bool:
        return attr in self.attrs and label in self.attrs[attr]


# ------------------------------------------------------------ per-track ----
@dataclass
class TrackSamples:
    first_seen: float
    samples: List[Dict[str, Dict[str, float]]] = field(default_factory=list)   # per sample: attr -> {label: prob}
    last_sample_at: float = -1e9
    best_crop_score: float = -1.0      # box height * confidence: which crop to keep
    best_crop_box: Optional[BBox] = None
    best_crop_at: float = 0.0
    best_crop: object = None           # whatever crop_fn returned (pixels), opaque here
    emitted: bool = False


@dataclass
class Tags:
    track_id: int
    ts: float
    attributes: Dict[str, str]          # attr -> label or "unknown"
    confidence: Dict[str, float]        # attr -> mean prob of the winner (0 for unknown)
    samples: int
    crop_box: Optional[BBox]            # the best crop's box
    crop_at: float
    crop: object = None                 # the best crop's pixels, if a crop_fn was given


def confidence_floors(min_confidence, attrs: Sequence[str]) -> Dict[str, float]:
    """Per-attribute floors from a config value that is either one number or
    a mapping {default: x, <attr>: y}.

    One floor cannot serve every attribute: 0.45 is a clear winner among 12
    colours (chance 0.08) but can never bite on a two-label attribute such as
    mask, whose winner is always >= 0.5 — so mask got a confident-looking
    answer every time and was wrong in both directions on real footage.
    A per-attribute floor (mask: 0.85) lets it say "unknown" instead."""
    if isinstance(min_confidence, dict):
        default = float(min_confidence.get("default", DEFAULT_MIN_CONFIDENCE))
        return {a: float(min_confidence.get(a, default)) for a in attrs}
    return {a: float(min_confidence) for a in attrs}


def vote(samples: Sequence[Dict[str, Dict[str, float]]], attrs: Sequence[str],
         min_confidence) -> Tuple[Dict[str, str], Dict[str, float]]:
    """Majority per attribute across samples; ties broken by summed
    probability; the winner's mean probability is its confidence, and a
    winner below that attribute's floor is reported as unknown.
    `min_confidence` is a float or a per-attribute mapping (see
    confidence_floors)."""
    floors = confidence_floors(min_confidence, attrs)
    labels: Dict[str, str] = {}
    conf: Dict[str, float] = {}
    for attr in attrs:
        votes: Dict[str, int] = {}
        prob_sum: Dict[str, float] = {}
        n = 0
        for s in samples:
            dist = s.get(attr)
            if not dist:
                continue
            n += 1
            top = max(dist, key=dist.get)
            votes[top] = votes.get(top, 0) + 1
            for lab, p in dist.items():
                prob_sum[lab] = prob_sum.get(lab, 0.0) + float(p)
        if not n:
            labels[attr], conf[attr] = "unknown", 0.0
            continue
        winner = max(votes, key=lambda lab: (votes[lab], prob_sum.get(lab, 0.0)))
        c = prob_sum.get(winner, 0.0) / n
        if c < floors[attr]:
            labels[attr], conf[attr] = "unknown", round(c, 3)
        else:
            labels[attr], conf[attr] = winner, round(c, 3)
    return labels, conf


class AttributeSampler:
    """Decides WHEN a track is scored and turns the scores into Tags.

    The scorer is injected: `score(frame, boxes) -> [attr -> {label: prob}]`,
    one dict per box. observe() returns the Tags of any track that has just
    completed its sample set; drop() returns Tags for a track that vanished
    with samples but had not completed (one good crop is still worth a row).
    """

    def __init__(self, vocab: Vocabulary, scorer, gate=None,
                 samples: int = DEFAULT_SAMPLES,
                 sample_interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
                 stable_age_s: float = DEFAULT_STABLE_AGE_S,
                 min_confidence=DEFAULT_MIN_CONFIDENCE,
                 budget_per_frame: int = 4, crop_fn=None):
        self.vocab = vocab
        self.scorer = scorer
        self.gate = gate                     # CropQualityGate-like: check(box, conf, w, h) -> (ok, reason)
        self.crop_fn = crop_fn               # (frame, box) -> pixels to keep for the best sample
        self.samples = max(1, int(samples))
        self.interval = float(sample_interval_s)
        self.stable_age = float(stable_age_s)
        # A float, or {default: x, <attr>: y} — resolved per attribute at vote time.
        self.min_confidence = (dict(min_confidence) if isinstance(min_confidence, dict)
                               else float(min_confidence))
        self.budget = max(1, int(budget_per_frame))
        self._tracks: Dict[int, TrackSamples] = {}
        self.stats = {"scored": 0, "emitted": 0, "gated_out": 0, "unknown_attrs": 0}

    def observe(self, frame, tracks: Sequence[Tuple[int, float, float, float, float]],
                confidences: Dict[int, float], now: float, frame_w: int, frame_h: int) -> List[Tags]:
        due: List[int] = []
        boxes: Dict[int, BBox] = {}
        for t in tracks:
            tid = int(t[0])
            box = (float(t[1]), float(t[2]), float(t[3]), float(t[4]))
            st = self._tracks.get(tid)
            if st is None:
                st = self._tracks[tid] = TrackSamples(first_seen=now)
            if st.emitted or len(st.samples) >= self.samples:
                continue
            if now - st.first_seen < self.stable_age:
                continue
            if now - st.last_sample_at < self.interval:
                continue
            if self.gate is not None:
                ok, _ = self.gate.check(box, confidences.get(tid, 1.0), frame_w, frame_h)
                if not ok:
                    self.stats["gated_out"] += 1
                    continue
            due.append(tid)
            boxes[tid] = box
            if len(due) >= self.budget:
                break
        out: List[Tags] = []
        if not due:
            return out
        dists = self.scorer(frame, [boxes[t] for t in due])
        for tid, dist in zip(due, dists):
            st = self._tracks[tid]
            st.samples.append(dist)
            st.last_sample_at = now
            self.stats["scored"] += 1
            box = boxes[tid]
            score = (box[3] - box[1]) * float(confidences.get(tid, 1.0))
            if score > st.best_crop_score:
                st.best_crop_score, st.best_crop_box, st.best_crop_at = score, box, now
                if self.crop_fn is not None:
                    try:
                        st.best_crop = self.crop_fn(frame, box)
                    except Exception:                       # noqa: BLE001
                        st.best_crop = None
            if len(st.samples) >= self.samples:
                out.append(self._finish(tid, st, now))
        return out

    def drop(self, tid: int, now: float) -> Optional[Tags]:
        st = self._tracks.pop(int(tid), None)
        if st is None or st.emitted or not st.samples:
            return None
        return self._finish(int(tid), st, now)

    def _finish(self, tid: int, st: TrackSamples, now: float) -> Tags:
        labels, conf = vote(st.samples, self.vocab.names(), self.min_confidence)
        st.emitted = True
        self.stats["emitted"] += 1
        self.stats["unknown_attrs"] += sum(1 for v in labels.values() if v == "unknown")
        tags = Tags(track_id=tid, ts=now, attributes=labels, confidence=conf,
                    samples=len(st.samples), crop_box=st.best_crop_box,
                    crop_at=st.best_crop_at, crop=st.best_crop)
        st.best_crop = None                  # release the pixels; the caller owns them now
        return tags

    def snapshot(self) -> dict:
        return dict(self.stats, tracked=len(self._tracks))


def describe(attributes: Dict[str, str]) -> str:
    """One line a human or a chatbot can read: 'blue top, black trousers,
    cap, mask, backpack'. Unknowns are omitted, 'none' is omitted."""
    parts = []
    order = ("outerwear", "upper_colour", "lower_colour", "headwear", "mask", "bag")
    words = {"upper_colour": "{} top", "lower_colour": "{} bottoms", "headwear": "{}",
             "mask": "face mask", "bag": "{}", "outerwear": "{}"}
    for attr in order + tuple(a for a in attributes if a not in order):
        v = attributes.get(attr)
        if not v or v in ("unknown", "none"):
            continue
        if attr == "mask":
            if v == "yes":
                parts.append("face mask")
            continue                   # "no" and "face not visible" say nothing
        parts.append(words.get(attr, "{}").format(v))
    return ", ".join(parts) if parts else "no attributes determined"
