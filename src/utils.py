from functools import wraps
import logging
import time
from flask import request, make_response
from flask_jwt_extended import verify_jwt_in_request, create_access_token, get_jwt_identity, get_jwt
import redis
from config import JWT_ACCESS_TOKEN_EXPIRES, PAYMENT_MAX_RETRY_ATTEMPTS, PAYMENT_RETRY_DELAY_SECONDS, REDIS_HOST, REDIS_PORT, REDIS_BLACKLIST_DB, REDIS_PASSWORD
from datetime import datetime

redis_blacklist_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_BLACKLIST_DB, password=REDIS_PASSWORD)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def jwt_required_and_refresh():
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                verify_jwt_in_request()
            except Exception as e:
                logger.error(f"Error verifying JWT: {e}")
                return {"msg": "Invalid token"}, 401
            existing_token = get_jwt()
            jti = existing_token['jti']
            
            if redis_blacklist_client.get(jti):
                return {"msg": "Token has been revoked"}, 401
            
            exp_timestamp = existing_token['exp']
            current_timestamp = datetime.now().timestamp()
            time_left = exp_timestamp - current_timestamp
            
            new_token = None
            if time_left < 300:  # auto refresh token
                current_user = get_jwt_identity()
                new_token = create_access_token(identity=current_user, expires_delta=JWT_ACCESS_TOKEN_EXPIRES)
                logger.info(f"Token refreshed for user: {current_user}")
            
            result = fn(*args, **kwargs)
            
            if isinstance(result, tuple):
                response = make_response(result[0], result[1])
            else:
                response = make_response(result)
            
            if new_token:
                response.headers['x-ds-token'] = new_token
            else:
                original_jwt = request.headers.get('Authorization', '').split('Bearer ')[-1]
                response.headers['x-ds-token'] = original_jwt
            
            return response

        return wrapper
    return decorator

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