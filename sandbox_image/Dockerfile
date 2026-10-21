FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends nodejs chromium fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright requests beautifulsoup4 pypdf python-docx pillow pandas matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils git ripgrep curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir reportlab pdfplumber PyYAML nbformat
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /workspace
USER 65534:65534
CMD ["python", "--version"]
