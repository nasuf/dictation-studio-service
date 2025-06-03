# Dictation Studio Backend Development Guide

## 📋 Project Overview

### Introduction

Dictation Studio backend is a RESTful API service built with Flask that provides comprehensive server-side functionality for the English listening and dictation practice platform. It handles user authentication, video transcript processing, progress tracking, payment management, and admin operations.

### Core Features

- 🔐 JWT-based authentication with Google OAuth integration
- 🎥 YouTube transcript extraction and caching
- 👤 User management and progress tracking
- 💳 Stripe payment integration and subscription management
- 📊 Quota system for free users
- 🔧 Admin dashboard APIs
- 📱 Multi-language content support

### Backend Architecture

```
Backend (Flask + Python)
├── Web Framework: Flask + Flask-RESTX
├── Database: Redis (Multi-database)
├── Authentication: JWT + Google OAuth
├── Payment: Stripe Integration
├── External APIs: YouTube Transcript API
├── Caching: Redis-based caching
└── Documentation: Auto-generated Swagger docs
```

---

## 🔧 Technology Stack Analysis

### Core Dependencies

```txt
Flask==2.3.2
flask-restx==1.1.0
redis==4.5.4
flask-jwt-extended==4.4.4
stripe==7.13.0
youtube-transcript-api==0.6.2
yt-dlp==2023.9.24
```

### Architecture Features

- **RESTful API**: Flask-RESTX based API design with auto-documentation
- **JWT Authentication**: Stateless authentication system
- **Redis Storage**: High-performance NoSQL database
- **Modular Design**: Function-based module structure

---

## 📁 Project Architecture

### Module Structure

```
src/
├── app.py              # Application entry point (Legacy)
├── service.py          # Main service module and routes
├── auth.py             # Authentication module
├── user.py             # User management module
├── payment.py          # Payment module
├── utils.py            # Utility functions
├── redis_manager.py    # Redis connection management
├── config.py           # Configuration file
└── error_handlers.py   # Error handling
```

### Redis Database Separation Strategy

```python
# Database separation configuration
REDIS_USER_DB = 0       # User data
REDIS_RESOURCE_DB = 1   # Resource data (videos/channels)
REDIS_BLACKLIST_DB = 2  # JWT blacklist

class RedisManager:
    @classmethod
    def get_user_client(cls):
        # User data connection pool
        if cls._user_pool is None:
            cls._user_pool = ConnectionPool(
                host=REDIS_HOST, port=REDIS_PORT,
                db=REDIS_USER_DB, password=REDIS_PASSWORD,
                decode_responses=True
            )
        return redis.Redis(connection_pool=cls._user_pool)
```

---

## 🚀 Core Module Details

### Authentication Module (auth.py)

**JWT Authentication Flow:**

```python
@auth_ns.route('/login')
class Login(Resource):
    @auth_ns.expect(login_model)
    def post(self):
        data = request.json
        email = data.get('email')

        # Check if user exists
        user_key = f"{USER_PREFIX}{email}"
        user_data = redis_user_client.hgetall(user_key)

        if user_data:
            # Generate JWT tokens
            access_token = create_access_token(identity=email)
            refresh_token = create_refresh_token(identity=email)

            response = make_response(jsonify(user_data))
            response.headers['x-ds-access-token'] = access_token
            response.headers['x-ds-refresh-token'] = refresh_token

            return response
```

**User Registration Flow:**

```python
@auth_ns.route('/register')
class Register(Resource):
    def post(self):
        data = request.json

        # Password encryption
        hashed_password = hash_password(data.get('password'))

        # Create user data
        user_data = {
            'username': data.get('username'),
            'email': data.get('email'),
            'password': hashed_password,
            'avatar': data.get('avatar'),
            'plan': USER_PLAN_DEFAULT,
            'role': USER_ROLE_DEFAULT,
            'dictation_config': USER_DICTATION_CONFIG_DEFAULT,
            'language': USER_LANGUAGE_DEFAULT
        }

        # Store to Redis
        user_key = f"{USER_PREFIX}{email}"
        redis_user_client.hset(user_key, mapping=user_data)
```

