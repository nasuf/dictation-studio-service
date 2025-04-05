import redis
import json
import os
import sys
from datetime import datetime
from config import (
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD,
    REDIS_USER_DB, USER_PREFIX
)
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Redis connection
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    db=REDIS_USER_DB,
    decode_responses=False
)

def migrate_quota_data():
    """
    Migrate dispersed quota data to user hash tables
    """
    logger.info("Starting quota data migration...")
    
    # Get all users
    user_keys = redis_client.keys(f"{USER_PREFIX}*")
    logger.info(f"Found {len(user_keys)} users")
    
    migrated_count = 0
    error_count = 0
    
    for user_key in user_keys:
        try:
            user_id = user_key.decode('utf-8').replace(USER_PREFIX, "")
            
            # Get existing user data
            user_data = redis_client.hgetall(user_key)
            
            # Check if user already has quota data
            has_quota = b'quota' in user_data
            if has_quota:
                logger.info(f"User {user_id} already has quota data, skipping")
                continue
            
            # Get old data
            first_use_key = f"dictation:first_use:{user_id}"
            quota_key = f"dictation:quota:{user_id}"
            history_key = f"dictation:history:{user_id}"
            
            first_use_time_bytes = redis_client.get(first_use_key)
            quota_videos = redis_client.smembers(quota_key)
            history_videos = redis_client.smembers(history_key)
            
            # Create new quota data structure
            quota_info = {
                "first_use_time": datetime.now().isoformat(),  # Default value
                "videos": [],
                "history": []
            }
            
            # If first use time exists, update
            if first_use_time_bytes:
                try:
                    quota_info["first_use_time"] = first_use_time_bytes.decode('utf-8')
                except Exception as e:
                    logger.warning(f"Failed to parse first use time for {user_id}: {e}")
            
            # Add quota videos
            if quota_videos:
                quota_info["videos"] = [v.decode('utf-8') for v in quota_videos]
            
            # Add history videos
            if history_videos:
                quota_info["history"] = [v.decode('utf-8') for v in history_videos]
            
            # Save new quota data
            redis_client.hset(user_key, "quota", json.dumps(quota_info))
            
            # Statistics
            migrated_count += 1
            logger.info(f"Migrated quota data for user {user_id}")
            
            # Optional: Delete old data
            # redis_client.delete(first_use_key, quota_key, history_key)
            
        except Exception as e:
            error_count += 1
            logger.error(f"Error migrating user quota data: {e}")
    
    logger.info(f"Quota data migration completed. Success: {migrated_count}, Errors: {error_count}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "quota":
            migrate_quota_data()
        else:
            print("Unknown migration type. Available options: quota")
    else:
        print("Please specify migration type. Available options: quota")