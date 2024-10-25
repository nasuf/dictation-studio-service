FROM python:3.8-slim

WORKDIR /app/src

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ ./

EXPOSE 4001

CMD ["gunicorn", "--workers", "3", "--bind", "0.0.0.0:4001", "service:app"]