### User Module (user.py)

**Progress Management:**

```python
@user_ns.route('/progress')
class UserProgress(Resource):
    @jwt_required()
    def post(self):
        # Save user learning progress
        user_email = get_jwt_identity()
        progress_data = request.json

        # Build progress data key
        progress_key = f"{user_email}:{channelId}:{videoId}"

        # Save to Redis
        redis_user_client.hset(progress_key, mapping={
            'userInput': json.dumps(progress_data.get('userInput', {})),
            'currentTime': progress_data.get('currentTime', 0),
            'overallCompletion': progress_data.get('overallCompletion', 0),
            'duration': progress_data.get('duration', 0)
        })

        # Update learning statistics
        update_user_duration(user_email, channel_name, duration)
```

**Quota Management System:**

```python
def check_dictation_quota(user_id, channel_id, video_id):
    user_key = f"{USER_PREFIX}{user_id}"
    user_data = redis_user_client.hgetall(user_key)

    # Check if user has paid plan
    plan_info = json.loads(user_data.get('plan', '{}'))
    if plan_info.get("name") and plan_info["name"] != "Free":
        return {"canProceed": True, "limit": -1}  # Unlimited

    # Free user quota check
    quota_info = json.loads(user_data.get('quota', '{}'))

    # 30-day cycle check
    cycle_init_time = datetime.fromisoformat(quota_info["cycle_init_time"])
    end_date = cycle_init_time + timedelta(days=30)

    if datetime.now() > end_date:
        # Reset quota
        quota_info["videos"] = []
        quota_info["cycle_init_time"] = datetime.now().isoformat()

    used_count = len(quota_info.get("videos", []))
    can_proceed = used_count < 4

    return {
        "used": used_count,
        "limit": 4,
        "canProceed": can_proceed,
        "startDate": cycle_init_time.strftime("%Y-%m-%d"),
        "endDate": end_date.strftime("%Y-%m-%d")
    }
```

### Service Module (service.py)

**YouTube Transcript Fetching:**

```python
@ns.route('/video-transcript/<string:channel_id>/<string:video_id>')
class VideoTranscript(Resource):
    @jwt_required()
    def get(self, channel_id, video_id):
        # Get cached transcript from Redis
        video_key = f"{VIDEO_PREFIX}{channel_id}:{video_id}"
        video_data = redis_resource_client.hgetall(video_key)

        if 'transcript' not in video_data:
            # Fetch YouTube transcript
            transcript = download_transcript_from_youtube_transcript_api(video_id)
            if transcript:
                # Cache transcript data
                redis_resource_client.hset(video_key, 'transcript', json.dumps(transcript))
            else:
                return {"error": "Transcript not available"}, 404

        transcript_data = json.loads(video_data['transcript'])
        return {
            "transcript": transcript_data,
            "title": video_data.get('title', 'Unknown')
        }
```

**Channel Management:**

```python
@ns.route('/channel')
class YouTubeChannel(Resource):
    def get(self):
        # Get all channel information
        visibility = request.args.get('visibility', VISIBILITY_ALL)
        language = request.args.get('language', LANGUAGE_ALL)

        all_channels = []
        for key in redis_resource_client.scan_iter(f"{CHANNEL_PREFIX}*"):
            channel_info = redis_resource_client.hgetall(key)

            # Apply filters
            if visibility != VISIBILITY_ALL and channel_info.get('visibility') != visibility:
                continue
            if language != LANGUAGE_ALL and channel_info.get('language') != language:
                continue

            all_channels.append(channel_info)

        return all_channels
```

### Payment Module (payment.py)

**Stripe Integration:**

