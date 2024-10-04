import os
from datetime import timedelta

# Redis Configuration
REDIS_HOST = 'localhost'
REDIS_PORT = 6379

# Redis Configuration for User Data
REDIS_USER_DB = 1 
REDIS_RESOURCE_DB = 0

CHANNEL_PREFIX = "channel:"
VIDEO_PREFIX = "video:"

# JWT Configuration
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'your-secret-key')
JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)