import json
import logging
from cachetools import LRUCache
from threading import Lock

from config import CHANNEL_PREFIX, VIDEO_PREFIX

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

channel_cache = LRUCache(maxsize=1000)
cache_lock = Lock()  

video_cache = LRUCache(maxsize=1000)
video_cache_lock = Lock()

def get_channel_from_cache_or_redis(channel_id, redis_client):
    """Get channel data from cache or Redis"""
    with cache_lock:
        if channel_id in channel_cache:
            logger.info(f"Cache hit for channel {channel_id}")
            return channel_cache[channel_id]
        
        # If not in cache, get from Redis
        channel_key = f"{CHANNEL_PREFIX}{channel_id}"
        channel_data = redis_client.hgetall(channel_key)
        
        if channel_data:
            # Convert bytes to string
            channel_data = {k.decode('utf-8'): v.decode('utf-8') for k, v in channel_data.items()}
            # Store in cache
            channel_cache[channel_id] = channel_data
            logger.info(f"Cache miss for channel {channel_id}, stored in cache")
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
            logger.info(f"Cache hit for video {cache_key}")
            return video_cache[cache_key]
        
        # If not in cache, get from Redis
        video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
        video_data = redis_client.hgetall(video_key)
        
        if video_data:
            # Convert bytes to string
            video_data = {k.decode('utf-8'): v.decode('utf-8') for k, v in video_data.items()}
            if 'transcript' in video_data:
                video_data['transcript'] = json.loads(video_data['transcript'])
            # Store in cache
            video_cache[cache_key] = video_data
            logger.info(f"Cache miss for video {cache_key}, stored in cache")
            return video_data
        return None

def update_video_cache(channel_id, video_id, video_data):
    """Update video cache"""
    cache_key = f"{channel_id}:{video_id}"
    with video_cache_lock:
        logger.info(f"Updating cache for video {cache_key}")
        video_cache[cache_key] = video_data

def remove_video_from_cache(channel_id, video_id):
    """Remove video from cache"""
    cache_key = f"{channel_id}:{video_id}"
    with video_cache_lock:
        logger.info(f"Removing video {cache_key} from cache")
        video_cache.pop(cache_key, None)