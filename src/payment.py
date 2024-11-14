from flask import request
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import get_jwt_identity
import stripe
import json
import logging
from datetime import datetime, timedelta
from config import (
    PAYMENT_MAX_RETRY_ATTEMPTS,
    PAYMENT_RETRY_DELAY_SECONDS,
    PAYMENT_RETRY_KEY_EXPIRE_SECONDS,
    STRIPE_SECRET_KEY, 
    STRIPE_WEBHOOK_SECRET, 
    STRIPE_SUCCESS_URL, 
    STRIPE_CANCEL_URL,
    USER_PREFIX
)
from utils import jwt_required_and_refresh, with_retry
from celery import shared_task
from datetime import datetime, timedelta
import json
import logging
from functools import wraps
from werkzeug.local import LocalProxy
from flask import current_app

# Configure logging
logger = logging.getLogger(__name__)

# Initialize Stripe
stripe.api_key = STRIPE_SECRET_KEY

# Create namespace
payment_ns = Namespace('payment', description='Payment operations')

# Define Stripe price IDs for different plans
STRIPE_PRICE_IDS = {
    'Premium': 'price_1QGCidIIAqSdCVKTT7Ngfnwt',  # Replace with your actual Premium plan price ID
    'Pro': 'price_1QGCidIIAqSdCVKTT7Ngfnwt'      # Replace with your actual Pro plan price ID
}

# Define models
payment_model = payment_ns.model('Payment', {
    'plan': fields.String(required=True, description='Plan name (Premium/Pro)', enum=['Premium', 'Pro']),
    'duration': fields.Integer(required=True, description='Duration in days'),
    'isRecurring': fields.Boolean(required=True, description='Whether the subscription is recurring')
})

redis_user_client = LocalProxy(lambda: current_app.config['redis_user_client'])

@payment_ns.route('/create-session')
class CreateCheckoutSession(Resource):
    @jwt_required_and_refresh()
    @payment_ns.expect(payment_model)
    @payment_ns.doc(
        responses={
            200: 'Success - Returns session ID and checkout URL',
            400: 'Invalid Input',
            401: 'Unauthorized',
            500: 'Server Error'
        },
        description='Create a Stripe checkout session for plan subscription'
    )
    def post(self):
        """Create Stripe checkout session for plan subscription"""
        try:
            user_email = get_jwt_identity()
            data = request.json
            plan_name = data.get('plan')
            duration = data.get('duration')
            isRecurring = data.get('isRecurring')

            if not plan_name or not duration:
                return {"error": "Plan and duration are required"}, 400

            if plan_name not in STRIPE_PRICE_IDS:
                return {"error": "Invalid plan selected"}, 400

            # Create metadata to store with the session
            metadata = {
                'user_email': user_email,
                'plan': plan_name,
                'duration': str(duration),
                'isRecurring': str(isRecurring)
            }

            # Create Stripe checkout session
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price': STRIPE_PRICE_IDS[plan_name],
                    'quantity': 1,
                }],
                mode='subscription',
                success_url=f"{STRIPE_SUCCESS_URL}?payment_session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=STRIPE_CANCEL_URL,
                customer_email=user_email,
                metadata=metadata
            )

            logger.info(f"Created Stripe session for user: {user_email}, plan: {plan_name}")
            return {
                "sessionId": session.id,
                "url": session.url # redirect url to stripe checkout page
            }, 200

        except stripe.error.StripeError as e:
            logger.error(f"Stripe error: {str(e)}")
            return {"error": str(e)}, 400
        except Exception as e:
            logger.error(f"Error creating payment session: {str(e)}")
            return {"error": "An error occurred while creating payment session"}, 500

@payment_ns.route('/webhook')
class StripeWebhook(Resource):
    @payment_ns.doc(
        responses={
            200: 'Success',
            400: 'Invalid signature or payload',
            500: 'Server Error'
        },
        description='Handle Stripe webhook events for checkout session completion'
    )
    def post(self):
        """Handle Stripe webhook events for checkout session completion"""
        try:
            payload = request.get_data()
            sig_header = request.headers.get('Stripe-Signature')

            try:
                event = stripe.Webhook.construct_event(
                    payload, sig_header, STRIPE_WEBHOOK_SECRET
                )
            except ValueError as e:
                logger.error("Invalid payload")
                return {"error": "Invalid payload"}, 400
            except stripe.error.SignatureVerificationError as e:
                logger.error("Invalid signature")
                return {"error": "Invalid signature"}, 400

            if event['type'] == 'checkout.session.completed':
                session = event['data']['object']
                
                if session.payment_status != 'paid':
                    logger.warning(f"Checkout session {session.id} not paid yet")
                    return {"success": True}, 200

                metadata = session.get('metadata', {})
                user_email = metadata.get('user_email')
                plan_name = metadata.get('plan')
                duration = int(metadata.get('duration', 0))
                isRecurring = metadata.get('isRecurring')
                logger.info(f"Received session metadata: {metadata}")

                if not all([user_email, plan_name, duration]):
                    logger.error(f"Missing required metadata in session {session.id}")
                    return {"error": "Missing required metadata"}, 400

                try:
                    # try to update user plan (with automatic retry)
                    plan_data = update_user_plan(user_email, plan_name, duration, isRecurring)
                    logger.info(f"Successfully updated plan for user {user_email}: {plan_data}")
                    
                    # check if there are failed records, if so, delete them
                    failed_key = f"failed_update:{session.id}"
                    redis_user_client.delete(failed_key)

                except Exception as e:
                    logger.error(f"Error updating user plan: {str(e)}")
                    # store failed records
                    store_failed_update(session.id, user_email, {
                        "name": plan_name,
                        "duration": duration
                    }, str(e))
                    # start background retry task
                    retry_failed_updates.apply_async(args=[session.id])
            else:
                logger.warning(f"Unhandled event type: {event['type']}")  

            return {"success": True}, 200

        except Exception as e:
            logger.error(f"Error processing webhook: {str(e)}")
            return {"error": "An error occurred while processing webhook"}, 500

