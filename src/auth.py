from flask import jsonify, request, make_response
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import create_access_token, create_refresh_token, get_jwt_identity, get_jwt, jwt_required, unset_jwt_cookies
import logging
from config import JWT_ACCESS_TOKEN_EXPIRES, JWT_REFRESH_TOKEN_EXPIRES, USER_DICTATION_CONFIG_DEFAULT, USER_LANGUAGE_DEFAULT, USER_PLAN_DEFAULT, USER_PREFIX, USER_ROLE_DEFAULT
from utils import add_token_to_blacklist, hash_password
import json
from datetime import datetime, timedelta
from redis_manager import RedisManager

# Configure logging
logger = logging.getLogger(__name__)

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
    'email': fields.String(required=True, description='Email'),
    'username': fields.String(required=True, description='Username'),
    'avatar': fields.String(required=True, description='User avatar URL')
})

# Add new model for email check
email_check_model = auth_ns.model('EmailCheck', {
    'email': fields.String(required=True, description='Email to check')
})

# Add new model for plan update
plan_update_model = auth_ns.model('PlanUpdate', {
    'email': fields.String(required=True, description='User email'),
    'plan': fields.String(required=True, description='New plan for the user'),
    'duration': fields.Integer(required=False, description='Plan duration for the user, only applicable for "Pro" and "Premium" plans')
})

# Add new model for role update
role_update_model = auth_ns.model('RoleUpdate', {
    'email': fields.String(required=True, description='User email'),
    'role': fields.String(required=True, description='New role for the user')
})

supabase_token_model = auth_ns.model('SupabaseToken', {
    'access_token': fields.String(required=True, description='Supabase Access Token')
})

# Define model for user info
user_info_model = auth_ns.model('UserInfo', {
    'email': fields.String(required=True, description='User email'),
    'avatar': fields.String(required=True, description='User avatar URL'),
    'username': fields.String(required=True, description='Username')
})

redis_manager = RedisManager()
redis_user_client = redis_manager.get_user_client()

@auth_ns.route('/userinfo')
class UserInfo(Resource):
    @auth_ns.expect(user_info_model)
    @auth_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 500: 'Server Error'})
    def post(self):
        """Update or create user information and return user details"""
        data = request.json
        user_key = f"{USER_PREFIX}{data['email']}"

        # Check if user exists
        user_exists = redis_user_client.exists(user_key)

        if user_exists:
            # User exists, update and retrieve additional details
            redis_user_client.hmset(user_key, {
                'email': data['email'],
                'avatar': data['avatar'],
                'username': data['username'],
                'updated_at': int(datetime.now().timestamp() * 1000)
            })
            user_data = redis_user_client.hgetall(user_key)
            # Parse JSON strings into objects for specific fields
            user_info = parse_user_data(user_data)
            
        else:
            # User does not exist, create new user
            user_info = {
                'email': data['email'],
                'avatar': data['avatar'],
                'username': data['username'],
                'plan': json.loads(USER_PLAN_DEFAULT),  # Parse JSON string to object
                'role': USER_ROLE_DEFAULT,
                'dictation_config': json.loads(USER_DICTATION_CONFIG_DEFAULT),  # Parse JSON string to object
                'language': USER_LANGUAGE_DEFAULT,
                'updated_at': int(datetime.now().timestamp() * 1000),
                'created_at': int(datetime.now().timestamp() * 1000)
            }
            # For Redis storage, we need to convert JSON objects back to strings
            redis_data = user_info.copy()
            redis_data['plan'] = USER_PLAN_DEFAULT  # Store as JSON string in Redis
            redis_data['dictation_config'] = USER_DICTATION_CONFIG_DEFAULT  # Store as JSON string in Redis
            redis_user_client.hmset(user_key, redis_data)

        # Create a new JWT token for the user
        access_token = create_access_token(identity=data['email'], expires_delta=JWT_ACCESS_TOKEN_EXPIRES)
        refresh_token = create_refresh_token(identity=data['email'], expires_delta=JWT_REFRESH_TOKEN_EXPIRES)

        # Prepare response
        response = make_response(jsonify({
            "message": "User information processed successfully",
            "user": user_info
        }), 200)
        response.headers['x-ds-access-token'] = access_token
        response.headers['x-ds-refresh-token'] = refresh_token
        return response

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

            # Handle password - check if it's already encrypted from frontend
            if ':' in password and len(password.split(':')) == 2:
                # Password is already encrypted from frontend (salt:hash format)
                salt_hex, hash_hex = password.split(':')
                try:
                    # Convert hex strings back to bytes
                    salt = bytes.fromhex(salt_hex)
                    hash_bytes = bytes.fromhex(hash_hex)
                    hashed_password = salt + hash_bytes
                    logger.info(f"Using frontend-encrypted password for user: {email}")
                except ValueError:
                    # If conversion fails, treat as plain text password
                    hashed_password = hash_password(password)
                    logger.info(f"Frontend encryption format invalid, using server-side hashing for user: {email}")
            else:
                # Plain text password - hash it server-side
                hashed_password = hash_password(password)
                logger.info(f"Using server-side password hashing for user: {email}")

            # Store user data in Redis
            user_data = {
                "username": username,
                "email": email,
                "password": hashed_password,
                "avatar": avatar,
                "plan": USER_PLAN_DEFAULT,
                "role": USER_ROLE_DEFAULT,
                "dictation_config": USER_DICTATION_CONFIG_DEFAULT,
                "language": USER_LANGUAGE_DEFAULT,
                "created_at": int(datetime.now().timestamp() * 1000)
            }
            redis_user_client.hmset(f"user:{email}", {k: v.encode('utf-8') if isinstance(v, str) else v for k, v in user_data.items()})

            # Create JWT token
            access_token = create_access_token(
                identity=email,
                expires_delta=JWT_ACCESS_TOKEN_EXPIRES
            )
            refresh_token = create_refresh_token(
                identity=email,
                expires_delta=JWT_REFRESH_TOKEN_EXPIRES
            )

            # Prepare user info to return (excluding password)
            user_info = {k: v for k, v in user_data.items() if k != 'password'}

            logger.info(f"User registered successfully: {email}")
            response_data = {
                "message": "User registered successfully",
                "user": parse_user_data(user_info)
            }
            response = make_response(jsonify(response_data), 200)
            response.headers['x-ds-access-token'] = access_token
            response.headers['x-ds-refresh-token'] = refresh_token
            return response

        except Exception as e:
            logger.error(f"Error during registration: {str(e)}")
            return {"error": "An error occurred during registration"}, 500

