from flask import request
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import get_jwt_identity, jwt_required
import json
import logging
from config import CHANNEL_PREFIX, USER_PREFIX, VIDEO_PREFIX
from datetime import datetime, timezone
import time
from utils import get_plan_name_by_duration, init_quota, update_user_plan, check_dictation_quota, register_dictation_video
import base64
import uuid
from redis_manager import RedisManager

redis_manager = RedisManager()
redis_resource_client = redis_manager.get_resource_client()
redis_user_client = redis_manager.get_user_client()

# Configure logging
logger = logging.getLogger(__name__)

# Create a namespace for user-related routes
user_ns = Namespace('user', description='User operations')

# Define model for dictation progress
dictation_progress_model = user_ns.model('DictationProgress', {
    'channelId': fields.String(required=True, description='Channel ID'),
    'videoId': fields.String(required=True, description='Video ID'),
    'userInput': fields.Raw(required=True, description='User input for dictation'),
    'currentTime': fields.Integer(required=True, description='Current timestamp'),
    'overallCompletion': fields.Integer(required=True, description='Overall completion percentage'),
    'duration': fields.Integer(required=True, description='Duration in milliseconds')
})

# Add channel recommendation model
channel_recommendation_model = user_ns.model('ChannelRecommendation', {
    'link': fields.String(required=True, description='YouTube channel link'),
    'language': fields.String(required=True, description='Channel language')
})

