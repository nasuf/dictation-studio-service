import sys
import os
import json
import re
import logging
from flask import Flask, request, jsonify
from flask_restx import Api, Resource, fields
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
import redis
import requests
from bs4 import BeautifulSoup
from config import CHANNEL_PREFIX, REDIS_HOST, REDIS_PORT, REDIS_RESOURCE_DB, REDIS_USER_DB, VIDEO_PREFIX, REDIS_PASSWORD
from flask_jwt_extended import JWTManager, jwt_required
from config import JWT_SECRET_KEY, JWT_ACCESS_TOKEN_EXPIRES
import yt_dlp as youtube_dl
from werkzeug.utils import secure_filename
from auth import auth_ns
from error_handlers import register_error_handlers
from user import user_ns
from payment import payment_ns

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def load_cookies():
    cookie_file = os.path.join(os.path.dirname(__file__), 'youtube.com_cookies.txt')
    if os.path.exists(cookie_file):
        return cookie_file
    return None

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

# Redis connection
redis_resource_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_RESOURCE_DB, password=REDIS_PASSWORD)
redis_user_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_USER_DB, password=REDIS_PASSWORD)

app.config['redis_resource_client'] = redis_resource_client
app.config['redis_user_client'] = redis_user_client

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

def download_transcript_from_youtube_transcript_api(video_id):
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

def download_transcript(video_id):
    """Download the transcript and return as a list of dictionaries."""
    try:
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en.*'],
            'outtmpl': '%(id)s.%(ext)s',
            'no_warnings': True,
            'ignoreerrors': True,
            'nocheckcertificate': True,
            'quiet': True,
            'no_color': True,
            'extractor_args': {'youtube': {'skip': ['dash', 'hls']}},
            'cookiefile': load_cookies(),
        }
        
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            
            if info is None:
                raise Exception("Unable to extract video information")

            subtitle_url = None
            for lang in info.get('subtitles', {}):
                if lang.startswith('en'):
                    subtitle_url = info['subtitles'][lang][0]['url']
                    break
            
            if subtitle_url is None:
                for lang in info.get('automatic_captions', {}):
                    if lang.startswith('en'):
                        subtitle_url = info['automatic_captions'][lang][0]['url']
                        break

            if subtitle_url is None:
                raise Exception("No English subtitles available")

            subtitle_content = requests.get(subtitle_url).text
            
            # Parse the JSON content
            subtitle_data = json.loads(subtitle_content)
            
            formatted_transcript = []
            for event in subtitle_data.get('events', []):
                start = event.get('tStartMs', 0) / 1000  # Convert to seconds
                duration = event.get('dDurationMs', 0) / 1000  # Convert to seconds
                end = start + duration
                text = ' '.join([seg.get('utf8', '') for seg in event.get('segs', [])])
                
                formatted_entry = {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "transcript": text.strip()
                }
                formatted_transcript.append(formatted_entry)

            return formatted_transcript
    except Exception as e:
        logger.error(f"Error downloading transcript for video {video_id}: {e}")
        return None

def convert_time_to_seconds(time_str):
    """Convert time string to seconds."""
    h, m, s = time_str.split(':')
    return round(int(h) * 3600 + int(m) * 60 + float(s), 2)

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
    
def parse_srt_file(file_path):
    """
    Parse SRT format content and return a list of dictionaries.
    
    Each dictionary contains:
    - start: start time in seconds
    - end: end time in seconds
    - transcript: the text of the subtitle
    """
    with open(file_path, 'r', encoding='utf-8') as file:
        srt_content = file.read()
    
    # Split the content into subtitle blocks
    subtitle_blocks = re.split(r'\n\n+', srt_content.strip())
    
    formatted_transcript = []
    for block in subtitle_blocks:
        lines = block.split('\n')
        if len(lines) >= 3:  # Ensure we have at least index, time, and text
            # Extract time information
            time_line = lines[1]
            start_time, end_time = time_line.split(' --> ')
            
            # Convert time to seconds
            start_seconds = convert_time_to_seconds(start_time)
            end_seconds = convert_time_to_seconds(end_time)
            
            # Join all lines after the time line as the transcript text
            transcript_text = ' '.join(lines[2:]).replace('\n', ' ').strip()
            
            formatted_transcript.append({
                "start": start_seconds,
                "end": end_seconds,
                "transcript": transcript_text
            })
    
    return formatted_transcript

