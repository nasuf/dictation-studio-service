from functools import wraps
import logging
from flask import jsonify, request
from flask_jwt_extended import verify_jwt_in_request, create_access_token, get_jwt_identity, get_jwt
import redis
from config import REDIS_HOST, REDIS_PORT, JWT_ACCESS_TOKEN_EXPIRES, REDIS_BLACKLIST_DB

redis_blacklist_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_BLACKLIST_DB)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def jwt_required_and_refresh():
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            verify_jwt_in_request()
            jti = get_jwt()['jti']
            if redis_blacklist_client.get(jti):
                return {"msg": "Token has been revoked"}, 401
            
            # Create a new token
            current_user = get_jwt_identity()
            new_token = create_access_token(identity=current_user)
            
            # Set the new token as an attribute of the class instance
            self = args[0]
            setattr(self, 'new_token', new_token)
            
            result = fn(*args, **kwargs)
            
            # If the result is a tuple (data, status_code), add the new token to the data
            if isinstance(result, tuple) and len(result) == 2:
                data, status_code = result
                if isinstance(data, dict):
                    data['jwt_token'] = new_token
                    return data, status_code
            
            # If it's not a tuple, assume it's just data and return it with 200 status
            return result, 200

        return wrapper
    return decorator

def add_token_to_blacklist(jti):
    redis_blacklist_client.set(jti, 'true')