@auth_ns.route('/login')
class Login(Resource):
    @auth_ns.expect(login_model)
    @auth_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 500: 'Server Error'})
    def post(self):
        """Login or create a user without password"""
        try:
            data = request.json
            email = data.get('email')
            username = data.get('username')
            avatar = data.get('avatar')

            if not all([email, username, avatar]):
                return {"error": "Username, email and avatar are required"}, 400

            user_key = f"user:{email}"
            
            # Get existing user data if it exists
            existing_user = redis_user_client.hgetall(user_key)
            
            # Prepare user data
            user_data = {
                "email": email,
                "username": username,
                "avatar": avatar,
            }

            if not existing_user:
                # New user - add plan
                user_data["plan"] = USER_PLAN_DEFAULT
                user_data["role"] = USER_ROLE_DEFAULT
                user_data["dictation_config"] = USER_DICTATION_CONFIG_DEFAULT
                user_data["language"] = USER_LANGUAGE_DEFAULT
                user_data["created_at"] = int(datetime.now().timestamp() * 1000)
                logger.info(f"Creating new user: {email}")
            else:
                # Existing user - preserve existing data that's not being updated
                existing_data = {k: v 
                               for k, v in existing_user.items()}
                # Preserve existing fields that are not being updated
                for key in existing_data:
                    if key not in user_data and key != 'password':
                        user_data[key] = existing_data[key]
                user_data["updated_at"] = int(datetime.now().timestamp() * 1000)
                logger.info(f"Updating existing user: {email}")

            # Update Redis with user data
            redis_user_client.hmset(user_key, user_data)

            # Create JWT token
            access_token = create_access_token(
                identity=email,
                expires_delta=JWT_ACCESS_TOKEN_EXPIRES
            )
            refresh_token = create_refresh_token(
                identity=email,
                expires_delta=JWT_REFRESH_TOKEN_EXPIRES
            )

            # Get complete user data to return
            updated_user_data = redis_user_client.hgetall(user_key)
            user_data = parse_user_data(updated_user_data)

            response_data = {
                "message": "Login successful",
                "user": user_data
            }
            
            response = make_response(jsonify(response_data), 200)
            response.headers['x-ds-access-token'] = access_token
            response.headers['x-ds-refresh-token'] = refresh_token
            return response

        except Exception as e:
            logger.error(f"Error during login: {str(e)}")
            return {"error": f"An error occurred during login: {str(e)}"}, 500

@auth_ns.route('/refresh-token')
class TokenRefresh(Resource):
    @jwt_required(refresh=True)
    @auth_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 500: 'Server Error'})
    def post(self):
        current_user = get_jwt_identity()
        new_access_token = create_access_token(identity=current_user)
        response = make_response(jsonify({"message": "Token refreshed"}), 200)
        response.headers['x-ds-access-token'] = new_access_token
        # return user info in body
        user_info = redis_user_client.hgetall(f"{USER_PREFIX}{current_user}")
        # Get complete user data to return
        user_data = parse_user_data(user_info)
        response.data = json.dumps(user_data)
        return response

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
                user_info = parse_user_data(user_data)
                users.append(user_info)

            logger.info(f"Retrieved {len(users)} users")
            return {"users": users}, 200

        except Exception as e:
            logger.error(f"Error retrieving users: {str(e)}")
            return {"error": "An error occurred while retrieving users"}, 500

