from flask import request, make_response, jsonify
from flask_restx import Namespace, Resource, fields
from flask_jwt_extended import get_jwt_identity, jwt_required
import hashlib
import json
import logging
import requests
import secrets
import time
from datetime import datetime, timedelta
from config import (
    USER_PREFIX,
    PAYMENT_MAX_RETRY_ATTEMPTS,
    PAYMENT_RETRY_DELAY_SECONDS,
    PAYMENT_RETRY_KEY_EXPIRE_SECONDS,
    ZPAY_NOTIFY_URL,
    ZPAY_RETURN_URL
)
from utils import update_user_plan, get_plan_name_by_duration
from celery import shared_task
from redis_manager import RedisManager

# Configure logging
logger = logging.getLogger(__name__)

# ZPAY Configuration
ZPAY_BASE_URL = "https://zpayz.cn"
ZPAY_PID = "2025032310362328"
ZPAY_KEY = "kR4VkjRaDnP3VI3zYmPQYwgwfO0rbbKn"
ZPAY_SUBMIT_URL = f"{ZPAY_BASE_URL}/submit.php"
ZPAY_API_URL = f"{ZPAY_BASE_URL}/api.php"


# Redis key prefixes
ZPAY_ORDER_PREFIX = "zpay_order:"
ZPAY_USER_ORDERS_PREFIX = "user_zpay_orders:"
ZPAY_CALLBACK_LOCK_PREFIX = "zpay_callback_lock:"

# Order status constants
ORDER_STATUS_PENDING = "pending"
ORDER_STATUS_PAID = "paid"
ORDER_STATUS_FAILED = "failed"
ORDER_STATUS_EXPIRED = "expired"

# Payment type mapping
ZPAY_PAYMENT_TYPES = {
    'alipay': '支付宝',
    'wxpay': '微信支付'
}

# Plan pricing (in CNY)
ZPAY_PLAN_PRICING = {
    'Basic': '0.01',    # 1 month
    'Pro': '49.00',      # 3 months  
    'Premium': '89.00'   # 6 months
}

# Create namespace
payment_zpay_ns = Namespace('payment/zpay', description='ZPAY payment operations')

# Redis manager
redis_manager = RedisManager()
redis_user_client = redis_manager.get_user_client()

# Define models
zpay_order_model = payment_zpay_ns.model('ZPayOrder', {
    'plan': fields.String(required=True, description='Plan name (Premium/Pro/Basic)', enum=['Premium', 'Pro', 'Basic']),
    'duration': fields.Integer(required=True, description='Duration in days', enum=[30, 90, 180]),
    'payType': fields.String(required=True, description='Payment type', enum=['alipay', 'wxpay'])
})

# Utility functions
def generate_order_id() -> str:
    """Generate unique order ID"""
    timestamp = int(time.time())
    random_part = secrets.token_hex(4).upper()
    return f"DS_ZPAY_{timestamp}_{random_part}"

def generate_zpay_signature(params: dict, key: str) -> str:
    """Generate ZPAY signature using MD5"""
    # Sort parameters by key name (ASCII order)
    sorted_params = sorted(params.items())
    
    # Build query string
    query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
    
    # Add key and generate MD5 hash
    sign_string = query_string + key
    signature = hashlib.md5(sign_string.encode('utf-8')).hexdigest()
    
    return signature

def verify_zpay_signature(params: dict, key: str, received_sign: str) -> bool:
    """Verify ZPAY callback signature"""
    # Remove sign and sign_type from params
    filtered_params = {k: v for k, v in params.items() if k not in ['sign', 'sign_type']}
    
    # Generate expected signature
    expected_sign = generate_zpay_signature(filtered_params, key)
    
    return expected_sign.lower() == received_sign.lower()

