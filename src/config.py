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

# JWT Configuration
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'your-secret-key')
JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=120)

USER_ROLE_DEFAULT = "Free Plan User"