# Add response model for channel recommendations
channel_recommendation_response_model = user_ns.model('ChannelRecommendationResponse', {
    'id': fields.String(description='Recommendation ID'),
    'name': fields.String(description='Channel name'),
    'link': fields.String(description='Channel link'),
    'imageUrl': fields.String(description='Channel image URL'),
    'submittedAt': fields.String(description='Submission timestamp'),
    'status': fields.String(description='Recommendation status (pending, approved, rejected)'),
    'language': fields.String(description='Channel language')
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

# Add new structured missed words model
structured_missed_words_model = user_ns.model('StructuredMissedWords', {
    'words': fields.Raw(required=True, description='Object of missed words grouped by language')
})

# Add feedback message model
feedback_message_model = user_ns.model('FeedbackMessage', {
    'message': fields.String(required=True, description='Feedback message content'),
    'type': fields.String(required=True, description='Feedback type (bug, feature, other)'),
    'email': fields.String(required=False, description='Contact email for follow-up')
})

# Add video error report model
video_error_report_model = user_ns.model('VideoErrorReport', {
    'channelId': fields.String(required=True, description='Channel ID'),
    'videoId': fields.String(required=True, description='Video ID'),
    'videoTitle': fields.String(required=True, description='Video title'),
    'errorType': fields.String(required=True, description='Type of error (transcript_error, timing_error, missing_content, other)'),
    'description': fields.String(required=True, description='Detailed description of the error')
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

            video_key = f"{VIDEO_PREFIX}{progress_data['channelId']}:{progress_data['videoId']}"
            if not redis_resource_client.exists(video_key):
                return {"error": "Video not found"}, 404

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Update dictation progress
            dictation_progress = json.loads(user_data.get('dictation_progress', '{}'))
            video_key = f"{progress_data['channelId']}:{progress_data['videoId']}"
            dictation_progress[video_key] = {
                'userInput': progress_data['userInput'],
                'currentTime': progress_data['currentTime'],
                'overallCompletion': progress_data['overallCompletion']
            }
            redis_user_client.hset(user_key, 'dictation_progress', json.dumps(dictation_progress))

            # Update structured duration data
            duration_data = json.loads(user_data.get('duration_data', '{"duration": 0, "channels": {}, "date": {}}'))

            channel_id = progress_data['channelId']
            video_id = progress_data['videoId']
            duration_increment = progress_data['duration']

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

            # Update daily duration using epoch milliseconds for start of day in UTC
            now_utc = datetime.now(timezone.utc)
            today_start_utc = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
            today_timestamp = str(int(today_start_utc.timestamp() * 1000))
            
            if today_timestamp not in duration_data['date']:
                duration_data['date'][today_timestamp] = 0
            duration_data['date'][today_timestamp] += duration_increment

            redis_user_client.hset(user_key, 'duration_data', json.dumps(duration_data))

            logger.info(f"Updated progress and duration for user: {user_email}, channel: {channel_id}, video: {video_id}")
            return {
                "message": "Dictation progress and video duration updated successfully",
                "videoDuration": duration_data['channels'][channel_id]['videos'][video_id],
                "channelTotalDuration": duration_data['channels'][channel_id]['duration'],
                "totalDuration": duration_data['duration'],
                "dailyDuration": duration_data['date'][today_timestamp]
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

            video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
            if not redis_resource_client.exists(video_key):
                return {"error": "Video not found"}, 404

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            dictation_progress = json.loads(user_data.get('dictation_progress', '{}'))
            video_key = f"{channel_id}:{video_id}"
            progress = dictation_progress.get(video_key)

            if not progress:
                return {
                    "channelId": channel_id, 
                    "videoId": video_id, 
                    "userInput": "", 
                    "currentTime": 0, 
                    "overallCompletion": 0
                }, 200

            logger.info(f"Retrieved dictation progress for user: {user_email}, channel: {channel_id}, video: {video_id}")
            return {
                "channelId": channel_id,
                "videoId": video_id,
                "userInput": progress['userInput'],
                "currentTime": progress['currentTime'],
                "overallCompletion": progress['overallCompletion']
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

            channel_key = f"{CHANNEL_PREFIX}{channel_id}"
            if not redis_resource_client.exists(channel_key):
                return {"error": "Channel not found"}, 404

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            pattern = f"{VIDEO_PREFIX}{channel_id}:*"
            video_keys = redis_resource_client.scan_iter(pattern)
            
            dictation_progress = json.loads(user_data.get('dictation_progress', '{}'))
            
            channel_progress = {}
            for video_key in video_keys:
                video_id = video_key.split(':')[-1]
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
            # Get user email from JWT token
            user_email = get_jwt_identity()

            # Get existing user data
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing dictation progress
            dictation_progress = json.loads(user_data.get('dictation_progress', '{}'))

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
    @jwt_required()
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
                    key_str = k
                    value_str = v
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
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get all dictation progress for the user with channel and video details"""
        try:
            user_email = get_jwt_identity()
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            dictation_progress = json.loads(user_data.get('dictation_progress', '{}'))

            all_progress = []
            for key, value in dictation_progress.items():
                # value['userInput'] could be {}, if so then skip it
                if not value['userInput']:
                    continue
                channel_id, video_id = key.split(':')
                
                channel_key = f"{CHANNEL_PREFIX}{channel_id}"
                channel_info = redis_resource_client.hgetall(channel_key)
                if not channel_info:
                    continue
                channel_name = channel_info['name']

                video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
                video_info = redis_resource_client.hgetall(video_key)
                if not video_info:
                    continue

                all_progress.append({
                    'channelId': channel_id,
                    'channelName': channel_name,
                    'videoId': video_id,
                    'videoTitle': video_info['title'],
                    'videoLink': video_info['link'],
                    'overallCompletion': value['overallCompletion']
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

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            duration_data = json.loads(user_data.get('duration_data', '{"duration": 0, "channels": {}, "date": {}}'))

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
                    existing_value = user_data.get(key, '{}')
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
                if k != 'password':
                    key = k
                    value = v
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

    @jwt_required()
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
                if k != 'password':
                    key = k
                    value = v
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

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Detect language for each word
            def detect_word_language(word):
                if not word:
                    return "other"
                    
                char_code = ord(word[0])
                
                # Chinese character range
                if 0x4e00 <= char_code <= 0x9fff:
                    return "zh"
                # Japanese character ranges (Hiragana, Katakana)
                elif (0x3040 <= char_code <= 0x309f) or (0x30a0 <= char_code <= 0x30ff):
                    return "ja"
                # Korean character range (Hangul)
                elif 0xac00 <= char_code <= 0xd7a3:
                    return "ko"
                # Basic Latin alphabet and common English characters
                elif (0x0020 <= char_code <= 0x007f) or (0x0080 <= char_code <= 0x00ff):
                    return "en"
                # Other languages/scripts
                else:
                    return "other"

            # Get existing missed words structure or initialize empty object
            missed_words_json = user_data.get('missed_words', '[]')
            try:
                # Try to parse as structured object first
                missed_words_struct = json.loads(missed_words_json)
                if not isinstance(missed_words_struct, dict):
                    # If it's the old list format, convert to structured format
                    old_words = missed_words_struct if isinstance(missed_words_struct, list) else []
                    missed_words_struct = {}
                    
                    # Group existing words by language
                    for word in old_words:
                        lang = detect_word_language(word)
                        if lang not in missed_words_struct:
                            missed_words_struct[lang] = []
                        if word not in missed_words_struct[lang]:
                            missed_words_struct[lang].append(word)
            except json.JSONDecodeError:
                # Initialize empty structure if parsing fails
                missed_words_struct = {}
            
            # Add new words to appropriate language categories
            for word in words_data['words']:
                lang = detect_word_language(word)
                if lang not in missed_words_struct:
                    missed_words_struct[lang] = []
                if word not in missed_words_struct[lang]:
                    missed_words_struct[lang].append(word)
            
            # Store the updated structure
            redis_user_client.hset(user_key, 'missed_words', json.dumps(missed_words_struct))

            # Create flattened list for backward compatibility
            flattened_words = [word for lang_words in missed_words_struct.values() for word in lang_words]

            logger.info(f"Updated missed words for user: {user_email}")
            return {
                "message": "Missed words updated successfully",
                "missed_words": flattened_words,
                "structured_missed_words": missed_words_struct
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
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Get missed words data
            missed_words_json = user_data.get('missed_words', '[]')
            
            try:
                # Try to parse as structured object first
                missed_words = json.loads(missed_words_json)
                
                # If it's not a dictionary (old format), convert to structured format
                if not isinstance(missed_words, dict):
                    old_words = missed_words if isinstance(missed_words, list) else []
                    structured_words = {}
                    
                    # Function to detect language
                    def detect_word_language(word):
                        if not word:
                            return "other"
                            
                        char_code = ord(word[0])
                        
                        # Chinese character range
                        if 0x4e00 <= char_code <= 0x9fff:
                            return "zh"
                        # Japanese character ranges (Hiragana, Katakana)
                        elif (0x3040 <= char_code <= 0x309f) or (0x30a0 <= char_code <= 0x30ff):
                            return "ja"
                        # Korean character range (Hangul)
                        elif 0xac00 <= char_code <= 0xd7a3:
                            return "ko"
                        # Basic Latin alphabet and common English characters
                        elif (0x0020 <= char_code <= 0x007f) or (0x0080 <= char_code <= 0x00ff):
                            return "en"
                        # Other languages/scripts
                        else:
                            return "other"
                    
                    # Group words by language
                    for word in old_words:
                        lang = detect_word_language(word)
                        if lang not in structured_words:
                            structured_words[lang] = []
                        structured_words[lang].append(word)
                    
                    # Save the structured format
                    redis_user_client.hset(user_key, 'missed_words', json.dumps(structured_words))
                    missed_words = structured_words
                
                # Create flattened list for backward compatibility
                flattened_words = [word for lang_words in missed_words.values() for word in lang_words]
                
                logger.info(f"Retrieved missed words for user: {user_email}")
                return {
                    "missed_words": flattened_words,
                    "structured_missed_words": missed_words
                }, 200
                
            except json.JSONDecodeError:
                # Return empty structures if parsing fails
                logger.warning(f"Failed to parse missed words for user: {user_email}")
                return {
                    "missed_words": [],
                    "structured_missed_words": {}
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

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Delete words from structured format
            words_to_delete = set(words_data['words'])
            
            # Get current missed words structure
            missed_words_json = user_data.get('missed_words', '[]')
            
            try:
                missed_words_struct = json.loads(missed_words_json)
                
                # If it's the old list format, handle differently
                if not isinstance(missed_words_struct, dict):
                    old_words = set(missed_words_struct if isinstance(missed_words_struct, list) else [])
                    updated_words = list(old_words - words_to_delete)
                    redis_user_client.hset(user_key, 'missed_words', json.dumps(updated_words))
                    return {
                        "message": "Words deleted successfully",
                        "missed_words": updated_words,
                        "structured_missed_words": {}
                    }, 200
                
                # Remove words from each language category
                for lang in missed_words_struct:
                    missed_words_struct[lang] = [word for word in missed_words_struct[lang] 
                                                if word not in words_to_delete]
                
                # Clean up empty language categories
                missed_words_struct = {lang: words for lang, words in missed_words_struct.items() if words}
                
                # Save the updated structure
                redis_user_client.hset(user_key, 'missed_words', json.dumps(missed_words_struct))
                
                # Create flattened list for backward compatibility
                flattened_words = [word for lang_words in missed_words_struct.values() for word in lang_words]
                
                logger.info(f"Deleted specified words for user: {user_email}")
                return {
                    "message": "Words deleted successfully",
                    "missed_words": flattened_words,
                    "structured_missed_words": missed_words_struct
                }, 200
                
            except json.JSONDecodeError:
                # Return empty if parsing fails
                redis_user_client.hset(user_key, 'missed_words', json.dumps({}))
                return {
                    "message": "Words deleted successfully",
                    "missed_words": [],
                    "structured_missed_words": {}
                }, 200
                
        except Exception as e:
            logger.error(f"Error deleting words: {str(e)}")
            return {"error": f"An error occurred while deleting words: {str(e)}"}, 500

@user_ns.route('/update-duration')
class UpdateUserDuration(Resource):
    @jwt_required()
    @user_ns.expect(user_ns.model('UpdateDuration', {
        'emails': fields.List(fields.String, required=True, description='List of user emails to update'),
        'duration': fields.Integer(required=True, description='Membership duration in days')
    }))
    def post(self):
        """Update user membership duration (Admin only)"""
        try:
            # Get admin identity
            admin_email = get_jwt_identity()
            
            # Check if user is admin
            admin_key = f"{USER_PREFIX}{admin_email}"
            admin_data = redis_user_client.hgetall(admin_key)
            
            if not admin_data:
                logger.error(f"Admin user not found: {admin_email}")
                return {"error": "Admin user not found"}, 404
                
            # Ensure role information is correctly decoded
            admin_role = None
            if 'role' in admin_data:
                admin_role = admin_data['role']
            
            logger.info(f"User {admin_email} with role {admin_role} attempting to update durations")
            
            # Use case-insensitive comparison here
            if admin_role.lower() != 'admin':
                logger.error(f"User {admin_email} with role {admin_role} attempted admin action")
                return {"error": "Only admin users can update user durations"}, 403
            
            # Get request data
            data = request.json
            emails = data.get('emails', [])
            days_duration = data.get('duration')
            
            logger.info(f"Admin {admin_email} updating duration for users: {emails} to {days_duration} days")
            
            if not emails:
                return {"error": "No emails provided"}, 400
                
            if days_duration is None:
                return {"error": "Duration is required"}, 400
            
            # Use utility method to get plan name
            plan_name = get_plan_name_by_duration(days_duration)
            
            # Update plan for each user
            results = []
            for email in emails:
                try:
                    # Update user plan
                    plan_data = update_user_plan(email, plan_name, days_duration, False)
                    results.append({
                        "email": email,
                        "success": True,
                        "plan": plan_data
                    })
                except Exception as e:
                    logger.error(f"Error updating plan for user {email}: {str(e)}")
                    results.append({
                        "email": email,
                        "success": False,
                        "error": str(e)
                    })
            
            return {
                "message": f"Updated {sum(1 for r in results if r['success'])} of {len(results)} users",
                "results": results
            }, 200
            
        except Exception as e:
            logger.error(f"Error updating user durations: {str(e)}")
            return {"error": f"An error occurred while updating user durations: {str(e)}"}, 500

@user_ns.route('/init-quota')
class InitQuota(Resource):
    @jwt_required()
    def post(self):
        """Initialize user's dictation quota"""
        try:
            user_email = get_jwt_identity()
            quota = init_quota(user_email)
            return quota, 200
        except Exception as e:
            logger.error(f"Error initializing user's dictation quota: {str(e)}")
            return {"error": f"An error occurred while initializing user's dictation quota: {str(e)}"}, 500

@user_ns.route('/dictation_quota')
class DictationQuota(Resource):
    @jwt_required()
    @user_ns.doc(params={'channelId': 'Channel ID', 'videoId': 'Video ID'}, 
                 responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 500: 'Server Error'})
    def get(self):
        """Check user's dictation quota"""
        try:
            user_email = get_jwt_identity()
            channel_id = request.args.get('channelId')
            video_id = request.args.get('videoId')
            
            if not channel_id or not video_id:
                return {"error": "Missing channelId or videoId"}, 400
            
            quota_info = check_dictation_quota(user_email, channel_id, video_id)
            return quota_info, 200
            
        except Exception as e:
            logger.error(f"Error checking dictation quota: {str(e)}")
            return {"error": f"An error occurred: {str(e)}"}, 500

@user_ns.route('/register_dictation')
class RegisterDictation(Resource):
    @jwt_required()
    @user_ns.expect(user_ns.model('RegisterDictation', {
        'channelId': fields.String(required=True, description='Channel ID'),
        'videoId': fields.String(required=True, description='Video ID')
    }))
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 403: 'Quota Exceeded', 500: 'Server Error'})
    def post(self):
        """Register a video to user's dictation quota and initialize empty progress"""
        try:
            user_email = get_jwt_identity()
            data = request.json
            
            channel_id = data.get('channelId')
            video_id = data.get('videoId')
            
            if not channel_id or not video_id:
                return {"error": "Missing channelId or videoId"}, 400
            
            # Check if video exists
            video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
            if not redis_resource_client.exists(video_key):
                return {"error": "Video not found"}, 404
            
            # Register the video to user's quota
            success = register_dictation_video(user_email, channel_id, video_id)
            
            if success:
                # Initialize empty dictation progress for this video
                user_key = f"{USER_PREFIX}{user_email}"
                user_data = redis_user_client.hgetall(user_key)
                
                if user_data:
                    # Get existing dictation progress or initialize empty object
                    dictation_progress = json.loads(user_data.get('dictation_progress', '{}'))
                    progress_key = f"{channel_id}:{video_id}"
                    
                    # Initialize empty progress if not exists
                    if progress_key not in dictation_progress:
                        dictation_progress[progress_key] = {
                            'userInput': {},  # Empty user input
                            'currentTime': int(time.time() * 1000),  # Current Unix epoch milliseconds
                            'overallCompletion': 0  # 0% completion
                        }
                        
                        # Save updated dictation progress
                        redis_user_client.hset(user_key, 'dictation_progress', json.dumps(dictation_progress))
                        
                        logger.info(f"Initialized empty dictation progress for user: {user_email}, channel: {channel_id}, video: {video_id}")
                
                return {"status": "success", "message": "Video registered and progress initialized"}, 200
            else:
                return {"error": "Failed to register video, quota exceeded"}, 403
                
        except Exception as e:
            logger.error(f"Error registering dictation video: {str(e)}")
            return {"error": f"An error occurred: {str(e)}"}, 500

@user_ns.route('/channel-recommendations')
class ChannelRecommendations(Resource):
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's channel recommendations"""
        try:
            user_email = get_jwt_identity()
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing channel recommendations or initialize empty array
            recommendations_json = user_data.get('channel_recommendations', '[]')
            try:
                recommendations = json.loads(recommendations_json)
            except json.JSONDecodeError:
                recommendations = []
            
            logger.info(f"Retrieved channel recommendations for user: {user_email}")
            return recommendations, 200
            
        except Exception as e:
            logger.error(f"Error retrieving channel recommendations: {str(e)}")
            return {"error": f"An error occurred while retrieving channel recommendations: {str(e)}"}, 500
    
    @jwt_required()
    @user_ns.expect(channel_recommendation_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self):
        """Submit a new channel recommendation"""
        try:
            user_email = get_jwt_identity()
            recommendation_data = request.json

            if 'link' not in recommendation_data:
                return {"error": "Channel link is required"}, 400
                
            if 'language' not in recommendation_data:
                return {"error": "Channel language is required"}, 400

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing channel recommendations or initialize empty array
            recommendations_json = user_data.get('channel_recommendations', '[]')
            try:
                recommendations = json.loads(recommendations_json)
            except json.JSONDecodeError:
                recommendations = []
            
            # Create a new recommendation
            new_recommendation = {
                'id': f"REC_{str(int(time.time()*1000))}",
                'name': recommendation_data['name'],
                'link': recommendation_data['link'],
                'submittedAt': int(datetime.now(timezone.utc).timestamp() * 1000),
                'status': 'pending',
                'language': recommendation_data['language']
            }
            
            # Add to recommendations
            recommendations.append(new_recommendation)
            
            # Save updated recommendations
            redis_user_client.hset(user_key, 'channel_recommendations', json.dumps(recommendations))
            
            logger.info(f"Channel recommendation submitted by user: {user_email}")
            return {"message": "Channel recommendation submitted successfully", "recommendation": new_recommendation}, 200
            
        except Exception as e:
            logger.error(f"Error submitting channel recommendation: {str(e)}")
            return {"error": f"An error occurred while submitting channel recommendation: {str(e)}"}, 500


@user_ns.route('/channel-recommendations/admin')
class AdminChannelRecommendations(Resource):
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 403: 'Forbidden', 500: 'Server Error'})
    def get(self):
        """Get all channel recommendations from all users (Admin only)"""
        try:
            # Get admin identity
            admin_email = get_jwt_identity()
            
            # Check if user is admin
            admin_key = f"{USER_PREFIX}{admin_email}"
            admin_data = redis_user_client.hgetall(admin_key)
            
            if not admin_data:
                logger.error(f"Admin user not found: {admin_email}")
                return {"error": "Admin user not found"}, 404
                
            # Ensure role information is correctly decoded
            admin_role = None
            if 'role' in admin_data:
                admin_role = admin_data['role']
            
            # Use case-insensitive comparison
            if not admin_role or admin_role.lower() != 'admin':
                logger.error(f"User {admin_email} with role {admin_role} attempted admin action")
                return {"error": "Only admin users can access all channel recommendations"}, 403
            
            # Get all user keys
            user_keys = redis_user_client.keys(f"{USER_PREFIX}*")
            all_recommendations = []
            
            for user_key in user_keys:
                try:
                    user_email = user_key.replace(USER_PREFIX, '')
                    user_data = redis_user_client.hgetall(user_key)
                    
                    if 'channel_recommendations' in user_data:
                        recommendations_json = user_data['channel_recommendations']
                        recommendations = json.loads(recommendations_json)
                        
                        # Add user email to each recommendation
                        for recommendation in recommendations:
                            recommendation['userEmail'] = user_email
                        
                        all_recommendations.extend(recommendations)
                except Exception as e:
                    logger.error(f"Error processing recommendations for user {user_key}: {str(e)}")
                    continue
            
            # Sort by submission date (newest first)
            all_recommendations.sort(key=lambda x: x.get('submittedAt', ''), reverse=True)
            
            logger.info(f"Admin {admin_email} retrieved all channel recommendations")
            return all_recommendations, 200
            
        except Exception as e:
            logger.error(f"Error retrieving all channel recommendations: {str(e)}")
            return {"error": f"An error occurred while retrieving all channel recommendations: {str(e)}"}, 500


@user_ns.route('/channel-recommendations/<string:recommendation_id>')
class ManageChannelRecommendation(Resource):
    @jwt_required()
    @user_ns.expect(user_ns.model('UpdateRecommendation', {
        'status': fields.String(required=True, description='Recommendation status (pending, approved, rejected)'),
        'name': fields.String(required=False, description='Channel name'),
        'imageUrl': fields.String(required=False, description='Channel image URL'),
        'channelId': fields.String(required=False, description='Channel ID'),
        'visibility': fields.String(required=False, description='Channel visibility'),
        'link': fields.String(required=False, description='Channel link'),
        'language': fields.String(required=False, description='Channel language'),
        'reason': fields.String(required=False, description='Rejected reason')
    }))
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 403: 'Forbidden', 404: 'Not Found', 500: 'Server Error'})
    def put(self, recommendation_id):
        """Update a channel recommendation (Admin only)"""
        try:
            # Get admin identity
            admin_email = get_jwt_identity()
            
            # Check if user is admin
            admin_key = f"{USER_PREFIX}{admin_email}"
            admin_data = redis_user_client.hgetall(admin_key)
            
            if not admin_data:
                logger.error(f"Admin user not found: {admin_email}")
                return {"error": "Admin user not found"}, 404
                
            # Ensure role information is correctly decoded
            admin_role = None
            if 'role' in admin_data:
                admin_role = admin_data['role']
            
            # Use case-insensitive comparison
            if not admin_role or admin_role.lower() != 'admin':
                logger.error(f"User {admin_email} with role {admin_role} attempted admin action")
                return {"error": "Only admin users can update channel recommendations"}, 403
            
            # Get update data
            update_data = request.json
            
            # Find the recommendation in all users
            user_keys = redis_user_client.keys(f"{USER_PREFIX}*")
            target_user_key = None
            updated_recommendation = None
            
            for user_key in user_keys:
                user_data = redis_user_client.hgetall(user_key)
                
                if 'channel_recommendations' in user_data:
                    recommendations_json = user_data['channel_recommendations']
                    recommendations = json.loads(recommendations_json)
                    
                    for i, recommendation in enumerate(recommendations):
                        if recommendation.get('id') == recommendation_id:
                            # Found the recommendation to update
                            target_user_key = user_key
                            
                            # Update the recommendation with new data
                            if 'status' in update_data:
                                recommendations[i]['status'] = update_data['status']
                            
                            updated_recommendation = recommendations[i]

                            # Save channel info into redis if the recommendation is approved
                            if update_data['status'] == 'approved':
                                channel_key = f"{CHANNEL_PREFIX}{update_data['channelId']}"
                                if redis_resource_client.exists(channel_key):
                                    logger.error(f"Channel {update_data['channelId']} already exists in redis")
                                    recommendations[i]['status'] = 'rejected'
                                    recommendations[i]['reason'] = 'Channel already exists'
                                    redis_user_client.hset(target_user_key, 'channel_recommendations', json.dumps(recommendations))
                                    return {"message": "Recommendation rejected successfully", "recommendation": recommendations[i]}, 200
                                else:
                                    recommendations[i]['status'] = 'approved'
                                    redis_user_client.hset(target_user_key, 'channel_recommendations', json.dumps(recommendations))

                                    channel_info = {
                                        'id': update_data['channelId'],
                                        'name': update_data['name'],
                                        'image_url': update_data['imageUrl'],
                                        'visibility': update_data['visibility'],
                                        'link': update_data['link'],
                                        'language': update_data['language']
                                    }
                                    redis_resource_client.hset(channel_key, mapping=channel_info)
                                    logger.info(f"Admin {admin_email} saved channel info for channel {update_data['channelId']} according to recommendation {recommendation_id} by user {target_user_key}")
                            else:
                                recommendations[i]['status'] = 'rejected'
                                if 'reason' in update_data: 
                                    recommendations[i]['reason'] = update_data['reason']
                                else:
                                    recommendations[i]['reason'] = 'No reason provided'
                                redis_user_client.hset(target_user_key, 'channel_recommendations', json.dumps(recommendations))
                                return {"message": "Recommendation rejected successfully", "recommendation": recommendations[i]}, 200
                            break
                
                if target_user_key:
                    break
            
            if not target_user_key:
                return {"error": "Recommendation not found"}, 404
            
            return {"message": "Recommendation approved successfully", "recommendation": updated_recommendation}, 200
            
        except Exception as e:
            logger.error(f"Error updating channel recommendation: {str(e)}")
            return {"error": f"An error occurred while updating channel recommendation: {str(e)}"}, 500