def get_zpay_order_data(order_id: str) -> dict:
    """Get order data from Redis"""
    order_key = f"{ZPAY_ORDER_PREFIX}{order_id}"
    order_data = redis_user_client.hgetall(order_key)
    
    if not order_data:
        return {}
    
    # Parse JSON fields
    parsed_data = {}
    for k, v in order_data.items():
        if k in ['metadata']:
            try:
                parsed_data[k] = json.loads(v)
            except json.JSONDecodeError:
                parsed_data[k] = v
        else:
            parsed_data[k] = v
    
    return parsed_data

def store_zpay_order_data(order_id: str, order_data: dict):
    """Store order data to Redis"""
    order_key = f"{ZPAY_ORDER_PREFIX}{order_id}"
    
    # Convert dict fields to JSON strings
    redis_data = {}
    for k, v in order_data.items():
        if isinstance(v, dict):
            redis_data[k] = json.dumps(v)
        else:
            redis_data[k] = str(v)
    
    # Store with 24 hour expiration
    redis_user_client.hset(order_key, mapping=redis_data)
    redis_user_client.expire(order_key, 86400)  # 24 hours

def add_order_to_user(user_email: str, order_id: str):
    """Add order ID to user's order list"""
    user_orders_key = f"{ZPAY_USER_ORDERS_PREFIX}{user_email}"
    redis_user_client.lpush(user_orders_key, order_id)
    redis_user_client.expire(user_orders_key, 86400 * 30)  # 30 days

@payment_zpay_ns.route('/create-order')
class CreateZPayOrder(Resource):
    @jwt_required()
    @payment_zpay_ns.expect(zpay_order_model)
    @payment_zpay_ns.doc(
        responses={
            200: 'Success - Returns order ID and payment URL',
            400: 'Invalid Input',
            401: 'Unauthorized',
            500: 'Server Error'
        },
        description='Create a ZPAY payment order'
    )
    def post(self):
        """Create ZPAY payment order"""
        try:
            user_email = get_jwt_identity()
            data = request.json
            
            plan_name = data.get('plan')
            duration = data.get('duration')
            pay_type = data.get('payType')
            
            # Validate input
            if not all([plan_name, duration, pay_type]):
                return {"error": "Plan, duration, and payType are required"}, 400
            
            if plan_name not in ZPAY_PLAN_PRICING:
                return {"error": "Invalid plan selected"}, 400
            
            # Validate duration matches plan (Basic: 30, Pro: 90, Premium: 180)
            expected_durations = {'Basic': 30, 'Pro': 90, 'Premium': 180}
            if plan_name in expected_durations and duration != expected_durations[plan_name]:
                return {"error": f"Invalid duration for {plan_name} plan. Expected {expected_durations[plan_name]} days"}, 400
            
            if pay_type not in ZPAY_PAYMENT_TYPES:
                return {"error": "Invalid payment type"}, 400
            
            # Get pricing
            amount = ZPAY_PLAN_PRICING[plan_name]
            
            # Generate order ID
            order_id = generate_order_id()
            
            # Prepare ZPAY parameters
            zpay_params = {
                'money': amount,
                'name': f"Dictation Studio - {plan_name} Plan - {duration} Days",
                'notify_url': ZPAY_NOTIFY_URL,
                'out_trade_no': order_id,
                'pid': ZPAY_PID,
                'return_url': ZPAY_RETURN_URL,
                'sitename': 'Dictation Studio',
                'type': pay_type
            }
            
            # Generate signature
            signature = generate_zpay_signature(zpay_params, ZPAY_KEY)
            
            # Build payment URL
            query_params = []
            for k, v in zpay_params.items():
                query_params.append(f"{k}={v}")
            query_params.append(f"sign={signature}")
            query_params.append("sign_type=MD5")
            
            payment_url = f"{ZPAY_SUBMIT_URL}?" + "&".join(query_params)
            
            # Store order data
            order_data = {
                'order_id': order_id,
                'user_email': user_email,
                'plan_name': plan_name,
                'duration': duration,
                'amount': amount,
                'pay_type': pay_type,
                'status': ORDER_STATUS_PENDING,
                'created_at': datetime.now().isoformat(),
                'payment_url': payment_url,
                'metadata': {
                    'zpay_params': zpay_params,
                    'signature': signature
                }
            }
            
            store_zpay_order_data(order_id, order_data)
            add_order_to_user(user_email, order_id)
            
            logger.info(f"Created ZPAY order for user: {user_email}, order: {order_id}, plan: {plan_name}")
            
            return {
                "orderId": order_id,
                "paymentUrl": payment_url,
                "amount": amount,
                "currency": "CNY"
            }, 200
            
        except Exception as e:
            logger.error(f"Error creating ZPAY order: {str(e)}")
            return {"error": "An error occurred while creating payment order"}, 500

