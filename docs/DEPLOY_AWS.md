# Deploying to an AWS EC2 test instance

Step by step. The install sequence is the same one verified on Ubuntu 22.04; the
AWS-specific parts are the instance choice, the security group, reaching your
cameras, and surviving a reboot.

Budget 30–45 minutes, most of it waiting on the torch download.

---

## 0. Before you start — the thing people get wrong

**Your cameras are on a private LAN. The EC2 instance is not.** Nothing in AWS
can reach `192.168.200.x` by default, and no amount of security-group
configuration changes that — those are inbound rules, and this is an outbound
problem.

You need one of:

* **Tailscale on the instance** (§6) — same as the pod, simplest
* A site-to-site VPN or AWS Direct Connect to the camera network
* Cameras exposed on public IPs — **do not do this**; these are unauthenticated
  RTSP streams of people

Sort this out before installing, because it determines whether the deployment is
useful at all.

---

## 1. Choose the instance

Sizing comes from measurements, not guesses: VRAM is not the constraint,
**RAM is** (~2–3 GB per camera process), and video decode runs on the CPU.

| Cameras | Instance | vCPU / RAM | GPU | Notes |
|---|---|---|---|---|
| 1–2 | `g4dn.xlarge` | 4 / 16 GB | T4 16 GB | fine for a first test |
| 4–6 | `g4dn.2xlarge` | 8 / 32 GB | T4 16 GB | adequate |
| 6–10 | `g5.2xlarge` | 8 / 32 GB | A10G 24 GB | newer, faster |
| **6–20** | **`g6.4xlarge`** | **16 / 64 GB** | **L4 24 GB** | **Ada Lovelace; RAM allows ~20 cameras** |
| CPU only | `c5.2xlarge` | 8 / 16 GB | — | ~3–5 FPS per camera instead of ~24 |

The L4 is a **data-centre** GPU, so prefer the `-server` driver packages over the
desktop ones — see §3.

**AMI:** Ubuntu 22.04 LTS. It matches the environment everything was built and
tested on — same Python 3.10.12.

*Shortcut:* the **AWS Deep Learning AMI (Ubuntu 22.04)** ships with NVIDIA
drivers already installed, which lets you skip §3. It is a larger image but
saves the driver dance.

**Storage: 50 GB gp3 minimum.** The venv alone is 7.5 GB, and measured database
growth was ~380 MB/day with a handful of cameras. The default 8 GB root volume
will not fit the install.

---

## 2. Security group

Inbound rules:

| Port | Source | Why |
|---|---|---|
| 22 | **your IP only** | SSH |
| 8000 | **your IP, or the FinBlade egress IP** | dashboard + API |

**Do not open 8000 to `0.0.0.0/0`.** Even with the API key enabled, that exposes
live video of people to the internet and invites brute-forcing. If FinBlade needs
to reach it, allow their specific egress range and nothing else.

Outbound: leave the default (all allowed). Tailscale and the package installs
need it.

---

## 3. NVIDIA driver — GPU instances only

Skip entirely on a CPU instance or the Deep Learning AMI.

First check whether one is already loaded — `lspci` showing the card proves
nothing, only that the hardware is attached:

```bash
nvidia-smi          # works => skip this section entirely
```

If it is missing:

```bash
sudo apt update
sudo apt install -y linux-headers-$(uname -r) ubuntu-drivers-common
sudo ubuntu-drivers autoinstall
sudo reboot
```

On **data-centre GPUs (L4, A10G, T4)** prefer the `-server` driver, which is the
variant NVIDIA supports for these cards, if `autoinstall` picks a desktop one:

```bash
sudo apt install -y nvidia-driver-550-server
sudo reboot
```

Reconnect, then **verify before going further** — everything downstream depends
on it, and torch will silently fall back to CPU rather than erroring:

```bash
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
```

Driver must be **≥ 525**; ≥ 570 preferred.

You do **not** need the CUDA Toolkit. The torch wheels bundle their own CUDA
runtime as pip packages. Installing the toolkit wastes several GB and risks a
version conflict.

---

## 4. System packages

```bash
sudo apt update
sudo apt install -y python3.10-venv python3-pip libgl1 libglib2.0-0 git curl
```

`libgl1` and `libglib2.0-0` are not optional. `ultralytics` depends on the full
`opencv-python`, which links `libGL.so.1` — absent on a headless server. Without
them you get `ImportError: libGL.so.1` from a stack whose requirements say
"headless".

---

## 5. Install the application

```bash
git clone https://github.com/abdulazizmohammed/Finblade-CCTV-Analytics.git ~/finblade-cctv
cd ~/finblade-cctv

python3 -m venv .venv
.venv/bin/pip install --upgrade pip

# -c constraints.txt is MANDATORY. Without it pip resolves boxmot to a build
# needing numpy 2.x, which is an ABI break for torch/torchvision/ultralytics/
# opencv — and it fails at RUNTIME, not install time.
.venv/bin/pip install -r requirements.txt -c constraints.txt \
    --extra-index-url https://download.pytorch.org/whl/cu128

.venv/bin/python scripts/get_weights.py            # yolov8n + osnet, ~9 MB
```

