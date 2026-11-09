# The photo OCR service (web.py) and the bake-off harness (bakeoff.py), one image.
# Tesseract and Pillow are built in. The other OCR back ends are large; add them with
#   docker compose build --build-arg EXTRAS="python-doctr[torch] paddleocr paddlepaddle"
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
ARG EXTRAS=""
RUN pip install --no-cache-dir Pillow $EXTRAS
ENV PYTHONUNBUFFERED=1 PHOTO_OCR_HOME=/data PHOTO_OCR_BAKEOFF_HOME=/data/bakeoff
WORKDIR /app
COPY *.py engines.json ./
COPY prompts ./prompts
CMD ["python", "web.py", "--host", "127.0.0.1", "--port", "8093"]
