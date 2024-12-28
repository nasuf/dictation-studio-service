import json
import logging
from cachetools import LRUCache
from threading import Lock

from config import CHANNEL_PREFIX, VIDEO_PREFIX, USER_PREFIX

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

channel_cache = LRUCache(maxsize=1000)
cache_lock = Lock()  

video_cache = LRUCache(maxsize=1000)
video_cache_lock = Lock()

user_cache = LRUCache(maxsize=1000)
user_cache_lock = Lock()

payment_cache = LRUCache(maxsize=1000)
payment_cache_lock = Lock()

def _decode_user_data(user_data_raw):
    """Helper function to decode and parse user data from Redis
    
    Args:
        user_data_raw: Raw user data from Redis with byte strings
        
    Returns:
        dict: Decoded user data with parsed JSON fields
    """
    decoded_data = {}
    for k, v in user_data_raw.items():
        key = k.decode('utf-8')
        value = v.decode('utf-8')
        try:
            # Try to parse JSON fields
            decoded_data[key] = json.loads(value)
        except json.JSONDecodeError:
            # If not JSON, keep as string
            decoded_data[key] = value
    return decoded_data

def get_channel_from_cache_or_redis(channel_id, redis_client):
    """Get channel data from cache or Redis"""
    with cache_lock:
        if channel_id in channel_cache:
            logger.debug(f"Cache hit for channel {channel_id}")
            return channel_cache[channel_id]
        
        # If not in cache, get from Redis
        logger.debug(f"Cache miss for channel {channel_id}")
        channel_key = f"{CHANNEL_PREFIX}{channel_id}"
        channel_data = redis_client.hgetall(channel_key)
        
        if channel_data:
            # Convert bytes to string
            channel_data = {k.decode('utf-8'): v.decode('utf-8') for k, v in channel_data.items()}
            # Store in cache
            channel_cache[channel_id] = channel_data
            return channel_data
        return None

def update_channel_cache(channel_id, channel_data):
    """Update channel cache"""
    with cache_lock:
        logger.info(f"Updating cache for channel {channel_id}")
        channel_cache[channel_id] = channel_data

def get_video_from_cache_or_redis(channel_id, video_id, redis_client):
    """Get video data from cache or Redis"""
    cache_key = f"{channel_id}:{video_id}"
    with video_cache_lock:
        if cache_key in video_cache:
            logger.debug(f"Cache hit for video {cache_key}")
            return video_cache[cache_key]
        
        # If not in cache, get from Redis
        logger.debug(f"Cache miss for video {cache_key}")
        video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
        video_data = redis_client.hgetall(video_key)
        
        if video_data:
            # Convert bytes to string
            video_data = {k.decode('utf-8'): v.decode('utf-8') for k, v in video_data.items()}
            if 'transcript' in video_data:
                video_data['transcript'] = json.loads(video_data['transcript'])
            # Store in cache
            video_cache[cache_key] = video_data
            return video_data
        return None

def update_video_cache(channel_id, video_id, video_data):
    """Update video cache"""
    cache_key = f"{channel_id}:{video_id}"
    with video_cache_lock:
        logger.debug(f"Updating cache for video {cache_key}")
        video_cache[cache_key] = video_data

def remove_video_from_cache(channel_id, video_id):
    """Remove video from cache"""
    cache_key = f"{channel_id}:{video_id}"
    with video_cache_lock:
        logger.debug(f"Removing video {cache_key} from cache")
        video_cache.pop(cache_key, None)

def get_user_from_cache_or_redis(user_email, redis_client):
    """Get user data from cache or Redis"""
    with user_cache_lock:
        if user_email in user_cache:
            logger.debug(f"Cache hit for user {user_email}")
            return user_cache[user_email]
        
        # If not in cache, get from Redis
        logger.debug(f"Cache miss for user {user_email}")
        user_key = f"{USER_PREFIX}{user_email}"
        user_data = redis_client.hgetall(user_key)
        
        if user_data:
            # Use helper function to decode and parse data
            decoded_data = _decode_user_data(user_data)
            
            # Store in cache
            user_cache[user_email] = decoded_data
            return decoded_data
        return None

def update_user_cache(user_email, user_data):
    """Update user cache"""
    with user_cache_lock:
        logger.debug(f"Updating cache for user {user_email}")
        user_cache[user_email] = user_data