Verify before continuing:

```bash
.venv/bin/python -c "
import numpy, torch
print('numpy', numpy.__version__, '| torch', torch.__version__,
      '| cuda', torch.cuda.is_available())"

.venv/bin/python -m unittest discover -s tests     # expect 398 OK
```

`numpy` must be **1.26.4**. If it is 2.x, the constraints file was not applied —
reinstall rather than proceeding.

On a **CPU-only** instance: drop the `+cu128` suffixes from `requirements.txt`,
omit `--extra-index-url`, and set `device: cpu` in `config/cameras.template.yaml`.

---

## 6. Reach the cameras

Tailscale is the least painful option and is what the previous environment used:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the printed URL to authorise. Then **prove the instance can reach a
camera before touching the application** — if this fails, no configuration in
the app will help:

```bash
tailscale status                                    # direct, or relayed?
nc -zv <camera-tailscale-ip> 554
```

If `tailscale status` says **relay** rather than direct, expect noticeably higher
latency; getting a direct connection matters more than any application tuning.

---

## 7. Run it

```bash
cd ~/finblade-cctv

# Generate a real key — do not invent one by hand.
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

FINBLADE_API_KEY='<the-generated-key>' bash scripts/start_stack.sh api
```

You should see `(API key auth enabled)` and no BLOCKER.

Verify from the instance:

```bash
KEY='<your-key>'
curl -s -o /dev/null -w 'with key:    %{http_code}\n' -H "Authorization: Bearer $KEY" localhost:8000/api/v1/cameras
curl -s -o /dev/null -w 'without key: %{http_code}\n' localhost:8000/api/v1/cameras
```

**200 then 401.** If the second returns 200, the key never reached the process.

Then open `http://<ec2-public-ip>:8000/web/cameras.html` and add cameras with
their RTSP URLs. The browser prompts for the key once and remembers it.

---

## 8. Survive a reboot — systemd

Unlike the container, an EC2 instance is long-lived and worth setting up
properly. `start_stack.sh` detaches processes but does not restart them if they
die or the instance reboots.

```bash
sudo tee /etc/systemd/system/finblade.service > /dev/null <<'EOF'
[Unit]
Description=FinBlade CCTV API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/finblade-cctv
EnvironmentFile=/home/ubuntu/finblade-cctv/.env
ExecStart=/home/ubuntu/finblade-cctv/.venv/bin/python -m uvicorn \
          services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Secrets in a root-only file, NOT in the unit — unit files are world-readable.
cat > ~/finblade-cctv/.env <<EOF
FINBLADE_API_KEY=<your-key>
FINBLADE_TOPOLOGY=config/topology.yaml
EOF
chmod 600 ~/finblade-cctv/.env

sudo systemctl daemon-reload
sudo systemctl enable --now finblade
systemctl status finblade --no-pager
```

Note this supervises the **API** only. Camera pipelines are launched by the API
when you add a camera, and it restarts them on its own. After a reboot the
cameras come back because their rows persist in `data/finblade.db`.

Logs: `journalctl -u finblade -f`

---

## 9. Once it is up

Two things decide whether the numbers are meaningful, and neither is an AWS
concern:

**Draw zones with the bottom edge at the very bottom of the frame.** Occupancy
counts foot points, and a person whose box is clipped by the frame edge has
their foot point ON that edge. A zone inset even slightly reports 0 occupancy
while boxes are plainly on people. The runner warns in its log when this happens
— `grep "in NO zone" scripts/cam_*.log`.

**Put your real camera IDs in `config/topology.yaml`** with measured walk times
between them. Watch `unknown_pair` in `/api/v1/identity/stats`; above 0 means
the file does not cover the cameras actually running, and cross-camera matching
is degraded.

---

## 10. AWS-specific things worth knowing

**Cost.** A `g4dn.2xlarge` is roughly $0.75/hour on-demand — about $540/month if
left running. Stop the instance when not testing; an EBS volume costs a few
dollars a month on its own.

**The public IP changes on stop/start** unless you attach an Elastic IP. If
FinBlade is configured to reach a specific address, allocate one.

**Database growth.** ~380 MB/day was measured with a handful of cameras.
Watch `df -h` on a long soak, and plan a retention job before leaving it running
for weeks.

**Snapshots.** `aws ec2 create-snapshot` on the EBS volume is the simplest
backup for `data/finblade.db` and any zones you have drawn.

**Bandwidth.** RTSP ingest is ~2–4 Mbit/s per camera, continuously, and AWS
charges for egress rather than ingress — so inbound video is free, but anything
you stream OUT (the MJPEG feed to a remote dashboard, at 20–40 Mbit/s per
viewer) is not. Prefer the pushed snapshots for remote viewing.
