import redis
import json
from config import REDIS_HOST, REDIS_PORT, REDIS_RESOURCE_DB

# Redis connection
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_RESOURCE_DB)

# Constants for key prefixes
CHANNEL_PREFIX = "channel:"
VIDEO_PREFIX = "video:"

def migrate_data():
    print("Starting data migration...")

    # Migrate channel data
    for key in redis_client.scan_iter(f"{CHANNEL_PREFIX}*"):
        channel_data = redis_client.get(key)
        if channel_data:
            channel_info = json.loads(channel_data)
            redis_client.delete(key)
            redis_client.hmset(key, channel_info)
            print(f"Migrated channel: {key}")

    # Migrate video list data
    for key in redis_client.scan_iter(f"{VIDEO_PREFIX}*"):
        video_list_data = redis_client.get(key)
        if video_list_data:
            video_list_info = json.loads(video_list_data)
            redis_client.delete(key)
            for field, value in video_list_info.items():
                redis_client.hset(key, field, json.dumps(value))
            print(f"Migrated video list: {key}")

    print("Data migration completed successfully.")

if __name__ == "__main__":
    migrate_data()