@user_ns.route('/feedback')
class UserFeedback(Resource):
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self):
        """Submit user feedback message"""
        try:
            user_email = get_jwt_identity()
            
            # Get form data
            message = request.form.get('message')
            
            if not message:
                return {"error": "Feedback message is required"}, 400
                
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404
            
            # Get user name
            user_name = None
            if 'username' in user_data:
                user_name = user_data['username']

            # Get existing feedback messages or initialize empty array
            feedback_json = user_data.get('feedback_messages', '[]')
            try:
                feedback_messages = json.loads(feedback_json)
            except json.JSONDecodeError:
                feedback_messages = []
            
            # Process uploaded files, store in db1 as attachment:<id>
            image_ids = []
            if 'images' in request.files:
                files = request.files.getlist('images')
                for file in files:
                    if file and file.filename:
                        file_content = file.read()
                        base64_content = base64.b64encode(file_content).decode('utf-8')  # 确保解码为字符串
                        mime_type = file.content_type or 'application/octet-stream'
                        data_url = f"data:{mime_type};base64,{base64_content}"
                        # Generate a unique id for the image
                        img_id = str(uuid.uuid4())
                        # Store using dedicated attachment client (no need to switch databases)
                        redis_resource_client.set(f'attachment:{img_id}', data_url)
                        image_ids.append(img_id)
            
            # Use epoch UTC milliseconds for timestamp
            timestamp = int(time.time() * 1000)
            
            # Create a new feedback message, store only image ids
            new_feedback = {
                'id': f"FB_{timestamp}",
                'sender': user_name,
                'senderType': "user",
                'message': message,
                'email': user_email,
                'timestamp': timestamp,
                'images': image_ids
            }
            
            # Add to feedback messages
            feedback_messages.append(new_feedback)
            
            # Save updated feedback messages
            redis_user_client.hset(user_key, 'feedback_messages', json.dumps(feedback_messages))
            
            logger.info(f"Feedback message submitted by user: {user_email}")
            return {"message": "Feedback message submitted successfully", "feedback": new_feedback}, 200
            
        except Exception as e:
            logger.error(f"Error submitting feedback message: {str(e)}")
            return {"error": f"An error occurred while submitting feedback message: {str(e)}"}, 500

    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def get(self):
        """Get user's feedback messages"""
        try:
            user_email = request.args.get('userEmail')
            if not user_email:
                user_email = get_jwt_identity()
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing feedback messages or initialize empty array
            feedback_json = user_data.get('feedback_messages', '[]')
            try:
                # Handle both string and bytes types
                feedback_messages = json.loads(feedback_json)
            except json.JSONDecodeError:
                feedback_messages = []
            
            # For each feedback, if images is a list of ids, fetch from db1
            for fb in feedback_messages:
                if 'images' in fb and isinstance(fb['images'], list):
                    image_urls = []
                    for img_id in fb['images']:
                        try:
                            data_url = redis_resource_client.get(f'attachment:{img_id}')
                            if data_url:
                                image_urls.append(data_url)
                        except Exception as e:
                            logger.error(f"Error fetching image {img_id}: {str(e)}")
                    fb['images'] = image_urls
            
            logger.info(f"Retrieved feedback messages for user: {user_email}")
            return feedback_messages, 200
            
        except Exception as e:
            logger.error(f"Error retrieving feedback messages: {str(e)}")
            return {"error": f"An error occurred while retrieving feedback messages: {str(e)}"}, 500