@payment_zpay_ns.route('/notify')
class ZPayNotify(Resource):
    @payment_zpay_ns.doc(
        responses={
            200: 'Success',
            400: 'Invalid signature or data',
            500: 'Server Error'
        },
        description='Handle ZPAY payment notification callback'
    )
    def get(self):
        """Handle ZPAY payment notification callback"""
        logger.info(f"Received ZPAY callback: {request.args.to_dict()}")
        
        try:
            # Get callback data (query parameters)
            callback_data = request.args.to_dict()
            
            # Validate required fields
            required_fields = ['trade_no', 'out_trade_no', 'type', 'name', 'money', 'trade_status', 'sign', 'sign_type']
            missing_fields = [field for field in required_fields if field not in callback_data]
            if missing_fields:
                logger.error(f"Missing required fields: {missing_fields}")
                return make_response("fail", 500)  # Return plain text response
            
            # Remove sign and sign_type for signature verification
            received_sign = callback_data.pop('sign', '')
            callback_data.pop('sign_type', '')
            
            # Sort parameters by key name (ASCII order)
            sorted_params = sorted(callback_data.items())
            
            # Build query string (exclude empty values)
            query_string = '&'.join([f"{k}={v}" for k, v in sorted_params if v])
            
            # Add key and generate MD5 hash
            sign_string = query_string + ZPAY_KEY
            expected_sign = hashlib.md5(sign_string.encode('utf-8')).hexdigest()
            
            # Verify signature
            if expected_sign.lower() != received_sign.lower():
                logger.error("Invalid ZPAY callback signature")
                logger.error(f"Expected: {expected_sign}, Received: {received_sign}")
                logger.error(f"Sign string: {sign_string}")
                return make_response("fail", 500)  # Return plain text response
            
            # Extract data
            trade_no = callback_data.get('trade_no')
            out_trade_no = callback_data.get('out_trade_no')
            trade_status = callback_data.get('trade_status')
            money = callback_data.get('money')
            
            # Check if payment is successful
            if trade_status != 'TRADE_SUCCESS':
                logger.warning(f"Payment not successful for order {out_trade_no}, status: {trade_status}")
                # Update order status to failed if payment failed
                order_data = get_zpay_order_data(out_trade_no)
                if order_data:
                    order_data['status'] = ORDER_STATUS_FAILED
                    store_zpay_order_data(out_trade_no, order_data)
                return make_response("success", 200)  # Return success to stop retries
            
            # Process payment with idempotency
            result = process_zpay_payment_idempotent(out_trade_no, trade_no, callback_data)
            
            if result['success']:
                logger.info(f"Successfully processed ZPAY payment for order: {out_trade_no}")
                return make_response("success", 200)  # Return plain text success
            else:
                logger.error(f"Failed to process ZPAY payment: {result['error']}")
                return make_response("fail", 500)  # Return plain text response
                
        except Exception as e:
            logger.error(f"Error processing ZPAY callback: {str(e)}")
            return make_response("fail", 500)  # Return plain text response

