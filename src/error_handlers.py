import logging
from flask_jwt_extended.exceptions import NoAuthorizationError, InvalidHeaderError, JWTDecodeError
from flask_restx import Api
from functools import wraps

logger = logging.getLogger(__name__)

def register_error_handlers(api_instance: Api):
    """Register global error handlers for Flask-RESTX Api instance"""
    
    @api_instance.errorhandler(NoAuthorizationError)
    def handle_auth_error(error):
        logger.warning("JWT validation failed: Missing Authorization Header")
        return {"error": "Missing Authorization Header"}, 401

    @api_instance.errorhandler(InvalidHeaderError)
    def handle_invalid_header_error(error):
        logger.warning(f"JWT validation failed: Invalid header - {str(error)}")
        return {"error": str(error)}, 401

    @api_instance.errorhandler(JWTDecodeError)
    def handle_jwt_decode_error(error):
        logger.warning(f"JWT validation failed: Token decode error - {str(error)}")
        return {"error": str(error)}, 401

    @api_instance.errorhandler(Exception)
    def handle_general_exception(error):
        logger.error(f"Unhandled exception: {str(error)}")
        return {"error": "Internal Server Error"}, 500

    return api_instance