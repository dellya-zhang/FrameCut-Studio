FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt agent.py framecut.py jobs.py studio.py visual.py ./
COPY .streamlit .streamlit

RUN --mount=type=secret,id=pip_ca,mode=0444 \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home framecut \
    && install -d -o framecut -g framecut /data

ENV FRAMECUT_WORKSPACE=/data HF_HOME=/data/model-cache
USER framecut
EXPOSE 8501
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"
CMD ["streamlit", "run", "studio.py", "--server.address=0.0.0.0", "--server.headless=true"]