```python
@payment_ns.route('/create-session')
class CreateStripeSession(Resource):
    @jwt_required()
    def post(self):
        user_email = get_jwt_identity()
        data = request.json

        # Create Stripe payment session
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {'name': f"{plan} Plan"},
                    'unit_amount': int(amount * 100),
                },
                'quantity': 1,
            }],
            mode='subscription' if is_recurring else 'payment',
            success_url=f"{STRIPE_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=STRIPE_CANCEL_URL,
            metadata={
                'user_email': user_email,
                'plan': plan,
                'duration': str(duration),
                'is_recurring': str(is_recurring)
            }
        )

        return {"session_id": session.id, "url": session.url}
```

**Membership Plan Management:**

```python
def update_user_plan(user_email, plan_name, duration, isRecurring=False):
    user_key = f"{USER_PREFIX}{user_email}"
    user_data = redis_user_client.hgetall(user_key)

    # Get current plan
    current_plan = json.loads(user_data.get('plan', '{}'))

    # Calculate new expiration time
    now = datetime.now()
    if current_plan.get('expireTime'):
        current_expire_time = datetime.strptime(current_plan['expireTime'], '%Y-%m-%d %H:%M:%S')
        # If current plan hasn't expired, add duration
        if current_expire_time > now:
            new_expire_time = current_expire_time + timedelta(days=duration)
        else:
            new_expire_time = now + timedelta(days=duration)
    else:
        new_expire_time = now + timedelta(days=duration)

    # Create new plan data
    plan_data = {
        "name": plan_name,
        "expireTime": new_expire_time.strftime('%Y-%m-%d %H:%M:%S') if not isRecurring else None,
        "nextPaymentTime": new_expire_time.strftime('%Y-%m-%d %H:%M:%S') if isRecurring else None,
        "isRecurring": isRecurring,
        "status": "active"
    }

    # Save plan data
    redis_user_client.hset(user_key, 'plan', json.dumps(plan_data))

    # Remove quota restrictions if upgraded to paid plan
    if plan_name != 'Free':
        redis_user_client.hdel(user_key, 'quota')

    return plan_data
```

---

## 📊 Data Design

### Redis Data Structures

**User Data Model:**

```
user:{email}
├── username: string
├── email: string
├── avatar: string
├── password: bytes (salt + hash)
├── plan: json {"name": "Premium", "expireTime": "2024-12-31 23:59:59", ...}
├── role: string ("User" | "Admin")
├── dictation_config: json {"playback_speed": 1, "auto_repeat": 0, ...}
├── language: string ("en" | "zh" | "ja" | "ko")
├── quota: json {"videos": [...], "cycle_init_time": "...", ...}
├── duration_data: json {"duration": 0, "channels": {...}, "date": {...}}
├── missed_words: json {"en": [...], "zh": [...], ...}
└── feedback_messages: json [{"id": "...", "message": "...", ...}]
```

**Video/Channel Data Model:**

```
channel:{channel_id}
├── name: string
├── id: string
├── image_url: string
├── visibility: string ("public" | "private")
├── language: string
└── link: string

video:{channel_id}:{video_id}
├── video_id: string
├── title: string
├── link: string
├── visibility: string
├── created_at: timestamp
├── updated_at: timestamp
└── transcript: json [{"start": 0, "end": 5, "transcript": "..."}, ...]
```

**Progress Data Model:**

```
{user_email}:{channel_id}:{video_id}
├── userInput: json {"0": "Hello world", "1": "How are you", ...}
├── currentTime: number
├── overallCompletion: number
├── duration: number
├── channelName: string
├── videoTitle: string
└── videoLink: string
```

---

## 📚 API Documentation

### Authentication Endpoints

```
POST /dictation-studio/auth/register      # User registration
POST /dictation-studio/auth/login         # User login
POST /dictation-studio/auth/logout        # User logout
POST /dictation-studio/auth/refresh-token # Token refresh
GET  /dictation-studio/auth/userinfo/{email} # Get user info
POST /dictation-studio/auth/userinfo      # Update user info
```

