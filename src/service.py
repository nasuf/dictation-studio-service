from datetime import datetime
import sys
import os
import json
import logging
from flask import Flask, request, jsonify
from flask_restx import Api, Resource, fields
from flask_cors import CORS
from config import CHANNEL_PREFIX, LANGUAGE_ALL, VIDEO_PREFIX, VISIBILITY_ALL
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity
from config import JWT_SECRET_KEY, JWT_ACCESS_TOKEN_EXPIRES
from werkzeug.utils import secure_filename
from auth import auth_ns
from error_handlers import register_error_handlers
from user import user_ns
from payment import payment_ns
from payment_zpay import payment_zpay_ns
from utils import download_transcript_from_youtube_transcript_api, get_video_id, parse_srt_file, download_transcript_with_ytdlp, get_video_info_with_ytdlp
from redis_manager import RedisManager
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, expose_headers=['x-ds-access-token', 'x-ds-refresh-token'])
app.config['JWT_SECRET_KEY'] = JWT_SECRET_KEY
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = JWT_ACCESS_TOKEN_EXPIRES
app.config['JWT_TOKEN_LOCATION'] = ['headers']  # Only allow JWT tokens in headers
jwt = JWTManager(app)

api = Api(
    app, 
    version='1.0', 
    title='Dictation Studio API', 
    description='API for Dictation Studio'
)
register_error_handlers(api)

ns = api.namespace('service', path='/dictation-studio/service', description='Dictation Studio Service Operations')
api.add_namespace(auth_ns, path='/dictation-studio/auth')
api.add_namespace(user_ns, path='/dictation-studio/user')
api.add_namespace(payment_ns, path='/dictation-studio/payment')
api.add_namespace(payment_zpay_ns, path='/dictation-studio/payment/zpay')

redis_manager = RedisManager()
# Redis connection
redis_resource_client = redis_manager.get_resource_client()
redis_user_client = redis_manager.get_user_client()

# Input models for Swagger
youtube_url_model = api.model('YouTubeURL', {
    'url': fields.String(required=True, description='YouTube video URL')
})

channel_info_model = api.model('ChannelInfo', {
    'channels': fields.List(fields.Nested(api.model('Channel', {
        'name': fields.String(required=True, description='YouTube channel name'),
        'id': fields.String(required=True, description='YouTube channel ID'),
        'image_url': fields.String(required=True, description='YouTube channel image URL')
    })))
})

video_list_model = api.model('VideoList', {
    'channel_id': fields.String(required=True, description='YouTube channel ID'),
    'video_links': fields.List(fields.String, required=True, description='List of YouTube video links'),
    'titles': fields.List(fields.String, required=True, description='List of video titles')
})

transcript_update_model = api.model('TranscriptUpdate', {
    'index': fields.Integer(required=True, description='Index of the transcript item to update'),
    'start': fields.Float(required=True, description='Start time of the transcript item'),
    'end': fields.Float(required=True, description='End time of the transcript item'),
    'transcript': fields.String(required=True, description='Updated transcript text')
})

full_transcript_update_model = api.model('FullTranscriptUpdate', {
    'transcript': fields.List(fields.Nested(api.model('TranscriptItem', {
        'start': fields.Float(required=True, description='Start time of the transcript item'),
        'end': fields.Float(required=True, description='End time of the transcript item'),
        'transcript': fields.String(required=True, description='Transcript text')
    })))
})

# New model for batch transcript updates
batch_transcript_update_model = api.model('BatchTranscriptUpdate', {
    'videos': fields.List(fields.Nested(api.model('VideoTranscriptUpdate', {
        'video_id': fields.String(required=True, description='Video ID'),
        'transcript': fields.List(fields.Nested(api.model('TranscriptItem', {
            'start': fields.Float(required=True, description='Start time of the transcript item'),
            'end': fields.Float(required=True, description='End time of the transcript item'),
            'transcript': fields.String(required=True, description='Transcript text')
        })))
    })))
})

# Common function to restore transcript for a single video
def restore_video_transcript(channel_id, video_id):
    """
    Common function to restore transcript for a single video
    Returns (success, message, status_code)
    """
    try:
        video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
        video_data = redis_resource_client.hgetall(video_key)
        
        if not video_data:
            return False, f"Video {video_id} not found in channel {channel_id}", 404

        restored = False
        # Firstly, try to restore from original_transcript
        if 'original_transcript' in video_data:
            try:
                original_transcript = json.loads(video_data['original_transcript'])
                # update transcript
                redis_resource_client.hset(video_key, 'transcript', json.dumps(original_transcript))
                # delete original_transcript field
                redis_resource_client.hdel(video_key, 'original_transcript')
                restored = True
                logger.info(f"Successfully restored transcript from original_transcript for video {video_id}")
            except Exception as e:
                logger.error(f"Failed to restore from original_transcript: {str(e)}")
                # try to restore from SRT file

        # if failed to restore from original_transcript, try to restore from SRT file
        if not restored:
            uploads_dir = os.getenv('UPLOADS_DIR', './uploads')
            filename = secure_filename(f"{video_id}.srt")
            file_path = os.path.join(uploads_dir, filename)

            if not os.path.exists(file_path):
                return False, f"SRT file not found and no original transcript available for video {video_id}", 404

            transcript = parse_srt_file(file_path)
            if transcript is None:
                return False, f"Unable to parse SRT file for video: {video_id}", 500

            redis_resource_client.hset(video_key, 'transcript', json.dumps(transcript))
            # delete original_transcript field
            redis_resource_client.hdel(video_key, 'original_transcript')
            logger.info(f"Successfully restored transcript from SRT file for video {video_id}")

        return True, f"Transcript restored successfully for video {video_id}", 200

    except Exception as e:
        logger.error(f"Error restoring transcript for video {video_id}: {str(e)}")
        return False, f"Error restoring transcript for video {video_id}: {str(e)}", 500

