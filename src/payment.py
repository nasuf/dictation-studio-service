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
    USER_PREFIX,
    VERIFICATION_CODE_EXPIRE_SECONDS
)
from utils import with_retry
from celery import shared_task
from datetime import datetime, timedelta
import json
import logging
from functools import wraps
from werkzeug.local import LocalProxy
from flask import current_app
import secrets
import hashlib

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

verification_code_model = payment_ns.model('VerificationCode', {
    'duration': fields.String(required=True, description='Membership duration (1month, 3months, 6months, permanent)', 
                             enum=['1month', '3months', '6months', 'permanent'])
})

verification_model = payment_ns.model('Verification', {
    'code': fields.String(required=True, description='Verification code to validate')
})

# Define membership duration mapping (days)
DURATION_MAPPING = {
    '1month': 30,
    '3months': 90,
    '6months': 180,
    'permanent': -1  # 使用-1表示永久
}

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
    @jwt_required()
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

@payment_ns.route('/cancel-subscription')
class CancelSubscription(Resource):
    @jwt_required()
    @payment_ns.doc(
        responses={
            200: 'Success - Subscription cancelled',
            400: 'Bad Request',
            401: 'Unauthorized',
            404: 'Subscription not found',
            500: 'Server Error'
        },
        description='Cancel user\'s current subscription'
    )
    def post(self):
        """Cancel user's current subscription"""
        try:
            user_email = get_jwt_identity()
            user_key = f"{USER_PREFIX}{user_email}"
            
            # Get user data from Redis
            user_data = redis_user_client.hgetall(user_key)
            if not user_data:
                return {"error": "User not found"}, 404

            # Get current plan data
            try:
                plan_data = json.loads(user_data.get(b'plan', b'{}').decode('utf-8'))
            except json.JSONDecodeError:
                plan_data = {}

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

            # Update plan data in Redis to reflect cancellation
            # expireTime should be set to original nextPaymentTime, and remove nextPaymentTime
            plan_data['cancelledAt'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            plan_data['status'] = 'cancelled'
            plan_data['expireTime'] = plan_data['nextPaymentTime']
            plan_data.pop('nextPaymentTime', None)
            redis_user_client.hset(user_key, 'plan', json.dumps(plan_data))

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
    user_key = f"{USER_PREFIX}{user_email}"

    # calculate plan expiration time
    expire_time = (datetime.now() + timedelta(days=duration)).strftime('%Y-%m-%d %H:%M:%S')
    next_payment_time = (datetime.now() + timedelta(days=duration)).strftime('%Y-%m-%d %H:%M:%S')
    # create new plan data
    # if isRecurring, do not set expireTime but set nextPaymentTime
    # turn isRecurring to boolean
    isRecurring = isRecurring == 'True'
    plan_data = {
        "name": plan_name,
        "expireTime": expire_time if not isRecurring else None,
        "nextPaymentTime": next_payment_time if isRecurring else None,
        "isRecurring": isRecurring,
        "status": "active"
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

@payment_ns.route('/generate-code')
class GenerateVerificationCode(Resource):
    @jwt_required()
    @payment_ns.expect(verification_code_model)
    @payment_ns.doc(
        responses={
            200: 'Success - Returns verification code',
            400: 'Invalid Input',
            401: 'Unauthorized',
            500: 'Server Error'
        },
        description='Generate a verification code for membership duration'
    )
    def post(self):
        """Generate a verification code for membership duration"""
        try:
            # 获取管理员身份，但不存储到校验码数据中
            admin_email = get_jwt_identity()
            data = request.json
            duration = data.get('duration')

            if not duration or duration not in DURATION_MAPPING:
                return {"error": "Invalid duration specified"}, 400

            # 生成随机校验码（16个字符的十六进制字符串）
            random_part = secrets.token_hex(8)
            
            # 创建包含时间戳和会员时长的数据，不包含用户邮箱
            timestamp = datetime.now().timestamp()
            code_data = {
                'timestamp': timestamp,
                'duration': duration,
                'days': DURATION_MAPPING[duration]  # 存储实际天数
            }
            
            # 将数据存储到Redis，设置1小时过期
            code_key = f"verification_code:{random_part}"
            redis_user_client.setex(
                code_key,
                VERIFICATION_CODE_EXPIRE_SECONDS,
                json.dumps(code_data)
            )
            
            # 生成校验码的哈希部分（用于验证）
            hash_input = f"{random_part}:{duration}:{timestamp}"
            hash_part = hashlib.sha256(hash_input.encode()).hexdigest()[:8]
            
            # 完整的校验码
            verification_code = f"{random_part}-{hash_part}"
            
            logger.info(f"Admin {admin_email} generated verification code for duration: {duration}")
            return {
                "code": verification_code,
                "expiresIn": VERIFICATION_CODE_EXPIRE_SECONDS,
                "duration": duration,
                "days": DURATION_MAPPING[duration]
            }, 200

        except Exception as e:
            logger.error(f"Error generating verification code: {str(e)}")
            return {"error": "An error occurred while generating verification code"}, 500

@payment_ns.route('/verify-code')
class VerifyCode(Resource):
    @jwt_required()
    @payment_ns.expect(verification_model)
    @payment_ns.doc(
        responses={
            200: 'Success - Code verified and membership applied',
            400: 'Invalid Input or Code',
            401: 'Unauthorized',
            404: 'Code Not Found or Expired',
            500: 'Server Error'
        },
        description='Verify a membership code and apply the membership'
    )
    def post(self):
        """Verify a membership code and apply the membership"""
        try:
            user_email = get_jwt_identity()
            data = request.json
            code = data.get('code')

            if not code or '-' not in code:
                return {"error": "Invalid verification code format"}, 400
            
            # 解析校验码
            random_part, hash_part = code.split('-')
            
            # 从Redis获取存储的数据
            code_key = f"verification_code:{random_part}"
            stored_data = redis_user_client.get(code_key)
            
            if not stored_data:
                return {"error": "Verification code not found or expired"}, 404
            
            code_data = json.loads(stored_data)
            duration = code_data.get('duration')
            timestamp = code_data.get('timestamp')
            days_duration = code_data.get('days')
            
            # 验证哈希部分
            hash_input = f"{random_part}:{duration}:{timestamp}"
            expected_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:8]
            
            if hash_part != expected_hash:
                return {"error": "Invalid verification code"}, 400
            
            # 应用会员时长
            plan_name = "Premium"  # 可以根据需要调整
            
            # 更新用户计划
            plan_data = update_user_plan(user_email, plan_name, days_duration, False)
            
            # 使用后删除验证码
            redis_user_client.delete(code_key)
            
            logger.info(f"Successfully applied membership for user {user_email}: {plan_data}")
            return {
                "message": "Membership successfully applied",
                "plan": plan_data
            }, 200

        except Exception as e:
            logger.error(f"Error verifying code: {str(e)}")
            return {"error": f"An error occurred while verifying code: {str(e)}"}, 500

@payment_ns.route('/verification-codes')
class VerificationCodes(Resource):
    @jwt_required()
    @payment_ns.doc(
        responses={
            200: 'Success - Returns all active verification codes',
            401: 'Unauthorized',
            403: 'Forbidden - Not an admin',
            500: 'Server Error'
        },
        description='Get all active verification codes (Admin only)'
    )
    def get(self):
        """Get all active verification codes (Admin only)"""
        try:
            # 获取用户身份
            user_email = get_jwt_identity()
            
            # 检查用户是否为管理员
            user_key = f"{USER_PREFIX}{user_email}"
            user_data = redis_user_client.hgetall(user_key)
            
            if not user_data or user_data.get(b'role', b'').decode('utf-8') != 'Admin':
                logger.warning(f"Non-admin user {user_email} attempted to access verification codes")
                return {"error": "Only administrators can access verification codes"}, 403
            
            # 获取所有校验码
            codes = []
            for key in redis_user_client.scan_iter(match="verification_code:*"):
                code_data = redis_user_client.get(key)
                if code_data:
                    code_info = json.loads(code_data)
                    random_part = key.decode('utf-8').split(':')[1]
                    
                    # 计算过期时间
                    created_time = datetime.fromtimestamp(code_info.get('timestamp', 0))
                    expires_at = created_time + timedelta(seconds=VERIFICATION_CODE_EXPIRE_SECONDS)
                    remaining_seconds = (expires_at - datetime.now()).total_seconds()
                    
                    if remaining_seconds > 0:
                        # 生成完整校验码
                        timestamp = code_info.get('timestamp')
                        duration = code_info.get('duration')
                        hash_input = f"{random_part}:{duration}:{timestamp}"
                        hash_part = hashlib.sha256(hash_input.encode()).hexdigest()[:8]
                        full_code = f"{random_part}-{hash_part}"
                        
                        codes.append({
                            'code_part': random_part,
                            'full_code': full_code,  # 添加完整校验码
                            'duration': duration,
                            'days': code_info.get('days'),
                            'created_at': created_time.isoformat(),
                            'expires_at': expires_at.isoformat(),
                            'remaining_seconds': int(remaining_seconds)
                        })
            
            return {
                "codes": codes,
                "count": len(codes)
            }, 200

        except Exception as e:
            logger.error(f"Error retrieving verification codes: {str(e)}")
            return {"error": f"An error occurred while retrieving verification codes: {str(e)}"}, 500