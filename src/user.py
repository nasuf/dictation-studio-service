from flask import request
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import get_jwt_identity, jwt_required
import json
import logging
from config import CHANNEL_PREFIX, USER_PREFIX, VIDEO_PREFIX
from datetime import datetime, timedelta
from werkzeug.local import LocalProxy
from flask import current_app
from utils import get_plan_name_by_duration, init_quota, update_user_plan

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

# Add new structured missed words model
structured_missed_words_model = user_ns.model('StructuredMissedWords', {
    'words': fields.Raw(required=True, description='Object of missed words grouped by language')
})

def check_dictation_quota(user_id, channel_id, video_id):
    """
    Check if a user has sufficient dictation quota
    
    Args:
        user_id (str): User ID
        channel_id (str): Channel ID
        video_id (str): Video ID
        
    Returns:
        dict: Dictionary containing quota information
    """
    user_key = f"{USER_PREFIX}{user_id}"
    user_data = redis_user_client.hgetall(user_key)
    
    # Get user plan information
    plan_info = None
    if user_data and b'plan' in user_data:
        try:
            plan_info = json.loads(user_data[b'plan'].decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            plan_info = None
    
    # If user has any plan, no limit
    if plan_info and plan_info.get("name"):
        return {
            "used": 0,
            "limit": -1,  # Use -1 to represent unlimited
            "canProceed": True,
            "notifyQuota": False,
        }
    
    # Video key
    video_key = f"{channel_id}:{video_id}"
    
    # Get quota information from user data
    quota_info = None
    if user_data and b'quota' in user_data:
        try:
            quota_info = json.loads(user_data[b'quota'].decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            quota_info = None
    
    # If no quota information, initialize
    if not quota_info:
        quota_info = {
            "first_use_time": datetime.now().isoformat(),
            "videos": [],
            "history": []
        }
    
    # Check if current video is already in history
    video_in_history = video_key in quota_info.get("history", [])
    
    # Get first use time
    try:
        first_use_time = datetime.fromisoformat(quota_info["first_use_time"])
    except (ValueError, KeyError):
        first_use_time = datetime.now()
        quota_info["first_use_time"] = first_use_time.isoformat()
    
    now = datetime.now()
    
    # Calculate end date of 30-day period
    end_date = first_use_time + timedelta(days=30)
    
    # If current time has passed end date, reset first use time and quota
    if now > end_date:
        # Calculate how many complete 30-day cycles have passed
        days_passed = (now - first_use_time).days
        cycles = days_passed // 30
        
        # Update first use time to start of most recent cycle
        first_use_time = first_use_time + timedelta(days=cycles * 30)
        quota_info["first_use_time"] = first_use_time.isoformat()
        
        # Update end date
        end_date = first_use_time + timedelta(days=30)
        
        # Clear quota records for current cycle (keep history records)
        quota_info["videos"] = []
    
    # Get user's used quota for current cycle
    used_videos = quota_info.get("videos", [])
    used_count = len(used_videos)
    
    # If video is already in history, allow continue but don't count in used quota
    if video_in_history:
        return {
            "used": used_count,
            "limit": 4,
            "canProceed": True,
            "notifyQuota": False,
            "startDate": first_use_time.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d")
        }
    
    # If not reached limit, can proceed
    if used_count < 4:
        return {
            "used": used_count,
            "limit": 4,
            "canProceed": True,
            "notifyQuota": True,
            "startDate": first_use_time.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d")
        }
    else:
        return {
            "used": used_count,
            "limit": 4,
            "canProceed": False,
            "notifyQuota": True,
            "startDate": first_use_time.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d")
        }

def register_dictation_video(user_id, channel_id, video_id):
    """
    Register a video to user's dictation quota
    
    Args:
        user_id (str): User ID
        channel_id (str): Channel ID
        video_id (str): Video ID
        
    Returns:
        bool: Whether registration was successful
    """
    logger.info(f"Registering video for user {user_id}")
    
    user_key = f"{USER_PREFIX}{user_id}"
    user_data = redis_user_client.hgetall(user_key)
    
    # Get user plan information
    plan_info = None
    if user_data and b'plan' in user_data:
        try:
            plan_info = json.loads(user_data[b'plan'].decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            plan_info = None
    
    # If user has any plan, no need to register, return success directly
    if plan_info and plan_info.get("name"):
        return True
    
    video_key = f"{channel_id}:{video_id}"
    
    # Get quota information from user data
    quota_info = None
    if user_data and b'quota' in user_data:
        try:
            quota_info = json.loads(user_data[b'quota'].decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            quota_info = None
    
    # If no quota information, initialize
    if not quota_info:
        quota_info = {
            "first_use_time": datetime.now().isoformat(),
            "videos": [],
            "history": []
        }
    
    # Check if video is already in history
    if video_key in quota_info.get("history", []):
        return True
    
    # Get first use time
    try:
        first_use_time = datetime.fromisoformat(quota_info["first_use_time"])
    except (ValueError, KeyError):
        first_use_time = datetime.now()
        quota_info["first_use_time"] = first_use_time.isoformat()
    
    now = datetime.now()
    
    # Calculate end date of 30-day period
    end_date = first_use_time + timedelta(days=30)
    
    # If current time has passed end date, reset first use time and quota
    if now > end_date:
        # Calculate how many complete 30-day cycles have passed
        days_passed = (now - first_use_time).days
        cycles = days_passed // 30
        
        # Update first use time to start of most recent cycle
        first_use_time = first_use_time + timedelta(days=cycles * 30)
        quota_info["first_use_time"] = first_use_time.isoformat()
        
        # Clear quota records for current cycle (keep history records)
        quota_info["videos"] = []
    
    # Get user's used quota for current cycle
    used_videos = quota_info.get("videos", [])
    used_count = len(used_videos)
    
    # If limit reached, return failure
    if used_count >= 4:
        return False
    
    # Add video to current cycle quota list
    if video_key not in used_videos:
        used_videos.append(video_key)
        quota_info["videos"] = used_videos
    
    # Add video to permanent history record
    history = quota_info.get("history", [])
    if video_key not in history:
        history.append(video_key)
        quota_info["history"] = history
    
    # Save updated quota information to user record
    redis_user_client.hset(user_key, "quota", json.dumps(quota_info))
    
    return True

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

            dictation_progress = json.loads(user_data.get(b'dictation_progress', b'{}').decode('utf-8'))
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
            
            dictation_progress = json.loads(user_data.get(b'dictation_progress', b'{}').decode('utf-8'))
            
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

            dictation_progress = json.loads(user_data.get(b'dictation_progress', b'{}').decode('utf-8'))

            all_progress = []
            for key, value in dictation_progress.items():
                channel_id, video_id = key.split(':')
                
                channel_key = f"{CHANNEL_PREFIX}{channel_id}"
                channel_info = redis_resource_client.hgetall(channel_key)
                if not channel_info:
                    continue
                channel_name = channel_info[b'name'].decode('utf-8')

                video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
                video_info = redis_resource_client.hgetall(video_key)
                if not video_info:
                    continue

                all_progress.append({
                    'channelId': channel_id,
                    'channelName': channel_name,
                    'videoId': video_id,
                    'videoTitle': video_info[b'title'].decode('utf-8'),
                    'videoLink': video_info[b'link'].decode('utf-8'),
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
            missed_words_json = user_data.get(b'missed_words', b'[]').decode('utf-8')
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
            missed_words_json = user_data.get(b'missed_words', b'[]').decode('utf-8')
            
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
            missed_words_json = user_data.get(b'missed_words', b'[]').decode('utf-8')
            
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
            if b'role' in admin_data:
                admin_role = admin_data[b'role'].decode('utf-8')
            
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
        """Register a video to user's dictation quota"""
        try:
            user_email = get_jwt_identity()
            data = request.json
            
            channel_id = data.get('channelId')
            video_id = data.get('videoId')
            
            if not channel_id or not video_id:
                return {"error": "Missing channelId or videoId"}, 400
            
            success = register_dictation_video(user_email, channel_id, video_id)
            
            if success:
                return {"status": "success"}, 200
            else:
                return {"error": "Failed to register video, quota exceeded"}, 403
                
        except Exception as e:
            logger.error(f"Error registering dictation video: {str(e)}")
            return {"error": f"An error occurred: {str(e)}"}, 500
