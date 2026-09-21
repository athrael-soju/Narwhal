# The build stage creates a wheel. The runtime image installs the wheel and
# its dependencies, then runs as the narwhal user.
#
#   docker build -t narwhal .
#   docker run --rm -p 8000:8000 -v "$PWD/config:/config:ro" \
#     -v "$PWD/runs:/home/narwhal/runs" narwhal \
#     narwhal-serve --fleet /config/fleet.json --host 0.0.0.0 --port 8000
#
# Profiles must be reachable at the config's profiles.path. The narwhal user
# needs write access to the journal and state paths in the runs mount.

FROM python:3.12-slim AS build
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.12-slim
RUN useradd --system --create-home narwhal
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER narwhal
WORKDIR /home/narwhal
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"
ENTRYPOINT []
CMD ["narwhal-serve", "--help"]