@auth_ns.route('/user/plan')
class UserPlan(Resource):
    @jwt_required()
    @auth_ns.expect(plan_update_model)
    @auth_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 403: 'Forbidden', 404: 'User Not Found', 500: 'Server Error'})
    def put(self):
        """Update user plan"""
        try:
            current_user_email = get_jwt_identity()
            current_user_data = redis_user_client.hgetall(f"user:{current_user_email}")
            
            # only allow admin to change user plan
            if current_user_data.get('role', '') != 'Admin':
                logger.warning(f"Non-admin user {current_user_email} attempted to change user plan")
                return {"error": "Only 'Admin' role can change user plans"}, 403

            data = request.json
            emails = data.get('emails', [])
            new_plan = data.get('plan')
            duration = data.get('duration')

            if not emails or not new_plan:
                return {"error": "Emails list and plan are required"}, 400

            # Calculate expiration date if duration is provided
            if duration:
                expire_time = int((datetime.now() + timedelta(days=duration)).timestamp() * 1000)
            else:
                expire_time = None

            # Create plan object structure
            plan_data = {
                "name": new_plan,
                "expireTime": expire_time
            }
            
            # Convert to JSON string for Redis storage
            plan_json = json.dumps(plan_data)

            results = []
            for email in emails:
                user_key = f"user:{email}"
                if not redis_user_client.exists(user_key):
                    logger.warning(f"Attempted to update plan for non-existent user: {email}")
                    results.append({
                        "email": email,
                        "success": False,
                        "message": "User not found"
                    })
                    continue

                try:
                    # Store the plan object as JSON string
                    redis_user_client.hset(user_key, 'plan', plan_json)
                    # Update updated_at
                    redis_user_client.hset(user_key, 'updated_at', int(datetime.now().timestamp() * 1000))
                    logger.info(f"Updated plan for user {email} to {plan_data}")
                    results.append({
                        "email": email,
                        "success": True,
                        "message": f"Plan updated to {new_plan} with expiration {expire_time}"
                    })
                except Exception as e:
                    logger.error(f"Error updating plan for user {email}: {str(e)}")
                    results.append({
                        "email": email,
                        "success": False,
                        "message": f"Error updating plan: {str(e)}"
                    })

            success_count = sum(1 for result in results if result['success'])
            failure_count = len(results) - success_count

            return {
                "message": f"Plan update completed. {success_count} successful, {failure_count} failed.",
                "results": results
            }, 200

        except Exception as e:
            logger.error(f"Error updating user plans: {str(e)}")
            return {"error": f"An error occurred while updating user plans: {str(e)}"}, 500

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
            if current_user_data.get('role', '') != 'Admin':
                logger.warning(f"Non-admin user {current_user_email} attempted to change user role")
                return {"error": "Only 'Admin' role can change user roles"}, 403

            data = request.json
            emails = data.get('emails', [])
            new_role = data.get('role')

            if not emails or not new_role:
                return {"error": "Emails list and role are required"}, 400

            results = []
            for email in emails:
                user_key = f"user:{email}"
                if not redis_user_client.exists(user_key):
                    logger.warning(f"Attempted to update role for non-existent user: {email}")
                    results.append({
                        "email": email,
                        "success": False,
                        "message": "User not found"
                    })
                    continue

                try:
                    redis_user_client.hset(user_key, 'role', new_role)
                    # Update updated_at
                    redis_user_client.hset(user_key, 'updated_at', int(datetime.now().timestamp() * 1000))
                    logger.info(f"Updated role for user {email} to {new_role}")
                    results.append({
                        "email": email,
                        "success": True,
                        "message": f"Role updated to {new_role}"
                    })
                except Exception as e:
                    logger.error(f"Error updating role for user {email}: {str(e)}")
                    results.append({
                        "email": email,
                        "success": False,
                        "message": f"Error updating role: {str(e)}"
                    })

            success_count = sum(1 for result in results if result['success'])
            failure_count = len(results) - success_count

            return {
                "message": f"Role update completed. {success_count} successful, {failure_count} failed.",
                "results": results
            }, 200

        except Exception as e:
            logger.error(f"Error updating user roles: {str(e)}")
            return {"error": f"An error occurred while updating user roles: {str(e)}"}, 500

def parse_user_data(user_data):
    user_info = {}
    for k, v in user_data.items():
        if k != 'password':
            key = k
            value = v
            # Try to parse JSON strings for specific fields
            try:
                # Attempt to parse each field as JSON
                user_info[key] = json.loads(value)
            except json.JSONDecodeError:
                # If parsing fails, keep it as a string
                user_info[key] = value
    return user_info