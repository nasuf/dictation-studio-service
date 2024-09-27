import sys
import os
import json
import re
import logging
from flask import Flask, request, jsonify, send_file
from flask_restx import Api, Resource, fields
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
import redis
import requests
from bs4 import BeautifulSoup
import tempfile
from config import REDIS_HOST, REDIS_PORT, REDIS_RESOURCE_DB, REDIS_USER_DB
from flask_jwt_extended import JWTManager, jwt_required
from config import JWT_SECRET_KEY, JWT_ACCESS_TOKEN_EXPIRES
from auth import auth_ns
from user import user_ns

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
app.config['JWT_SECRET_KEY'] = JWT_SECRET_KEY
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = JWT_ACCESS_TOKEN_EXPIRES
app.config['JWT_TOKEN_LOCATION'] = ['headers']  # Only allow JWT tokens in headers
jwt = JWTManager(app)

CHANNEL_PREFIX = "channel:"
VIDEO_PREFIX = "video:"

api = Api(app, version='1.0', title='Daily Dictation Service API',
          description='API for daily dictation service')

ns = api.namespace('service', path='/daily-dictation/service', description='Daily Dictation Service Operations')

# Redis connection
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_RESOURCE_DB)
redis_user_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_USER_DB)

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
    'video_links': fields.List(fields.String, required=True, description='List of YouTube video links')
})

transcript_update_model = api.model('TranscriptUpdate', {
    'index': fields.Integer(required=True, description='Index of the transcript item to update'),
    'start': fields.Float(required=True, description='Start time of the transcript item'),
    'end': fields.Float(required=True, description='End time of the transcript item'),
    'transcript': fields.String(required=True, description='Updated transcript text')
})

def get_video_id(url):
    """Extract the video ID from a YouTube URL."""
    video_id = None
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:embed\/|v\/|youtu.be\/)([0-9A-Za-z_-]{11})',
        r'(?:watch\?v=)([0-9A-Za-z_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            break
    return video_id

def download_transcript(video_id):
    """Download the transcript and return as a list of dictionaries."""
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        
        available_languages = [t.language_code for t in transcript_list]
        logger.info(f"Available languages for video {video_id}: {', '.join(available_languages)}")

        english_languages = [lang for lang in available_languages if lang.startswith('en')]

        if english_languages:
            selected_language = english_languages[0]
        elif available_languages:
            selected_language = available_languages[0]
        else:
            raise Exception("No transcripts available")

        transcript = transcript_list.find_transcript([selected_language])
        transcript_data = transcript.fetch()
        
        formatted_transcript = []
        for entry in transcript_data:
            formatted_entry = {
                "start": round(entry['start'], 2),
                "end": round(entry['start'] + entry['duration'], 2),
                "transcript": entry['text']
            }
            formatted_transcript.append(formatted_entry)

        return formatted_transcript
    except Exception as e:
        logger.error(f"Error downloading transcript for video {video_id}: {e}")
        return None

