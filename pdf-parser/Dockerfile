# The PDF parser service (web.py) and command line (parse_pdf.py), one image. Tesseract is built in.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt
ENV PYTHONUNBUFFERED=1 PDF_PARSER_HOME=/data
WORKDIR /app
COPY *.py ./
CMD ["python", "web.py", "--host", "127.0.0.1", "--port", "8096"]
