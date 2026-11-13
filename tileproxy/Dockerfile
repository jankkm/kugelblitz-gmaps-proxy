FROM python:3.13-slim

RUN pip install --no-cache-dir pillow

WORKDIR /app
COPY tileproxy.py cachelimit.py /app/

EXPOSE 8090
CMD ["python", "-u", "/app/tileproxy.py"]
