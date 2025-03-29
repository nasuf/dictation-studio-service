FROM python:3.8-slim

WORKDIR /app/src

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ ./

RUN mkdir -p /app/uploads && chmod 777 /app/uploads

COPY celerybeat-schedule.py /app/

EXPOSE 4001

CMD ["sh", "-c", "gunicorn -w 4 -b 0.0.0.0:5000 app:app & celery -A celery_worker worker --loglevel=info & celery -A celerybeat-schedule beat & wait"]