@user_ns.route('/feedback/admin/list')
class AdminFeedback(Resource):
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 403: 'Forbidden', 500: 'Server Error'})
    def get(self):
        """Get all feedback messages from all users (Admin only)"""
        try:
            # Get admin identity
            admin_email = get_jwt_identity()
            
            # Check if user is admin
            admin_key = f"{USER_PREFIX}{admin_email}"
            admin_data = redis_user_client.hgetall(admin_key)
            
            if not admin_data:
                logger.error(f"Admin user not found: {admin_email}")
                return {"error": "Admin user not found"}, 404
                
            # Ensure role information is correctly decoded
            admin_role = None
            if 'role' in admin_data:
                admin_role = admin_data['role']
            
            # Use case-insensitive comparison
            if not admin_role or admin_role.lower() != 'admin':
                logger.error(f"User {admin_email} with role {admin_role} attempted admin action")
                return {"error": "Only admin users can access all feedback messages"}, 403
            
            # Get all user keys
            user_keys = redis_user_client.keys(f"{USER_PREFIX}*")
            feedback_user_list = []
            
            for user_key in user_keys:
                try:
                    user_email = user_key.replace(USER_PREFIX, '')
                    user_data = redis_user_client.hgetall(user_key)
                    
                    if 'feedback_messages' in user_data:
                        feedback_json = user_data['feedback_messages']
                        feedback_messages = json.loads(feedback_json)
                        feedback_messages.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
                        # then get the latest timestamp
                        latest_timestamp = feedback_messages[0].get('timestamp', 0)
                        feedback_user_list.append({
                            'email': user_email,
                            'timestamp': latest_timestamp
                        })
                except Exception as e:
                    logger.error(f"Error processing feedback for user {user_key}: {str(e)}")
                    continue
            
            logger.info(f"Admin {admin_email} retrieved all feedback messages")
            return feedback_user_list, 200
            
        except Exception as e:
            logger.error(f"Error retrieving all feedback messages: {str(e)}")
            return {"error": f"An error occurred while retrieving all feedback messages: {str(e)}"}, 500