def process_zpay_payment_idempotent(order_id: str, trade_no: str, callback_data: dict) -> dict:
    """Process ZPAY payment with idempotency"""
    lock_key = f"{ZPAY_CALLBACK_LOCK_PREFIX}{order_id}"
    
    try:
        # Try to acquire lock (30 second timeout)
        if not redis_user_client.set(lock_key, "1", nx=True, ex=30):
            return {"success": False, "error": "Payment already being processed"}
        
        # Get order data
        order_data = get_zpay_order_data(order_id)
        if not order_data:
            return {"success": False, "error": "Order not found"}
        
        # Check if already processed
        if order_data.get('status') == ORDER_STATUS_PAID:
            logger.info(f"Order {order_id} already processed")
            return {"success": True, "message": "Already processed"}
        
        # Validate amount
        expected_amount = order_data.get('amount')
        received_amount = callback_data.get('money')
        if expected_amount != received_amount:
            logger.error(f"Amount mismatch for order {order_id}: expected {expected_amount}, received {received_amount}")
            return {"success": False, "error": "Amount mismatch"}
        
        # Update order status BEFORE updating user plan
        order_data['status'] = ORDER_STATUS_PAID
        order_data['trade_no'] = trade_no
        order_data['paid_at'] = datetime.now().isoformat()
        order_data['callback_data'] = callback_data
        
        store_zpay_order_data(order_id, order_data)
        
        # Update user plan
        try:
            plan_data = update_user_plan(
                order_data['user_email'],
                order_data['plan_name'],
                int(order_data['duration']),
                False  # ZPAY doesn't support recurring payments
            )
            
            order_data['plan_update_result'] = plan_data
            store_zpay_order_data(order_id, order_data)
            
            logger.info(f"Successfully updated plan for user {order_data['user_email']}: {plan_data}")
            
        except Exception as e:
            logger.error(f"Error updating user plan for order {order_id}: {str(e)}")
            # Store failed update for retry
            store_failed_zpay_update(order_id, order_data, str(e))
            # Start background retry task
            retry_zpay_order_processing.apply_async(args=[order_id])
            # Still return success since payment was processed
            return {"success": True}
        
        return {"success": True}
        
    finally:
        # Release lock
        redis_user_client.delete(lock_key)

@payment_zpay_ns.route('/order-status/<string:order_id>')
class ZPayOrderStatus(Resource):
    @jwt_required()
    @payment_zpay_ns.doc(
        responses={
            200: 'Success - Returns order status',
            401: 'Unauthorized',
            404: 'Order not found',
            500: 'Server Error'
        },
        description='Get ZPAY order status'
    )
    def get(self, order_id):
        """Get ZPAY order status"""
        try:
            user_email = get_jwt_identity()
            
            # Get order data
            order_data = get_zpay_order_data(order_id)
            if not order_data:
                return {"error": "Order not found"}, 404
            
            # Verify order belongs to user
            if order_data.get('user_email') != user_email:
                return {"error": "Order not found"}, 404
            
            # If order is still pending, try to query ZPAY for latest status
            if order_data.get('status') == ORDER_STATUS_PENDING:
                zpay_status = query_zpay_order_status(order_id)
                if zpay_status and zpay_status.get('status') == 'paid':
                    # Update local status
                    order_data['status'] = ORDER_STATUS_PAID
                    order_data['trade_no'] = zpay_status.get('trade_no')
                    order_data['paid_at'] = datetime.now().isoformat()
                    store_zpay_order_data(order_id, order_data)
                    
                    # Update user plan
                    try:
                        plan_data = update_user_plan(
                            order_data['user_email'],
                            order_data['plan_name'],
                            int(order_data['duration']),
                            False
                        )
                        order_data['plan_update_result'] = plan_data
                        store_zpay_order_data(order_id, order_data)
                    except Exception as e:
                        logger.error(f"Error updating user plan during status check: {str(e)}")
            
            # Get updated user info if payment is successful
            user_info = None
            if order_data.get('status') == ORDER_STATUS_PAID:
                user_key = f"{USER_PREFIX}{user_email}"
                user_data = redis_user_client.hgetall(user_key)
                if user_data:
                    user_info = {}
                    for k, v in user_data.items():
                        if k != 'password':
                            try:
                                user_info[k] = json.loads(v)
                            except json.JSONDecodeError:
                                user_info[k] = v
            
            return {
                "orderId": order_id,
                "status": order_data.get('status'),
                "tradeNo": order_data.get('trade_no'),
                "amount": order_data.get('amount'),
                "planName": order_data.get('plan_name'),
                "duration": order_data.get('duration'),
                "createdAt": order_data.get('created_at'),
                "paidAt": order_data.get('paid_at'),
                "userInfo": user_info
            }, 200
            
        except Exception as e:
            logger.error(f"Error getting order status: {str(e)}")
            return {"error": "An error occurred while getting order status"}, 500

