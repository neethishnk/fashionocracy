FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# Render injects PORT and RENDER_EXTERNAL_URL; the Concierge binds 0.0.0.0:$PORT, agents stay on localhost
EXPOSE 10000
CMD ["python", "run_demo.py", "--no-browser"]
