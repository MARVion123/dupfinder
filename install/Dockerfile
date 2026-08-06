# Alternative to the systemd install: run under Container Manager.
#
#   docker build -t dupfinder .
#   docker run -d --name dupfinder \
#       -p 8777:8777 \
#       -v /volume1:/volume1 \
#       -v /volume1/docker/dupfinder:/data \
#       -e ANTHROPIC_API_KEY=sk-ant-... \
#       dupfinder
#
# Mount every volume you want to scan at the SAME path inside the container,
# otherwise the paths shown in the UI will not match the paths on the NAS.

FROM python:3.12-slim

RUN pip install --no-cache-dir anthropic Pillow

WORKDIR /app
COPY dupfinder/ /app/dupfinder/

ENV DUPFINDER_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8777

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8777/api/status', timeout=4)" || exit 1

ENTRYPOINT ["python3", "-m", "dupfinder", "serve"]