def query_zpay_order_status(order_id: str) -> dict:
    """Query order status from ZPAY API"""
    try:
        url = f"{ZPAY_API_URL}?act=order&pid={ZPAY_PID}&key={ZPAY_KEY}&out_trade_no={order_id}"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 1:
                return {
                    'status': 'paid' if result.get('trade_status') == 'TRADE_SUCCESS' else 'pending',
                    'trade_no': result.get('trade_no'),
                    'money': result.get('money')
                }
        
        return None
        
    except Exception as e:
        logger.error(f"Error querying ZPAY order status: {str(e)}")
        return None

@payment_zpay_ns.route('/orders')
class ZPayUserOrders(Resource):
    @jwt_required()
    @payment_zpay_ns.doc(
        responses={
            200: 'Success - Returns user orders',
            401: 'Unauthorized',
            500: 'Server Error'
        },
        description='Get user ZPAY orders'
    )
    def get(self):
        """Get user ZPAY orders"""
        try:
            user_email = get_jwt_identity()
            
            # Get user's order IDs
            user_orders_key = f"{ZPAY_USER_ORDERS_PREFIX}{user_email}"
            order_ids = redis_user_client.lrange(user_orders_key, 0, -1)
            
            orders = []
            for order_id in order_ids:
                order_data = get_zpay_order_data(order_id)
                if order_data:
                    orders.append({
                        "orderId": order_id,
                        "status": order_data.get('status'),
                        "planName": order_data.get('plan_name'),
                        "duration": order_data.get('duration'),
                        "amount": order_data.get('amount'),
                        "payType": order_data.get('pay_type'),
                        "createdAt": order_data.get('created_at'),
                        "paidAt": order_data.get('paid_at')
                    })
            
            # Sort by creation time (newest first)
            orders.sort(key=lambda x: x.get('createdAt', ''), reverse=True)
            
            return {
                "orders": orders,
                "count": len(orders)
            }, 200
            
        except Exception as e:
            logger.error(f"Error getting user orders: {str(e)}")
            return {"error": "An error occurred while getting orders"}, 500

# Background tasks
def store_failed_zpay_update(order_id: str, order_data: dict, error: str, retry_count: int = 0):
    """Store failed ZPAY update for retry"""
    try:
        failed_update = {
            'order_id': order_id,
            'user_email': order_data['user_email'],
            'plan_name': order_data['plan_name'],
            'duration': order_data['duration'],
            'error': str(error),
            'retry_count': retry_count,
            'timestamp': datetime.now().isoformat(),
            'next_retry': (datetime.now() + timedelta(seconds=PAYMENT_RETRY_DELAY_SECONDS)).isoformat()
        }
        
        key = f"failed_zpay_update:{order_id}"
        redis_user_client.setex(
            key,
            PAYMENT_RETRY_KEY_EXPIRE_SECONDS,
            json.dumps(failed_update)
        )
        
        logger.error(f"Stored failed ZPAY update for order {order_id}, retry count: {retry_count}")
    except Exception as e:
        logger.error(f"Error storing failed ZPAY update: {str(e)}")

