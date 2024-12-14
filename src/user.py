from flask import request
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import get_jwt_identity, jwt_required
import json
import logging
from config import CHANNEL_PREFIX, USER_PREFIX, VIDEO_PREFIX
from datetime import datetime
from werkzeug.local import LocalProxy
from flask import current_app
from cache import (
    get_channel_from_cache_or_redis,
    get_video_from_cache_or_redis,
    get_user_from_cache_or_redis,
    update_user_cache,
    get_user_progress_from_cache_or_redis,
    get_user_duration_from_cache_or_redis,
    get_user_config_from_cache_or_redis,
    get_user_missed_words_from_cache_or_redis
)

# Configure logging
logger = logging.getLogger(__name__)

# Create a namespace for user-related routes
user_ns = Namespace('user', description='User operations')

redis_user_client = LocalProxy(lambda: current_app.config['redis_user_client'])
redis_resource_client = LocalProxy(lambda: current_app.config['redis_resource_client'])

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

# Add new model definition for missed words
missed_words_model = user_ns.model('MissedWords', {
    'words': fields.List(fields.String, required=True, description='Array of missed words')
})

@user_ns.route('/progress')
class DictationProgress(Resource):
    @jwt_required()
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

            # Check if video exists using cache
            video_data = get_video_from_cache_or_redis(progress_data['channelId'], progress_data['videoId'], redis_resource_client)
            if not video_data:
                return {"error": "Video not found"}, 404

            # Get user data using cache
            user_data = get_user_from_cache_or_redis(user_email, redis_user_client)
            if not user_data:
                return {"error": "User not found"}, 404

            # Update dictation progress
            dictation_progress = user_data.get('dictation_progress', {})
            video_key = f"{progress_data['channelId']}:{progress_data['videoId']}"
            dictation_progress[video_key] = {
                'userInput': progress_data['userInput'],
                'currentTime': progress_data['currentTime'],
                'overallCompletion': progress_data['overallCompletion']
            }

            # Update duration data
            duration_data = user_data.get('duration_data', {"duration": 0, "channels": {}, "date": {}})
            channel_id = progress_data['channelId']
            video_id = progress_data['videoId']
            duration_increment = progress_data['duration']

            # Update total duration
            duration_data['duration'] = duration_data.get('duration', 0) + duration_increment

            # Update channel duration
            if channel_id not in duration_data['channels']:
                duration_data['channels'][channel_id] = {"duration": 0, "videos": {}}
            duration_data['channels'][channel_id]['duration'] += duration_increment

            # Update video duration
            if video_id not in duration_data['channels'][channel_id]['videos']:
                duration_data['channels'][channel_id]['videos'][video_id] = 0
            duration_data['channels'][channel_id]['videos'][video_id] += duration_increment

            # Update daily duration
            today = datetime.now().strftime('%Y-%m-%d')
            if 'date' not in duration_data:
                duration_data['date'] = {}
            if today not in duration_data['date']:
                duration_data['date'][today] = 0
            duration_data['date'][today] += duration_increment

            # Update Redis and cache
            user_key = f"{USER_PREFIX}{user_email}"
            redis_user_client.hset(user_key, 'dictation_progress', json.dumps(dictation_progress))
            redis_user_client.hset(user_key, 'duration_data', json.dumps(duration_data))

            # Update user cache with new data
            user_data['dictation_progress'] = dictation_progress
            user_data['duration_data'] = duration_data
            update_user_cache(user_email, user_data)

            logger.info(f"Updated progress and duration for user: {user_email}, channel: {channel_id}, video: {video_id}")
            return {
                "message": "Dictation progress and video duration updated successfully",
                "videoDuration": duration_data['channels'][channel_id]['videos'][video_id],
                "channelTotalDuration": duration_data['channels'][channel_id]['duration'],
                "totalDuration": duration_data['duration'],
                "dailyDuration": duration_data['date'][today]
            }, 200

        except Exception as e:
            logger.error(f"Error updating progress and duration: {str(e)}")
            return {"error": f"An error occurred while updating progress and duration: {str(e)}"}, 500

    @jwt_required()
    @user_ns.doc(params={'channelId': 'Channel ID', 'videoId': 'Video ID'}, 
                 responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's dictation progress for a specific video"""
        try:
            user_email = get_jwt_identity()
            channel_id = request.args.get('channelId')
            video_id = request.args.get('videoId')

            if not channel_id or not video_id:
                return {"error": "channelId and videoId are required"}, 400

            # Check if video exists using cache
            video_data = get_video_from_cache_or_redis(channel_id, video_id, redis_resource_client)
            if not video_data:
                return {"error": "Video not found"}, 404

            # Get user progress using cache
            dictation_progress = get_user_progress_from_cache_or_redis(user_email, redis_user_client)
            if not dictation_progress:
                return {
                    "channelId": channel_id, 
                    "videoId": video_id, 
                    "userInput": "", 
                    "currentTime": 0, 
                    "overallCompletion": 0
                }, 200

            video_key = f"{channel_id}:{video_id}"
            progress = dictation_progress.get(video_key, {
                "userInput": "", 
                "currentTime": 0, 
                "overallCompletion": 0
            })

            logger.info(f"Retrieved dictation progress for user: {user_email}, channel: {channel_id}, video: {video_id}")
            return {
                "channelId": channel_id,
                "videoId": video_id,
                "userInput": progress.get('userInput', ""),
                "currentTime": progress.get('currentTime', 0),
                "overallCompletion": progress.get('overallCompletion', 0)
            }, 200

        except Exception as e:
            logger.error(f"Error retrieving dictation progress: {str(e)}")
            return {"error": "An error occurred while retrieving dictation progress"}, 500

@user_ns.route('/progress/channel')
class ChannelDictationProgress(Resource):
    @jwt_required()
    @user_ns.doc(params={'channelId': 'Channel ID'}, 
                 responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's dictation progress for all videos in a specific channel"""
        try:
            user_email = get_jwt_identity()
            channel_id = request.args.get('channelId')

            if not channel_id:
                return {"error": "channelId is required"}, 400

            # Check if channel exists using cache
            channel_data = get_channel_from_cache_or_redis(channel_id, redis_resource_client)
            if not channel_data:
                return {"error": "Channel not found"}, 404

            # Get user progress using cache
            dictation_progress = get_user_progress_from_cache_or_redis(user_email, redis_user_client)
            
            pattern = f"{VIDEO_PREFIX}{channel_id}:*"
            video_keys = redis_resource_client.scan_iter(pattern)
            
            channel_progress = {}
            for video_key in video_keys:
                video_id = video_key.decode().split(':')[-1]
                progress_key = f"{channel_id}:{video_id}"
                progress = dictation_progress.get(progress_key, {})
                channel_progress[video_id] = progress.get('overallCompletion', 0)

            logger.info(f"Retrieved dictation progress for user: {user_email}, channel: {channel_id}")
            return {"channelId": channel_id, "progress": channel_progress}, 200

        except Exception as e:
            logger.error(f"Error retrieving dictation progress: {str(e)}")
            return {"error": "An error occurred while retrieving dictation progress"}, 500

@user_ns.route('/progress/<string:channel_id>')
class ChannelDictationProgress(Resource):
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self, channel_id):
        """Get all dictation progress for a specific channel"""
        try:
            user_email = get_jwt_identity()

            # Get user progress using cache
            dictation_progress = get_user_progress_from_cache_or_redis(user_email, redis_user_client)

            # Filter progress for the specific channel
            channel_progress = []
            for key, value in dictation_progress.items():
                if key.startswith(f"{channel_id}:"):
                    video_id = key.split(':')[1]
                    overall_completion = value.get('overallCompletion', 0)
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
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 500: 'Server Error'})
    def get(self):
        """Get all users' information"""
        try:
            # Get all user keys
            user_keys = redis_user_client.keys(f"{USER_PREFIX}*")
            users = []
            for key in user_keys:
                user_email = key.decode('utf-8').replace(USER_PREFIX, '')
                user_data = get_user_from_cache_or_redis(user_email, redis_user_client)
                if user_data:
                    users.append(user_data)

            logger.info(f"Retrieved information for {len(users)} users")
            return {"users": users}, 200

        except Exception as e:
            logger.error(f"Error retrieving all users' information: {str(e)}")
            return {"error": "An error occurred while retrieving users' information"}, 500

