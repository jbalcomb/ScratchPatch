# Bake-off harness: Tesseract and Pillow built in. The other OCR back ends are large; add them with
#   docker compose build --build-arg EXTRAS="python-doctr[torch] paddleocr paddlepaddle"
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
ARG EXTRAS=""
RUN pip install --no-cache-dir Pillow $EXTRAS
ENV PYTHONUNBUFFERED=1 RECEIPTS_BAKEOFF_HOME=/data
WORKDIR /app
COPY bakeoff.py engines.json ./
ENTRYPOINT ["python", "bakeoff.py"]
