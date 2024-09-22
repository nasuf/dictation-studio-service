from flask import jsonify, request
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity, unset_jwt_cookies
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import redis
import logging
from config import REDIS_HOST, REDIS_PORT, REDIS_USER_DB

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

            userid = user_info['id']
            email = user_info['email']
            name = user_info.get('name')
            picture = user_info.get('picture')

            # Create a JWT token
            jwt_token = create_access_token(identity=email)

            # Store user information in Redis
            user_data = {
                "user_id": userid,
                "email": email,
                "name": name,
                "picture": picture
            }
            redis_user_client.hset(f"user:{email}", mapping=user_data)

            logger.info(f"Successfully verified Google token and stored user info for: {email}")
            return {
                "message": "Token verified successfully",
                "user_id": userid,
                "email": email,
                "name": name,
                "picture": picture,
                "jwt_token": jwt_token
            }, 200

        except Exception as e:
            logger.warning(f"Invalid Google token: {str(e)}")
            return {"error": "Invalid token"}, 400

@auth_ns.route('/check-login')
class CheckLogin(Resource):
    @jwt_required()
    @auth_ns.doc(responses={200: 'Success', 401: 'Unauthorized', 500: 'Server Error'})
    def get(self):
        """Check if the user is logged in, return user info, and refresh the JWT token"""
        try:
            current_user_email = get_jwt_identity()
            user_data = redis_user_client.hgetall(f"user:{current_user_email}")
            
            if not user_data:
                logger.warning(f"User data not found for email: {current_user_email}")
                return {"error": "User not found"}, 401

            # Convert byte strings to regular strings
            user_info = {k.decode('utf-8'): v.decode('utf-8') for k, v in user_data.items()}

            # Create a new JWT token
            new_token = create_access_token(identity=current_user_email)

            response_data = {
                "message": "User is logged in",
                "user": user_info,
                "jwt_token": new_token
            }

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
            current_user_email = get_jwt_identity()
            
            response = jsonify({"message": "Successfully logged out"})
            unset_jwt_cookies(response)
            
            logger.info(f"User {current_user_email} successfully logged out")
            return response

        except Exception as e:
            logger.error(f"Error during logout: {str(e)}")
            return {"error": "An error occurred during logout"}, 500