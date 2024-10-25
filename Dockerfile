FROM python:3.8-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 4001

CMD ["gunicorn", "--workers", "3", "--bind", "0.0.0.0:4001", "service:app"]