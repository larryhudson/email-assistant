FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         curl \
         git \
         ripgrep \
         jq \
         poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

CMD ["sleep", "infinity"]