# Common function to update transcript for a single video
def update_video_transcript(channel_id, video_id, new_transcript):
    """
    Common function to update transcript for a single video
    Returns (success, message, status_code)
    """
    try:
        if not new_transcript:
            return False, "Transcript data is required", 400

        video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
        video_data = redis_resource_client.hgetall(video_key)
        
        if not video_data:
            return False, f"Video {video_id} not found in channel {channel_id}", 404

        # copy original transcript to original_transcript
        # if original_transcript field is not existing, get current transcript from redis then copy to original_transcript
        if 'original_transcript' not in video_data:
            original_transcript = json.loads(video_data['transcript'])
            redis_resource_client.hset(video_key, 'original_transcript', json.dumps(original_transcript))   

        # update updated_at
        redis_resource_client.hset(video_key, 'updated_at', int(datetime.now().timestamp() * 1000))
        redis_resource_client.hset(video_key, 'transcript', json.dumps(new_transcript))
        
        logger.info(f"Updated transcript for video {video_id} in channel {channel_id}")
        return True, f"Transcript updated successfully for video {video_id}", 200

    except Exception as e:
        logger.error(f"Error updating transcript for video {video_id}: {str(e)}")
        return False, f"Error updating transcript for video {video_id}: {str(e)}", 500

@ns.route('/transcript')
class YouTubeTranscript(Resource):
    @jwt_required()
    @ns.expect(youtube_url_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def post(self):
        """Get the transcript for a YouTube video"""
        data = request.json
        youtube_url = data.get('url')
        
        video_id = get_video_id(youtube_url)
        if not video_id:
            logger.warning(f"Invalid YouTube URL: {youtube_url}")
            return {"error": "Invalid YouTube URL"}, 400

        transcript = download_transcript_from_youtube_transcript_api(video_id)
        if transcript is None:
            logger.error(f"Unable to download transcript for video: {video_id}")
            return {"error": "Unable to download transcript"}, 500

        logger.info(f"Successfully retrieved transcript for video: {video_id}")
        return jsonify(transcript)

@ns.route('/channel')
class YouTubeChannel(Resource):
    @jwt_required()
    @ns.expect(channel_info_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def post(self):
        """Save YouTube channel information to Redis"""
        data = request.json
        channels = data.get('channels', [])
        
        if not channels:
            logger.warning("Invalid input: 'channels' list is empty")
            return {"error": "Invalid input. 'channels' list is required."}, 400

        try:
            for channel in channels:
                channel_name = channel.get('name')
                channel_id = channel.get('id')
                channel_image_url = channel.get('image_url')
                channel_visibility = channel.get('visibility', 'public')  # Default to 'public' if not provided
                channel_link = channel.get('link')
                channel_language = channel.get('language', 'en')
                
                if not channel_name or not channel_image_url or not channel_id:
                    logger.warning(f"Invalid input for channel {channel_id}")
                    return {"error": f"Invalid input for channel {channel_id}. Name, id, and image_url are required."}, 400

                channel_key = f"{CHANNEL_PREFIX}{channel_id}"
                channel_info = {
                    'id': channel_id,
                    'name': channel_name,
                    'image_url': channel_image_url,
                    'visibility': channel_visibility,
                    'link': channel_link,
                    'language': channel_language
                }
                redis_resource_client.hset(channel_key, mapping=channel_info)
                logger.info(f"Saved/updated channel: {channel_id}")
            
            logger.info(f"Successfully saved/updated {len(channels)} channel(s)")
            return {"message": f"{len(channels)} channel(s) information saved or updated successfully"}, 200
        except Exception as e:
            logger.error(f"Error saving channel information: {str(e)}")
            return {"error": f"Error saving channel information: {str(e)}"}, 500

    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 500: 'Server Error'})
    def get(self):
        """Get all YouTube channel information from Redis"""
        try:
            visibility = request.args.get('visibility', VISIBILITY_ALL)
            language = request.args.get('language', LANGUAGE_ALL)
            all_channels = []
            for key in redis_resource_client.scan_iter(f"{CHANNEL_PREFIX}*"):
                channel_info = redis_resource_client.hgetall(key)
                channel_data = {k: v for k, v in channel_info.items()}
                # if visibility is not public, skip
                if visibility != VISIBILITY_ALL and channel_data.get('visibility') != visibility:
                    continue
                if language != LANGUAGE_ALL and channel_data.get('language') != language:
                    continue
                if 'videos' in channel_data:
                    try:
                        channel_data['videos'] = json.loads(channel_data['videos'])
                    except Exception:
                        channel_data['videos'] = []
                all_channels.append(channel_data)
            
            logger.info(f"Retrieved {len(all_channels)} channels from Redis")
            return all_channels, 200
        except Exception as e:
            logger.error(f"Error retrieving channel information: {str(e)}")
            return {"error": f"Error retrieving channel information: {str(e)}"}, 500