def remove_user_from_cache(user_email):
    """Remove user from cache"""
    with user_cache_lock:
        logger.debug(f"Removing user {user_email} from cache")
        user_cache.pop(user_email, None)

def get_user_progress_from_cache_or_redis(user_email, redis_client):
    """Get user progress data from cache or Redis"""
    user_data = get_user_from_cache_or_redis(user_email, redis_client)
    if user_data and 'dictation_progress' in user_data:
        return user_data['dictation_progress']
    return {}

def get_user_duration_from_cache_or_redis(user_email, redis_client):
    """Get user duration data from cache or Redis"""
    user_data = get_user_from_cache_or_redis(user_email, redis_client)
    if user_data and 'duration_data' in user_data:
        return user_data['duration_data']
    return {"duration": 0, "channels": {}, "date": {}}

def get_user_config_from_cache_or_redis(user_email, redis_client):
    """Get user config from cache or Redis"""
    user_data = get_user_from_cache_or_redis(user_email, redis_client)
    if user_data:
        # Filter out sensitive data like password
        config = {k: v for k, v in user_data.items() if k != 'password'}
        return config
    return {}

def get_user_missed_words_from_cache_or_redis(user_email, redis_client):
    """Get user missed words from cache or Redis"""
    user_data = get_user_from_cache_or_redis(user_email, redis_client)
    if user_data and 'missed_words' in user_data:
        return user_data['missed_words']
    return []

def get_user_plan_from_cache_or_redis(user_email, redis_client):
    """Get user plan data from cache or Redis"""
    user_data = get_user_from_cache_or_redis(user_email, redis_client)
    if user_data and 'plan' in user_data:
        return user_data['plan']
    return None

def update_user_plan_in_cache(user_email, plan_data, redis_client):
    """Update user plan in cache and Redis"""
    with user_cache_lock:
        user_key = f"{USER_PREFIX}{user_email}"
        
        # Directly access cache and Redis
        if user_email in user_cache:
            user_data = user_cache[user_email]
            logger.debug(f"Cache hit for user {user_email} during plan update")
        else:
            # Get from Redis
            user_data_raw = redis_client.hgetall(user_key)
            if user_data_raw:
                # Use helper function to decode and parse data
                user_data = _decode_user_data(user_data_raw)
                logger.debug(f"Cache miss for user {user_email} during plan update")
            else:
                user_data = {}
        
        # Update plan data
        user_data['plan'] = plan_data
        
        # Update Redis
        redis_client.hset(user_key, 'plan', json.dumps(plan_data))
        
        # Update cache
        user_cache[user_email] = user_data
        
        logger.debug(f"Updated plan in cache for user {user_email}")
        return user_data

def get_failed_update_from_cache_or_redis(session_id, redis_client):
    """Get failed update data from cache or Redis"""
    with payment_cache_lock:
        cache_key = f"failed_update:{session_id}"
        if cache_key in payment_cache:
            logger.debug(f"Cache hit for failed update {cache_key}")
            return payment_cache[cache_key]
        
        # If not in cache, get from Redis
        logger.debug(f"Cache miss for failed update {cache_key}")
        failed_data = redis_client.get(cache_key)
        if failed_data:
            failed_update = json.loads(failed_data)
            # Store in cache
            payment_cache[cache_key] = failed_update
            return failed_update
        return None

def update_failed_update_in_cache(session_id, failed_update, expire_seconds, redis_client):
    """Update failed update in cache and Redis"""
    with payment_cache_lock:
        cache_key = f"failed_update:{session_id}"
        
        # Update Redis with expiration
        redis_client.setex(
            cache_key,
            expire_seconds,
            json.dumps(failed_update)
        )
        
        # Update cache
        payment_cache[cache_key] = failed_update
        logger.debug(f"Updated failed update in cache for session {session_id}")

def remove_failed_update_from_cache(session_id, redis_client):
    """Remove failed update from cache and Redis"""
    with payment_cache_lock:
        cache_key = f"failed_update:{session_id}"
        
        # Remove from Redis
        redis_client.delete(cache_key)
        
        # Remove from cache
        payment_cache.pop(cache_key, None)
        logger.debug(f"Removed failed update from cache for session {session_id}")