### Service Endpoints

```
GET  /dictation-studio/service/channel                          # Get channels
POST /dictation-studio/service/channel                          # Create channels
GET  /dictation-studio/service/video-list/{channel_id}          # Get videos
POST /dictation-studio/service/video-list                       # Upload videos
GET  /dictation-studio/service/video-transcript/{channel_id}/{video_id} # Get transcript
```

### User Endpoints

```
GET  /dictation-studio/user/progress      # Get user progress
POST /dictation-studio/user/progress      # Save user progress
GET  /dictation-studio/user/duration      # Get user duration
POST /dictation-studio/user/missed-words  # Save missed words
GET  /dictation-studio/user/missed-words  # Get missed words
POST /dictation-studio/user/feedback      # Submit feedback
```

### Payment Endpoints

```
POST /dictation-studio/payment/create-session           # Create Stripe session
POST /dictation-studio/payment/verify-session/{session_id} # Verify payment
POST /dictation-studio/payment/generate-code            # Generate verification code
POST /dictation-studio/payment/verify-code              # Verify membership code
```

---

## 🔌 Third-Party Service Integration

### YouTube Transcript API

```python
def download_transcript_from_youtube_transcript_api(video_id):
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        # Prioritize English subtitles
        available_languages = [t.language_code for t in transcript_list]
        english_languages = [lang for lang in available_languages if lang.startswith('en')]

        if english_languages:
            selected_language = english_languages[0]
        elif available_languages:
            selected_language = available_languages[0]
        else:
            raise Exception("No transcripts available")

        transcript = transcript_list.find_transcript([selected_language])
        transcript_data = transcript.fetch()

        # Format transcript data
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
```

### Stripe Payment Integration

```python
# Webhook handling payment success events
@payment_ns.route('/webhook')
class StripeWebhook(Resource):
    def post(self):
        payload = request.data
        sig_header = request.headers.get('Stripe-Signature')

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except ValueError:
            return {"error": "Invalid payload"}, 400
        except stripe.error.SignatureVerificationError:
            return {"error": "Invalid signature"}, 400

        # Handle payment success event
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            metadata = session['metadata']

            # Update user membership plan
            update_user_plan(
                metadata['user_email'],
                metadata['plan'],
                int(metadata['duration']),
                metadata['is_recurring'] == 'True'
            )

        return {"status": "success"}
```

---

## 🚀 Development Environment Setup

### Backend Environment

```bash
# Create virtual environment
cd dictation-studio-service
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Start development server
python src/service.py
```

### Environment Variables Configuration

```env
# Redis Configuration
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=your_redis_password

# JWT Configuration
JWT_SECRET_KEY=your_secret_key

# Stripe Configuration
STRIPE_SECRET_KEY=sk_test_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx
STRIPE_SUCCESS_URL=http://localhost:5173/payment/success
STRIPE_CANCEL_URL=http://localhost:5173/payment/cancel

# Google OAuth Configuration
GOOGLE_CLIENT_ID=your_google_client_id
```

---

## 🐳 Docker Deployment

### Backend Dockerfile

```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "src.service:app"]
```

### Docker Compose Configuration

```yaml
version: "3.8"
services:
  backend:
    build: ./dictation-studio-service
    ports:
      - "5000:5000"
    environment:
      - REDIS_HOST=redis
      - REDIS_PORT=6379
      - REDIS_PASSWORD=${REDIS_PASSWORD}
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    command: redis-server --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data

volumes:
  redis_data:
```

### Production Deployment

```bash
# Build production image
docker build -t dictation-studio-service .

# Run with environment variables
docker run -p 5000:5000 \
  -e REDIS_HOST=your_redis_host \
  -e STRIPE_SECRET_KEY=your_stripe_key \
  dictation-studio-service
```

---

## 📖 Development Guidelines

### Code Standards

