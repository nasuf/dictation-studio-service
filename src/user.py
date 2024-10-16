from flask import request
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import get_jwt_identity
import json
import logging
from config import CHANNEL_PREFIX, VIDEO_PREFIX
from jwt_utils import jwt_required_and_refresh

# Configure logging
logger = logging.getLogger(__name__)

# Create a namespace for user-related routes
user_ns = Namespace('user', description='User operations')

# We'll define these functions to get Redis clients
def get_redis_resource_client():
    from flask import current_app
    return current_app.config['redis_resource_client']

def get_redis_user_client():
    from flask import current_app
    return current_app.config['redis_user_client']

# Define model for dictation progress
dictation_progress_model = user_ns.model('DictationProgress', {
    'channelId': fields.String(required=True, description='Channel ID'),
    'videoId': fields.String(required=True, description='Video ID'),
    'userInput': fields.Raw(required=True, description='User input for dictation'),
    'currentTime': fields.Integer(required=True, description='Current timestamp'),
    'overallCompletion': fields.Integer(required=True, description='Overall completion percentage'),
    'duration': fields.Integer(required=True, description='Duration in milliseconds')
})

# Add new model definition
video_duration_model = user_ns.model('VideoDuration', {
    'channelId': fields.String(required=True, description='Channel ID'),
    'videoId': fields.String(required=True, description='Video ID'),
    'duration': fields.Integer(required=True, description='Duration in milliseconds')
})

# Add new model definition for user configuration
user_config_model = user_ns.model('UserConfig', {
    'playback_speed': fields.Float(description='Playback speed'),
    'auto_repeat': fields.Boolean(description='Auto-repeat setting'),
    'language_preference': fields.String(description='Language preference'),
    'theme_preference': fields.String(description='Theme preference'),
    'shortcuts': fields.Raw(description='Custom shortcuts')
})

