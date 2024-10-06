from flask import jsonify, request
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import create_access_token, get_jwt_identity, get_jwt, jwt_required, unset_jwt_cookies
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import redis
import logging
from config import JWT_ACCESS_TOKEN_EXPIRES, REDIS_HOST, REDIS_PORT, REDIS_USER_DB
from jwt_utils import jwt_required_and_refresh, add_token_to_blacklist
import hashlib
import os
import json

# Configure logging
logger = logging.getLogger(__name__)

# Redis connection for user data
redis_user_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_USER_DB)

# Create a namespace for user-related routes
auth_ns = Namespace('auth', description='Authentication operations')

# Define models
google_token_model = auth_ns.model('GoogleToken', {
    'token': fields.String(required=True, description='Google Access token')
})

# Add new model for registration
register_model = auth_ns.model('Register', {
    'username': fields.String(required=True, description='Username'),
    'email': fields.String(required=True, description='User email'),
    'password': fields.String(required=True, description='User password (salt:hash)'),
    'avatar': fields.String(required=True, description='User avatar URL')
})

# Add new model for login
login_model = auth_ns.model('Login', {
    'username_or_email': fields.String(required=True, description='Username or Email'),
    'password': fields.String(required=True, description='User password')
})

# Add new model for email check
email_check_model = auth_ns.model('EmailCheck', {
    'email': fields.String(required=True, description='Email to check')
})

# Add new model for role update
role_update_model = auth_ns.model('RoleUpdate', {
    'email': fields.String(required=True, description='User email'),
    'role': fields.String(required=True, description='New role for the user')
})

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

@auth_ns.route('/verify-google-token')
class GoogleTokenVerification(Resource):
    @auth_ns.expect(google_token_model)
    @auth_ns.doc(responses={200: 'Success', 400: 'Invalid Token', 500: 'Server Error'})
    def post(self):
        """Verify Google Access token and get user info"""
        data = request.json
        token = data.get('token')

        try:
            credentials = Credentials(token=token)
            service = build('oauth2', 'v2', credentials=credentials)
            user_info = service.userinfo().get().execute()

            email = user_info['email']
            name = user_info.get('name')
            avatar = user_info.get('picture')

            # Check if user already exists in Redis
            user_exists = redis_user_client.exists(f"user:{email}")

            # Create a JWT token
            jwt_token = create_access_token(identity=email)

            if user_exists:
                # Update user information in Redis, preserving the existing role
                existing_user_data = redis_user_client.hgetall(f"user:{email}")
                existing_role = existing_user_data.get(b'role', b'user').decode('utf-8')
                
                user_data = {
                    "email": email,
                    "name": name,
                    "avatar": avatar,
                    "role": existing_role
                }
                redis_user_client.hmset(f"user:{email}", {k: v.encode('utf-8') if isinstance(v, str) else v for k, v in user_data.items()})
                logger.info(f"Updated existing user via Google: {email}")
            else:
                # Store new user information in Redis
                user_data = {
                    "email": email,
                    "name": name,
                    "avatar": avatar,
                    "role": "user"
                }
                redis_user_client.hmset(f"user:{email}", {k: v.encode('utf-8') if isinstance(v, str) else v for k, v in user_data.items()})
                logger.info(f"New user registered via Google: {email}")

            # Retrieve user info from Redis to ensure we return the most up-to-date information
            stored_user_data = redis_user_client.hgetall(f"user:{email}")
            user_info = {k.decode('utf-8'): v.decode('utf-8') for k, v in stored_user_data.items() if k != b'password'}

            return {
                "message": "Token verified successfully",
                "user": user_info,
                "jwt_token": jwt_token
            }, 200

        except Exception as e:
            logger.warning(f"Invalid Google token: {str(e)}")
            return {"error": "Invalid token"}, 400

@auth_ns.route('/refresh-login-status')
class CheckLogin(Resource):
    @jwt_required_and_refresh()
    @auth_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 500: 'Server Error'})
    def get(self):
        """Check if the user is logged in, return user info, and refresh the JWT token"""
        try:
            current_user_email = get_jwt_identity()
            user_data = redis_user_client.hgetall(f"user:{current_user_email}")
            
            if not user_data:
                logger.warning(f"User data not found for email: {current_user_email}")
                return {"error": "User not found"}, 401

            # Convert byte strings to regular strings and parse JSON fields
            user_info = {}
            for k, v in user_data.items():
                if k != b'password' and k != b'dictation_progress':
                    key = k.decode('utf-8')
                    value = v.decode('utf-8')
                    try:
                        user_info[key] = json.loads(value)
                    except json.JSONDecodeError:
                        user_info[key] = value

            response_data = {
                "message": "User is logged in",
                "user": user_info,
            }

            # Get the new token from the decorator
            new_token = getattr(self, 'new_token', None)
            if new_token:
                response_data['jwt_token'] = new_token

            logger.info(f"Successfully checked login and refreshed token for user: {current_user_email}")
            return response_data, 200

        except Exception as e:
            logger.error(f"Error in check-login: {str(e)}")
            return {"error": "An error occurred while checking login"}, 500

@auth_ns.route('/logout')
class Logout(Resource):
    @jwt_required()
    @auth_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 500: 'Server Error'})
    def post(self):
        """Logout the current user"""
        try:
            jti = get_jwt()['jti']
            add_token_to_blacklist(jti)
            
            response = jsonify({"message": "Successfully logged out"})
            unset_jwt_cookies(response)
            
            logger.info(f"User successfully logged out")
            return response

        except Exception as e:
            logger.error(f"Error during logout: {str(e)}")
            return {"error": "An error occurred during logout"}, 500