@user_ns.route('/all-progress')
class AllDictationProgress(Resource):
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get all dictation progress for the user with channel and video details"""
        try:
            user_email = get_jwt_identity()
            
            # Get user progress using cache
            dictation_progress = get_user_progress_from_cache_or_redis(user_email, redis_user_client)
            if not dictation_progress:
                return {"error": "User not found"}, 404

            all_progress = []
            for key, value in dictation_progress.items():
                channel_id, video_id = key.split(':')
                
                # Get channel info from cache
                channel_info = get_channel_from_cache_or_redis(channel_id, redis_resource_client)
                if not channel_info:
                    continue
                channel_name = channel_info.get('name')

                # Get video info from cache
                video_info = get_video_from_cache_or_redis(channel_id, video_id, redis_resource_client)
                if not video_info:
                    continue

                all_progress.append({
                    'channelId': channel_id,
                    'channelName': channel_name,
                    'videoId': video_id,
                    'videoTitle': video_info.get('title'),
                    'videoLink': video_info.get('link'),
                    'overallCompletion': value.get('overallCompletion', 0)
                })

            logger.info(f"Retrieved all dictation progress for user: {user_email}")
            return {"progress": all_progress}, 200

        except Exception as e:
            logger.error(f"Error retrieving all dictation progress: {str(e)}")
            return {"error": "An error occurred while retrieving all dictation progress"}, 500

@user_ns.route('/duration')
class UserDuration(Resource):
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's total duration and daily durations"""
        try:
            user_email = get_jwt_identity()

            # Get user duration using cache
            duration_data = get_user_duration_from_cache_or_redis(user_email, redis_user_client)
            if not duration_data:
                return {"error": "User not found"}, 404

            total_duration = duration_data.get('duration', 0)
            daily_durations = duration_data.get('date', {})

            logger.info(f"Retrieved total and daily durations for user: {user_email}")
            return {
                "totalDuration": total_duration,
                "dailyDurations": daily_durations
            }, 200

        except Exception as e:
            logger.error(f"Error retrieving total and daily durations: {str(e)}")
            return {"error": f"An error occurred while retrieving durations: {str(e)}"}, 500

