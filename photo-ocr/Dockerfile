# The receipts service (web.py) and the bake-off harness (bakeoff.py), one image.
# Tesseract and Pillow are built in. The other OCR back ends are large; add them with
#   docker compose build --build-arg EXTRAS="python-doctr[torch] paddleocr paddlepaddle"
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
ARG EXTRAS=""
RUN pip install --no-cache-dir Pillow $EXTRAS
ENV PYTHONUNBUFFERED=1 RECEIPTS_HOME=/data RECEIPTS_BAKEOFF_HOME=/data/bakeoff
WORKDIR /app
COPY *.py engines.json ./
CMD ["python", "web.py", "--host", "127.0.0.1", "--port", "8093"]
