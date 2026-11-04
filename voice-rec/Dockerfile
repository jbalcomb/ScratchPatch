FROM python:3.12-slim
# GPU=1 adds the CUDA 12 libraries faster-whisper needs (cuBLAS, cuDNN 9); the setup script sets it
# when Docker has the NVIDIA runtime.
ARG GPU=0
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/models \
    LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$GPU" = "1" ]; then pip install --no-cache-dir nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"; fi
COPY *.py categories.txt ./
CMD ["python", "voicerec.py", "serve"]
