import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify
from flask_restx import Api, Resource, fields
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
import redis
import re
from config import REDIS_HOST, REDIS_PORT, REDIS_DB  # Changed from relative to absolute import
import json
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)

api = Api(app, version='1.0', title='YouTube Transcript Downloader API',
          description='API for downloading YouTube video transcripts')

ns = api.namespace('api', description='YouTube Transcript operations')

# Redis connection
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)

# Input model for Swagger
youtube_url_model = api.model('YouTubeURL', {
    'url': fields.String(required=True, description='YouTube video URL')
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
        print(f"Available languages: {', '.join(available_languages)}")

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
        print(f"Error downloading transcript: {e}")
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
        print(f"Error getting video title: {e}")
        return None

# Update the channel_info_model
channel_info_model = api.model('ChannelInfo', {
    'channels': fields.List(fields.Nested(api.model('Channel', {
        'name': fields.String(required=True, description='YouTube channel name'),
        'id': fields.String(required=True, description='YouTube channel ID'),
        'image_url': fields.String(required=True, description='YouTube channel image URL')
    })))
})

# Add this new model
video_list_model = api.model('VideoList', {
    'channel_id': fields.String(required=True, description='YouTube channel ID'),
    'video_links': fields.List(fields.String, required=True, description='List of YouTube video links')
})

@ns.route('/transcript')
class YouTubeTranscript(Resource):
    @ns.expect(youtube_url_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid URL', 500: 'Server Error'})
    def post(self):
        """Get the transcript for a YouTube video"""
        data = request.json
        youtube_url = data.get('url')
        
        video_id = get_video_id(youtube_url)
        if not video_id:
            return {"error": "Invalid YouTube URL"}, 400

        transcript = download_transcript(video_id)
        if transcript is None:
            return {"error": "Unable to download transcript"}, 500

        return jsonify(transcript)

@ns.route('/channel')
class YouTubeChannel(Resource):
    @ns.expect(channel_info_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 500: 'Server Error'})
    def post(self):
        """Save YouTube channel information to Redis"""
        data = request.json
        channels = data.get('channels', [])
        
        if not channels:
            return {"error": "Invalid input. 'channels' list is required."}, 400

        try:
            for channel in channels:
                channel_name = channel.get('name')
                channel_id = channel.get('id')
                channel_image_url = channel.get('image_url')
                
                if not channel_name or not channel_image_url or not channel_id:
                    return {"error": f"Invalid input for channel {channel_id}. Name, id, and image_url are required."}, 400

                channel_info = {
                    'id': channel_id,
                    'name': channel_name,
                    'image_url': channel_image_url
                }
                redis_client.hset('video_channel', channel_id, json.dumps(channel_info))
            
            return {"message": f"{len(channels)} channel(s) information saved successfully"}, 200
        except Exception as e:
            return {"error": f"Error saving channel information: {str(e)}"}, 500

    @ns.doc(responses={200: 'Success', 500: 'Server Error'})
    def get(self):
        """Get all YouTube channel information from Redis"""
        try:
            all_channels = redis_client.hgetall('video_channel')
            channels = []
            for _, value in all_channels.items():
                channel_info = json.loads(value.decode())
                channels.append(channel_info)
            return channels, 200  # Return the list directly, don't use jsonify
        except Exception as e:
            return {"error": f"Error retrieving channel information: {str(e)}"}, 500

@ns.route('/video-list')
class YouTubeVideoList(Resource):
    @ns.expect(video_list_model)
    @ns.doc(responses={200: 'Success', 400: 'Invalid Input', 500: 'Server Error'})
    def post(self):
        """Save YouTube video list with transcripts and titles for a channel to Redis"""
        data = request.json
        channel_id = data.get('channel_id')
        video_links = data.get('video_links', [])
        
        if not channel_id or not video_links:
            return {"error": "Invalid input. 'channel_id' and 'video_links' are required."}, 400

        try:
            videos = []
            for link in video_links:
                video_id = get_video_id(link)
                if not video_id:
                    return {"error": f"Invalid YouTube URL: {link}"}, 400
                
                transcript = download_transcript(video_id)
                if transcript is None:
                    return {"error": f"Unable to download transcript for video: {link}"}, 500
                
                title = get_video_title(video_id)
                
                videos.append({
                    "link": link,
                    "video_id": video_id,
                    "title": title,
                    "transcript": transcript
                })
            
            video_info = {
                'channel_id': channel_id,
                'videos': videos
            }
            redis_client.hset('video_list', channel_id, json.dumps(video_info))
            
            return {"message": f"Video list with transcripts and titles for channel {channel_id} saved successfully"}, 200
        except Exception as e:
            return {"error": f"Error saving video list with transcripts and titles: {str(e)}"}, 500

    @ns.doc(responses={200: 'Success', 500: 'Server Error'})
    def get(self):
        """Get all YouTube video lists with transcripts from Redis"""
        try:
            all_video_lists = redis_client.hgetall('video')
            video_lists = []
            for _, value in all_video_lists.items():
                video_list_info = json.loads(value.decode())
                video_lists.append(video_list_info)
            return video_lists, 200
        except Exception as e:
            return {"error": f"Error retrieving video lists with transcripts: {str(e)}"}, 500

@ns.route('/video-list/<string:channel_id>')
class YouTubeVideoList(Resource):
    @ns.doc(responses={200: 'Success', 404: 'Channel not found', 500: 'Server Error'})
    def get(self, channel_id):
        """Get video IDs and links for a specific channel"""
        try:
            video_info = redis_client.hget('video_list', channel_id)
            if video_info is None:
                return {"error": "Channel not found"}, 404
            
            video_info = json.loads(video_info.decode())
            videos = video_info.get('videos', [])
            
            simplified_videos = [
                {"video_id": video["video_id"], "link": video["link"], "title": video["title"]}
                for video in videos
            ]
            
            return {"channel_id": channel_id, "videos": simplified_videos}, 200
        except Exception as e:
            return {"error": f"Error retrieving video list: {str(e)}"}, 500

@ns.route('/video-transcript/<string:channel_id>/<string:video_id>')
class VideoTranscript(Resource):
    @ns.doc(responses={200: 'Success', 404: 'Not Found', 500: 'Server Error'})
    def get(self, channel_id, video_id):
        """Get transcript for a specific video in a channel"""
        try:
            video_info = redis_client.hget('video_list', channel_id)
            if video_info is None:
                return {"error": "Channel not found"}, 404
            
            video_info = json.loads(video_info.decode())
            videos = video_info.get('videos', [])
            
            for video in videos:
                if video["video_id"] == video_id:
                    return {"channel_id": channel_id, "video_id": video_id, "title": video["title"], "transcript": video["transcript"]}, 200
            
            return {"error": "Video not found in the channel"}, 404
        except Exception as e:
            return {"error": f"Error retrieving video transcript: {str(e)}"}, 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=4001)
