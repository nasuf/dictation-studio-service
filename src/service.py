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
from config import CHANNEL_PREFIX, REDIS_HOST, REDIS_PORT, REDIS_RESOURCE_DB, REDIS_USER_DB, VIDEO_PREFIX
from flask_jwt_extended import JWTManager, jwt_required
from config import JWT_SECRET_KEY, JWT_ACCESS_TOKEN_EXPIRES
from auth import auth_ns
from user import user_ns
import yt_dlp as youtube_dl
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename
import shutil  # 添加这个导入
from jwt_utils import jwt_required_and_refresh

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
CORS(app)
app.config['JWT_SECRET_KEY'] = JWT_SECRET_KEY
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = JWT_ACCESS_TOKEN_EXPIRES
app.config['JWT_TOKEN_LOCATION'] = ['headers']  # Only allow JWT tokens in headers
jwt = JWTManager(app)


api = Api(app, version='1.0', title='Dictation Studio API',
          description='API for Dictation Studio')

ns = api.namespace('service', path='/dictation-studio/service', description='Dictation Studio Service Operations')

# Redis connection
redis_resource_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_RESOURCE_DB)
redis_user_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_USER_DB)

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
    @jwt_required_and_refresh()
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
    @jwt_required_and_refresh()
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
                all_channels.append({k.decode(): v.decode() for k, v in channel_info.items()})
            logger.info(f"Retrieved {len(all_channels)} channels from Redis")
            return all_channels, 200
        except Exception as e:
            logger.error(f"Error retrieving channel information: {str(e)}")
            return {"error": f"Error retrieving channel information: {str(e)}"}, 500

@ns.route('/video-list')
class YouTubeVideoList(Resource):
    @jwt_required_and_refresh()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    @ns.param('data', 'JSON array of video data', type='string', required=True)
    @ns.param('transcript_files', 'Transcript files', type='file', required=True)
    def post(self):
        """Save multiple YouTube videos with transcripts for multiple channels to Redis"""
        try:
            data = json.loads(request.form.get('data', '[]'))
            transcript_files = request.files.getlist('transcript_files')

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
                uploads_dir = 'uploads'
                filename = secure_filename(f"{video_id}.srt")
                file_path = os.path.join(uploads_dir, filename)
                os.makedirs(uploads_dir, exist_ok=True)
                transcript_file.save(file_path)

                # Parse the SRT file
                transcript = parse_srt_file(file_path)
                if transcript is None:
                    logger.error(f"Unable to parse SRT file for video: {video_link}")
                    results.append({"error": f"Unable to parse SRT file for video: {video_link}"})
                    continue

                video_list_key = f"{VIDEO_PREFIX}{channel_id}"
                existing_videos = redis_resource_client.hget(video_list_key, 'videos')
                if existing_videos:
                    existing_videos = json.loads(existing_videos.decode())
                else:
                    existing_videos = []

                new_video = {
                    "link": video_link,
                    "video_id": video_id,
                    "title": title,
                    "transcript": transcript
                }

                # Update existing video or add new one
                updated = False
                for i, video in enumerate(existing_videos):
                    if video['video_id'] == video_id:
                        existing_videos[i] = new_video
                        updated = True
                        break
                if not updated:
                    existing_videos.append(new_video)

                redis_resource_client.hset(video_list_key, 'videos', json.dumps(existing_videos))
                redis_resource_client.hset(video_list_key, 'channel_id', channel_id)

                logger.info(f"Successfully saved/updated video {video_id} for channel {channel_id}")
                results.append({"success": f"Video {video_id} saved/updated successfully for channel {channel_id}"})

            # Clean up uploaded files
            for filename in os.listdir(uploads_dir):
                file_path = os.path.join(uploads_dir, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    logger.error(f'Failed to delete {file_path}. Reason: {e}')

            logger.info("Uploads folder cleared successfully")
            return {"results": results}, 200
        except Exception as e:
            logger.error(f"Error saving video list: {str(e)}")
            return {"error": f"Error saving video list with transcripts: {str(e)}"}, 500

    @jwt_required_and_refresh()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def get(self):
        """Get all YouTube video lists with transcripts from Redis"""
        try:
            video_lists = []
            for key in redis_resource_client.scan_iter(f"{VIDEO_PREFIX}*"):
                video_list_data = redis_resource_client.hgetall(key)
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
            video_data = redis_resource_client.hget(video_list_key, 'videos')
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
    @jwt_required_and_refresh()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 500: 'Server Error'})
    def get(self, channel_id, video_id):
        """Get transcript for a specific video in a channel"""
        try:
            video_list_key = f"{VIDEO_PREFIX}{channel_id}"
            video_data = redis_resource_client.hget(video_list_key, 'videos')
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
    @jwt_required_and_refresh()
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
            video_data = redis_resource_client.hget(video_list_key, 'videos')
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
                        redis_resource_client.hset(video_list_key, 'videos', json.dumps(videos))
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

