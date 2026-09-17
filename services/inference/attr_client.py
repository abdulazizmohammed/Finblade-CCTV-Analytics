"""Worker-side appearance scorer: CLIP zero-shot over a fixed vocabulary.

Same dependency-isolation contract as ppe_client.py: missing weights, no
open_clip, or a corrupt checkpoint switch tagging OFF loudly and the pipeline
runs on. Nothing torch or open_clip escapes this file — `score()` returns
plain dicts of probabilities, which is what lets finblade/attributes.py be
tested exhaustively with no model at all.

WHY CLIP ZERO-SHOT. The vocabulary is a list of prompts, so a site can add
"clipboard" or remove "abaya" in config without training anything — and the
project forbids training. The price is that CLIP was not built for CCTV
crops: a 100-px person under fluorescent light is a hard image, and "grey"
versus "blue" on a shadowed shirt is a coin toss. That is why the sampler
votes across several crops and reports "unknown" below a confidence floor,
and why every tag ships with a crop for a human to check.

THE CHECKPOINT: models/clip_vit_b32_laion2b.safetensors — open_clip ViT-B/32
trained on LAION-2B (laion2b_s34b_b79k), MIT licence, safetensors so it loads
under torch's weights-only default. See models/MANIFEST.yaml.

Text prompts are encoded ONCE at load; each frame only encodes the crops, so
scoring costs ~10 ms per crop on the GPU, and the sampler asks for a handful
of crops per track in its whole life.
"""

import logging
import os
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("finblade.attributes")

BBox = Tuple[float, float, float, float]
DEFAULT_WEIGHTS = "models/clip_vit_b32_laion2b.safetensors"
MODEL_NAME = "ViT-B-32"


class ClipAttributeScorer:
    def __init__(self, vocab, weights: Optional[str] = None, device: str = "0",
                 enabled: bool = True, half: bool = True):
        self.vocab = vocab
        self.weights = weights or DEFAULT_WEIGHTS
        self.device_req = str(device)
        self.enabled = bool(enabled)
        self.half = bool(half)
        self._model = None
        self._preprocess = None
        self._text: Dict[str, object] = {}     # attr -> normalised text features (tensor)
        self._torch = None
        self.device = "cpu"
        self.status = "disabled" if not self.enabled else "not loaded"

    # ---- lifecycle ---------------------------------------------------------
    def load(self) -> bool:
        if not self.enabled:
            return False
        if not os.path.exists(self.weights):
            self.status = f"weights missing: {self.weights}"
            log.warning("attribute tagging OFF — %s", self.status)
            self.enabled = False
            return False
        try:
            import torch
            import open_clip
        except Exception as e:                              # noqa: BLE001
            self.status = f"open_clip/torch unavailable: {e}"
            log.warning("attribute tagging OFF — %s", self.status)
            self.enabled = False
            return False
        try:
            want_cuda = self.device_req not in ("cpu", "-1") and torch.cuda.is_available()
            self.device = "cuda" if want_cuda else "cpu"
            model, _, preprocess = open_clip.create_model_and_transforms(
                MODEL_NAME, pretrained=self.weights, device=self.device)
            model.eval()
            if self.half and self.device == "cuda":
                model = model.half()
            tok = open_clip.get_tokenizer(MODEL_NAME)
            with torch.no_grad():
                for attr in self.vocab.names():
                    # One row per LABEL: the mean of its prompt ensemble,
                    # renormalised. Averaging several phrasings is the standard
                    # way to make a zero-shot class robust to how it is worded.
                    rows = []
                    for prompts in self.vocab.prompts(attr):
                        t = tok(prompts).to(self.device)
                        f = model.encode_text(t)
                        f = f / f.norm(dim=-1, keepdim=True)
                        m = f.mean(dim=0)
                        rows.append(m / m.norm())
                    self._text[attr] = torch.stack(rows)
            self._model, self._preprocess, self._torch = model, preprocess, torch
            self.status = f"loaded ({self.device}{', fp16' if self.half and self.device == 'cuda' else ''})"
            log.info("attribute tagging: %s from %s, %d attributes", self.status, self.weights,
                     len(self._text))
            return True
        except Exception as e:                              # noqa: BLE001
            self.status = f"load failed: {e}"
            log.exception("attribute tagging OFF — %s", self.status)
            self.enabled = False
            return False

    @property
    def ready(self) -> bool:
        return self._model is not None

    # ---- scoring ------------------------------------------------------------
    def score(self, frame, boxes: Sequence[BBox]) -> List[Dict[str, Dict[str, float]]]:
        """One {attr: {label: prob}} per box. frame is BGR (OpenCV).

        Each attribute is judged on ITS region of the person (head band for
        headwear and mask, torso for the top, legs for the bottoms — see
        attributes.DEFAULT_REGIONS). Scoring everything on the whole crop
        let the dominant colour answer every question. Distinct regions are
        encoded once and shared, so six attributes cost about four image
        encodes per box.
        """
        if not self.ready or not boxes:
            return [{} for _ in boxes]
        from PIL import Image
        torch = self._torch
        h, w = frame.shape[:2]
        regions = sorted({self.vocab.region(a) for a in self._text})
        tensors, index = [], {}          # (box i, region) -> row in the batch
        for i, (x1, y1, x2, y2) in enumerate(boxes):
            bw, bh = (x2 - x1), (y2 - y1)
            for (top, bottom) in regions:
                # A little side context helps bags and sleeves; the sampler
                # already rejected truncated boxes.
                ry1, ry2 = y1 + bh * top, y1 + bh * bottom
                pw = bw * 0.10
                cx1, cy1 = max(0, int(x1 - pw)), max(0, int(ry1))
                cx2, cy2 = min(w, int(x2 + pw)), min(h, int(ry2))
                crop = frame[cy1:cy2, cx1:cx2]
                if crop.size == 0 or cy2 - cy1 < 4 or cx2 - cx1 < 4:
                    crop = frame[max(0, int(y1)):max(2, int(y2)), max(0, int(x1)):max(2, int(x2))]
                index[(i, (top, bottom))] = len(tensors)
                tensors.append(self._preprocess(Image.fromarray(crop[:, :, ::-1])))
        x = torch.stack(tensors).to(self.device)
        if self.half and self.device == "cuda":
            x = x.half()
        out: List[Dict[str, Dict[str, float]]] = [{} for _ in boxes]
        with torch.no_grad():
            f = self._model.encode_image(x)
            f = f / f.norm(dim=-1, keepdim=True)
            for attr, tf in self._text.items():
                reg = self.vocab.region(attr)
                rows = torch.stack([f[index[(i, reg)]] for i in range(len(boxes))])
                probs = (100.0 * rows @ tf.T).softmax(dim=-1).float().cpu().tolist()
                labels = self.vocab.labels(attr)
                for i, row in enumerate(probs):
                    out[i][attr] = {lab: round(p, 4) for lab, p in zip(labels, row)}
        return out

    def snapshot(self) -> dict:
        return {"enabled": self.enabled, "status": self.status, "device": self.device,
                "attributes": list(self._text)}
