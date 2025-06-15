# redis_manager.py
import redis
from redis import ConnectionPool
from config import REDIS_BLACKLIST_DB, REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_RESOURCE_DB, REDIS_USER_DB

class RedisManager:
    _resource_pool = None
    _user_pool = None
    _blacklist_pool = None
    
    @classmethod
    def get_resource_client(cls):
        if cls._resource_pool is None:
            cls._resource_pool = ConnectionPool(
                host=REDIS_HOST, 
                port=REDIS_PORT, 
                db=REDIS_RESOURCE_DB, 
                password=REDIS_PASSWORD,
                decode_responses=True,  # automatically decode responses
                max_connections=20,     # Increase max connections for concurrent processing
                socket_timeout=60,      # Increase socket timeout to 60 seconds
                socket_connect_timeout=15,  # Increase connection timeout to 15 seconds
                retry_on_timeout=True,  # Retry on timeout
                health_check_interval=30,  # Health check interval
                socket_keepalive=True,  # Enable socket keepalive
                socket_keepalive_options={}  # Use default keepalive options
            )
        return redis.Redis(connection_pool=cls._resource_pool)
    
    @classmethod
    def get_user_client(cls):
        if cls._user_pool is None:
            cls._user_pool = ConnectionPool(
                host=REDIS_HOST, 
                port=REDIS_PORT, 
                db=REDIS_USER_DB, 
                password=REDIS_PASSWORD,
                decode_responses=True,  # automatically decode responses
                max_connections=10,     # Sufficient for user operations
                socket_timeout=60,      # Increase socket timeout to 60 seconds
                socket_connect_timeout=15,  # Increase connection timeout to 15 seconds
                retry_on_timeout=True,  # Retry on timeout
                health_check_interval=30,  # Health check interval
                socket_keepalive=True,  # Enable socket keepalive
                socket_keepalive_options={}  # Use default keepalive options
            )
        return redis.Redis(connection_pool=cls._user_pool)
    
    @classmethod
    def get_blacklist_client(cls):
        if cls._blacklist_pool is None:
            cls._blacklist_pool = ConnectionPool(
                host=REDIS_HOST, 
                port=REDIS_PORT, 
                db=REDIS_BLACKLIST_DB, 
                password=REDIS_PASSWORD,
                decode_responses=True,  # automatically decode responses
                max_connections=5,      # Sufficient for blacklist operations
                socket_timeout=60,      # Increase socket timeout to 60 seconds
                socket_connect_timeout=15,  # Increase connection timeout to 15 seconds
                retry_on_timeout=True,  # Retry on timeout
                health_check_interval=30,  # Health check interval
                socket_keepalive=True,  # Enable socket keepalive
                socket_keepalive_options={}  # Use default keepalive options
            )
        return redis.Redis(connection_pool=cls._blacklist_pool)