@shared_task(bind=True, max_retries=PAYMENT_MAX_RETRY_ATTEMPTS)
def retry_zpay_order_processing(self, order_id: str):
    """Background task to retry failed ZPAY order processing"""
    try:
        failed_key = f"failed_zpay_update:{order_id}"
        failed_data = redis_user_client.get(failed_key)
        
        if not failed_data:
            logger.info(f"No failed ZPAY update found for order {order_id}")
            return
        
        failed_update = json.loads(failed_data)
        retry_count = failed_update.get('retry_count', 0)
        
        if retry_count >= PAYMENT_MAX_RETRY_ATTEMPTS:
            logger.error(f"Max retries reached for ZPAY order {order_id}")
            return
        
        try:
            # Retry updating user plan
            plan_data = update_user_plan(
                failed_update['user_email'],
                failed_update['plan_name'],
                int(failed_update['duration']),
                False
            )
            
            # Update successful, delete failed record
            redis_user_client.delete(failed_key)
            
            # Update order data with successful plan update
            order_data = get_zpay_order_data(order_id)
            if order_data:
                order_data['plan_update_result'] = plan_data
                store_zpay_order_data(order_id, order_data)
            
            logger.info(f"ZPAY retry successful for order {order_id}")
            
        except Exception as e:
            # Update failed record and schedule next retry
            store_failed_zpay_update(
                order_id,
                failed_update,
                str(e),
                retry_count + 1
            )
            
            # If not reached max retries, schedule next retry
            if retry_count + 1 < PAYMENT_MAX_RETRY_ATTEMPTS:
                self.retry(countdown=PAYMENT_RETRY_DELAY_SECONDS)
                
    except Exception as e:
        logger.error(f"Error in ZPAY retry task: {str(e)}")
        if self.request.retries < PAYMENT_MAX_RETRY_ATTEMPTS:
            self.retry(countdown=PAYMENT_RETRY_DELAY_SECONDS)

@shared_task(name="sync_pending_zpay_orders")
def sync_pending_zpay_orders():
    """Sync pending ZPAY orders status"""
    try:
        # Find all pending orders
        pattern = f"{ZPAY_ORDER_PREFIX}*"
        pending_orders = []
        
        for key in redis_user_client.scan_iter(match=pattern):
            order_data = redis_user_client.hgetall(key)
            if order_data.get('status') == ORDER_STATUS_PENDING:
                order_id = order_data.get('order_id')
                if order_id:
                    pending_orders.append(order_id)
        
        logger.info(f"Found {len(pending_orders)} pending ZPAY orders to sync")
        
        # Query status for each pending order
        for order_id in pending_orders:
            try:
                zpay_status = query_zpay_order_status(order_id)
                if zpay_status and zpay_status.get('status') == 'paid':
                    # Process the payment
                    order_data = get_zpay_order_data(order_id)
                    if order_data:
                        # Simulate callback data for processing
                        callback_data = {
                            'trade_no': zpay_status.get('trade_no'),
                            'out_trade_no': order_id,
                            'money': zpay_status.get('money'),
                            'trade_status': 'TRADE_SUCCESS'
                        }
                        
                        result = process_zpay_payment_idempotent(order_id, zpay_status.get('trade_no'), callback_data)
                        if result['success']:
                            logger.info(f"Successfully synced payment for order {order_id}")
                        else:
                            logger.error(f"Failed to sync payment for order {order_id}: {result['error']}")
                            
            except Exception as e:
                logger.error(f"Error syncing order {order_id}: {str(e)}")
                continue
        
        return {"message": f"Synced {len(pending_orders)} pending orders"}
        
    except Exception as e:
        logger.error(f"Error syncing pending ZPAY orders: {str(e)}")
        return {"error": f"An error occurred while syncing orders: {str(e)}"} 