@ns.route('/channel/<string:channel_id>')
class YouTubeChannelOperations(Resource):
    @jwt_required()
    @ns.expect(api.model('ChannelUpdate', {
        'name': fields.String(required=False, description='Updated channel name'),
        'image_url': fields.String(required=False, description='Updated channel image URL'),
        'visibility': fields.String(required=False, description='Channel visibility (public, hidden, or user:user_id)')
    }))
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def put(self, channel_id):
        """Update specific fields of a YouTube channel"""
        try:
            data = request.json
            if not data:
                logger.warning("No update data provided")
                return {"error": "No update data provided"}, 400

            channel_key = f"{CHANNEL_PREFIX}{channel_id}"
            
            # Check if channel exists
            if not redis_resource_client.exists(channel_key):
                logger.warning(f"Channel not found: {channel_id}")
                return {"error": "Channel not found"}, 404
            
            # Get current channel info
            channel_info = redis_resource_client.hgetall(channel_key)
            decoded_info = {k: v for k, v in channel_info.items()}
            
            # Update only the fields that are provided in the request
            decoded_info.update({k: v for k, v in data.items() if v is not None})
            
            # Save updated channel info
            redis_resource_client.hmset(channel_key, decoded_info)
            logger.info(f"Successfully updated channel: {channel_id}")
            return {"message": f"Channel {channel_id} updated successfully"}, 200
        
        except Exception as e:
            logger.error(f"Error updating channel {channel_id}: {str(e)}")
            return {"error": f"Error updating channel: {str(e)}"}, 500

    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 404: 'Not Found', 500: 'Server Error'})
    def get(self, channel_id):
        """Get a specific YouTube channel information from Redis"""
        try:
            channel_key = f"{CHANNEL_PREFIX}{channel_id}"
            
            if not redis_resource_client.exists(channel_key):
                logger.warning(f"Channel not found: {channel_id}")
                return {"error": "Channel not found"}, 404
            
            channel_info = redis_resource_client.hgetall(channel_key)
            channel_data = {k: v for k, v in channel_info.items()}
            
            logger.info(f"Retrieved channel information for: {channel_id}")
            return channel_data, 200
        
        except Exception as e:
            logger.error(f"Error retrieving channel information: {str(e)}")
            return {"error": f"Error retrieving channel information: {str(e)}"}, 500