@user_ns.route('/feedback/admin')
class AdminSendFeedback(Resource):
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 403: 'Forbidden', 404: 'Not Found', 500: 'Server Error'})
    def post(self):
        """Admin sends a feedback message to a user (as a new message in feedback_messages)"""
        try:
            admin_email = get_jwt_identity()
            admin_key = f"{USER_PREFIX}{admin_email}"
            admin_data = redis_user_client.hgetall(admin_key)
            if not admin_data:
                logger.error(f"Admin user not found: {admin_email}")
                return {"error": "Admin user not found"}, 404
            admin_role = None
            if 'role' in admin_data:
                admin_role = admin_data['role']
            admin_name = None
            if 'username' in admin_data:
                admin_name = admin_data['username']
            if not admin_role or admin_role.lower() != 'admin':
                logger.error(f"User {admin_email} with role {admin_role} attempted admin action")
                return {"error": "Only admin users can send feedback messages"}, 403

            # Accept both JSON and multipart/form-data
            if request.content_type and request.content_type.startswith('multipart/form-data'):
                message = request.form.get('response')
                user_email = request.form.get('email')
            else:
                data = request.json or {}
                message = data.get('response')
                user_email = data.get('email')
            if not message or not message.strip():
                return {"error": "Feedback message is required"}, 400

            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)
            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing feedback messages or initialize empty array
            feedback_json = user_data.get('feedback_messages', '[]')
            try:
                feedback_messages = json.loads(feedback_json)
            except json.JSONDecodeError:
                feedback_messages = []

            # Process uploaded images if present
            image_ids = []
            if 'images' in request.files:
                files = request.files.getlist('images')
                for file in files:
                    if file and file.filename:
                        file_content = file.read()
                        base64_content = base64.b64encode(file_content).decode('utf-8')  # 确保解码为字符串
                        mime_type = file.content_type or 'application/octet-stream'
                        data_url = f"data:{mime_type};base64,{base64_content}"
                        img_id = str(uuid.uuid4())
                        redis_resource_client.set(f'attachment:{img_id}', data_url)
                        image_ids.append(img_id)

            # Use epoch UTC milliseconds for timestamp
            timestamp = int(time.time() * 1000)

            # Create a new feedback message, sender is admin
            new_feedback = {
                'id': f"FB_{timestamp}",
                'sender': admin_name,
                'senderType': "admin",
                'message': message,
                'email': user_email,
                'timestamp': timestamp,
                'images': image_ids
            }

            # Add to feedback messages
            feedback_messages.append(new_feedback)

            # Save updated feedback messages
            redis_user_client.hset(user_key, 'feedback_messages', json.dumps(feedback_messages))

            logger.info(f"Admin {admin_email} sent feedback message to user: {user_email}")
            return {"message": "Feedback message sent successfully", "feedback": new_feedback}, 200

        except Exception as e:
            logger.error(f"Error sending admin feedback message: {str(e)}")
            return {"error": f"An error occurred while sending feedback message: {str(e)}"}, 500

