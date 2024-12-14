from flask import request
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import get_jwt_identity, jwt_required
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
from utils import with_retry
from celery import shared_task
from datetime import datetime, timedelta
import json
import logging
from functools import wraps
from werkzeug.local import LocalProxy
from flask import current_app
from cache import (
    get_user_from_cache_or_redis,
    update_user_plan_in_cache,
    get_failed_update_from_cache_or_redis,
    update_failed_update_in_cache,
    remove_failed_update_from_cache
)

# Configure logging
logger = logging.getLogger(__name__)

# Initialize Stripe
stripe.api_key = STRIPE_SECRET_KEY

# Create namespace
payment_ns = Namespace('payment', description='Payment operations')

# Define Stripe price IDs for different plans
STRIPE_PRICE_IDS = {
    'Basic': {
        'OneTime': 'price_1QLQ1uIIAqSdCVKTeFznb96J',
        'Recurring': 'price_1QKKPbIIAqSdCVKTirwsdkhZ'
    },
    'Pro': {
        'OneTime': 'price_1QLQ5eIIAqSdCVKTLUhszqnD',
        'Recurring': 'price_1QKKRaIIAqSdCVKT95VKqeru'
    },
    'Premium': {
        'OneTime': 'price_1QNqxqIIAqSdCVKTIoYfNQxq',
        'Recurring': 'price_1QNqz4IIAqSdCVKTgjuYjaGB'
    }
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
    @jwt_required()
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
            if isRecurring:
                price_key = 'Recurring'
            else:
                price_key = 'OneTime'

            # Create Stripe checkout session
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price': STRIPE_PRICE_IDS[plan_name][price_key],
                    'quantity': 1,
                }],
                mode= 'subscription' if isRecurring else 'payment',
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
    @payment_ns.doc(responses={200: 'Success', 400: 'Invalid signature or payload', 500: 'Server Error'})
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
                    # Try to update user plan (with automatic retry)
                    plan_data = update_user_plan(user_email, plan_name, duration, isRecurring)
                    logger.info(f"Successfully updated plan for user {user_email}: {plan_data}")
                    
                    # Remove any existing failed records from cache and Redis
                    remove_failed_update_from_cache(session.id, redis_user_client)

                except Exception as e:
                    logger.error(f"Error updating user plan: {str(e)}")
                    # Store failed records in cache and Redis
                    store_failed_update(session.id, user_email, {
                        "name": plan_name,
                        "duration": duration,
                        "isRecurring": isRecurring
                    }, str(e))
                    # Start background retry task
                    retry_failed_updates.apply_async(args=[session.id])
            else:
                logger.warning(f"Unhandled event type: {event['type']}")  

            return {"success": True}, 200

        except Exception as e:
            logger.error(f"Error processing webhook: {str(e)}")
            return {"error": "An error occurred while processing webhook"}, 500

@payment_ns.route('/verify-session/<string:session_id>')
class VerifyPayment(Resource):
    @jwt_required()
    @payment_ns.doc(responses={200: 'Success', 400: 'Invalid session ID', 401: 'Unauthorized', 500: 'Server Error'})
    def post(self, session_id):
        """Verify payment session status"""
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            logger.info(f"Session verification successful: {session}")
            
            # Get metadata from session
            metadata = session.get('metadata', {})
            user_email = metadata.get('user_email')
            plan_name = metadata.get('plan')
            duration = int(metadata.get('duration', 0))
            isRecurring = metadata.get('isRecurring')

            # If payment is successful, update the plan
            logger.info(f"Session payment status: {session.payment_status}")
            if session.payment_status == 'paid':
                logger.info(f"Payment successful for session {session_id}, updating user plan")
                try:
                    # Update user plan
                    plan_data = update_user_plan(user_email, plan_name, duration, isRecurring)
                    logger.info(f"Successfully updated plan for user {user_email}: {plan_data}")
                except Exception as e:
                    logger.error(f"Error updating user plan during verification: {str(e)}")
                    # Store failed update for retry
                    store_failed_update(session_id, user_email, {
                        "name": plan_name,
                        "duration": duration,
                        "isRecurring": isRecurring
                    }, str(e))
                    retry_failed_updates.apply_async(args=[session_id])

            # Get latest user data after potential update
            user_data = get_user_from_cache_or_redis(user_email, redis_user_client)
            
            # Filter out password from user data
            user_info = {}
            if user_data:
                user_info = {k: v for k, v in user_data.items() if k != 'password'}
            
            return {
                "status": session.payment_status,
                "userInfo": user_info,
                "metadata": metadata
            }, 200

        except stripe.error.StripeError as e:
            logger.error(f"Stripe error: {str(e)}")
            return {"error": str(e)}, 400
        except Exception as e:
            logger.error(f"Error verifying payment session: {str(e)}")
            return {"error": "An error occurred while verifying payment session"}, 500