@ns.route('/video-list')
class YouTubeVideoList(Resource):
    @jwt_required()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    @ns.param('data', 'JSON array of video data', type='string', required=True)
    @ns.param('transcript_files', 'Transcript files (optional)', type='file', required=False)
    def post(self):
        try:
            data = json.loads(request.form.get('data', '[]'))
            transcript_files = request.files.getlist('transcript_files')
            uploads_dir = os.getenv('UPLOADS_DIR', './uploads')
            os.makedirs(uploads_dir, exist_ok=True)

            if not data:
                logger.warning("Invalid input: data is required")
                return {"error": "Invalid input. Data is required."}, 400

            results = []
            duplicate_video_ids = []
            
            # Create a mapping of video IDs to uploaded files
            file_mapping = {}
            for file in transcript_files:
                if file and file.filename:
                    # Extract video ID from filename (assuming format: video_id.srt)
                    filename = secure_filename(file.filename)
                    video_id_from_file = filename.replace('.srt', '')
                    file_mapping[video_id_from_file] = file

            for video_data in data:
                channel_id = video_data.get('channel_id')
                video_link = video_data.get('video_link')
                title = video_data.get('title')
                visibility = video_data.get('visibility', 'hidden')

                if not channel_id or not video_link:
                    logger.warning(f"Invalid input for video: {video_link}")
                    results.append({"error": f"Invalid input for video: {video_link}. channel_id and video_link are required."})
                    continue

                channel_key = f"{CHANNEL_PREFIX}{channel_id}"
                if not redis_resource_client.exists(channel_key):
                    logger.warning(f"Channel with id {channel_id} does not exist")
                    results.append({"error": f"Channel with id {channel_id} does not exist."})
                    continue

                video_id = get_video_id(video_link)
                if not video_id:
                    logger.warning(f"Invalid YouTube URL: {video_link}")
                    results.append({"error": f"Invalid YouTube URL: {video_link}"})
                    continue

                video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
                # If video_key already exists, skip and collect duplicate
                if redis_resource_client.exists(video_key):
                    logger.info(f"Duplicate video: {video_id} in channel {channel_id}, skipping update.")
                    duplicate_video_ids.append(video_id)
                    results.append({"duplicate": video_id})
                    continue

                # Try to get transcript from uploaded file first
                transcript = None
                transcript_source = "unknown"
                
                if video_id in file_mapping:
                    # Use uploaded SRT file
                    try:
                        filename = secure_filename(f"{video_id}.srt")
                        file_path = os.path.join(uploads_dir, filename)
                        file_mapping[video_id].save(file_path)
                        logger.info(f"File saved successfully: {file_path}")
                        
                        transcript = parse_srt_file(file_path)
                        if transcript:
                            transcript_source = "uploaded_srt"
                            logger.info(f"Successfully parsed uploaded SRT file for video {video_id}")
                        else:
                            logger.warning(f"Failed to parse uploaded SRT file for video {video_id}")
                    except Exception as e:
                        logger.error(f"Error processing uploaded SRT file for video {video_id}: {str(e)}")

                # If no transcript from uploaded file, try to download using yt-dlp
                if transcript is None:
                    try:
                        logger.info(f"Attempting to download transcript for video {video_id} using yt-dlp")
                        yt_dlp_transcript = download_transcript_with_ytdlp(video_id)
                        if yt_dlp_transcript:
                            # Convert yt-dlp format to our standard format
                            transcript = []
                            for segment in yt_dlp_transcript:
                                transcript.append({
                                    'start': segment.get('start', 0),
                                    'end': segment.get('end', 0),
                                    'transcript': segment.get('text', '')
                                })
                            transcript_source = "ytdlp_download"
                            logger.info(f"Successfully downloaded transcript for video {video_id} using yt-dlp")
                        else:
                            logger.warning(f"Failed to download transcript for video {video_id}")
                    except Exception as e:
                        logger.error(f"Error downloading transcript for video {video_id}: {str(e)}")

                # If still no transcript, continue without transcript (optional)
                if transcript is None:
                    logger.warning(f"No transcript available for video {video_id}, saving video without transcript")
                    transcript = []
                    transcript_source = "none"

                # Get video title if not provided
                if not title:
                    try:
                        video_info = get_video_info_with_ytdlp(video_id)
                        if video_info and video_info.get('title'):
                            title = video_info['title']
                            logger.info(f"Retrieved title for video {video_id}: {title}")
                        else:
                            title = f"Video {video_id}"  # Fallback title
                            logger.warning(f"Could not retrieve title for video {video_id}, using fallback")
                    except Exception as e:
                        title = f"Video {video_id}"  # Fallback title
                        logger.error(f"Error retrieving title for video {video_id}: {str(e)}")

                video_info = {
                    "link": video_link,
                    "video_id": video_id,
                    "title": title,
                    "visibility": visibility,
                    "transcript": json.dumps(transcript),
                    "transcript_source": transcript_source,
                    "created_at": int(datetime.now().timestamp() * 1000)
                }
                redis_resource_client.hset(video_key, mapping=video_info)

                # Add video_id to channel's 'videos' field (JSON array in hash)
                videos_json = redis_resource_client.hget(channel_key, 'videos')
                try:
                    videos_list = json.loads(videos_json) if videos_json else []
                except Exception:
                    videos_list = []
                if video_id not in videos_list:
                    videos_list.append(video_id)
                    redis_resource_client.hset(channel_key, 'videos', json.dumps(videos_list))

                logger.info(f"Successfully saved video {video_id} for channel {channel_id} with transcript source: {transcript_source}")
                results.append({
                    "success": f"Video {video_id} saved successfully for channel {channel_id}",
                    "transcript_source": transcript_source,
                    "transcript_count": len(transcript) if transcript else 0
                })

            if duplicate_video_ids:
                return {
                    "message": "partially success",
                    "results": results,
                    "duplicate_video_ids": duplicate_video_ids
                }, 200
            return {"results": results}, 200
        except Exception as e:
            logger.error(f"Error saving video list: {str(e)}")
            return {"error": f"Error saving video list with transcripts: {str(e)}"}, 500

    @jwt_required()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def get(self):
        """Get all YouTube video lists with transcripts from Redis"""
        try:
            video_lists = {}
            pattern = f"{VIDEO_PREFIX}*"
            for key in redis_resource_client.scan_iter(pattern):
                key_str = key
                channel_id = key_str.split(':')[1]  # video:channel_id:video_id
                video_data = redis_resource_client.hgetall(key)
                
                if not video_data:
                    continue

                video_info = {
                    'link': video_data['link'],
                    'video_id': video_data['video_id'],
                    'title': video_data['title'],
                    'transcript': json.loads(video_data['transcript']),
                    'created_at': video_data['created_at']
                }

                if channel_id not in video_lists:
                    video_lists[channel_id] = []
                video_lists[channel_id].append(video_info)

            result = []
            for channel_id, videos in video_lists.items():
                result.append({
                    "channel_id": channel_id,
                    "videos": videos
                })

            logger.info(f"Retrieved video lists for {len(result)} channels")
            return result, 200
        except Exception as e:
            logger.error(f"Error retrieving video lists: {str(e)}")
            return {"error": f"Error retrieving video lists with transcripts: {str(e)}"}, 500


