import json
import os
from datetime import timedelta

# Redis Configuration
REDIS_HOST = os.getenv('REDIS_HOST')
REDIS_PORT = os.getenv('REDIS_PORT')
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD')

# Redis Configuration for User Data
REDIS_USER_DB = 0 
REDIS_RESOURCE_DB = 1
REDIS_BLACKLIST_DB = 2

CHANNEL_PREFIX = "channel:"
VIDEO_PREFIX = "video:"
USER_PREFIX = "user:"

# JWT Configuration
JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'your-secret-key')
JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=120)
JWT_REFRESH_TOKEN_EXPIRES = False

USER_PLAN_DEFAULT = json.dumps({"name": "Free"}) 
USER_ROLE_DEFAULT = "User"
USER_DICTATION_CONFIG_DEFAULT = json.dumps({"playback_speed": 1, "auto_repeat": 0, "shortcuts": {"repeat": "Tab", "next": "Enter", "prev": "ControlLeft"}})
USER_LANGUAGE_DEFAULT = "en"

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
STRIPE_SUCCESS_URL = os.getenv('STRIPE_SUCCESS_URL')
STRIPE_CANCEL_URL = os.getenv('STRIPE_CANCEL_URL')

# ZPAY Configuration
ZPAY_NOTIFY_URL = os.getenv('ZPAY_NOTIFY_URL')
ZPAY_RETURN_URL = os.getenv('ZPAY_RETURN_URL')

# Payment Retry Configuration
PAYMENT_MAX_RETRY_ATTEMPTS = 5
PAYMENT_RETRY_DELAY_SECONDS = 5
PAYMENT_RETRY_KEY_EXPIRE_SECONDS = 3600  # expire after 1 hour

# Visibility
VISIBILITY_PUBLIC = "public"
VISIBILITY_PRIVATE = "private"
VISIBILITY_ALL = "all"

# Language
LANGUAGE_EN = "en"
LANGUAGE_ZH = "zh"
LANGUAGE_JA = "ja"
LANGUAGE_KO = "ko"
LANGUAGE_ALL = "all"

# 验证码过期时间（秒）
# Verification code expiration time (seconds)
VERIFICATION_CODE_EXPIRE_SECONDS = 3600  # 1 hour
