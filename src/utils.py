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
    Determine the plan name based on membership duration in days
    
    Args:
        days_duration (int): Membership duration in days, -1 indicates permanent/lifetime
        
    Returns:
        str: Plan name (Premium/Pro/Basic/Free)
    """
    if days_duration == -1:  # Permanent/lifetime membership
        return "Premium"
    elif days_duration >= 90:  # 90 days or more
        return "Premium"
    elif days_duration >= 60:  # 60-89 days
        return "Pro"
    elif days_duration >= 30:  # 30-59 days
        return "Basic"
    elif days_duration > 0:  # Less than 30 days but greater than 0
        return "Basic"
    else:  # 0 or negative (except -1)
        return "Free"

def update_user_plan(user_email, plan_name, duration, isRecurring=False, from_order=None, from_code=None):
    """
    Update user's membership plan with comprehensive handling of existing plans
    
    Args:
        user_email (str): User's email
        plan_name (str): Plan name (Premium/Pro/Basic/Free)
        duration (int): Number of days to add
        isRecurring (bool): Whether the plan is recurring
        from_order (str, optional): Order number that triggered this update
        from_code (str, optional): Verification code that triggered this update
    
    Returns:
        dict: Updated plan data
    """
    user_key = f"{USER_PREFIX}{user_email}"
    
    # 获取用户当前计划
    user_data = redis_user_client.hgetall(user_key)
    current_plan = None
    if b'plan' in user_data:
        try:
            current_plan = json.loads(user_data[b'plan'].decode('utf-8'))
        except json.JSONDecodeError:
            current_plan = None

    # 计算新的过期时间
    now = datetime.now()
    new_expire_time = now + timedelta(days=duration)

    # 如果有当前计划且未过期，累加时长
    if current_plan and current_plan.get('expireTime'):
        try:
            current_expire_time = datetime.strptime(current_plan['expireTime'], '%Y-%m-%d %H:%M:%S')
            # 如果当前计划未过期，从当前过期时间开始累加
            if current_expire_time > now:
                new_expire_time = current_expire_time + timedelta(days=duration)
            
            # 确定最终的计划名称（保留较高级别的计划）
            current_plan_name = current_plan.get('name', 'Free')
            plan_levels = {'Premium': 3, 'Pro': 2, 'Basic': 1, 'Free': 0}
            current_level = plan_levels.get(current_plan_name, 0)
            new_level = plan_levels.get(plan_name, 0)
            final_plan_name = current_plan_name if current_level >= new_level else plan_name

            # 保持原有的recurring状态，如果当前是recurring的话
            isRecurring = current_plan.get('isRecurring', isRecurring)
            
            plan_data = {
                "name": final_plan_name,
                "expireTime": new_expire_time.strftime('%Y-%m-%d %H:%M:%S') if not isRecurring else None,
                "nextPaymentTime": new_expire_time.strftime('%Y-%m-%d %H:%M:%S') if isRecurring else None,
                "isRecurring": isRecurring,
                "status": "active"
            }
        except (ValueError, TypeError):
            # 如果解析当前过期时间失败，使用新计算的值
            plan_data = {
                "name": plan_name,
                "expireTime": new_expire_time.strftime('%Y-%m-%d %H:%M:%S') if not isRecurring else None,
                "nextPaymentTime": new_expire_time.strftime('%Y-%m-%d %H:%M:%S') if isRecurring else None,
                "isRecurring": isRecurring,
                "status": "active"
            }
    else:
        # 如果没有当前计划，创建新的计划数据
        plan_data = {
            "name": plan_name,
            "expireTime": new_expire_time.strftime('%Y-%m-%d %H:%M:%S') if not isRecurring else None,
            "nextPaymentTime": new_expire_time.strftime('%Y-%m-%d %H:%M:%S') if isRecurring else None,
            "isRecurring": isRecurring,
            "status": "active"
        }

    # 记录计划更新历史
    update_history = []
    if b'plan_update_history' in user_data:
        try:
            update_history = json.loads(user_data[b'plan_update_history'].decode('utf-8'))
        except json.JSONDecodeError:
            update_history = []

    # 创建更新记录
    update_record = {
        'time': now.isoformat(),
        'days_added': duration
    }

    if from_order:
        update_record['order_id'] = from_order
    if from_code:
        update_record['code'] = from_code

    update_history.append(update_record)

    # 如果用户获得了付费计划（不是Free），删除quota信息
    if plan_name != 'Free':
        redis_user_client.hdel(user_key, 'quota')
        logger.info(f"Deleted quota information for user {user_email} after upgrading to {plan_name} plan")

    # 存储计划数据和历史记录到Redis
    redis_user_client.hset(user_key, 'plan', json.dumps(plan_data))
    redis_user_client.hset(user_key, 'plan_update_history', json.dumps(update_history))

    return plan_data

def is_plan_valid(user_info):
    """Check if user's plan is valid"""
    # If user has permanent plan, always return True
    if user_info.get("isPermanent", False):
        return True
    
    # Check expiration time for non-permanent plans
    expire_time = user_info.get("expireTime")
    if not expire_time:
        return False
    
    try:
        expire_date = datetime.fromisoformat(expire_time)
        current_time = datetime.now()
        return expire_date > current_time
    except (ValueError, TypeError):
        return False

def init_quota(user_email):
    user_key = f"{USER_PREFIX}{user_email}"
    user_data = redis_user_client.hgetall(user_key)
    if not user_data:
        return {"error": "User not found"}, 404
    quota = {
        "first_use_time": datetime.now().isoformat(),
        "videos": [],
        "history": []
    }
    redis_user_client.hset(user_key, 'quota', json.dumps(quota))
    return quota

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