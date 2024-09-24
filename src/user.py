from flask import request
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import jwt_required, get_jwt_identity
import redis
import json
import logging
from config import REDIS_HOST, REDIS_PORT, REDIS_USER_DB

# Configure logging
logger = logging.getLogger(__name__)

# Redis connection for user data
redis_user_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_USER_DB)

# Create a namespace for user-related routes
user_ns = Namespace('user', description='User operations')

# Define model for dictation progress
dictation_progress_model = user_ns.model('DictationProgress', {
    'channelId': fields.String(required=True, description='Channel ID'),
    'videoId': fields.String(required=True, description='Video ID'),
    'userInput': fields.Raw(required=True, description='User input for dictation'),
    'currentTime': fields.Integer(required=True, description='Current timestamp'),
    'overallCompletion': fields.Integer(required=True, description='Overall completion percentage')
})

@user_ns.route('/progress')
class DictationProgress(Resource):
    @jwt_required()
    @user_ns.expect(dictation_progress_model)
    @user_ns.doc(responses={200: 'Success', 400: 'Invalid Input', 401: 'Unauthorized', 500: 'Server Error'})
    def post(self):
        """Update user's dictation progress"""
        try:
            # Get user email from JWT token
            user_email = get_jwt_identity()

            # Get progress data from request
            progress_data = request.json

            # Validate input data
            required_fields = ['channelId', 'videoId', 'userInput', 'currentTime', 'overallCompletion']
            if not all(field in progress_data for field in required_fields):
                return {"error": "Missing required fields"}, 400

            # Get existing user data
            user_key = f"user:{user_email}"
            user_data = redis_user_client.hgetall(user_key)

            if not user_data:
                return {"error": "User not found"}, 404

            # Get existing dictation progress or initialize new
            dictation_progress = json.loads(user_data.get(b'dictation_progress', b'{}').decode('utf-8'))

            # Update dictation progress
            video_key = f"{progress_data['channelId']}:{progress_data['videoId']}"
            dictation_progress[video_key] = {
                'userInput': progress_data['userInput'],
                'currentTime': progress_data['currentTime'],
                'overallCompletion': progress_data['overallCompletion']
            }

            # Save updated dictation progress
            redis_user_client.hset(user_key, 'dictation_progress', json.dumps(dictation_progress))

            logger.info(f"Updated dictation progress for user: {user_email}, channel: {progress_data['channelId']}, video: {video_key}")
            return {"message": "Dictation progress updated successfully"}, 200

        except Exception as e:
            logger.error(f"Error updating dictation progress: {str(e)}")
            return {"error": "An error occurred while updating dictation progress"}, 500

# Add more user-related routes here if needed