@ns.route('/video-list/<string:channel_id>')
class YouTubeVideoListByChannel(Resource):
    @jwt_required()
    @ns.doc(params={'visibility': 'Visibility of the videos to retrieve'})
    def get(self, channel_id):
        """Get video IDs and links for a specific channel"""
        visibility = request.args.get('visibility', VISIBILITY_ALL)
        pattern = f"{VIDEO_PREFIX}{channel_id}:*"
        videos = []
        
        for video_key in redis_resource_client.scan_iter(pattern):
            video_data = redis_resource_client.hgetall(video_key)
            
            if video_data:
                if visibility != VISIBILITY_ALL and video_data['visibility'] != visibility:
                    continue
                
                # Parse JSON fields
                if 'transcript' in video_data:
                    video_data['transcript'] = json.loads(video_data['transcript'])
                if 'original_transcript' in video_data:
                    video_data['original_transcript'] = json.loads(video_data['original_transcript'])
                if 'created_at' in video_data:
                    video_data['created_at'] = int(video_data['created_at'])
                if 'updated_at' in video_data:
                    video_data['updated_at'] = int(video_data['updated_at'])
                videos.append(video_data)

        logger.info(f"Retrieved {len(videos)} videos for channel: {channel_id}")
        return {"channel_id": channel_id, "videos": videos}, 200

@ns.route('/video-transcript/<string:channel_id>/<string:video_id>')
class VideoTranscript(Resource):
    @jwt_required()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def get(self, channel_id, video_id):
        """Get transcript for a specific video in a channel"""
        try:
            video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
            video_data = redis_resource_client.hgetall(video_key)
            
            if not video_data:
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found"}, 404
            
            logger.info(f"Retrieved transcript for video {video_id} in channel {channel_id}")
            return {
                "channel_id": channel_id,
                "video_id": video_id,
                "title": video_data['title'],
                "transcript": json.loads(video_data['transcript'])
            }, 200

        except Exception as e:
            logger.error(f"Error retrieving transcript for video {video_id} in channel {channel_id}: {str(e)}")
            return {"error": f"Error retrieving video transcript: {str(e)}"}, 500

