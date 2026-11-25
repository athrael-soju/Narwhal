# vLLM engine image for a Narwhal fleet that serves a GGUF model on CUDA.
#
# It adds the vLLM GGUF plugin to the vLLM 0.30.0 image, applies the plugin sources the Narwhal
# vLLM dev templates qualify, and fails the build unless the packages and the plugin digests
# match those templates. See docs/deploy/01-Discover.md, "Building a GGUF engine image".
FROM vllm/vllm-openai:v0.30.0@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90

ARG GGUF_PLUGIN_WHEEL=https://github.com/vllm-project/vllm-gguf-plugin/releases/download/v0.0.5/vllm_gguf_plugin-0.0.5-cp310-abi3-manylinux_2_28_x86_64.whl
ARG GGUF_PLUGIN_REVISION=d4c1f0d082fc7cd4350da56689109a01c1f29d6c
ARG GGUF_PLUGIN_PYTHON_SHA256=b08b2ffdf18e4314ed7e4a8cfd49de3520edd5d9bc9be4564d8a71b2265771e0
ARG GGUF_PLUGIN_EXTENSION_SHA256=64521127698503e4b72053cd06adc9aa0448debb07715b2663b1250a816b2c1e

COPY check_packages.py install_plugin_sources.py /opt/narwhal-image/

# --no-deps keeps the base image's CUDA libraries; the plugin's one missing requirement is gguf.
RUN python3 -m pip install --no-cache-dir --no-deps "${GGUF_PLUGIN_WHEEL}" 'gguf==0.19.0' \
    && python3 -c 'import gguf, vllm_gguf_plugin' \
    && python3 /opt/narwhal-image/install_plugin_sources.py \
        "${GGUF_PLUGIN_REVISION}" "${GGUF_PLUGIN_PYTHON_SHA256}" "${GGUF_PLUGIN_EXTENSION_SHA256}" \
    && python3 /opt/narwhal-image/check_packages.py \
        'vllm==0.30.0' 'nixl==1.4.1' 'vllm-gguf-plugin==0.0.5' 'torch==2.13.0' \
        'transformers==5.17.0' 'nixl-cu13==1.4.1'
