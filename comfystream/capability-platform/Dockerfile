# syntax=docker/dockerfile:1
ARG CUDA_BASE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04@sha256:9175fa92f96de35a8cfb9493f0dfcf9435c7a597e9d95ad41d2cae382a95e3f9
FROM ${CUDA_BASE} AS runtime
ARG COMFYSTREAM_REF
ARG PROFILE=media
ARG COMFY_REF=a178b53bde7782b46a41eb12bfbee1bfc2feb4a2
ENV DEBIAN_FRONTEND=noninteractive UV_LINK_MODE=copy UV_TOOL_DIR=/opt/tools UV_TOOL_BIN_DIR=/usr/local/bin
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv git ca-certificates ffmpeg sox libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/bootstrap \
    && /opt/bootstrap/bin/pip install --no-cache-dir uv==0.12.9
ENV PATH=/opt/venv/bin:/opt/bootstrap/bin:/usr/local/bin:/usr/bin:/bin
RUN uv venv --python /usr/bin/python3 /opt/venv
RUN test -n "$COMFYSTREAM_REF" && test ${#COMFYSTREAM_REF} -eq 40 \
    && git init /opt/source \
    && git -C /opt/source remote add origin https://github.com/eliteprox/comfystream.git \
    && git -C /opt/source fetch --depth 1 origin "$COMFYSTREAM_REF" \
    && git -C /opt/source checkout --detach FETCH_HEAD \
    && uv pip sync --python /opt/venv/bin/python "/opt/source/requirements-linux-${PROFILE}.lock" \
    && uv pip install --python /opt/venv/bin/python \
       --constraint "/opt/source/requirements-linux-${PROFILE}.lock" \
       "comfystream[${PROFILE},live-runner,ffmpeg] @ git+https://github.com/eliteprox/comfystream.git@${COMFYSTREAM_REF}"
RUN uv tool install --python /usr/bin/python3 \
    'comfy-cli @ git+https://github.com/Comfy-Org/comfy-cli.git@acfd665a997262c29c5a04afe3a9b383f36c197b'
RUN mkdir -p /workspace/ComfyUI \
    && git init /workspace/ComfyUI \
    && git -C /workspace/ComfyUI remote add origin https://github.com/eliteprox/forks-pip-and-uv-installable-ComfyUI.git \
    && git -C /workspace/ComfyUI fetch --depth 1 origin "$COMFY_REF" \
    && git -C /workspace/ComfyUI checkout --detach FETCH_HEAD \
    && ln -s /opt/venv /workspace/ComfyUI/.venv \
    && comfy --skip-prompt --workspace /workspace/ComfyUI install \
       --url https://github.com/eliteprox/forks-pip-and-uv-installable-ComfyUI.git \
       --version nightly --commit "$COMFY_REF" --restore --nvidia --skip-torch-or-directml --skip-requirement --skip-manager
COPY provision.py /opt/provision.py
RUN python /opt/provision.py
ENV COMFYUI_CWD=/workspace/ComfyUI HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
ENV STREAM_PACK_INPUT_DIR=/state/inputs STREAM_PACK_OUTPUT_DIR=/state/outputs
WORKDIR /state
EXPOSE 8991
ENTRYPOINT ["comfystream-live-runner"]

FROM runtime AS media
FROM runtime AS chat