@user_ns.route('/video-error-reports')
class VideoErrorReport(Resource):
    @jwt_required()
    @user_ns.expect(video_error_report_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self):
        """Submit a video error report"""
        try:
            user_email = get_jwt_identity()
            data = request.json
            
            # Validate required fields
            required_fields = ['channelId', 'channelName', 'videoId', 'videoTitle', 'errorType', 'description']
            if not all(field in data for field in required_fields):
                return {"error": "Missing required fields"}, 400
            
            # Validate error type
            valid_error_types = ['transcript_error', 'timing_error', 'missing_content', 'other']
            if data['errorType'] not in valid_error_types:
                return {"error": "Invalid error type"}, 400
            
            # Check if video exists
            video_key = f"{VIDEO_PREFIX}{data['channelId']}:{data['videoId']}"
            if not redis_resource_client.exists(video_key):
                return {"error": "Video not found"}, 404
            
            # Get user information
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)
            
            if not user_data:
                return {"error": "User not found"}, 404
            
            user_name = user_data.get('username', 'Unknown User')
            
            # Generate unique report ID
            timestamp = int(time.time() * 1000)
            report_id = f"VER_{timestamp}_{user_email.replace('@', '_').replace('.', '_')}"
            
            # Create error report
            error_report = {
                'id': report_id,
                'channelId': data['channelId'],
                'channelName': data['channelName'],
                'videoId': data['videoId'],
                'videoTitle': data['videoTitle'],
                'userEmail': user_email,
                'userName': user_name,
                'errorType': data['errorType'],
                'description': data['description'].strip(),
                'status': 'pending',
                'timestamp': timestamp
            }
            
            # Get existing video error reports from user data
            video_error_reports_json = user_data.get('video_error_reports', '[]')
            try:
                video_error_reports = json.loads(video_error_reports_json)
            except json.JSONDecodeError:
                video_error_reports = []
            
            # Add new report to user's reports
            video_error_reports.append(error_report)
            
            # Save updated reports back to user data
            redis_user_client.hset(user_key, 'video_error_reports', json.dumps(video_error_reports))
            
            logger.info(f"Video error report submitted by user: {user_email} for video: {data['videoId']}")
            return {"message": "Video error report submitted successfully", "reportId": report_id}, 200
            
        except Exception as e:
            logger.error(f"Error submitting video error report: {str(e)}")
            return {"error": f"An error occurred while submitting video error report: {str(e)}"}, 500

    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 500: 'Server Error'})
    def get(self):
        """Get user's video error reports"""
        try:
            user_email = get_jwt_identity()
            channel_id = request.args.get('channelId')
            video_id = request.args.get('videoId')
            
            # Get user data
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)
            
            if not user_data:
                return {"error": "User not found"}, 404
            
            # Get video error reports from user data
            video_error_reports_json = user_data.get('video_error_reports', '[]')
            try:
                reports = json.loads(video_error_reports_json)
            except json.JSONDecodeError:
                reports = []
            
            # Filter by channel and video if specified
            filtered_reports = []
            for report in reports:
                if channel_id and report.get('channelId') != channel_id:
                    continue
                if video_id and report.get('videoId') != video_id:
                    continue
                filtered_reports.append(report)
            
            # Sort by timestamp (newest first)
            filtered_reports.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            
            logger.info(f"Retrieved {len(filtered_reports)} video error reports for user: {user_email}")
            return filtered_reports, 200
            
        except Exception as e:
            logger.error(f"Error retrieving video error reports: {str(e)}")
            return {"error": f"An error occurred while retrieving video error reports: {str(e)}"}, 500

