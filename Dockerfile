# The build stage creates a wheel. The runtime image installs the wheel and
# its dependencies at the versions constraints-dev.txt pins, then runs as the
# narwhal user. Both stages start from the same base image digest.
#
#   docker build -t narwhal .
#   docker run --rm -p 8000:8000 -v "$PWD/config:/config:ro" \
#     -v "$PWD/runs:/home/narwhal/runs" -e NARWHAL_ROUTER_TOKEN narwhal \
#     narwhal-serve --fleet /config/fleet.json --host 0.0.0.0 --port 8000
#
# Profiles must be reachable at the config's profiles.path. The narwhal user,
# UID and GID 10001, needs write access to the journal and state paths in the
# runs mount. The Helm chart's pods run as the same IDs.

FROM python:3.12-slim@sha256:5b5fe805d367f8f5a359ded6ab2652b7f9a206453ee6ee789c0045ba6ec15016 AS build
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir -c constraints-dev.txt build setuptools \
 && python -m build --wheel --no-isolation

FROM python:3.12-slim@sha256:5b5fe805d367f8f5a359ded6ab2652b7f9a206453ee6ee789c0045ba6ec15016
RUN groupadd --system --gid 10001 narwhal \
 && useradd --system --uid 10001 --gid 10001 --create-home narwhal
COPY --from=build /src/dist/*.whl /src/constraints-dev.txt /tmp/
RUN pip install --no-cache-dir -c /tmp/constraints-dev.txt /tmp/*.whl \
 && rm /tmp/*.whl /tmp/constraints-dev.txt
USER 10001:10001
WORKDIR /home/narwhal
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"
ENTRYPOINT []
CMD ["narwhal-serve", "--help"]
