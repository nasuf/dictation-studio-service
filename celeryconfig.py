# 确保 payment.py 中的任务被包含在 Celery 任务中
imports = ('src.payment',)

broker_url = 'redis://redis:6379/0'
result_backend = 'redis://redis:6379/0'

task_serializer = 'json'
result_serializer = 'json'
accept_content = ['json']
timezone = 'UTC'
enable_utc = True 