def convert_time_to_seconds(time_str):
    """Convert SRT time format (HH:MM:SS,mmm) to seconds."""
    hours, minutes, rest = time_str.split(':')
    seconds, milliseconds = rest.split(',')
    total_seconds = int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000
    return round(total_seconds, 2)

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
                
                if not channel_name or not channel_image_url or not channel_id:
                    logger.warning(f"Invalid input for channel {channel_id}")
                    return {"error": f"Invalid input for channel {channel_id}. Name, id, and image_url are required."}, 400

                channel_key = f"{CHANNEL_PREFIX}{channel_id}"
                channel_info = {
                    'id': channel_id,
                    'name': channel_name,
                    'image_url': channel_image_url,
                    'visibility': channel_visibility
                }
                redis_resource_client.hmset(channel_key, channel_info)
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
            for key in redis_resource_client.scan_iter(f"{CHANNEL_PREFIX}*"):
                channel_info = redis_resource_client.hgetall(key)
                channel_data = {k.decode(): v.decode() for k, v in channel_info.items()}
                # if visibility is not public, skip
                if channel_data.get('visibility') != 'public':
                    continue
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
        'visibility': fields.String(required=False, description='Channel visibility (open, hidden, or user:user_id)')
    }))
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def put(self, channel_id):
        """Update specific fields of a YouTube channel"""
        try:
            data = request.json
            channel_key = f"{CHANNEL_PREFIX}{channel_id}"
            
            if not redis_resource_client.exists(channel_key):
                logger.warning(f"Channel not found: {channel_id}")
                return {"error": "Channel not found"}, 404
            
            channel_info = redis_resource_client.hgetall(channel_key)
            
            # Update fields if provided
            if 'name' in data:
                channel_info[b'name'] = data['name'].encode()
            if 'image_url' in data:
                channel_info[b'image_url'] = data['image_url'].encode()
            if 'visibility' in data:
                channel_info[b'visibility'] = data['visibility'].encode()
            
            # Save updated channel info
            redis_resource_client.hmset(channel_key, channel_info)
            
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
            channel_data = {k.decode(): v.decode() for k, v in channel_info.items()}
            
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
    @ns.param('transcript_files', 'Transcript files', type='file', required=True)
    def post(self):
        """Save multiple YouTube videos with transcripts for multiple channels to Redis"""
        try:
            data = json.loads(request.form.get('data', '[]'))
            transcript_files = request.files.getlist('transcript_files')
            uploads_dir = os.getenv('UPLOADS_DIR', './uploads')
            os.makedirs(uploads_dir, exist_ok=True)

            if not data or len(data) != len(transcript_files):
                logger.warning("Invalid input: data and transcript files mismatch")
                return {"error": "Invalid input. Data and transcript files must match."}, 400

            results = []
            for video_data, transcript_file in zip(data, transcript_files):
                channel_id = video_data.get('channel_id')
                video_link = video_data.get('video_link')
                title = video_data.get('title')

                if not channel_id or not video_link or not title:
                    logger.warning(f"Invalid input for video: {video_link}")
                    results.append({"error": f"Invalid input for video: {video_link}. channel_id, video_link, and title are required."})
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

                # Save the uploaded transcript file
                filename = secure_filename(f"{video_id}.srt")
                file_path = os.path.join(uploads_dir, filename)
                
                try:
                    transcript_file.save(file_path)
                    logger.info(f"File saved successfully: {file_path}")
                except Exception as e:
                    logger.error(f"Error saving file {filename}: {str(e)}")
                    results.append({"error": f"Error saving file for video {video_id}: {str(e)}"})
                    continue

                # Parse the SRT file
                transcript = parse_srt_file(file_path)
                if transcript is None:
                    logger.error(f"Unable to parse SRT file for video: {video_link}")
                    results.append({"error": f"Unable to parse SRT file for video: {video_link}"})
                    continue

                video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
                video_info = {
                    "link": video_link,
                    "video_id": video_id,
                    "title": title,
                    "transcript": json.dumps(transcript)
                }
                redis_resource_client.hmset(video_key, video_info)

                logger.info(f"Successfully saved/updated video {video_id} for channel {channel_id}")
                results.append({"success": f"Video {video_id} saved/updated successfully for channel {channel_id}"})

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
                key_str = key.decode('utf-8')
                channel_id = key_str.split(':')[1]  # video:channel_id:video_id
                video_data = redis_resource_client.hgetall(key)
                
                if not video_data:
                    continue

                video_info = {
                    'link': video_data[b'link'].decode(),
                    'video_id': video_data[b'video_id'].decode(),
                    'title': video_data[b'title'].decode(),
                    'transcript': json.loads(video_data[b'transcript'].decode())
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
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 500: 'Server Error'})
    def get(self, channel_id):
        """Get video IDs and links for a specific channel"""
        try:
            pattern = f"{VIDEO_PREFIX}{channel_id}:*"
            videos = []
            
            for key in redis_resource_client.scan_iter(pattern):
                video_data = redis_resource_client.hgetall(key)
                if video_data:
                    videos.append({
                        "video_id": video_data[b'video_id'].decode(),
                        "link": video_data[b'link'].decode(),
                        "title": video_data[b'title'].decode(),
                        'transcript': json.loads(video_data[b'transcript'].decode())
                    })
            
            logger.info(f"Retrieved {len(videos)} videos for channel: {channel_id}")
            return {"channel_id": channel_id, "videos": videos}, 200
            
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
            video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
            video_data = redis_resource_client.hgetall(video_key)
            
            if not video_data:
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found"}, 404
            
            logger.info(f"Retrieved transcript for video {video_id} in channel {channel_id}")
            return {
                "channel_id": channel_id,
                "video_id": video_id,
                "title": video_data[b'title'].decode(),
                "transcript": json.loads(video_data[b'transcript'].decode())
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

            transcript = json.loads(video_data[b'transcript'].decode())
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
        try:
            data = request.json
            new_transcript = data.get('transcript')

            if not new_transcript:
                logger.warning("No transcript data provided")
                return {"error": "Transcript data is required"}, 400

            video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
            video_data = redis_resource_client.hgetall(video_key)
            
            if not video_data:
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found"}, 404

            # copy original transcript to original_transcript
            # if original_transcript field is not existing, get current transcript from redis then copy to original_transcript
            if b'original_transcript' not in video_data:
                original_transcript = json.loads(video_data[b'transcript'].decode())
                redis_resource_client.hset(video_key, 'original_transcript', json.dumps(original_transcript))   

            redis_resource_client.hset(video_key, 'transcript', json.dumps(new_transcript))
            logger.info(f"Updated full transcript for video {video_id} in channel {channel_id}")
            return {"message": "Full transcript updated successfully"}, 200

        except Exception as e:
            logger.error(f"Error updating full transcript: {str(e)}")
            return {"error": f"Error updating full transcript: {str(e)}"}, 500

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

            for user_key in redis_user_client.scan_iter("user:*"):
                user_data = redis_user_client.hgetall(user_key)
                if b'dictation_progress' in user_data:
                    dictation_progress = json.loads(user_data[b'dictation_progress'].decode('utf-8'))
                    video_key = f"{channel_id}:{video_id}"
                    if video_key in dictation_progress:
                        del dictation_progress[video_key]
                        redis_user_client.hset(user_key, 'dictation_progress', json.dumps(dictation_progress))
                        logger.info(f"Removed dictation progress for video {video_id} from user {user_key.decode('utf-8')}")

            logger.info(f"Successfully deleted video {video_id} from channel {channel_id}")
            return {"message": f"Video {video_id} deleted successfully from channel {channel_id}"}, 200

        except Exception as e:
            logger.error(f"Error deleting video {video_id} from channel {channel_id}: {str(e)}")
            return {"error": f"Error deleting video: {str(e)}"}, 500

@ns.route('/video-list/<string:channel_id>/<string:video_id>')
class YouTubeVideoUpdate(Resource):
    @jwt_required()
    @ns.expect(api.model('VideoUpdate', {
        'link': fields.String(required=True, description='Updated YouTube video URL'),
        'title': fields.String(required=True, description='Updated video title')
    }))
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def put(self, channel_id, video_id):
        """Update a specific video's attributes (except transcript) in a channel"""
        try:
            data = request.json
            new_link = data.get('link')
            new_title = data.get('title')

            if not new_link or not new_title:
                logger.warning("Invalid input: link and title are required")
                return {"error": "Invalid input. Both link and title are required."}, 400

            new_video_id = get_video_id(new_link)
            if not new_video_id:
                logger.warning(f"Invalid YouTube URL: {new_link}")
                return {"error": f"Invalid YouTube URL: {new_link}"}, 400

            old_video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
            video_data = redis_resource_client.hgetall(old_video_key)
            
            if not video_data:
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found"}, 404

            if new_video_id != video_id:
                new_video_key = f"{VIDEO_PREFIX}{channel_id}:{new_video_id}"
                redis_resource_client.hmset(new_video_key, {
                    'link': new_link,
                    'video_id': new_video_id,
                    'title': new_title,
                    'transcript': video_data[b'transcript'].decode()
                })
                redis_resource_client.delete(old_video_key)
            else:
                redis_resource_client.hmset(old_video_key, {
                    'link': new_link,
                    'title': new_title
                })

            logger.info(f"Successfully updated video {video_id} in channel {channel_id}")
            return {"message": f"Video {video_id} updated successfully"}, 200

        except Exception as e:
            logger.error(f"Error updating video: {str(e)}")
            return {"error": f"Error updating video: {str(e)}"}, 500

@ns.route('/<string:channel_id>/<string:video_id>/restore-transcript')
class RestoreVideoTranscript(Resource):
    @jwt_required()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self, channel_id, video_id):
        """Restore transcript for a specific video from original_transcript or SRT file"""
        try:
            video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
            video_data = redis_resource_client.hgetall(video_key)
            
            if not video_data:
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found"}, 404

            restored = False
            # Firstly, try to restore from original_transcript
            if b'original_transcript' in video_data:
                try:
                    original_transcript = json.loads(video_data[b'original_transcript'].decode())
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
                    logger.warning(f"SRT file not found for video {video_id}")
                    return {"error": f"SRT file not found and no original transcript available for video {video_id}"}, 404

                transcript = parse_srt_file(file_path)
                if transcript is None:
                    logger.error(f"Unable to parse SRT file for video: {video_id}")
                    return {"error": f"Unable to parse SRT file for video: {video_id}"}, 500

                redis_resource_client.hset(video_key, 'transcript', json.dumps(transcript))
                # delete original_transcript field
                redis_resource_client.hdel(video_key, 'original_transcript')
                logger.info(f"Successfully restored transcript from SRT file for video {video_id}")

            return {
                "channel_id": channel_id,
                "video_id": video_id,
                "title": video_data[b'title'].decode(),
                "transcript": json.loads(redis_resource_client.hget(video_key, 'transcript').decode())
            }, 200

        except Exception as e:
            logger.error(f"Error restoring transcript: {str(e)}")
            return {"error": f"Error restoring transcript: {str(e)}"}, 500

# Add user namespace to API
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=4001)