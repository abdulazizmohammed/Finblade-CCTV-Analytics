FROM python:3.11-slim

# ffmpeg for OpenCV RTSP decode; libGL for opencv; Intel GPU userspace comes via
# /dev/dri passthrough + the compute-runtime packages below (for Arc OpenVINO GPU).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 gnupg wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Intel compute runtime for Arc (Level Zero / OpenCL) so OpenVINO device=GPU works.
# If your Arc box already exposes the driver via /dev/dri and host libs, you can
# thin this out — verify with: python -c "import openvino; print(openvino.Core().available_devices)"
RUN wget -qO- https://repositories.intel.com/gpu/intel-graphics.key | gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu jammy unified" > /etc/apt/sources.list.d/intel-gpu.list \
    ; apt-get update && apt-get install -y --no-install-recommends \
        intel-opencl-icd intel-level-zero-gpu level-zero || true \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD ["python", "main.py"]