@auth_ns.route('/register')
class Register(Resource):
    @auth_ns.expect(register_model)
    @auth_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 500: 'Server Error'})
    def post(self):
        """Register a new user"""
        data = request.json
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')
        avatar = data.get('avatar')

        if not all([username, email, password, avatar]):
            return {"error": "All fields are required"}, 400

        try:
            # Check if user already exists
            if redis_user_client.exists(f"user:{email}"):
                return {"error": "User with this email already exists"}, 400

            # Hash the password
            hashed_password = hash_password(password)

            # Store user data in Redis
            user_data = {
                "username": username,
                "email": email,
                "password": hashed_password,
                "avatar": avatar,
                "role": "user"  # Add the role field
            }
            redis_user_client.hmset(f"user:{email}", {k: v.encode('utf-8') if isinstance(v, str) else v for k, v in user_data.items()})

            # Create JWT token
            jwt_token = create_access_token(identity=email)

            # Prepare user info to return (excluding password)
            user_info = {k: v for k, v in user_data.items() if k != 'password'}

            logger.info(f"User registered successfully: {email}")
            return {
                "message": "User registered successfully",
                "user": user_info,
                "jwt_token": jwt_token
            }, 200

        except Exception as e:
            logger.error(f"Error during registration: {str(e)}")
            return {"error": "An error occurred during registration"}, 500

@auth_ns.route('/login')
class Login(Resource):
    @auth_ns.expect(login_model)
    @auth_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 500: 'Server Error'})
    def post(self):
        """Login a user"""
        data = request.json
        username_or_email = data.get('username_or_email')
        password = data.get('password')

        if not username_or_email or not password:
            return {"error": "Username/Email and password are required"}, 400

        try:
            # Check if the input is an email or username
            if '@' in username_or_email:
                user_key = f"user:{username_or_email}"
            else:
                # Find the user by username
                for key in redis_user_client.scan_iter("user:*"):
                    user_data = redis_user_client.hgetall(key)
                    if user_data.get(b'username', b'').decode('utf-8') == username_or_email:
                        user_key = key.decode('utf-8')
                        break
                else:
                    return {"error": "User not found"}, 401

            user_data = redis_user_client.hgetall(user_key)
            if not user_data:
                return {"error": "User not found"}, 401

            stored_password = user_data.get(b'password')
            if not stored_password or not verify_password(stored_password, password):
                return {"error": "Invalid credentials"}, 401

            # User authenticated, create JWT token
            email = user_data.get(b'email').decode('utf-8')
            jwt_token = create_access_token(identity=email)

            # Prepare user info
            user_info = {k.decode('utf-8'): v.decode('utf-8') for k, v in user_data.items() if k != b'password'}

            logger.info(f"User logged in successfully: {email}")
            return {
                "message": "Login successful",
                "user": user_info,
                "jwt_token": jwt_token
            }, 200

        except Exception as e:
            logger.error(f"Error during login: {str(e)}")
            return {"error": "An error occurred during login"}, 500

@auth_ns.route('/check-email')
class CheckEmail(Resource):
    @auth_ns.expect(email_check_model)
    @auth_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 500: 'Server Error'})
    def post(self):
        """Check if an email already exists"""
        data = request.json
        email = data.get('email')

        if not email:
            return {"error": "Email is required"}, 400

        try:
            # Check if user already exists
            user_exists = redis_user_client.exists(f"user:{email}")

            if user_exists:
                logger.info(f"Email check: {email} already exists")
                return {"exists": True, "message": "Email already exists"}, 200
            else:
                logger.info(f"Email check: {email} is available")
                return {"exists": False, "message": "Email is available"}, 200

        except Exception as e:
            logger.error(f"Error checking email existence: {str(e)}")
            return {"error": "An error occurred while checking email"}, 500
        
@auth_ns.route('/users')
class Users(Resource):
    @jwt_required()
    @auth_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 500: 'Server Error'})
    def get(self):
        """Get all users"""
        try:
            # Get all user keys
            user_keys = redis_user_client.keys("user:*")
            users = []
            for key in user_keys:
                user_data = redis_user_client.hgetall(key)
                user_info = {k.decode('utf-8'): v.decode('utf-8') for k, v in user_data.items() if k != b'password'}
                users.append(user_info)

            logger.info(f"Retrieved {len(users)} users")
            return {"users": users}, 200

        except Exception as e:
            logger.error(f"Error retrieving users: {str(e)}")
            return {"error": "An error occurred while retrieving users"}, 500

@auth_ns.route('/user/role')
class UserRole(Resource):
    @jwt_required()
    @auth_ns.expect(role_update_model)
    @auth_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 403: 'Forbidden', 404: 'User Not Found', 500: 'Server Error'})
    def put(self):
        """Update user role"""
        try:
            current_user_email = get_jwt_identity()
            current_user_data = redis_user_client.hgetall(f"user:{current_user_email}")
            
            # only allow admin to change user role
            if current_user_data.get(b'role', b'').decode('utf-8') != 'admin':
                logger.warning(f"Non-admin user {current_user_email} attempted to change user role")
                return {"error": "Only administrators can change user roles"}, 403

            data = request.json
            email = data.get('email')
            new_role = data.get('role')

            if not email or not new_role:
                return {"error": "Email and role are required"}, 400

            user_key = f"user:{email}"
            if not redis_user_client.exists(user_key):
                logger.warning(f"Attempted to update role for non-existent user: {email}")
                return {"error": "User not found"}, 404

            redis_user_client.hset(user_key, 'role', new_role)

            logger.info(f"Updated role for user {email} to {new_role}")
            return {"message": f"Role for user {email} updated to {new_role}"}, 200

        except Exception as e:
            logger.error(f"Error updating user role: {str(e)}")
            return {"error": f"An error occurred while updating user role: {str(e)}"}, 500