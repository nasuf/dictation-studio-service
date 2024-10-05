from functools import wraps
import logging
from flask import jsonify, request
from flask_jwt_extended import verify_jwt_in_request, create_access_token, get_jwt_identity, get_jwt
import redis
from config import REDIS_HOST, REDIS_PORT, JWT_ACCESS_TOKEN_EXPIRES

redis_blacklist_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=2)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def jwt_required_and_refresh():
    def wrapper(fn):
        @wraps(fn)
        def decorator(*args, **kwargs):
            verify_jwt_in_request()
            jwt = get_jwt()
            jti = jwt['jti']
            
            if redis_blacklist_client.exists(jti):
                return {"msg": "Token has been revoked"}, 401
            
            current_user = get_jwt_identity()
            resp = fn(*args, **kwargs)
            
            if request.endpoint != 'auth_logout':
                new_token = create_access_token(identity=current_user)
                if isinstance(resp, tuple):
                    data, code = resp
                    data['jwt_token'] = new_token
                    return data, code
                else:
                    return {'data': resp, 'jwt_token': new_token}
            return resp
        return decorator
    return wrapper

def add_token_to_blacklist(jti):
    redis_blacklist_client.set(jti, 'revoked', ex=int(JWT_ACCESS_TOKEN_EXPIRES.total_seconds()))