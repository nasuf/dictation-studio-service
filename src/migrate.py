import redis
import json
from config import REDIS_HOST, REDIS_PORT, REDIS_RESOURCE_DB

# Redis connection
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_RESOURCE_DB)

# Constants for new key prefixes
CHANNEL_PREFIX = "channel:"
VIDEO_PREFIX = "video:"

def migrate_data():
    print("Starting data migration...")

    # Migrate channel data
    old_channels = redis_client.hgetall('video_channel')
    for channel_id, channel_data in old_channels.items():
        channel_id = channel_id.decode('utf-8')
        channel_info = json.loads(channel_data.decode('utf-8'))
        new_key = f"{CHANNEL_PREFIX}{channel_id}"
        redis_client.set(new_key, json.dumps(channel_info))
        print(f"Migrated channel: {channel_id}")

    # Migrate video list data
    old_video_lists = redis_client.hgetall('video_list')
    for channel_id, video_list_data in old_video_lists.items():
        channel_id = channel_id.decode('utf-8')
        video_list_info = json.loads(video_list_data.decode('utf-8'))
        new_key = f"{VIDEO_PREFIX}{channel_id}"
        redis_client.set(new_key, json.dumps(video_list_info))
        print(f"Migrated video list for channel: {channel_id}")

    # Remove old keys
    redis_client.delete('video_channel')
    redis_client.delete('video_list')

    print("Data migration completed successfully.")

if __name__ == "__main__":
    migrate_data()