@user_ns.route('/progress')
class DictationProgress(Resource):
    @jwt_required_and_refresh()
    @user_ns.expect(dictation_progress_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 500: 'Server Error'})
    def post(self):
        """Update user's dictation progress and video duration"""
        try:
            user_email = get_jwt_identity()
            progress_data = request.json

            required_fields = ['channelId', 'videoId', 'userInput', 'currentTime', 'overallCompletion', 'duration']
            if not all(field in progress_data for field in required_fields):
                return {"error": "Missing required fields"}, 400

            user_key = f"user:{user_email}"
            redis_user_client = get_redis_user_client()
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Update dictation progress
            dictation_progress = json.loads(user_data.get(b'dictation_progress', b'{}').decode('utf-8'))
            video_key = f"{progress_data['channelId']}:{progress_data['videoId']}"
            dictation_progress[video_key] = {
                'userInput': progress_data['userInput'],
                'currentTime': progress_data['currentTime'],
                'overallCompletion': progress_data['overallCompletion']
            }
            redis_user_client.hset(user_key, 'dictation_progress', json.dumps(dictation_progress))

            # Update structured duration data
            duration_data = json.loads(user_data.get(b'duration_data', b'{"duration": 0, "channels": {}}').decode('utf-8'))

            channel_id = progress_data['channelId']
            video_id = progress_data['videoId']
            new_duration = progress_data['duration']

            if channel_id not in duration_data['channels']:
                duration_data['channels'][channel_id] = {"duration": 0, "videos": {}}

            channel_data = duration_data['channels'][channel_id]

            if video_id in channel_data['videos']:
                old_duration = channel_data['videos'][video_id]
                duration_diff = new_duration - old_duration
            else:
                duration_diff = new_duration

            channel_data['videos'][video_id] = new_duration
            channel_data['duration'] += duration_diff
            duration_data['duration'] += duration_diff

            redis_user_client.hset(user_key, 'duration_data', json.dumps(duration_data))

            logger.info(f"Updated progress and duration for user: {user_email}, channel: {channel_id}, video: {video_id}")
            return {
                "message": "Dictation progress and video duration updated successfully",
                "videoDuration": new_duration,
                "channelTotalDuration": channel_data['duration'],
                "totalDuration": duration_data['duration']
            }, 200

        except Exception as e:
            logger.error(f"Error updating progress and duration: {str(e)}")
            return {"error": f"An error occurred while updating progress and duration: {str(e)}"}, 500

    @jwt_required_and_refresh()
    @user_ns.doc(params={'channelId': 'Channel ID', 'videoId': 'Video ID'}, responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's dictation progress for a specific video"""
        try:
            user_email = get_jwt_identity()
            channel_id = request.args.get('channelId')
            video_id = request.args.get('videoId')

            if not channel_id or not video_id:
                return {"error": "channelId and videoId are required"}, 400

            user_key = f"user:{user_email}"
            redis_user_client = get_redis_user_client()
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            dictation_progress = json.loads(user_data.get(b'dictation_progress', b'{}').decode('utf-8'))
            video_key = f"{channel_id}:{video_id}"
            progress = dictation_progress.get(video_key)

            if not progress:
                return {"channelId": channel_id, "videoId": video_id, "userInput": "", "currentTime": 0, "overallCompletion": 0}, 200

            logger.info(f"Retrieved dictation progress for user: {user_email}, channel: {channel_id}, video: {video_key}")
            return {"channelId": channel_id, "videoId": video_id, "userInput": progress['userInput'],
                     "currentTime": progress['currentTime'], "overallCompletion": progress['overallCompletion']}, 200

        except Exception as e:
            logger.error(f"Error retrieving dictation progress: {str(e)}")
            return {"error": "An error occurred while retrieving dictation progress"}, 500

@user_ns.route('/progress/channel')
class ChannelDictationProgress(Resource):
    @jwt_required_and_refresh()
    @user_ns.doc(params={'channelId': 'Channel ID'}, responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's dictation progress for all videos in a specific channel"""
        try:
            user_email = get_jwt_identity()
            channel_id = request.args.get('channelId')

            if not channel_id:
                return {"error": "channelId is required"}, 400

            user_key = f"user:{user_email}"
            redis_user_client = get_redis_user_client()
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            dictation_progress = json.loads(user_data.get(b'dictation_progress', b'{}').decode('utf-8'))

            channel_progress = {}
            for key, value in dictation_progress.items():
                if key.startswith(f"{channel_id}:"):
                    video_id = key.split(':')[1]
                    channel_progress[video_id] = value['overallCompletion']

            logger.info(f"Retrieved dictation progress for user: {user_email}, channel: {channel_id}")
            return {"channelId": channel_id, "progress": channel_progress}, 200

        except Exception as e:
            logger.error(f"Error retrieving dictation progress: {str(e)}")
            return {"error": "An error occurred while retrieving dictation progress"}, 500

@user_ns.route('/progress/<string:channel_id>')
class ChannelDictationProgress(Resource):
    @jwt_required_and_refresh()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self, channel_id):
        """Get all dictation progress for a specific channel"""
        try:
            # Get user email from JWT token
            user_email = get_jwt_identity()

            # Get existing user data
            user_key = f"user:{user_email}"
            redis_user_client = get_redis_user_client()
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing dictation progress
            dictation_progress = json.loads(user_data.get(b'dictation_progress', b'{}').decode('utf-8'))

            # Filter progress for the specific channel
            channel_progress = []
            for key, value in dictation_progress.items():
                if key.startswith(f"{channel_id}:"):
                    video_id = key.split(':')[1]
                    overall_completion = value['overallCompletion']
                    channel_progress.append({
                        'videoId': video_id,
                        'overallCompletion': overall_completion
                    })

            logger.info(f"Retrieved all dictation progress for user: {user_email}, channel: {channel_id}")
            return {"channelId": channel_id, "progress": channel_progress}, 200

        except Exception as e:
            logger.error(f"Error retrieving channel dictation progress: {str(e)}")
            return {"error": "An error occurred while retrieving channel dictation progress"}, 500

@user_ns.route('/all')
class AllUsers(Resource):
    @jwt_required_and_refresh()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 500: 'Server Error'})
    def get(self):
        """Get all users' information"""
        try:
            # Get all user keys
            redis_user_client = get_redis_user_client()
            user_keys = redis_user_client.keys("user:*")
            users = []
            for key in user_keys:
                user_data = redis_user_client.hgetall(key)
                user_info = {k.decode('utf-8'): v.decode('utf-8') for k, v in user_data.items() if k != b'password'}
                users.append(user_info)

            logger.info(f"Retrieved information for {len(users)} users")
            return {"users": users}, 200

        except Exception as e:
            logger.error(f"Error retrieving all users' information: {str(e)}")
            return {"error": "An error occurred while retrieving users' information"}, 500