#### Python/Flask Standards

```python
# Class naming: PascalCase
class UserProgress(Resource):
    @jwt_required()
    @user_ns.expect(progress_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Bad Request'})
    def post(self):
        """Save user learning progress"""
        try:
            # Get request data
            data = request.json
            user_email = get_jwt_identity()

            # Business logic processing
            result = process_progress_data(data, user_email)

            # Return result
            return {"message": "Success", "data": result}, 200

        except Exception as e:
            logger.error(f"Error saving progress: {str(e)}")
            return {"error": "Internal server error"}, 500

# Function naming: snake_case
def process_progress_data(data: dict, user_email: str) -> dict:
    """Process progress data business logic"""
    # Implementation logic
    pass
```

### New Feature Development Flow

#### 1. Requirements Analysis and Design

- Define feature requirements and use cases
- Design API interfaces and data models
- Plan database schema changes

#### 2. Backend API Development

```python
# 1. Add new Resource class to appropriate module
@user_ns.route('/new-feature')
class NewFeature(Resource):
    @jwt_required()
    def post(self):
        # Implement business logic
        pass

# 2. Add data model validation
new_feature_model = user_ns.model('NewFeature', {
    'field1': fields.String(required=True),
    'field2': fields.Integer(required=False)
})

# 3. Write unit tests
def test_new_feature():
    # Test logic
    pass
```

### Testing Strategy

#### Backend Testing

```python
import pytest
from src.service import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_get_progress(client):
    response = client.get('/user/progress?channelId=test&videoId=test')
    assert response.status_code == 200
    assert 'progress' in response.json

def test_jwt_required_endpoint(client):
    response = client.get('/user/progress')
    assert response.status_code == 401
    assert 'Authorization' in response.json['error']
```

---

## 🐛 Common Issues and Solutions

### Issue 1: Redis Connection Pool State Pollution

**Problem**: Using `SELECT` command to switch databases causes connection pool state confusion

**Solution**: Create independent connection pools for different databases

```python
# Wrong approach
redis_client.execute_command('SELECT', 1)  # Pollutes connection pool

# Correct approach
resource_client = RedisManager.get_resource_client()  # DB 1 dedicated
user_client = RedisManager.get_user_client()         # DB 0 dedicated
```

### Issue 2: JWT Token Blacklisting

**Problem**: Logout doesn't properly invalidate tokens

**Solution**: Implement token blacklisting with Redis

```python
@auth_ns.route('/logout')
class Logout(Resource):
    @jwt_required()
    def post(self):
        jti = get_jwt()['jti']
        # Add token to blacklist
        add_token_to_blacklist(jti)
        return {"message": "Successfully logged out"}, 200

# Token blacklist check
@jwt.token_in_blocklist_loader
def check_if_token_revoked(jwt_header, jwt_payload):
    jti = jwt_payload['jti']
    token_in_redis = redis_blacklist_client.get(jti)
    return token_in_redis is not None
```

### Issue 3: Database Encoding Issues

**Problem**: Redis returns bytes instead of strings

**Solution**: Use `decode_responses=True` in connection pool

```python
class RedisManager:
    @classmethod
    def get_user_client(cls):
        if cls._user_pool is None:
            cls._user_pool = ConnectionPool(
                host=REDIS_HOST, port=REDIS_PORT,
                db=REDIS_USER_DB, password=REDIS_PASSWORD,
                decode_responses=True  # Automatically decode responses
            )
        return redis.Redis(connection_pool=cls._user_pool)
```

### Issue 4: API Rate Limiting

**Problem**: YouTube API rate limits affecting transcript fetching

**Solution**: Implement caching and fallback mechanisms

```python
def download_transcript_with_fallback(video_id):
    # First try YouTube Transcript API
    transcript = download_transcript_from_youtube_transcript_api(video_id)

    if transcript is None:
        # Fallback to yt-dlp
        transcript = download_transcript_with_ytdlp(video_id)

    if transcript is None:
        # Final fallback or return cached version
        transcript = get_cached_transcript(video_id)

    return transcript
```