@user_ns.route('/video-error-reports/admin')
class AdminVideoErrorReports(Resource):
    @jwt_required()
    @user_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 403: 'Forbidden', 500: 'Server Error'})
    def get(self):
        """Get all video error reports (Admin only)"""
        try:
            admin_email = get_jwt_identity()
            
            # Check if user is admin
            admin_key = f"{USER_PREFIX}{admin_email}"
            admin_data = redis_user_client.hgetall(admin_key)
            
            if not admin_data:
                return {"error": "Admin user not found"}, 404
            
            admin_role = admin_data.get('role', '').lower()
            if admin_role != 'admin':
                return {"error": "Only admin users can access all video error reports"}, 403
            
            # Get all user keys and collect their video error reports
            user_keys = redis_user_client.keys(f"{USER_PREFIX}*")
            all_reports = []
            
            for user_key in user_keys:
                try:
                    user_data = redis_user_client.hgetall(user_key)
                    if 'video_error_reports' in user_data:
                        video_error_reports_json = user_data['video_error_reports']
                        try:
                            user_reports = json.loads(video_error_reports_json)
                            all_reports.extend(user_reports)
                        except json.JSONDecodeError:
                            continue
                except Exception as e:
                    logger.error(f"Error processing user reports from {user_key}: {str(e)}")
                    continue
            
            # Sort by timestamp (newest first)
            all_reports.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            
            logger.info(f"Admin {admin_email} retrieved {len(all_reports)} video error reports")
            return all_reports, 200
            
        except Exception as e:
            logger.error(f"Error retrieving all video error reports: {str(e)}")
            return {"error": f"An error occurred while retrieving all video error reports: {str(e)}"}, 500

