import json
import os
from datetime import timedelta

# Redis Configuration
REDIS_HOST = '192.210.235.115'
REDIS_PORT = 6380
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

USER_PLAN_DEFAULT = json.dumps({"plan": "Free"}) 
USER_ROLE_DEFAULT = "User"

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
STRIPE_SUCCESS_URL = os.getenv('STRIPE_SUCCESS_URL')
STRIPE_CANCEL_URL = os.getenv('STRIPE_CANCEL_URL')

# Payment Retry Configuration
PAYMENT_MAX_RETRY_ATTEMPTS = 5
PAYMENT_RETRY_DELAY_SECONDS = 5
PAYMENT_RETRY_KEY_EXPIRE_SECONDS = 3600  # expire after 1 hour