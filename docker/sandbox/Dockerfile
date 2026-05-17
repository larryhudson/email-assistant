FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         curl \
         git \
         ripgrep \
         jq \
         poppler-utils \
         pandoc \
         libreoffice-writer \
         unzip \
         zip \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
    python-docx==1.2.0 \
    lxml==6.1.0

WORKDIR /workspace

CMD ["sleep", "infinity"]
