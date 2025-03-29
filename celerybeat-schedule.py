from celery import Celery

app = Celery('dictation-studio')
app.config_from_object('celeryconfig')

# 添加定时任务
app.conf.beat_schedule = {
    'check-expired-plans': {
        'task': 'check_expired_plans',
        'schedule': 60.0,  # 每60秒执行一次
    },
} 