FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    BLUR_ENV=/opt/blur_env \
    PYTHONPATH=/usr/local/lib/python3.12/site-packages:/usr/local/lib/python3.12/dist-packages \
    LD_LIBRARY_PATH=/usr/local/lib:/opt/blur_env/plugins

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    python3 \
    python3-pip \
    python3-dev \
    cython3 \
    git \
    build-essential \
    autoconf \
    automake \
    libtool \
    pkg-config \
    libzimg-dev \
    libzimg2 \
    libgl1 \
    libegl1 \
    libvulkan1 \
    mesa-vulkan-drivers \
    libfftw3-double3 \
    libfftw3-single3 \
    libavcodec-dev \
    libavformat-dev \
    libavutil-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --branch R70 --depth 1 https://github.com/vapoursynth/vapoursynth.git /tmp/vs && \
    cd /tmp/vs && \
    ./autogen.sh && \
    ./configure --prefix=/usr/local --enable-python-module && \
    make -j"$(nproc)" && \
    make install && \
    ldconfig && \
    rm -rf /tmp/vs

RUN python3 -m pip install --break-system-packages --no-cache-dir \
    numpy \
    fastapi \
    "uvicorn[standard]" \
    python-multipart

RUN mkdir -p /opt/blur_env/plugins /opt/blur_env/scripts /opt/blur_env/models && \
    curl -fsSL "https://github.com/f0e/blur/releases/download/v2.45/blur-Linux-Release-x64.tar.gz" -o /tmp/blur_rel.tar.gz && \
    mkdir -p /tmp/blur_extract && \
    tar -xzf /tmp/blur_rel.tar.gz -C /tmp/blur_extract && \
    cp -f /tmp/blur_extract/vapoursynth-plugins/*.so /opt/blur_env/plugins/ && \
    cp -rn /tmp/blur_extract/models/* /opt/blur_env/models/ || true && \
    cp -rf /tmp/blur_extract/lib/* /opt/blur_env/scripts/ && \
    rm -rf /tmp/blur_rel.tar.gz /tmp/blur_extract

RUN python3 -c "import vapoursynth as vs; c = vs.core; \
    c.std.LoadPlugin('/opt/blur_env/plugins/libsvpflow1_vs64.so'); \
    c.std.LoadPlugin('/opt/blur_env/plugins/libsvpflow2_vs64.so'); \
    c.std.LoadPlugin('/opt/blur_env/plugins/libakarin.so'); \
    print('VS:', vs.__version__, 'SVP1:', hasattr(c,'svp1'), 'SVP2:', hasattr(c,'svp2'), 'AKARIN:', hasattr(c,'akarin')); \
    assert hasattr(c,'svp1') and hasattr(c,'svp2'), 'SVP failed to load'" && \
    which vspipe

WORKDIR /app
COPY . /app

EXPOSE 8080
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
