import torch
print("torch", torch.__version__)
print("is_available", torch.cuda.is_available())
try:
    print("device_count", torch.cuda.device_count())
except Exception as e:
    print("device_count ERR", repr(e))
# force init + real op
try:
    torch.cuda.init()
    print("after init device_count", torch.cuda.device_count())
except Exception as e:
    print("init ERR", repr(e))
try:
    x = torch.rand(1000, 1000, device="cuda")
    y = (x @ x).sum().item()
    print("cuda matmul OK, sum=", round(y, 1))
    print("device name", torch.cuda.get_device_name(0))
    cap = torch.cuda.get_device_capability(0)
    print("compute capability sm_", cap)
except Exception as e:
    print("cuda op ERR", repr(e))
print("arch list:", torch.cuda.get_arch_list())