@payment_ns.route('/verify-session/<string:session_id>')
class VerifyPayment(Resource):
    @jwt_required_and_refresh()
    @payment_ns.doc(
        responses={
            200: 'Success - Returns payment status',
            400: 'Invalid session ID',
            401: 'Unauthorized',
            500: 'Server Error'
        },
        description='Verify payment session status'
    )
    def post(self, session_id):
        """Verify payment session status"""
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            logger.info(f"Session verification successful: {session}")
            # get existing plan expiration time from redis
            user_key = f"{USER_PREFIX}{session.metadata.get('user_email')}"
            user_data = redis_user_client.hgetall(user_key)
            # Parse JSON strings into objects for specific fields
            user_info = {}
            for k, v in user_data.items():
                if k != b'password':
                    key = k.decode('utf-8')
                    value = v.decode('utf-8')
                    # Try to parse JSON strings for specific fields
                    try:
                        # Attempt to parse each field as JSON
                        user_info[key] = json.loads(value)
                    except json.JSONDecodeError:
                        # If parsing fails, keep it as a string
                        user_info[key] = value

            return {
                "status": session.payment_status,
                "userInfo": user_info
            }, 200
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error: {str(e)}")
            return {"error": str(e)}, 400
        except Exception as e:
            logger.error(f"Error verifying payment session: {str(e)}")
            return {"error": "An error occurred while verifying payment session"}, 500


@with_retry()
def update_user_plan(user_email, plan_name, duration, isRecurring):
    """Update user plan core logic"""
    user_key = f"{USER_PREFIX}{user_email}"

    # calculate plan expiration time
    expire_time = (datetime.now() + timedelta(days=duration)).strftime('%Y-%m-%d %H:%M:%S')

    # create new plan data
    plan_data = {
        "name": plan_name,
        "expireTime": expire_time,
        "isRecurring": isRecurring
    }

    # store plan data to Redis
    redis_user_client.hset(user_key, 'plan', json.dumps(plan_data))
    return plan_data

def store_failed_update(session_id, user_email, plan_data, error, retry_count=0):
    """Store failed update records"""
    try:
        failed_update = {
            'session_id': session_id,
            'user_email': user_email,
            'plan_data': plan_data,
            'error': str(error),
            'retry_count': retry_count,
            'timestamp': datetime.now().isoformat(),
            'next_retry': (datetime.now() + timedelta(seconds=PAYMENT_RETRY_DELAY_SECONDS)).isoformat()
        }
        
        # use session_id as key to store failed records
        key = f"failed_update:{session_id}"
        redis_user_client.setex(
            key,
            PAYMENT_RETRY_KEY_EXPIRE_SECONDS,
            json.dumps(failed_update)
        )
        
        logger.error(f"Stored failed update for session {session_id}, retry count: {retry_count}")
    except Exception as e:
        logger.error(f"Error storing failed update: {str(e)}")

@shared_task(bind=True, max_retries=PAYMENT_MAX_RETRY_ATTEMPTS)
def retry_failed_updates(self, session_id):
    """Background task to handle failed updates"""
    try:
        failed_key = f"failed_update:{session_id}"
        failed_data = redis_user_client.get(failed_key)

        if not failed_data:
            logger.info(f"No failed update found for session {session_id}")
            return

        failed_update = json.loads(failed_data)
        retry_count = failed_update.get('retry_count', 0)

        if retry_count >= PAYMENT_MAX_RETRY_ATTEMPTS:
            logger.error(f"Max retries reached for session {session_id}")
            return

        # update retry count
        failed_update['retry_count'] = retry_count + 1

        try:
            # retry update user plan
            update_user_plan(
                failed_update['user_email'],
                failed_update['plan_data']['name'],
                failed_update['plan_data']['duration'],
                failed_update['plan_data']['isRecurring']
            )
            
            # update successful, delete failed records
            redis_user_client.delete(failed_key)
            logger.info(f"Retry successful for session {session_id}")

        except Exception as e:
            # update failed records and schedule next retry
            store_failed_update(
                session_id,
                failed_update['user_email'],
                failed_update['plan_data'],
                str(e),
                retry_count + 1
            )
            
            # if not reached max retry times, schedule next retry
            if retry_count + 1 < PAYMENT_MAX_RETRY_ATTEMPTS:
                self.retry(countdown=PAYMENT_RETRY_DELAY_SECONDS)

    except Exception as e:
        logger.error(f"Error in retry task: {str(e)}")
        if self.request.retries < PAYMENT_MAX_RETRY_ATTEMPTS:
            self.retry(countdown=PAYMENT_RETRY_DELAY_SECONDS)