@user_ns.route('/config')
class UserConfig(Resource):
    @jwt_required()
    @user_ns.expect(user_config_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self):
        """Update user's configuration"""
        try:
            user_email = get_jwt_identity()
            config_data = request.json

            # Get user data using cache
            user_data = get_user_from_cache_or_redis(user_email, redis_user_client)
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

            # Update user data
            user_key = f"{USER_PREFIX}{user_email}"
            for key, value in config_data.items():
                if isinstance(value, (dict, list)):
                    existing_value = user_data.get(key, {})
                    updated_value = update_nested_dict(existing_value, value) if isinstance(value, dict) else value
                    user_data[key] = updated_value
                    redis_user_client.hset(user_key, key, json.dumps(updated_value))
                else:
                    user_data[key] = value
                    redis_user_client.hset(user_key, key, value)

            # Update cache
            update_user_cache(user_email, user_data)

            logger.info(f"Updated configuration for user: {user_email}")
            return {"message": "User configuration updated successfully", "config": user_data}, 200

        except json.JSONDecodeError as e:
            logger.error(f"JSON Decode Error: {str(e)}")
            return {"error": f"Invalid JSON format in configuration: {str(e)}"}, 400
        except Exception as e:
            logger.error(f"Error updating user configuration: {str(e)}")
            return {"error": f"An error occurred while updating user configuration: {str(e)}"}, 500

    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's configuration"""
        try:
            user_email = get_jwt_identity()

            # Get user config using cache
            config = get_user_config_from_cache_or_redis(user_email, redis_user_client)
            if not config:
                return {"error": "User not found"}, 404

            logger.info(f"Retrieved configuration for user: {user_email}")
            return {"config": config}, 200

        except Exception as e:
            logger.error(f"Error retrieving user configuration: {str(e)}")
            return {"error": f"An error occurred while retrieving user configuration: {str(e)}"}, 500

