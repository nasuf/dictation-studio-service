from flask import request
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import get_jwt_identity
import json
import logging
from config import CHANNEL_PREFIX, USER_PREFIX, VIDEO_PREFIX
from utils import jwt_required_and_refresh
from datetime import datetime
from werkzeug.local import LocalProxy
from flask import current_app

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

            user_key = f"{USER_PREFIX}{user_email}"
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
            duration_data = json.loads(user_data.get(b'duration_data', b'{"duration": 0, "channels": {}, "date": {}}').decode('utf-8'))

            channel_id = progress_data['channelId']
            video_id = progress_data['videoId']
            duration_increment = progress_data['duration']  # This is now the increment

            # Update total duration
            duration_data['duration'] += duration_increment

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
            if today not in duration_data['date']:
                duration_data['date'][today] = 0
            duration_data['date'][today] += duration_increment

            redis_user_client.hset(user_key, 'duration_data', json.dumps(duration_data))

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

            user_key = f"{USER_PREFIX}{user_email}"
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

            user_key = f"{USER_PREFIX}{user_email}"
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
            user_key = f"{USER_PREFIX}{user_email}"
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
            user_keys = redis_user_client.keys(f"{USER_PREFIX}*")
            users = []
            for key in user_keys:
                user_data = redis_user_client.hgetall(key)
                user_info = {}
                for k, v in user_data.items():
                    key_str = k.decode('utf-8')
                    value_str = v.decode('utf-8')
                    try:
                        # Attempt to parse each field as JSON
                        user_info[key_str] = json.loads(value_str)
                    except json.JSONDecodeError:
                        # If parsing fails, keep it as a string
                        user_info[key_str] = value_str
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
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            dictation_progress = json.loads(user_data.get(b'dictation_progress', b'{}').decode('utf-8'))

            all_progress = []
            for key, value in dictation_progress.items():
                channel_id, video_id = key.split(':')
                
                # Get channel info
                channel_key = f"{CHANNEL_PREFIX}{channel_id}"
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
        """Get user's total duration and daily durations"""
        try:
            user_email = get_jwt_identity()

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            duration_data = json.loads(user_data.get(b'duration_data', b'{"duration": 0, "channels": {}, "date": {}}').decode('utf-8'))

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
    @jwt_required_and_refresh()
    @user_ns.expect(user_config_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self):
        """Update user's configuration"""
        try:
            user_email = get_jwt_identity()
            config_data = request.json

            user_key = f"{USER_PREFIX}{user_email}"
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
                if isinstance(value, (dict, list)):
                    existing_value = user_data.get(key.encode(), b'{}').decode('utf-8')
                    try:
                        existing_dict = json.loads(existing_value)
                    except json.JSONDecodeError:
                        existing_dict = {}
                    updated_value = update_nested_dict(existing_dict, value) if isinstance(value, dict) else value
                    redis_user_client.hset(user_key, key, json.dumps(updated_value))
                else:
                    redis_user_client.hset(user_key, key, value)

            logger.info(f"Updated configuration for user: {user_email}")
            
            # Fetch updated user data
            updated_user_data = redis_user_client.hgetall(user_key)
            updated_config = {}
            for k, v in updated_user_data.items():
                if k != b'password':
                    key = k.decode('utf-8')
                    value = v.decode('utf-8')
                    try:
                        updated_config[key] = json.loads(value)
                    except json.JSONDecodeError:
                        updated_config[key] = value
            
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

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Convert all values to JSON, except for the password
            config = {}
            for k, v in user_data.items():
                if k != b'password':
                    key = k.decode('utf-8')
                    value = v.decode('utf-8')
                    try:
                        config[key] = json.loads(value)
                    except json.JSONDecodeError:
                        config[key] = value

            logger.info(f"Retrieved configuration for user: {user_email}")
            return {"config": config}, 200

        except Exception as e:
            logger.error(f"Error retrieving user configuration: {str(e)}")
            return {"error": f"An error occurred while retrieving user configuration: {str(e)}"}, 500

@user_ns.route('/missed-words')
class MissedWords(Resource):
    @jwt_required_and_refresh()
    @user_ns.expect(missed_words_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self):
        """Add new missed words to user's missed words list"""
        try:
            user_email = get_jwt_identity()
            words_data = request.json

            if 'words' not in words_data or not isinstance(words_data['words'], list):
                return {"error": "Invalid input format. Expected 'words' array"}, 400

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing missed words or initialize empty set
            missed_words = set(json.loads(user_data.get(b'missed_words', b'[]').decode('utf-8')))
            
            # Add new words (set will automatically handle duplicates)
            missed_words.update(words_data['words'])
            
            # Convert back to list and store
            missed_words_list = list(missed_words)
            redis_user_client.hset(user_key, 'missed_words', json.dumps(missed_words_list))

            logger.info(f"Updated missed words for user: {user_email}")
            return {
                "message": "Missed words updated successfully",
                "missed_words": missed_words_list
            }, 200

        except Exception as e:
            logger.error(f"Error updating missed words: {str(e)}")
            return {"error": f"An error occurred while updating missed words: {str(e)}"}, 500

    @jwt_required_and_refresh()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's missed words list"""
        try:
            user_email = get_jwt_identity()
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Get missed words or return empty list if none exist
            missed_words = json.loads(user_data.get(b'missed_words', b'[]').decode('utf-8'))

            logger.info(f"Retrieved missed words for user: {user_email}")
            return {
                "missed_words": missed_words
            }, 200

        except Exception as e:
            logger.error(f"Error retrieving missed words: {str(e)}")
            return {"error": f"An error occurred while retrieving missed words: {str(e)}"}, 500

    @jwt_required_and_refresh()
    @user_ns.expect(missed_words_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def delete(self):
        """Delete specified words from user's missed words list"""
        try:
            user_email = get_jwt_identity()
            words_data = request.json

            if 'words' not in words_data or not isinstance(words_data['words'], list):
                return {"error": "Invalid input format. Expected 'words' array"}, 400

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing missed words
            missed_words = set(json.loads(user_data.get(b'missed_words', b'[]').decode('utf-8')))
            
            # Remove specified words
            missed_words = missed_words - set(words_data['words'])
            
            # Convert back to list and store
            missed_words_list = list(missed_words)
            redis_user_client.hset(user_key, 'missed_words', json.dumps(missed_words_list))

            logger.info(f"Deleted specified words for user: {user_email}")
            return {
                "message": "Words deleted successfully",
                "missed_words": missed_words_list
            }, 200

        except Exception as e:
            logger.error(f"Error deleting words: {str(e)}")
            return {"error": f"An error occurred while deleting words: {str(e)}"}, 500

