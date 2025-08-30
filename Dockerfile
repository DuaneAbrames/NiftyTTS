FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY . /srv/
# Install Python deps
RUN pip install --no-cache-dir -r app/requirements.txt 

# Location for Piper voice models; mount host folder here
RUN mkdir -p /models
VOLUME ["/models"]
ENV NIFTYTTS_PIPER_MODEL=/models \
    NIFTYTTS_UID=99 \
    NIFTYTTS_GID=100 \
    PYTHONUNBUFFERED=1

# Add entrypoint
RUN chmod +x /srv/entrypoint.sh

EXPOSE 7230
CMD ["/srv/entrypoint.sh"]