def get_video_title(video_id):
    """Get the title of a YouTube video."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        title = soup.find('meta', property='og:title')['content']
        return title
    except Exception as e:
        logger.error(f"Error getting title for video {video_id}: {e}")
        return None
    
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

        transcript = download_transcript(video_id)
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
                
                if not channel_name or not channel_image_url or not channel_id:
                    logger.warning(f"Invalid input for channel {channel_id}")
                    return {"error": f"Invalid input for channel {channel_id}. Name, id, and image_url are required."}, 400

                channel_key = f"{CHANNEL_PREFIX}{channel_id}"
                channel_info = {
                    'id': channel_id,
                    'name': channel_name,
                    'image_url': channel_image_url
                }
                redis_client.hmset(channel_key, channel_info)
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
            all_channels = []
            for key in redis_client.scan_iter(f"{CHANNEL_PREFIX}*"):
                channel_info = redis_client.hgetall(key)
                all_channels.append({k.decode(): v.decode() for k, v in channel_info.items()})
            logger.info(f"Retrieved {len(all_channels)} channels from Redis")
            return all_channels, 200
        except Exception as e:
            logger.error(f"Error retrieving channel information: {str(e)}")
            return {"error": f"Error retrieving channel information: {str(e)}"}, 500

@ns.route('/video-list')
class YouTubeVideoList(Resource):
    @jwt_required()
    @ns.expect(video_list_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def post(self):
        """Save YouTube video list with transcripts and titles for a channel to Redis"""
        data = request.json
        channel_id = data.get('channel_id')
        video_links = data.get('video_links', [])
        
        if not channel_id or not video_links:
            logger.warning("Invalid input: 'channel_id' or 'video_links' is missing")
            return {"error": "Invalid input. 'channel_id' and 'video_links' are required."}, 400

        try:
            channel_key = f"{CHANNEL_PREFIX}{channel_id}"
            if not redis_client.exists(channel_key):
                logger.warning(f"Channel with id {channel_id} does not exist")
                return {"error": f"Channel with id {channel_id} does not exist."}, 400

            video_list_key = f"{VIDEO_PREFIX}{channel_id}"
            existing_videos = redis_client.hget(video_list_key, 'videos')
            if existing_videos:
                existing_videos = json.loads(existing_videos.decode())
            else:
                existing_videos = []

            new_videos = []
            for link in video_links:
                video_id = get_video_id(link)
                if not video_id:
                    logger.warning(f"Invalid YouTube URL: {link}")
                    return {"error": f"Invalid YouTube URL: {link}"}, 400
                
                if any(video['video_id'] == video_id for video in existing_videos):
                    logger.info(f"Using existing data for video: {video_id}")
                else:
                    transcript = download_transcript(video_id)
                    if transcript is None:
                        logger.error(f"Unable to download transcript for video: {link}")
                        return {"error": f"Unable to download transcript for video: {link}"}, 500
                    
                    title = get_video_title(video_id)
                    
                    new_videos.append({
                        "link": link,
                        "video_id": video_id,
                        "title": title,
                        "transcript": transcript
                    })
                    logger.info(f"Added new video: {video_id}")

            all_videos = existing_videos + new_videos

            redis_client.hset(video_list_key, 'videos', json.dumps(all_videos))
            redis_client.hset(video_list_key, 'channel_id', channel_id)
            
            logger.info(f"Successfully saved video list for channel {channel_id}")
            return {"message": f"Video list with transcripts and titles for channel {channel_id} saved successfully"}, 200
        except Exception as e:
            logger.error(f"Error saving video list: {str(e)}")
            return {"error": f"Error saving video list with transcripts and titles: {str(e)}"}, 500

    @jwt_required()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def get(self):
        """Get all YouTube video lists with transcripts from Redis"""
        try:
            video_lists = []
            for key in redis_client.scan_iter(f"{VIDEO_PREFIX}*"):
                video_list_data = redis_client.hgetall(key)
                videos = json.loads(video_list_data[b'videos'].decode())
                channel_id = video_list_data[b'channel_id'].decode()
                video_lists.append({
                    "channel_id": channel_id,
                    "videos": videos
                })
            logger.info(f"Retrieved video lists for {len(video_lists)} channels")
            return video_lists, 200
        except Exception as e:
            logger.error(f"Error retrieving video lists: {str(e)}")
            return {"error": f"Error retrieving video lists with transcripts: {str(e)}"}, 500


@ns.route('/video-list/<string:channel_id>')
class YouTubeVideoListByChannel(Resource):
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 500: 'Server Error'})
    def get(self, channel_id):
        """Get video IDs and links for a specific channel"""
        try:
            video_list_key = f"{VIDEO_PREFIX}{channel_id}"
            video_data = redis_client.hget(video_list_key, 'videos')
            if not video_data:
                logger.info(f"No videos found for channel: {channel_id}")
                return {"channel_id": channel_id, "videos": []}, 200
            
            videos = json.loads(video_data.decode())
            simplified_videos = []
            for video in videos:
                simplified_videos.append({
                    "video_id": video["video_id"],
                    "link": video["link"],
                    "title": video["title"]
                })
            
            logger.info(f"Retrieved {len(simplified_videos)} videos for channel: {channel_id}")
            return {"channel_id": channel_id, "videos": simplified_videos}, 200
        except Exception as e:
            logger.error(f"Error retrieving video list for channel {channel_id}: {str(e)}")
            return {"error": f"Error retrieving video list: {str(e)}"}, 500

@ns.route('/video-transcript/<string:channel_id>/<string:video_id>')
class VideoTranscript(Resource):
    @jwt_required()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def get(self, channel_id, video_id):
        """Get transcript for a specific video in a channel"""
        try:
            video_list_key = f"{VIDEO_PREFIX}{channel_id}"
            video_data = redis_client.hget(video_list_key, 'videos')
            if video_data is None:
                logger.warning(f"Channel not found: {channel_id}")
                return {"error": "Channel not found"}, 404
            
            videos = json.loads(video_data.decode())
            for video in videos:
                if video["video_id"] == video_id:
                    logger.info(f"Retrieved transcript for video {video_id} in channel {channel_id}")
                    return {
                        "channel_id": channel_id,
                        "video_id": video_id,
                        "title": video["title"],
                        "transcript": video["transcript"]
                    }, 200
            
            logger.warning(f"Video {video_id} not found in channel {channel_id}")
            return {"error": "Video not found in the channel"}, 404
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

            video_list_key = f"{VIDEO_PREFIX}{channel_id}"
            video_data = redis_client.hget(video_list_key, 'videos')
            if video_data is None:
                logger.warning(f"Channel not found: {channel_id}")
                return {"error": "Channel not found"}, 404

            videos = json.loads(video_data.decode())
            video_found = False
            for video in videos:
                if video["video_id"] == video_id:
                    video_found = True
                    if 0 <= index < len(video["transcript"]):
                        video["transcript"][index] = transcript_item
                        redis_client.hset(video_list_key, 'videos', json.dumps(videos))
                        logger.info(f"Updated transcript item {index} for video {video_id} in channel {channel_id}")
                        return {"message": "Transcript item updated successfully"}, 200
                    else:
                        logger.warning(f"Invalid transcript index: {index}")
                        return {"error": "Invalid transcript index"}, 400

            if not video_found:
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found in the channel"}, 404

        except Exception as e:
            logger.error(f"Error updating transcript: {str(e)}")
            return {"error": f"Error updating transcript: {str(e)}"}, 500

@ns.route('/export-data')
class ExportData(Resource):
    @jwt_required()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def get(self):
        """Export all data from Redis to a JSON file"""
        try:
            data = {
                'channels': {},
                'video_lists': {}
            }

            for key in redis_client.scan_iter(f"{CHANNEL_PREFIX}*"):
                channel_id = key.decode().split(':')[-1]
                data['channels'][channel_id] = {k.decode(): v.decode() for k, v in redis_client.hgetall(key).items()}

            for key in redis_client.scan_iter(f"{VIDEO_PREFIX}*"):
                channel_id = key.decode().split(':')[-1]
                data['video_lists'][channel_id] = {k.decode(): json.loads(v.decode()) for k, v in redis_client.hgetall(key).items()}

            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp_file:
                json.dump(data, temp_file, indent=2)
                temp_file_path = temp_file.name

            logger.info("Successfully exported all data to JSON file")
            return send_file(temp_file_path, as_attachment=True, download_name='redis_export.json', mimetype='application/json')

        except Exception as e:
            logger.error(f"Error exporting data: {str(e)}")
            return {"error": f"Error exporting data: {str(e)}"}, 500
        finally:
            if 'temp_file_path' in locals():
                os.unlink(temp_file_path)

@ns.route('/import-data')
class ImportData(Resource):
    @jwt_required()
    @ns.expect(api.parser().add_argument('file', location='files', type='file', required=True))
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def post(self):
        """Import data from a JSON file to Redis"""
        try:
            if 'file' not in request.files:
                logger.warning("No file part in the request")
                return {"error": "No file part in the request"}, 400
            
            file = request.files['file']
            if file.filename == '':
                logger.warning("No selected file")
                return {"error": "No selected file"}, 400
            
            if not file.filename.lower().endswith('.json'):
                logger.warning("Invalid file type")
                return {"error": "Invalid file type. Please upload a JSON file"}, 400

            data = json.load(file)

            for channel_id, value in data.get('channels', {}).items():
                redis_client.hmset(f"{CHANNEL_PREFIX}{channel_id}", value)

            for channel_id, value in data.get('video_lists', {}).items():
                for video_id, video_data in value.items():
                    redis_client.hset(f"{VIDEO_PREFIX}{channel_id}", video_id, json.dumps(video_data))

            logger.info("Successfully imported data from JSON file")
            return {"message": "Data imported successfully"}, 200

        except json.JSONDecodeError:
            logger.error("Invalid JSON format in the uploaded file")
            return {"error": "Invalid JSON format in the uploaded file"}, 400
        except Exception as e:
            logger.error(f"Error importing data: {str(e)}")
            return {"error": f"Error importing data: {str(e)}"}, 500

# Add user namespace to API
api.add_namespace(auth_ns, path='/daily-dictation/auth')
api.add_namespace(user_ns, path='/daily-dictation/user')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=4001)