@ns.route('/<string:channel_id>/<string:video_id>/full-transcript')
class FullVideoTranscriptUpdate(Resource):
    @jwt_required_and_refresh()
    @ns.expect(full_transcript_update_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def put(self, channel_id, video_id):
        """Update the entire transcript for a video"""
        try:
            data = request.json
            new_transcript = data.get('transcript')

            if not new_transcript:
                logger.warning("No transcript data provided")
                return {"error": "Transcript data is required"}, 400

            video_list_key = f"{VIDEO_PREFIX}{channel_id}"
            video_data = redis_resource_client.hget(video_list_key, 'videos')
            if video_data is None:
                logger.warning(f"Channel not found: {channel_id}")
                return {"error": "Channel not found"}, 404

            videos = json.loads(video_data.decode())
            video_found = False
            for video in videos:
                if video["video_id"] == video_id:
                    video_found = True
                    video["transcript"] = new_transcript
                    redis_resource_client.hset(video_list_key, 'videos', json.dumps(videos))
                    logger.info(f"Updated full transcript for video {video_id} in channel {channel_id}")
                    return {"message": "Full transcript updated successfully"}, 200

            if not video_found:
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found in the channel"}, 404

        except Exception as e:
            logger.error(f"Error updating full transcript: {str(e)}")
            return {"error": f"Error updating full transcript: {str(e)}"}, 500

@ns.route('/video-list/<string:channel_id>/<string:video_id>')
class YouTubeVideoDelete(Resource):
    @jwt_required_and_refresh()
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized Access', 404: 'Not Found', 500: 'Server Error'})
    def delete(self, channel_id, video_id):
        """Delete a specific video from a channel"""
        try:
            video_list_key = f"{VIDEO_PREFIX}{channel_id}"
            videos_data = redis_resource_client.hget(video_list_key, 'videos')
            
            if videos_data is None:
                logger.warning(f"Channel not found: {channel_id}")
                return {"error": "Channel not found"}, 404

            videos = json.loads(videos_data.decode())
            
            # Find and remove the video
            updated_videos = [video for video in videos if video['video_id'] != video_id]
            
            if len(videos) == len(updated_videos):
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found in the channel"}, 404

            # Update Redis with the new video list
            redis_resource_client.hset(video_list_key, 'videos', json.dumps(updated_videos))

            logger.info(f"Successfully deleted video {video_id} from channel {channel_id}")
            return {"message": f"Video {video_id} deleted successfully from channel {channel_id}"}, 200

        except Exception as e:
            logger.error(f"Error deleting video {video_id} from channel {channel_id}: {str(e)}")
            return {"error": f"Error deleting video: {str(e)}"}, 500

@ns.route('/video-list/<string:channel_id>/<string:video_id>')
class YouTubeVideoUpdate(Resource):
    @jwt_required_and_refresh()
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

            video_list_key = f"{VIDEO_PREFIX}{channel_id}"
            videos_data = redis_resource_client.hget(video_list_key, 'videos')
            
            if videos_data is None:
                logger.warning(f"Channel not found: {channel_id}")
                return {"error": "Channel not found"}, 404

            videos = json.loads(videos_data.decode())
            
            video_found = False
            for video in videos:
                if video['video_id'] == video_id:
                    video_found = True
                    video['link'] = new_link
                    video['video_id'] = new_video_id
                    video['title'] = new_title
                    break
            
            if not video_found:
                logger.warning(f"Video {video_id} not found in channel {channel_id}")
                return {"error": "Video not found in the channel"}, 404

            # Update Redis with the modified video list
            redis_resource_client.hset(video_list_key, 'videos', json.dumps(videos))

            logger.info(f"Successfully updated video {video_id} in channel {channel_id}")
            return {"message": f"Video {video_id} updated successfully in channel {channel_id}"}, 200

        except Exception as e:
            logger.error(f"Error updating video {video_id} in channel {channel_id}: {str(e)}")
            return {"error": f"Error updating video: {str(e)}"}, 500

# Add user namespace to API
api.add_namespace(auth_ns, path='/dictation-studio/auth')
api.add_namespace(user_ns, path='/dictation-studio/user')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=4001)