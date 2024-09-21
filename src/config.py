import os
from datetime import timedelta

# Redis Configuration
REDIS_HOST = 'localhost'
REDIS_PORT = 6379
REDIS_RESOURCE_DB = 0

# Redis Configuration for User Data
REDIS_USER_DB = 1  # 使用不同的数据库编号来存储用户数据

# JWT Configuration
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'your-secret-key')  # 在生产环境中使用环境变量
JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)  # 令牌过期时间