FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
RUN python -m pip install --no-cache-dir playwright==1.63.0 fastapi uvicorn Pillow
WORKDIR /app
COPY server.py /app/server.py
ENV HOME=/tmp
USER 1000:1000
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8088"]
