FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/
# Install Python deps and Piper TTS binary
RUN pip install --no-cache-dir -r requirements.txt 

# Location for Piper voice models; mount host folder here
RUN mkdir -p /models
VOLUME ["/models"]
ENV NIFTYTTS_PIPER_MODEL=/models

# Add entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 7230
CMD ["/app/entrypoint.sh"]