@ns.route('/<string:channel_id>/<string:video_id>/transcript')
class VideoTranscriptUpdate(Resource):
    @jwt_required()
    @ns.expect(transcript_update_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def put(self, channel_id, video_id):
        """Update a specific transcript item for a video"""
        try:
            data = request.json
            index = data.get('index')
            transcript_item = {
                'start': data.get('start'),
                'end': data.get('end'),
                'transcript': data.get('transcript')
            }

            video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
            video_data = redis_resource_client.hgetall(video_key)
            
            if not video_data:
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found"}, 404

            transcript = json.loads(video_data['transcript'])
            if 0 <= index < len(transcript):
                transcript[index] = transcript_item
                redis_resource_client.hset(video_key, 'transcript', json.dumps(transcript))
                logger.info(f"Updated transcript item {index} for video {video_id} in channel {channel_id}")
                return {"message": "Transcript item updated successfully"}, 200
            else:
                logger.warning(f"Invalid transcript index: {index}")
                return {"error": "Invalid transcript index"}, 400

        except Exception as e:
            logger.error(f"Error updating transcript: {str(e)}")
            return {"error": f"Error updating transcript: {str(e)}"}, 500

@ns.route('/<string:channel_id>/<string:video_id>/full-transcript')
class FullVideoTranscriptUpdate(Resource):
    @jwt_required()
    @ns.expect(full_transcript_update_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def put(self, channel_id, video_id):
        """Update the entire transcript for a video, meanwhile copy original transcript to original_transcript"""
        data = request.json
        new_transcript = data.get('transcript')
        
        success, message, status_code = update_video_transcript(channel_id, video_id, new_transcript)
        
        if success:
            return {"message": message}, status_code
        else:
            return {"error": message}, status_code

def process_single_video_transcript(channel_id, video_data):
    """Process a single video transcript update in a thread"""
    video_id = video_data.get('video_id')
    transcript = video_data.get('transcript')
    
    if not video_id or not transcript:
        error_msg = f"Invalid data for video {video_id}: video_id and transcript are required"
        return {"video_id": video_id, "success": False, "message": error_msg, "status_code": 400}
    
    success, message, status_code = update_video_transcript(channel_id, video_id, transcript)
    
    return {
        "video_id": video_id,
        "success": success,
        "message": message,
        "status_code": status_code
    }

@ns.route('/<string:channel_id>/batch-transcript-update')
class BatchTranscriptUpdate(Resource):
    @jwt_required()
    @ns.expect(batch_transcript_update_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def put(self, channel_id):
        """Update transcripts for multiple videos in a channel using multithreading"""
        try:
            data = request.json
            videos = data.get('videos', [])
            
            if not videos:
                logger.warning("No video data provided")
                return {"error": "Videos data is required"}, 400

            results = []
            success_count = 0
            error_count = 0
            
            # Use ThreadPoolExecutor for concurrent processing
            max_workers = min(10, len(videos))  # Limit to 10 concurrent threads
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all tasks
                future_to_video = {
                    executor.submit(process_single_video_transcript, channel_id, video_data): video_data
                    for video_data in videos
                }
                
                # Collect results as they complete
                for future in as_completed(future_to_video):
                    try:
                        result = future.result()
                        results.append(result)
                        
                        if result["success"]:
                            success_count += 1
                        else:
                            error_count += 1
                            
                    except Exception as e:
                        video_data = future_to_video[future]
                        video_id = video_data.get('video_id', 'unknown')
                        error_result = {
                            "video_id": video_id,
                            "success": False,
                            "message": f"Thread execution error: {str(e)}",
                            "status_code": 500
                        }
                        results.append(error_result)
                        error_count += 1
                        logger.error(f"Thread execution error for video {video_id}: {str(e)}")

            # Sort results by video_id for consistent ordering
            results.sort(key=lambda x: x.get('video_id', ''))

            logger.info(f"Batch transcript update completed for channel {channel_id}: {success_count} success, {error_count} errors")
            
            return {
                "message": f"Batch update completed: {success_count} successful, {error_count} failed",
                "success_count": success_count,
                "error_count": error_count,
                "results": results
            }, 200

        except Exception as e:
            logger.error(f"Error in batch transcript update: {str(e)}")
            return {"error": f"Error in batch transcript update: {str(e)}"}, 500

@ns.route('/video-list/<string:channel_id>/<string:video_id>')
class YouTubeVideoDelete(Resource):
    @jwt_required()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def delete(self, channel_id, video_id):
        """Delete a specific video from a channel and remove related user progress"""
        try:
            video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
            if not redis_resource_client.exists(video_key):
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found"}, 404

            redis_resource_client.delete(video_key)

            # Remove video_id from channel's 'videos' field (JSON array in hash)
            channel_key = f"{CHANNEL_PREFIX}{channel_id}"
            videos_json = redis_resource_client.hget(channel_key, 'videos')
            try:
                videos_list = json.loads(videos_json) if videos_json else []
            except Exception:
                videos_list = []
            if video_id in videos_list:
                videos_list.remove(video_id)
                redis_resource_client.hset(channel_key, 'videos', json.dumps(videos_list))

            for user_key in redis_user_client.scan_iter("user:*"):
                user_data = redis_user_client.hgetall(user_key)
                if 'dictation_progress' in user_data:
                    dictation_progress = json.loads(user_data['dictation_progress'])
                    video_key = f"{channel_id}:{video_id}"
                    if video_key in dictation_progress:
                        del dictation_progress[video_key]
                        redis_user_client.hset(user_key, 'dictation_progress', json.dumps(dictation_progress))
                        logger.info(f"Removed dictation progress for video {video_id} from user {user_key}")

            logger.info(f"Successfully deleted video {video_id} from channel {channel_id}")
            return {"message": f"Video {video_id} deleted successfully from channel {channel_id}"}, 200

        except Exception as e:
            logger.error(f"Error deleting video {video_id} from channel {channel_id}: {str(e)}")
            return {"error": f"Error deleting video: {str(e)}"}, 500

@ns.route('/video-list/<string:channel_id>/<string:video_id>')
class YouTubeVideoUpdate(Resource):
    @jwt_required()
    @ns.expect(api.model('VideoUpdate', {
        'title': fields.String(required=False, description='Updated video title'),
        'visibility': fields.String(required=False, description='Updated video visibility'),
    }))
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def put(self, channel_id, video_id):
        """Update a specific video's attributes in a channel (only title and visibility, keep all other fields unchanged)"""
        old_video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
        video_info = redis_resource_client.hgetall(old_video_key)
        if not video_info:
            logger.warning(f"Video {video_id} not found in channel {channel_id}")
            return {"error": "Video not found"}, 404

        data = request.json
        if not data:
            logger.warning("No update data provided")
            return {"error": "No update data provided"}, 400

        # Only update title and visibility if provided
        if 'title' in data and data['title'] is not None:
            video_info['title'] = data['title']
        if 'visibility' in data and data['visibility'] is not None:
            video_info['visibility'] = data['visibility']
        video_info['updated_at'] = int(datetime.now().timestamp() * 1000)

        # Save to Redis, do not touch any other fields (e.g., transcript, original_transcript)
        video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
        redis_resource_client.hmset(video_key, video_info)
        logger.info(f"Successfully updated video {video_id} in channel {channel_id}")
        return {"message": f"Video {video_id} updated successfully"}, 200

def process_single_video_restore(channel_id, video_data):
    """Process a single video transcript restore in a thread"""
    video_id = video_data.get('video_id')
    
    if not video_id:
        error_msg = f"Invalid data: video_id is required"
        return {"video_id": video_id, "success": False, "message": error_msg, "status_code": 400}
    
    success, message, status_code = restore_video_transcript(channel_id, video_id)
    
    return {
        "video_id": video_id,
        "success": success,
        "message": message,
        "status_code": status_code
    }

# New model for batch restore
batch_restore_model = api.model('BatchRestore', {
    'videos': fields.List(fields.Nested(api.model('VideoRestore', {
        'video_id': fields.String(required=True, description='Video ID')
    })))
})

@ns.route('/<string:channel_id>/batch-restore-transcripts')
class BatchRestoreTranscripts(Resource):
    @jwt_required()
    @ns.expect(batch_restore_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def put(self, channel_id):
        """Restore transcripts for multiple videos in a channel using multithreading"""
        try:
            data = request.json
            videos = data.get('videos', [])
            
            if not videos:
                logger.warning("No video data provided")
                return {"error": "Videos data is required"}, 400

            results = []
            success_count = 0
            error_count = 0
            
            # Use ThreadPoolExecutor for concurrent processing
            max_workers = min(10, len(videos))  # Limit to 10 concurrent threads
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all tasks
                future_to_video = {
                    executor.submit(process_single_video_restore, channel_id, video_data): video_data
                    for video_data in videos
                }
                
                # Collect results as they complete
                for future in as_completed(future_to_video):
                    try:
                        result = future.result()
                        results.append(result)
                        
                        if result["success"]:
                            success_count += 1
                        else:
                            error_count += 1
                            
                    except Exception as e:
                        video_data = future_to_video[future]
                        video_id = video_data.get('video_id', 'unknown')
                        error_result = {
                            "video_id": video_id,
                            "success": False,
                            "message": f"Thread execution error: {str(e)}",
                            "status_code": 500
                        }
                        results.append(error_result)
                        error_count += 1
                        logger.error(f"Thread execution error for video {video_id}: {str(e)}")

            # Sort results by video_id for consistent ordering
            results.sort(key=lambda x: x.get('video_id', ''))

            logger.info(f"Batch transcript restore completed for channel {channel_id}: {success_count} success, {error_count} errors")
            
            return {
                "message": f"Batch restore completed: {success_count} successful, {error_count} failed",
                "success_count": success_count,
                "error_count": error_count,
                "results": results
            }, 200

        except Exception as e:
            logger.error(f"Error in batch transcript restore: {str(e)}")
            return {"error": f"Error in batch transcript restore: {str(e)}"}, 500

@ns.route('/<string:channel_id>/<string:video_id>/restore-transcript')
class RestoreVideoTranscript(Resource):
    @jwt_required()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self, channel_id, video_id):
        """Restore transcript for a specific video from original_transcript or SRT file"""
        success, message, status_code = restore_video_transcript(channel_id, video_id)
        
        if success:
            # Get the restored transcript to return
            video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
            video_data = redis_resource_client.hgetall(video_key)
            
            return {
                "channel_id": channel_id,
                "video_id": video_id,
                "title": video_data['title'],
                "transcript": json.loads(redis_resource_client.hget(video_key, 'transcript'))
            }, status_code
        else:
            return {"error": message}, status_code
        
def schedule_check_expired_plans():
    from payment import check_expired_plans
    try:
        check_expired_plans()
    except Exception as e:
        print(f"Error in scheduled check_expired_plans: {e}")
    # check every 300 seconds
    threading.Timer(300, schedule_check_expired_plans).start()

def start_initial_check_expired_plans():
    """Start the first check after 10 seconds delay"""
    logger.info("Starting initial check_expired_plans")
    threading.Timer(10, schedule_check_expired_plans).start()

@ns.route('/<string:channel_id>/transcript-summary')
class ChannelTranscriptSummary(Resource):
    @jwt_required()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def get(self, channel_id):
        """Get transcript summary for all videos in a channel"""
        try:
            pattern = f"{VIDEO_PREFIX}{channel_id}:*"
            transcript_summaries = []
            
            for video_key in redis_resource_client.scan_iter(pattern):
                video_data = redis_resource_client.hgetall(video_key)
                
                if video_data:
                    video_id = video_data.get('video_id')
                    title = video_data.get('title', '')
                    
                    # Parse transcript to get count
                    transcript_count = 0
                    if 'transcript' in video_data:
                        try:
                            transcript = json.loads(video_data['transcript'])
                            transcript_count = len(transcript) if transcript else 0
                        except Exception:
                            transcript_count = 0
                    
                    # Check if original transcript exists
                    has_original = 'original_transcript' in video_data
                    
                    # Get last updated timestamp
                    last_updated = None
                    if 'updated_at' in video_data:
                        try:
                            last_updated = int(video_data['updated_at'])
                        except Exception:
                            last_updated = None
                    
                    transcript_summaries.append({
                        'video_id': video_id,
                        'title': title,
                        'transcript_count': transcript_count,
                        'has_original': has_original,
                        'last_updated': last_updated
                    })
            
            # Sort by video_id for consistent ordering
            transcript_summaries.sort(key=lambda x: x.get('video_id', ''))
            
            logger.info(f"Retrieved transcript summary for {len(transcript_summaries)} videos in channel {channel_id}")
            return {
                "channel_id": channel_id,
                "total_videos": len(transcript_summaries),
                "summaries": transcript_summaries
            }, 200

        except Exception as e:
            logger.error(f"Error retrieving transcript summary for channel {channel_id}: {str(e)}")
            return {"error": f"Error retrieving transcript summary: {str(e)}"}, 500

# Common function to update visibility for a single video
def update_video_visibility(channel_id, video_id, visibility):
    """
    Common function to update visibility for a single video
    Returns (success, message, status_code)
    """
    try:
        video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
        video_info = redis_resource_client.hgetall(video_key)
        
        if not video_info:
            return False, f"Video {video_id} not found in channel {channel_id}", 404

        # Update visibility and timestamp
        video_info['visibility'] = visibility
        video_info['updated_at'] = int(datetime.now().timestamp() * 1000)

        # Save to Redis
        redis_resource_client.hmset(video_key, video_info)
        logger.info(f"Successfully updated visibility for video {video_id} in channel {channel_id} to {visibility}")
        return True, f"Video {video_id} visibility updated successfully", 200

    except Exception as e:
        logger.error(f"Error updating visibility for video {video_id}: {str(e)}")
        return False, f"Error updating visibility for video {video_id}: {str(e)}", 500

def process_single_video_visibility_update(channel_id, video_id, visibility):
    """Process a single video visibility update in a thread"""
    success, message, status_code = update_video_visibility(channel_id, video_id, visibility)
    
    return {
        "video_id": video_id,
        "success": success,
        "message": message,
        "status_code": status_code
    }

# Model for batch visibility update
batch_visibility_update_model = api.model('BatchVisibilityUpdate', {
    'visibility': fields.String(required=True, description='New visibility setting for all videos (public, hidden, or user:user_id)')
})

@ns.route('/<string:channel_id>/batch-visibility-update')
class BatchVideoVisibilityUpdate(Resource):
    @jwt_required()
    @ns.expect(batch_visibility_update_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def put(self, channel_id):
        """Update visibility for all videos in a channel using multithreading"""
        try:
            data = request.json
            new_visibility = data.get('visibility')
            
            if not new_visibility:
                logger.warning("Visibility parameter is required")
                return {"error": "Visibility parameter is required"}, 400

            # Get all videos in the channel
            pattern = f"{VIDEO_PREFIX}{channel_id}:*"
            video_keys = list(redis_resource_client.scan_iter(pattern))
            
            if not video_keys:
                logger.warning(f"No videos found in channel {channel_id}")
                return {"error": f"No videos found in channel {channel_id}"}, 404

            # Extract video IDs from keys
            video_ids = []
            for video_key in video_keys:
                # Extract video_id from key format: "video:channel_id:video_id"
                video_id = video_key.split(':')[-1]
                video_ids.append(video_id)

            results = []
            success_count = 0
            error_count = 0
            
            # Use ThreadPoolExecutor for concurrent processing
            max_workers = min(10, len(video_ids))  # Limit to 10 concurrent threads
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all tasks
                future_to_video = {
                    executor.submit(process_single_video_visibility_update, channel_id, video_id, new_visibility): video_id
                    for video_id in video_ids
                }
                
                # Collect results as they complete
                for future in as_completed(future_to_video):
                    try:
                        result = future.result()
                        results.append(result)
                        
                        if result["success"]:
                            success_count += 1
                        else:
                            error_count += 1
                            
                    except Exception as e:
                        video_id = future_to_video[future]
                        error_result = {
                            "video_id": video_id,
                            "success": False,
                            "message": f"Thread execution error: {str(e)}",
                            "status_code": 500
                        }
                        results.append(error_result)
                        error_count += 1
                        logger.error(f"Thread execution error for video {video_id}: {str(e)}")

            # Sort results by video_id for consistent ordering
            results.sort(key=lambda x: x.get('video_id', ''))

            logger.info(f"Batch visibility update completed for channel {channel_id}: {success_count} success, {error_count} errors")
            
            return {
                "message": f"Batch visibility update completed: {success_count} successful, {error_count} failed",
                "channel_id": channel_id,
                "new_visibility": new_visibility,
                "total_videos": len(video_ids),
                "success_count": success_count,
                "error_count": error_count,
                "results": results
            }, 200

        except Exception as e:
            logger.error(f"Error in batch visibility update: {str(e)}")
            return {"error": f"Error in batch visibility update: {str(e)}"}, 500

# Add user namespace to API
if __name__ == '__main__':
    # Only start the timer in the main process, not in the reloader process
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_initial_check_expired_plans()
    app.run(debug=True, host='0.0.0.0', port=4001, threaded=True)