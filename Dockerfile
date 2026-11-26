FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    BLUR_ENV=/opt/blur_env

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    python3 \
    python3-pip \
    python3-dev \
    libgl1 \
    libegl1 \
    libvulkan1 \
    mesa-vulkan-drivers \
    libfftw3-double3 \
    libfftw3-single3 \
    libzimg2 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --break-system-packages --no-cache-dir \
    "vapoursynth==80" \
    numpy \
    fastapi \
    "uvicorn[standard]" \
    python-multipart

RUN VS_DIR=$(python3 -c "import vapoursynth as vs, pathlib; print(pathlib.Path(vs.__file__).parent)") && \
    if [ -x "$VS_DIR/vspipe" ]; then ln -sf "$VS_DIR/vspipe" /usr/local/bin/vspipe; fi && \
    python3 -c "import vapoursynth as vs; print('VapourSynth:', vs.__version__)" && \
    which vspipe

RUN mkdir -p /opt/blur_env/plugins /opt/blur_env/scripts /opt/blur_env/models && \
    curl -fsSL "https://github.com/f0e/blur/releases/download/v2.45/blur-Linux-Release-x64.tar.gz" -o /tmp/blur_rel.tar.gz && \
    mkdir -p /tmp/blur_extract && \
    tar -xzf /tmp/blur_rel.tar.gz -C /tmp/blur_extract && \
    cp -f /tmp/blur_extract/vapoursynth-plugins/*.so /opt/blur_env/plugins/ && \
    cp -rn /tmp/blur_extract/models/* /opt/blur_env/models/ || true && \
    cp -rf /tmp/blur_extract/lib/* /opt/blur_env/scripts/ && \
    rm -rf /tmp/blur_rel.tar.gz /tmp/blur_extract

WORKDIR /app
COPY . /app

EXPOSE 8080
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