@payment_ns.route('/cancel-subscription')
class CancelSubscription(Resource):
    @jwt_required()
    @payment_ns.doc(responses={200: 'Success', 400: 'Bad Request', 401: 'Unauthorized', 404: 'Not Found', 500: 'Server Error'})
    def post(self):
        """Cancel user's current subscription"""
        try:
            user_email = get_jwt_identity()
            
            # Get user data using cache
            logger.info(f"Getting user data for {user_email} to cancel subscription")
            user_data = get_user_from_cache_or_redis(user_email, redis_user_client)
            if not user_data:
                return {"error": "User not found"}, 404

            # Get current plan data
            plan_data = user_data.get('plan', {})
            if not plan_data or not plan_data.get('isRecurring'):
                return {"error": "No active recurring subscription found"}, 404

            # Find customer's subscription in Stripe
            customers = stripe.Customer.list(email=user_email, limit=1)
            if not customers.data:
                return {"error": "No Stripe customer found"}, 404

            customer = customers.data[0]
            subscriptions = stripe.Subscription.list(customer=customer.id, limit=1)
            
            if not subscriptions.data:
                return {"error": "No active subscription found"}, 404

            subscription = subscriptions.data[0]

            # Cancel the subscription at period end
            stripe.Subscription.modify(
                subscription.id,
                cancel_at_period_end=True
            )

            # Update plan data
            plan_data['cancelledAt'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            plan_data['status'] = 'cancelled'
            plan_data['expireTime'] = plan_data['nextPaymentTime']
            plan_data.pop('nextPaymentTime', None)
            
            # Update cache and Redis
            update_user_plan_in_cache(user_email, plan_data, redis_user_client)

            logger.info(f"Subscription cancelled for user: {user_email}")
            return {
                "message": "Subscription cancelled successfully",
                "plan": plan_data
            }, 200

        except stripe.error.StripeError as e:
            logger.error(f"Stripe error while cancelling subscription: {str(e)}")
            return {"error": str(e)}, 400
        except Exception as e:
            logger.error(f"Error cancelling subscription: {str(e)}")
            return {"error": "An error occurred while cancelling subscription"}, 500

@with_retry()
def update_user_plan(user_email, plan_name, duration, isRecurring):
    """Update user plan core logic"""
    # Calculate plan expiration time
    expire_time = (datetime.now() + timedelta(days=duration)).strftime('%Y-%m-%d %H:%M:%S')
    next_payment_time = (datetime.now() + timedelta(days=duration)).strftime('%Y-%m-%d %H:%M:%S')
    
    # Convert isRecurring to boolean
    isRecurring = isRecurring == 'True'
    
    # Create new plan data
    plan_data = {
        "name": plan_name,
        "expireTime": expire_time if not isRecurring else None,
        "nextPaymentTime": next_payment_time if isRecurring else None,
        "isRecurring": isRecurring,
        "status": "active"
    }

    # Update cache and Redis
    update_user_plan_in_cache(user_email, plan_data, redis_user_client)
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
        
        # Update cache and Redis
        update_failed_update_in_cache(
            session_id,
            failed_update,
            PAYMENT_RETRY_KEY_EXPIRE_SECONDS,
            redis_user_client
        )
        
        logger.error(f"Stored failed update for session {session_id}, retry count: {retry_count}")
    except Exception as e:
        logger.error(f"Error storing failed update: {str(e)}")

@shared_task(bind=True, max_retries=PAYMENT_MAX_RETRY_ATTEMPTS)
def retry_failed_updates(self, session_id):
    """Background task to handle failed updates"""
    try:
        # Get failed update data from cache
        failed_update = get_failed_update_from_cache_or_redis(session_id, redis_user_client)
        if not failed_update:
            logger.info(f"No failed update found for session {session_id}")
            return

        retry_count = failed_update.get('retry_count', 0)
        if retry_count >= PAYMENT_MAX_RETRY_ATTEMPTS:
            logger.error(f"Max retries reached for session {session_id}")
            return

        # Update retry count
        failed_update['retry_count'] = retry_count + 1

        try:
            # Retry update user plan
            update_user_plan(
                failed_update['user_email'],
                failed_update['plan_data']['name'],
                failed_update['plan_data']['duration'],
                failed_update['plan_data']['isRecurring']
            )
            
            # Update successful, remove failed records from cache and Redis
            remove_failed_update_from_cache(session_id, redis_user_client)
            logger.info(f"Retry successful for session {session_id}")

        except Exception as e:
            # Update failed records and schedule next retry
            store_failed_update(
                session_id,
                failed_update['user_email'],
                failed_update['plan_data'],
                str(e),
                retry_count + 1
            )
            
            # If not reached max retry times, schedule next retry
            if retry_count + 1 < PAYMENT_MAX_RETRY_ATTEMPTS:
                self.retry(countdown=PAYMENT_RETRY_DELAY_SECONDS)

    except Exception as e:
        logger.error(f"Error in retry task: {str(e)}")
        if self.request.retries < PAYMENT_MAX_RETRY_ATTEMPTS:
            self.retry(countdown=PAYMENT_RETRY_DELAY_SECONDS)