@user_ns.route('/missed-words')
class MissedWords(Resource):
    @jwt_required()
    @user_ns.expect(missed_words_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self):
        """Add new missed words to user's missed words list"""
        try:
            user_email = get_jwt_identity()
            words_data = request.json

            if 'words' not in words_data or not isinstance(words_data['words'], list):
                return {"error": "Invalid input format. Expected 'words' array"}, 400

            # Get user data using cache
            user_data = get_user_from_cache_or_redis(user_email, redis_user_client)
            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing missed words or initialize empty set
            missed_words = set(user_data.get('missed_words', []))
            
            # Add new words
            missed_words.update(words_data['words'])
            
            # Convert back to list and store
            missed_words_list = list(missed_words)
            user_key = f"{USER_PREFIX}{user_email}"
            redis_user_client.hset(user_key, 'missed_words', json.dumps(missed_words_list))

            # Update cache
            user_data['missed_words'] = missed_words_list
            update_user_cache(user_email, user_data)

            logger.info(f"Updated missed words for user: {user_email}")
            return {
                "message": "Missed words updated successfully",
                "missed_words": missed_words_list
            }, 200

        except Exception as e:
            logger.error(f"Error updating missed words: {str(e)}")
            return {"error": f"An error occurred while updating missed words: {str(e)}"}, 500

    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's missed words list"""
        try:
            user_email = get_jwt_identity()
            
            # Get missed words using cache
            missed_words = get_user_missed_words_from_cache_or_redis(user_email, redis_user_client)
            if missed_words is None:
                return {"error": "User not found"}, 404

            logger.info(f"Retrieved missed words for user: {user_email}")
            return {
                "missed_words": missed_words
            }, 200

        except Exception as e:
            logger.error(f"Error retrieving missed words: {str(e)}")
            return {"error": f"An error occurred while retrieving missed words: {str(e)}"}, 500

    @jwt_required()
    @user_ns.expect(missed_words_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def delete(self):
        """Delete specified words from user's missed words list"""
        try:
            user_email = get_jwt_identity()
            words_data = request.json

            if 'words' not in words_data or not isinstance(words_data['words'], list):
                return {"error": "Invalid input format. Expected 'words' array"}, 400

            # Get user data using cache
            user_data = get_user_from_cache_or_redis(user_email, redis_user_client)
            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing missed words
            missed_words = set(user_data.get('missed_words', []))
            
            # Remove specified words
            missed_words = missed_words - set(words_data['words'])
            
            # Convert back to list and store
            missed_words_list = list(missed_words)
            user_key = f"{USER_PREFIX}{user_email}"
            redis_user_client.hset(user_key, 'missed_words', json.dumps(missed_words_list))

            # Update cache
            user_data['missed_words'] = missed_words_list
            update_user_cache(user_email, user_data)

            logger.info(f"Deleted specified words for user: {user_email}")
            return {
                "message": "Words deleted successfully",
                "missed_words": missed_words_list
            }, 200

        except Exception as e:
            logger.error(f"Error deleting words: {str(e)}")
            return {"error": f"An error occurred while deleting words: {str(e)}"}, 500