@user_ns.route('/video-error-reports/<string:report_id>')
class VideoErrorReportUpdate(Resource):
    @jwt_required()
    @user_ns.expect(user_ns.model('VideoErrorReportUpdate', {
        'status': fields.String(required=True, description='New status (pending, resolved, rejected)'),
        'adminResponse': fields.String(required=False, description='Admin response message')
    }))
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 403: 'Forbidden', 404: 'Not Found', 500: 'Server Error'})
    def put(self, report_id):
        """Update video error report status (Admin only)"""
        try:
            admin_email = get_jwt_identity()
            data = request.json
            
            # Check if user is admin
            admin_key = f"{USER_PREFIX}{admin_email}"
            admin_data = redis_user_client.hgetall(admin_key)
            
            if not admin_data:
                return {"error": "Admin user not found"}, 404
            
            admin_role = admin_data.get('role', '').lower()
            if admin_role != 'admin':
                return {"error": "Only admin users can update video error reports"}, 403
            
            # Validate status
            valid_statuses = ['pending', 'resolved', 'rejected']
            new_status = data.get('status')
            if new_status not in valid_statuses:
                return {"error": "Invalid status"}, 400
            
            # Find the report in all users' data
            user_keys = redis_user_client.keys(f"{USER_PREFIX}*")
            report_found = False
            
            for user_key in user_keys:
                try:
                    user_data = redis_user_client.hgetall(user_key)
                    if 'video_error_reports' in user_data:
                        video_error_reports_json = user_data['video_error_reports']
                        try:
                            user_reports = json.loads(video_error_reports_json)
                            
                            # Find and update the specific report
                            for i, report in enumerate(user_reports):
                                if report.get('id') == report_id:
                                    # Update the report
                                    current_timestamp = int(time.time() * 1000)
                                    user_reports[i]['status'] = new_status
                                    
                                    if new_status in ['resolved', 'rejected']:
                                        user_reports[i]['resolvedAt'] = current_timestamp
                                    
                                    if data.get('adminResponse'):
                                        user_reports[i]['adminResponse'] = data['adminResponse']
                                    
                                    # Save updated reports back to user data
                                    redis_user_client.hset(user_key, 'video_error_reports', json.dumps(user_reports))
                                    report_found = True
                                    break
                            
                            if report_found:
                                break
                                
                        except json.JSONDecodeError:
                            continue
                except Exception as e:
                    logger.error(f"Error processing user reports from {user_key}: {str(e)}")
                    continue
            
            if not report_found:
                return {"error": "Video error report not found"}, 404
            
            logger.info(f"Admin {admin_email} updated video error report {report_id} to status: {new_status}")
            return {"message": f"Video error report updated successfully to {new_status}"}, 200
            
        except Exception as e:
            logger.error(f"Error updating video error report: {str(e)}")
            return {"error": f"An error occurred while updating video error report: {str(e)}"}, 500