@user_ns.route('/all-progress')
class AllDictationProgress(Resource):
    @jwt_required_and_refresh()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get all dictation progress for the user with channel and video details"""
        try:
            user_email = get_jwt_identity()
            user_key = f"user:{user_email}"
            redis_user_client = get_redis_user_client()
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            dictation_progress = json.loads(user_data.get(b'dictation_progress', b'{}').decode('utf-8'))

            all_progress = []
            for key, value in dictation_progress.items():
                channel_id, video_id = key.split(':')
                
                # Get channel info
                channel_key = f"{CHANNEL_PREFIX}{channel_id}"
                redis_resource_client = get_redis_resource_client()
                channel_info = redis_resource_client.hgetall(channel_key)
                channel_name = channel_info.get(b'name', b'Unknown Channel').decode('utf-8')

                # Get video info
                video_list_key = f"{VIDEO_PREFIX}{channel_id}"
                videos_data = redis_resource_client.hget(video_list_key, 'videos')
                if videos_data:
                    videos = json.loads(videos_data.decode())
                    video_info = next((v for v in videos if v['video_id'] == video_id), None)
                    if video_info:
                        video_title = video_info.get('title', 'Unknown Video')
                        video_link = video_info.get('link', '')
                    else:
                        video_title = 'Unknown Video'
                        video_link = ''
                else:
                    video_title = 'Unknown Video'
                    video_link = ''

                progress_info = {
                    'channelId': channel_id,
                    'channelName': channel_name,
                    'videoId': video_id,
                    'videoTitle': video_title,
                    'videoLink': video_link,
                    'overallCompletion': value['overallCompletion']
                }
                all_progress.append(progress_info)

            logger.info(f"Retrieved all dictation progress for user: {user_email}")
            return {"progress": all_progress}, 200

        except Exception as e:
            logger.error(f"Error retrieving all dictation progress: {str(e)}")
            return {"error": f"An error occurred while retrieving all dictation progress: {str(e)}"}, 500

@user_ns.route('/duration')
class UserDuration(Resource):
    @jwt_required_and_refresh()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's total duration"""
        try:
            user_email = get_jwt_identity()
            redis_user_client = get_redis_user_client()

            user_key = f"user:{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            duration_data = json.loads(user_data.get(b'duration_data', b'{"duration": 0, "channels": {}}').decode('utf-8'))

            total_duration = duration_data.get('duration', 0)

            logger.info(f"Retrieved total duration for user: {user_email}")
            return {"totalDuration": total_duration}, 200

        except Exception as e:
            logger.error(f"Error retrieving total duration: {str(e)}")
            return {"error": f"An error occurred while retrieving total duration: {str(e)}"}, 500

@user_ns.route('/config')
class UserConfig(Resource):
    @jwt_required_and_refresh()
    @user_ns.expect(user_config_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self):
        """Update user's configuration"""
        try:
            user_email = get_jwt_identity()
            config_data = request.json

            user_key = f"user:{user_email}"
            redis_user_client = get_redis_user_client()
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Helper function to update nested dictionaries
            def update_nested_dict(d, u):
                for k, v in u.items():
                    if isinstance(v, dict):
                        d[k] = update_nested_dict(d.get(k, {}), v)
                    else:
                        d[k] = v
                return d

            # Update user data with new values
            for key, value in config_data.items():
                if isinstance(value, dict):
                    existing_value = user_data.get(key.encode(), b'{}').decode('utf-8')
                    try:
                        existing_dict = json.loads(existing_value)
                    except json.JSONDecodeError:
                        existing_dict = {}
                    updated_value = update_nested_dict(existing_dict, value)
                    redis_user_client.hset(user_key, key, json.dumps(updated_value))
                else:
                    redis_user_client.hset(user_key, key, json.dumps(value))

            logger.info(f"Updated configuration for user: {user_email}")
            
            # Fetch updated user data
            updated_user_data = redis_user_client.hgetall(user_key)
            updated_config = {}
            for k, v in updated_user_data.items():
                if k != b'password':
                    try:
                        updated_config[k.decode('utf-8')] = json.loads(v.decode('utf-8'))
                    except json.JSONDecodeError:
                        updated_config[k.decode('utf-8')] = v.decode('utf-8')
            
            return {"message": "User configuration updated successfully", "config": updated_config}, 200

        except json.JSONDecodeError as e:
            logger.error(f"JSON Decode Error: {str(e)}")
            return {"error": f"Invalid JSON format in configuration: {str(e)}"}, 400
        except Exception as e:
            logger.error(f"Error updating user configuration: {str(e)}")
            return {"error": f"An error occurred while updating user configuration: {str(e)}"}, 500

    @jwt_required_and_refresh()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's configuration"""
        try:
            user_email = get_jwt_identity()

            user_key = f"user:{user_email}"
            redis_user_client = get_redis_user_client()
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Convert all values to JSON, except for the password
            config = {k.decode('utf-8'): json.loads(v.decode('utf-8')) for k, v in user_data.items() if k != b'password'}

            logger.info(f"Retrieved configuration for user: {user_email}")
            return {"config": config}, 200

        except Exception as e:
            logger.error(f"Error retrieving user configuration: {str(e)}")
            return {"error": f"An error occurred while retrieving user configuration: {str(e)}"}, 500
