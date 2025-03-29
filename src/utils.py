from functools import wraps
import logging
import time
import redis
import re
import os
import json
import requests
import hashlib
from config import JWT_ACCESS_TOKEN_EXPIRES, PAYMENT_MAX_RETRY_ATTEMPTS, PAYMENT_RETRY_DELAY_SECONDS, REDIS_HOST, REDIS_PORT, REDIS_BLACKLIST_DB, REDIS_PASSWORD, REDIS_USER_DB, USER_PREFIX
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp as youtube_dl
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

redis_blacklist_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_BLACKLIST_DB, password=REDIS_PASSWORD)
redis_user_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_USER_DB, password=REDIS_PASSWORD)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def add_token_to_blacklist(jti):
    redis_blacklist_client.set(jti, 'true', ex=JWT_ACCESS_TOKEN_EXPIRES)

def with_retry(max_attempts=PAYMENT_MAX_RETRY_ATTEMPTS, delay_seconds=PAYMENT_RETRY_DELAY_SECONDS):
    """Retry decorator"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    logger.warning(f"Attempt {attempt + 1} failed: {str(e)}")
                    if attempt < max_attempts - 1:
                        time.sleep(delay_seconds)
            raise last_exception
        return wrapper
    return decorator

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

def load_cookies():
    cookie_file = os.path.join(os.path.dirname(__file__), 'youtube.com_cookies.txt')
    if os.path.exists(cookie_file):
        return cookie_file
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

def hash_password(password):
    """
    Perform server-side encryption on the password.
    """
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return salt + key

def verify_password(stored_password, provided_password):
    """
    Verify the provided password against the stored password.
    """
    salt = stored_password[:32]
    stored_key = stored_password[32:]
    new_key = hashlib.pbkdf2_hmac('sha256', provided_password.encode('utf-8'), salt, 100000)
    return new_key == stored_key

def get_plan_name_by_duration(days_duration):
    """
    根据会员天数确定对应的计划名称
    
    Args:
        days_duration (int): 会员时长（天数），-1表示永久
        
    Returns:
        str: 计划名称 (Premium/Pro/Basic/Free)
    """
    if days_duration == -1:  # 永久会员
        return "Premium"
    elif days_duration >= 90:  # 90天及以上
        return "Premium"
    elif days_duration >= 60:  # 60-89天
        return "Pro"
    elif days_duration >= 30:  # 30-59天
        return "Basic"
    elif days_duration > 0:  # 少于30天但大于0
        return "Basic"
    else:  # 0或负数（非-1）
        return "Free"

def update_user_plan(user_email, plan_name, days_duration, is_recurring=False):
    """
    更新用户的会员计划
    
    Args:
        user_email (str): 用户邮箱
        plan_name (str): 计划名称
        days_duration (int): 会员时长（天数），-1表示永久
        is_recurring (bool): 是否为周期性付款
        
    Returns:
        dict: 更新后的计划数据
    """
    try:
        # 获取用户数据
        user_key = f"{USER_PREFIX}{user_email}"
        user_data = redis_user_client.hgetall(user_key)
        
        if not user_data:
            raise Exception(f"User {user_email} not found")
        
        # 检查用户是否已有计划
        current_plan_data = {}
        if b'plan' in user_data:
            try:
                current_plan_data = json.loads(user_data[b'plan'].decode('utf-8'))
            except:
                logger.warning(f"Could not parse existing plan data for user {user_email}")
        
        # 如果是永久会员，直接设置为永久
        if days_duration == -1:
            expire_time = datetime.max.isoformat()
        else:
            # 如果已有计划且不是永久会员
            if current_plan_data and 'expireTime' in current_plan_data:
                current_expire_time = current_plan_data.get('expireTime')
                
                # 如果当前是永久会员，保持永久
                if current_expire_time == datetime.max.isoformat():
                    expire_time = datetime.max.isoformat()
                else:
                    try:
                        # 解析当前过期时间
                        current_expire_datetime = datetime.fromisoformat(current_expire_time)
                        
                        # 如果当前计划已过期，从现在开始计算
                        if current_expire_datetime < datetime.now():
                            expire_time = (datetime.now() + timedelta(days=days_duration)).isoformat()
                        else:
                            # 如果当前计划未过期，累加天数
                            expire_time = (current_expire_datetime + timedelta(days=days_duration)).isoformat()
                    except:
                        # 如果解析失败，从现在开始计算
                        logger.warning(f"Could not parse expire time for user {user_email}, using current time")
                        expire_time = (datetime.now() + timedelta(days=days_duration)).isoformat()
            else:
                # 如果没有现有计划，从现在开始计算
                expire_time = (datetime.now() + timedelta(days=days_duration)).isoformat()
        
        # 根据新的过期时间重新确定计划名称
        if expire_time == datetime.max.isoformat():
            # 永久会员使用Premium计划
            final_plan_name = "Premium"
        else:
            try:
                # 计算从现在到过期时间的天数
                expire_datetime = datetime.fromisoformat(expire_time)
                remaining_days = (expire_datetime - datetime.now()).days
                
                # 根据剩余天数确定计划名称
                final_plan_name = get_plan_name_by_duration(remaining_days)
            except:
                # 如果计算失败，使用传入的计划名称
                logger.warning(f"Could not calculate remaining days for user {user_email}, using provided plan name")
                final_plan_name = plan_name
        
        # 创建计划数据
        plan_data = {
            'name': final_plan_name,
            'expireTime': expire_time,
            'isRecurring': str(is_recurring).lower()
        }
        
        # 保存计划数据
        redis_user_client.hset(user_key, 'plan', json.dumps(plan_data))
        
        return plan_data
    except Exception as e:
        logger.error(f"Error updating user plan: {str(e)}")
        raise e