---

## 📊 Performance Optimization

### Redis Optimization

```python
# Use pipeline for batch operations
def update_multiple_users(user_updates):
    pipe = redis_user_client.pipeline()
    for user_email, data in user_updates.items():
        user_key = f"{USER_PREFIX}{user_email}"
        pipe.hset(user_key, mapping=data)
    pipe.execute()

# Use connection pooling
class RedisManager:
    _pools = {}

    @classmethod
    def get_client(cls, db_num):
        if db_num not in cls._pools:
            cls._pools[db_num] = ConnectionPool(
                host=REDIS_HOST, port=REDIS_PORT,
                db=db_num, password=REDIS_PASSWORD,
                decode_responses=True,
                max_connections=20  # Optimize pool size
            )
        return redis.Redis(connection_pool=cls._pools[db_num])
```

### API Response Caching

```python
from functools import wraps
import json

def cache_response(timeout=300):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # Generate cache key
            cache_key = f"cache:{f.__name__}:{hash(str(args) + str(kwargs))}"

            # Try to get from cache
            cached = redis_resource_client.get(cache_key)
            if cached:
                return json.loads(cached)

            # Execute function and cache result
            result = f(*args, **kwargs)
            redis_resource_client.setex(cache_key, timeout, json.dumps(result))
            return result
        return wrapper
    return decorator

@cache_response(timeout=600)  # Cache for 10 minutes
def get_channel_list(visibility, language):
    # Expensive operation
    return fetch_channels_from_database(visibility, language)
```

---

## 🔐 Security Best Practices

### Input Validation

```python
from flask_restx import fields
from marshmallow import Schema, fields as ma_fields, validate

# Request validation with Flask-RESTX
user_registration_model = api.model('UserRegistration', {
    'email': fields.String(required=True, pattern=r'^[^@]+@[^@]+\.[^@]+$'),
    'password': fields.String(required=True, min_length=8),
    'username': fields.String(required=True, min_length=3, max_length=50)
})

# Additional validation with Marshmallow
class UserRegistrationSchema(Schema):
    email = ma_fields.Email(required=True)
    password = ma_fields.Str(required=True, validate=validate.Length(min=8))
    username = ma_fields.Str(required=True, validate=validate.Length(min=3, max=50))
```

### Password Security

```python
import hashlib
import os

def hash_password(password):
    """Secure password hashing with salt"""
    salt = os.urandom(32)  # 32 bytes = 256 bits
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return salt + key

def verify_password(stored_password, provided_password):
    """Verify password against stored hash"""
    salt = stored_password[:32]
    stored_key = stored_password[32:]
    new_key = hashlib.pbkdf2_hmac('sha256', provided_password.encode('utf-8'), salt, 100000)
    return new_key == stored_key
```

---

## 🎯 Summary

The Dictation Studio backend provides a robust and scalable API service with:

### Technical Highlights

- **Flask + Redis**: Lightweight yet powerful architecture
- **JWT Authentication**: Secure stateless authentication
- **Modular Design**: Clean separation of concerns
- **Redis Multi-DB**: Efficient data organization
- **Stripe Integration**: Complete payment processing

### Core Capabilities

- 🔐 Comprehensive user management
- 🎥 YouTube content processing
- 💳 Subscription and billing management
- 📊 Real-time progress tracking
- 🔧 Admin operations and analytics

### Architecture Benefits

- **Scalable**: Redis-based caching and session management
- **Secure**: JWT tokens, password hashing, input validation
- **Maintainable**: Modular structure with clear dependencies
- **Observable**: Comprehensive logging and error handling
- **Extensible**: Easy to add new features and integrations

This backend demonstrates modern Python web service best practices with robust error handling, security measures, and performance optimizations suitable for production deployment.
