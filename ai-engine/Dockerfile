FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libreoffice-writer \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Pre-install CPU-only torch FROM PyTorch's CPU wheel index. sentence-transformers
# (in requirements.txt) depends on torch — without this, pip pulls the GPU build
# (~2 GB of CUDA libraries this VPS has no GPU to use). Order matters:
# torch must be satisfied before sentence-transformers is resolved.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    'torch>=2.0,<3.0'

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
