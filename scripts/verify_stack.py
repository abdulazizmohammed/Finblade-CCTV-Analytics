import numpy, torch, torchvision, cv2, ultralytics
print("numpy", numpy.__version__)
print("torch", torch.__version__, "cuda?", torch.cuda.is_available())
print("torchvision", torchvision.__version__)
print("cv2", cv2.__version__)
print("ultralytics", ultralytics.__version__)
# Exercise the op that failed before (torchvision::nms).
boxes = torch.tensor([[0, 0, 10, 10], [1, 1, 11, 11]], dtype=torch.float32)
scores = torch.tensor([0.9, 0.8])
keep = torchvision.ops.nms(boxes, scores, 0.5)
print("nms ok, kept", keep.tolist())
