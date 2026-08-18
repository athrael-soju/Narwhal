# The router as an image. Build stage makes the wheel; the runtime stage
# installs only it, so the image carries the package and its four runtime
# dependencies, not the toolchain.
#
#   docker build -t narwhal .
#   docker run --rm -v $PWD/config:/config narwhal \
#     narwhal-serve --fleet /config/fleet.json --host 0.0.0.0 --port 8000
#
# The engines are not in this image, by design (§5.6): the router speaks to
# them over HTTP. Profiles must be reachable at the config's profiles_path;
# mount the runs directory alongside the config. With a read-only runs
# mount, pass --journal to a writable path - the default journal lands
# beside the profiles and refuses a read-only filesystem.

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
