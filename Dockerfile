FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        tesseract-ocr-eng \
        libglib2.0-0 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY . /opt/priv-sanitizer
WORKDIR /opt/priv-sanitizer

RUN python -m pip install --no-cache-dir ".[vision]" \
    && python -c 'import cv2, pytesseract; assert hasattr(cv2, "CascadeClassifier"); print(cv2.__version__); print(pytesseract.get_tesseract_version())' \
    && chmod +x /opt/priv-sanitizer/priv-sanitizer

WORKDIR /work
ENTRYPOINT ["/opt/priv-sanitizer/priv-sanitizer"]
