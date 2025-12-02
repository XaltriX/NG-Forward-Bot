import asyncio
import json
import os
import re
import hashlib
import sys
from datetime import datetime, timedelta
from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError, SessionPasswordNeededError, ChannelPrivateError, ChatAdminRequiredError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, Channel, Chat
from telethon.tl.functions.channels import GetFullChannelRequest
import logging
from motor.motor_asyncio import AsyncIOMotorClient

# ============= FIX WINDOWS UNICODE ERROR =============
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ============= CONFIGURATION =============
API_ID = 27352735
API_HASH = "8c4512c1052a60e05b05522a2ea12e5e"
BOT_TOKEN = "8088555712:AAFnrzKtWsleexdzsvc4LEoYIMHzlrCQBJo"
MONGO_URL = "mongodb+srv://tutybhai:786780@cluster0.iueiubc.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

AUTHORIZED_USER = "NeonGhost"
OWNER_ID = None
USER_SESSION = "user_account1"

# ============= LOGGING SETUP (FIXED FOR WINDOWS) =============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ============= MONGODB SETUP =============
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client.telegram_forwarder

users_col = db.users
sessions_col = db.sessions
stats_col = db.stats
channels_col = db.channels
forwarded_messages_col = db.forwarded_messages
auto_forward_col = db.auto_forward_rules

# ============= CLIENTS =============
user_client = None
bot_client = TelegramClient('bot', API_ID, API_HASH)

# ============= GLOBAL STORAGE =============
active_tasks = {}
user_states = {}
login_states = {}
auto_forward_listeners = {}
forwarding_queue = {}

# ============= FORWARD SESSION CLASS =============
class ForwardSession:
    def __init__(self, user_id):
        self.user_id = user_id
        self.source_channel = None
        self.dest_channel = None
        self.filters = []
        self.total = 0
        self.forwarded = 0
        self.failed = 0
        self.skipped = 0
        self.last_msg_id = 0
        self.is_running = False
        self.task = None
        self.start_time = None
        self.end_time = None
        self.temp_mode = None
        
        # Advanced features
        self.date_filter_start = None
        self.date_filter_end = None
        self.keyword_include = []
        self.keyword_exclude = []
        self.min_file_size = 0
        self.max_file_size = 0
        self.caption_mode = "original"
        self.custom_caption = ""
        self.add_watermark = False
        self.watermark_text = ""
        self.delay_between_msgs = 1.5
        self.reverse_order = False
        self.remove_urls = False
        self.replace_text = {}
        self.duplicate_check = True
        
    def to_dict(self):
        return {
            '_id': str(self.user_id),
            'user_id': self.user_id,
            'source_channel': self.source_channel,
            'dest_channel': self.dest_channel,
            'filters': self.filters,
            'total': self.total,
            'forwarded': self.forwarded,
            'failed': self.failed,
            'skipped': self.skipped,
            'last_msg_id': self.last_msg_id,
            'date_filter_start': self.date_filter_start,
            'date_filter_end': self.date_filter_end,
            'keyword_include': self.keyword_include,
            'keyword_exclude': self.keyword_exclude,
            'min_file_size': self.min_file_size,
            'max_file_size': self.max_file_size,
            'caption_mode': self.caption_mode,
            'custom_caption': self.custom_caption,
            'add_watermark': self.add_watermark,
            'watermark_text': self.watermark_text,
            'delay_between_msgs': self.delay_between_msgs,
            'reverse_order': self.reverse_order,
            'remove_urls': self.remove_urls,
            'replace_text': self.replace_text,
            'duplicate_check': self.duplicate_check,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'updated_at': datetime.now().isoformat()
        }
    
    @classmethod
    def from_dict(cls, data):
        session = cls(data['user_id'])
        for key, value in data.items():
            if hasattr(session, key) and key not in ['_id', 'updated_at']:
                setattr(session, key, value)
        return session

# ============= DATABASE FUNCTIONS =============
async def save_progress(session):
    try:
        await sessions_col.update_one(
            {'_id': str(session.user_id)},
            {'$set': session.to_dict()},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving progress: {e}")

async def load_progress(user_id):
    try:
        data = await sessions_col.find_one({'_id': str(user_id)})
        if data:
            return ForwardSession.from_dict(data)
    except Exception as e:
        logger.error(f"Error loading progress: {e}")
    return None

async def is_authorized(user_id):
    try:
        user = await users_col.find_one({'user_id': user_id})
        return user is not None and user.get('authorized', False)
    except:
        return False

async def save_authorized_user(user_id, username):
    try:
        await users_col.update_one(
            {'user_id': user_id},
            {'$set': {
                'user_id': user_id,
                'username': username,
                'authorized': True,
                'joined_at': datetime.now().isoformat()
            }},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving user: {e}")

async def save_forwarded_message(user_id, source_id, dest_id, msg_id, file_hash=None):
    try:
        await forwarded_messages_col.insert_one({
            'user_id': user_id,
            'source_channel': source_id,
            'dest_channel': dest_id,
            'message_id': msg_id,
            'file_hash': file_hash,
            'forwarded_at': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Error saving forwarded message: {e}")

async def is_duplicate(user_id, file_hash):
    if not file_hash:
        return False
    try:
        exists = await forwarded_messages_col.find_one({
            'user_id': user_id,
            'file_hash': file_hash
        })
        return exists is not None
    except:
        return False

async def update_stats(user_id, forwarded=0, failed=0, skipped=0):
    try:
        await stats_col.update_one(
            {'user_id': user_id},
            {
                '$inc': {
                    'total_forwarded': forwarded,
                    'total_failed': failed,
                    'total_skipped': skipped
                },
                '$set': {
                    'last_activity': datetime.now().isoformat()
                }
            },
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error updating stats: {e}")

async def save_auto_forward_rule(user_id, source_channel, dest_channels, filters, settings):
    try:
        rule_id = f"{user_id}_{source_channel}"
        await auto_forward_col.update_one(
            {'rule_id': rule_id},
            {'$set': {
                'rule_id': rule_id,
                'user_id': user_id,
                'source_channel': source_channel,
                'dest_channels': dest_channels,
                'filters': filters,
                'settings': settings,
                'enabled': True,
                'created_at': datetime.now().isoformat(),
                'total_forwarded': 0
            }},
            upsert=True
        )
        logger.info(f"Auto-forward rule saved: {rule_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving auto-forward rule: {e}")
        return False

async def get_auto_forward_rules(user_id=None):
    try:
        query = {'enabled': True}
        if user_id:
            query['user_id'] = user_id
        cursor = auto_forward_col.find(query)
        rules = await cursor.to_list(length=None)
        return rules
    except Exception as e:
        logger.error(f"Error getting auto-forward rules: {e}")
        return []

async def delete_auto_forward_rule(rule_id):
    try:
        await auto_forward_col.update_one(
            {'rule_id': rule_id},
            {'$set': {'enabled': False}}
        )
        return True
    except Exception as e:
        logger.error(f"Error deleting rule: {e}")
        return False

async def increment_auto_forward_count(rule_id):
    try:
        await auto_forward_col.update_one(
            {'rule_id': rule_id},
            {'$inc': {'total_forwarded': 1}}
        )
    except Exception as e:
        logger.error(f"Error incrementing count: {e}")

# ============= UTILITY FUNCTIONS =============
async def parse_channel_input(channel_input):
    channel_input = channel_input.strip()
    channel_input = re.sub(r'https?://', '', channel_input)
    if 't.me/' in channel_input:
        channel_input = channel_input.split('t.me/')[-1].split('/')[0]
    if channel_input.lstrip('-').isdigit():
        return int(channel_input)
    if channel_input.startswith('@'):
        return channel_input
    else:
        return f"@{channel_input}" if not channel_input.isdigit() else int(channel_input)

async def get_channel_info(client, channel_identifier):
    try:
        entity = await client.get_entity(channel_identifier)
        info = {
            'id': entity.id,
            'title': entity.title if hasattr(entity, 'title') else 'Unknown',
            'username': entity.username if hasattr(entity, 'username') else None,
            'members_count': 0
        }
        if isinstance(entity, Channel):
            try:
                full_channel = await client(GetFullChannelRequest(entity))
                info['members_count'] = full_channel.full_chat.participants_count
            except:
                pass
        return info
    except ChannelPrivateError:
        raise Exception("Channel is private or you're not a member!")
    except Exception as e:
        raise Exception(f"Error: {str(e)}")

def get_file_size_mb(message):
    if message.document:
        return message.document.size / (1024 * 1024)
    elif message.photo:
        return 0.5
    elif message.video:
        return message.video.size / (1024 * 1024) if message.video.size else 0
    return 0

def get_file_hash(message):
    try:
        if message.document:
            return hashlib.md5(f"{message.document.id}_{message.document.size}".encode()).hexdigest()
        elif message.photo:
            return hashlib.md5(f"{message.photo.id}".encode()).hexdigest()
        elif message.video:
            return hashlib.md5(f"{message.video.id}_{message.video.size}".encode()).hexdigest()
    except:
        pass
    return None

def process_caption(text, settings):
    if not text:
        text = ""
    caption_mode = settings.get('caption_mode', 'original')
    if caption_mode == "remove":
        text = ""
    elif caption_mode == "custom":
        text = settings.get('custom_caption', '')
    elif caption_mode == "append":
        custom = settings.get('custom_caption', '')
        if custom:
            text = f"{text}\n\n{custom}"
    if settings.get('remove_urls', False):
        text = re.sub(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', '', text)
    if settings.get('add_watermark', False):
        watermark = settings.get('watermark_text', '')
        if watermark:
            text = f"{text}\n\n{watermark}" if text else watermark
    return text.strip()

def check_keywords(text, include_keywords, exclude_keywords):
    if not text:
        text = ""
    text_lower = text.lower()
    if exclude_keywords:
        for keyword in exclude_keywords:
            if keyword.lower() in text_lower:
                return False, f"Excluded: {keyword}"
    if include_keywords:
        for keyword in include_keywords:
            if keyword.lower() in text_lower:
                return True, "Matched"
        return False, "No match"
    return True, "Passed"

async def should_forward_message(message, filters, settings):
    message_text = message.text or message.caption or ""
    include_kw = settings.get('keyword_include', [])
    exclude_kw = settings.get('keyword_exclude', [])
    passed, reason = check_keywords(message_text, include_kw, exclude_kw)
    if not passed:
        return False, reason
    if message.media:
        file_size = get_file_size_mb(message)
        min_size = settings.get('min_file_size', 0)
        max_size = settings.get('max_file_size', 0)
        if min_size > 0 and file_size < min_size:
            return False, f"Too small ({file_size:.1f}MB)"
        if max_size > 0 and file_size > max_size:
            return False, f"Too large ({file_size:.1f}MB)"
    if 'all' in filters:
        return True, "All media"
    if 'video' in filters and message.video:
        return True, "Video"
    if 'image' in filters and message.photo:
        return True, "Image"
    if 'document' in filters and message.document:
        return True, "Document"
    if 'audio' in filters and message.audio:
        return True, "Audio"
    if message.document and hasattr(message.document, 'attributes'):
        for attr in message.document.attributes:
            if hasattr(attr, 'file_name'):
                file_name = attr.file_name.lower()
                for f in filters:
                    if file_name.endswith(f):
                        return True, f"Extension: {f}"
    return False, "No filter match"

async def add_to_queue(user_id, session):
    if user_id not in forwarding_queue:
        forwarding_queue[user_id] = []
    forwarding_queue[user_id].append(session)
    return len(forwarding_queue[user_id])

async def process_queue(user_id):
    if user_id not in forwarding_queue or not forwarding_queue[user_id]:
        return
    while forwarding_queue[user_id]:
        session = forwarding_queue[user_id][0]
        try:
            await bot_client.send_message(
                user_id,
                f"[Queue] Processing\n\nSource: `{session.source_channel}`\nDestination: `{session.dest_channel}`\nQueue Position: 1/{len(forwarding_queue[user_id])}"
            )
        except:
            pass
        progress_msg = await bot_client.send_message(user_id, "Starting from queue...")
        await forward_messages(session, progress_msg)
        forwarding_queue[user_id].pop(0)
        await asyncio.sleep(2)
    try:
        await bot_client.send_message(user_id, "All Queue Items Processed!", buttons=get_main_menu())
    except:
        pass

def get_main_menu():
    return [
        [Button.inline("Start New Forward", b"new_forward")],
        [Button.inline("Resume Last Session", b"resume")],
        [Button.inline("Auto Forward Setup", b"auto_forward_menu")],
        [Button.inline("View Status", b"status"), Button.inline("Statistics", b"stats")],
        [Button.inline("Advanced Settings", b"advanced")],
        [Button.inline("Stop Forward", b"stop")]
    ]

def get_auto_forward_menu():
    return [
        [Button.inline("Add Auto Forward Rule", b"auto_add")],
        [Button.inline("View Active Rules", b"auto_list")],
        [Button.inline("Delete Rule", b"auto_delete")],
        [Button.inline("Restart Listeners", b"auto_restart")],
        [Button.inline("Back", b"back_main")]
    ]

def get_filter_menu():
    return [
        [Button.inline("Videos", b"filter_video"), Button.inline("Images", b"filter_image")],
        [Button.inline("Documents", b"filter_document"), Button.inline("Audio", b"filter_audio")],
        [Button.inline("All Media", b"filter_all")],
        [Button.inline("Back", b"back_main")]
    ]

def get_advanced_menu():
    return [
        [Button.inline("Keyword Filter", b"adv_keyword"), Button.inline("Size Filter", b"adv_size")],
        [Button.inline("Caption Settings", b"adv_caption"), Button.inline("Duplicate Check", b"adv_duplicate")],
        [Button.inline("Remove URLs", b"adv_urls"), Button.inline("Add Watermark", b"adv_watermark")],
        [Button.inline("Back", b"back_main")]
    ]

def get_caption_menu():
    return [
        [Button.inline("Keep Original", b"caption_original")],
        [Button.inline("Remove Caption", b"caption_remove")],
        [Button.inline("Custom Caption", b"caption_custom")],
        [Button.inline("Append to Original", b"caption_append")],
        [Button.inline("Back", b"advanced")]
    ]
# ============= AUTO FORWARD MESSAGE HANDLER =============
async def auto_forward_message(message, rule):
    try:
        user_id = rule['user_id']
        dest_channels = rule['dest_channels']
        filters = rule['filters']
        settings = rule.get('settings', {})
        
        should_forward_check, reason = await should_forward_message(message, filters, settings)
        if not should_forward_check:
            logger.info(f"Skipped auto-forward: {reason}")
            return
        
        if settings.get('duplicate_check', True):
            file_hash = get_file_hash(message)
            if file_hash and await is_duplicate(user_id, file_hash):
                logger.info(f"Skipped duplicate: {file_hash}")
                return
        
        caption = message.text or message.caption or ""
        processed_caption = process_caption(caption, settings)
        
        for dest_channel in dest_channels:
            try:
                dest_entity = await user_client.get_entity(dest_channel)
                
                if message.photo:
                    await user_client.send_file(dest_entity, message.photo, caption=processed_caption)
                elif message.video:
                    await user_client.send_file(dest_entity, message.video, caption=processed_caption)
                elif message.document:
                    await user_client.send_file(dest_entity, message.document, caption=processed_caption)
                elif message.audio:
                    await user_client.send_file(dest_entity, message.audio, caption=processed_caption)
                else:
                    await user_client.send_message(dest_entity, processed_caption or "Media")
                
                file_hash = get_file_hash(message)
                await save_forwarded_message(user_id, rule['source_channel'], dest_channel, message.id, file_hash)
                await increment_auto_forward_count(rule['rule_id'])
                await update_stats(user_id, forwarded=1)
                
                logger.info(f"Auto-forwarded msg {message.id} to {dest_channel}")
                await asyncio.sleep(0.5)
                
            except ChatAdminRequiredError:
                logger.error(f"Admin rights required for {dest_channel}")
                await update_stats(user_id, failed=1)
            except Exception as e:
                logger.error(f"Error forwarding to {dest_channel}: {e}")
                await update_stats(user_id, failed=1)
        
    except Exception as e:
        logger.error(f"Error in auto_forward_message: {e}")

# ============= AUTO FORWARD LISTENER SETUP =============
async def setup_auto_forward_listeners():
    global auto_forward_listeners
    
    if not user_client or not user_client.is_connected():
        logger.warning("User client not connected")
        return
    
    auto_forward_listeners = {}
    rules = await get_auto_forward_rules()
    
    logger.info(f"Setting up {len(rules)} auto-forward rules")
    
    for rule in rules:
        try:
            source_channel = rule['source_channel']
            source_entity = await user_client.get_entity(source_channel)
            
            async def create_handler(rule_data):
                @user_client.on(events.NewMessage(chats=source_entity))
                async def handler(event):
                    logger.info(f"New message in {rule_data['source_channel']}")
                    await auto_forward_message(event.message, rule_data)
                return handler
            
            handler = await create_handler(rule)
            auto_forward_listeners[rule['rule_id']] = handler
            logger.info(f"Listener added for {source_channel}")
            
        except Exception as e:
            logger.error(f"Error setting up listener: {e}")
    
    logger.info(f"Auto-forward setup complete! Monitoring {len(auto_forward_listeners)} channels")

# ============= MANUAL FORWARDING FUNCTION =============
async def forward_messages(session, progress_msg):
    try:
        if not user_client or not user_client.is_connected():
            await bot_client.edit_message(
                progress_msg.chat_id, 
                progress_msg.id,
                "Error: User client not connected!\n\nPlease login first using /login",
                buttons=get_main_menu()
            )
            return
            
        session.is_running = True
        session.start_time = datetime.now()
        source = await user_client.get_entity(session.source_channel)
        dest = await user_client.get_entity(session.dest_channel)
        
        if session.total == 0:
            async for _ in user_client.iter_messages(source, limit=None):
                session.total += 1
        
        logger.info(f"Starting forward from {source.title} to {dest.title}")
        
        offset_id = session.last_msg_id if session.last_msg_id > 0 else 0
        
        async for message in user_client.iter_messages(source, offset_id=offset_id, reverse=session.reverse_order):
            if not session.is_running:
                await bot_client.edit_message(
                    progress_msg.chat_id, 
                    progress_msg.id,
                    "Forwarding Stopped\n\nYou can resume anytime!",
                    buttons=get_main_menu()
                )
                break
            
            try:
                should_forward_check, reason = await should_forward_message(
                    message, 
                    session.filters,
                    {
                        'keyword_include': session.keyword_include,
                        'keyword_exclude': session.keyword_exclude,
                        'min_file_size': session.min_file_size,
                        'max_file_size': session.max_file_size,
                        'caption_mode': session.caption_mode,
                        'custom_caption': session.custom_caption,
                        'add_watermark': session.add_watermark,
                        'watermark_text': session.watermark_text,
                        'remove_urls': session.remove_urls
                    }
                )
                
                if not should_forward_check:
                    session.skipped += 1
                    continue
                
                if session.duplicate_check:
                    file_hash = get_file_hash(message)
                    if file_hash and await is_duplicate(session.user_id, file_hash):
                        session.skipped += 1
                        continue
                
                caption = message.text or message.caption or ""
                processed_caption = process_caption(caption, {
                    'caption_mode': session.caption_mode,
                    'custom_caption': session.custom_caption,
                    'add_watermark': session.add_watermark,
                    'watermark_text': session.watermark_text,
                    'remove_urls': session.remove_urls
                })
                
                retry_count = 0
                max_retries = 3
                
                while retry_count < max_retries:
                    try:
                        if message.photo:
                            await user_client.send_file(dest, message.photo, caption=processed_caption)
                        elif message.video:
                            await user_client.send_file(dest, message.video, caption=processed_caption)
                        elif message.document:
                            await user_client.send_file(dest, message.document, caption=processed_caption)
                        elif message.audio:
                            await user_client.send_file(dest, message.audio, caption=processed_caption)
                        else:
                            await user_client.send_message(dest, processed_caption or "Media")
                        
                        session.forwarded += 1
                        session.last_msg_id = message.id
                        
                        file_hash = get_file_hash(message)
                        await save_forwarded_message(session.user_id, session.source_channel, session.dest_channel, message.id, file_hash)
                        await update_stats(session.user_id, forwarded=1)
                        
                        break
                        
                    except ChatAdminRequiredError:
                        logger.error(f"Admin rights required for destination channel")
                        session.failed += 1
                        await update_stats(session.user_id, failed=1)
                        break
                        
                    except FloodWaitError as e:
                        wait_time = e.seconds
                        logger.warning(f"FloodWait: {wait_time}s")
                        
                        await bot_client.edit_message(
                            progress_msg.chat_id,
                            progress_msg.id,
                            f"Rate Limited!\n\nWaiting {wait_time} seconds...\nProgress: {session.forwarded}/{session.total}\nFailed: {session.failed}"
                        )
                        
                        await asyncio.sleep(wait_time)
                        retry_count += 1
                        
                    except Exception as e:
                        logger.error(f"Error forwarding message {message.id}: {e}")
                        session.failed += 1
                        await update_stats(session.user_id, failed=1)
                        retry_count += 1
                        await asyncio.sleep(2)
                
                if session.forwarded % 5 == 0:
                    progress_percent = (session.forwarded / session.total * 100) if session.total > 0 else 0
                    progress_bar = "=" * int(progress_percent / 10) + "-" * (10 - int(progress_percent / 10))
                    elapsed = datetime.now() - session.start_time
                    elapsed_str = f"{elapsed.seconds // 60}m {elapsed.seconds % 60}s"
                    
                    await bot_client.edit_message(
                        progress_msg.chat_id,
                        progress_msg.id,
                        f"Forwarding in Progress\n\n"
                        f"Progress: [{progress_bar}] {progress_percent:.1f}%\n\n"
                        f"Forwarded: {session.forwarded}\n"
                        f"Skipped: {session.skipped}\n"
                        f"Failed: {session.failed}\n"
                        f"Total: {session.total}\n\n"
                        f"Time: {elapsed_str}",
                        buttons=[[Button.inline("Stop", b"stop")]]
                    )
                    
                    await save_progress(session)
                
                await asyncio.sleep(session.delay_between_msgs)
                
            except Exception as e:
                logger.error(f"Error processing message {message.id}: {e}")
                session.failed += 1
        
        if session.is_running:
            session.end_time = datetime.now()
            elapsed = session.end_time - session.start_time
            elapsed_str = f"{elapsed.seconds // 60}m {elapsed.seconds % 60}s"
            
            await bot_client.edit_message(
                progress_msg.chat_id,
                progress_msg.id,
                f"Forwarding Complete!\n\n"
                f"Forwarded: {session.forwarded}\n"
                f"Skipped: {session.skipped}\n"
                f"Failed: {session.failed}\n"
                f"Total: {session.total}\n"
                f"Time: {elapsed_str}\n\n"
                f"All done!",
                buttons=get_main_menu()
            )
            session.is_running = False
            await save_progress(session)
        
    except Exception as e:
        logger.error(f"Fatal error in forward_messages: {e}")
        await bot_client.send_message(
            progress_msg.chat_id,
            f"Error occurred:\n`{str(e)}`\n\nProgress saved!",
            buttons=get_main_menu()
        )
        session.is_running = False
        await save_progress(session)

# ============= BOT COMMAND HANDLERS =============
@bot_client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    username = sender.username if sender.username else ""
    
    if username.lower() != AUTHORIZED_USER.lower() and not await is_authorized(user_id):
        await event.respond(
            "Access Denied!\n\n"
            "This is a personal bot made with love by @NeonGhost.\n\n"
            "Contact @NeonGhost for your own bot!"
        )
        logger.warning(f"Unauthorized access from {user_id} (@{username})")
        return
    
    await save_authorized_user(user_id, username)
    global OWNER_ID
    OWNER_ID = user_id
    
    if not user_client or not user_client.is_connected():
        await event.respond(
            "Welcome to Advanced Channel Forwarder Bot!\n\n"
            "You need to login first!\n\n"
            "Use /login to connect your Telegram account.",
            buttons=[[Button.inline("Login Now", b"start_login")]]
        )
        return
    
    queue_text = ""
    if user_id in forwarding_queue and forwarding_queue[user_id]:
        queue_text = f"\n\nQueue: {len(forwarding_queue[user_id])} tasks waiting"
    
    await event.respond(
        f"Advanced Telegram Forwarder Bot\n\n"
        f"Features:\n"
        f"• Auto-forwarding from multiple channels\n"
        f"• Smart filtering (media, keywords, size)\n"
        f"• Duplicate detection\n"
        f"• Caption customization\n"
        f"• Live progress tracking\n"
        f"• Resume support{queue_text}\n\n"
        f"Choose an option below:",
        buttons=get_main_menu()
    )

@bot_client.on(events.NewMessage(pattern='/login'))
async def login_handler(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    username = sender.username if sender.username else ""
    
    if username.lower() != AUTHORIZED_USER.lower() and not await is_authorized(user_id):
        await event.respond("Access Denied!")
        return
    
    if user_client and user_client.is_connected():
        await event.respond(
            "You're already logged in!\n\nUse /start to begin.",
            buttons=get_main_menu()
        )
        return
    
    login_states[user_id] = {'step': 'phone'}
    await event.respond(
        "Login to Telegram\n\n"
        "Send your phone number with country code.\n\n"
        "Example: +919876543210",
        buttons=[[Button.inline("Cancel", b"cancel_login")]]
    )
# ============= CALLBACK QUERY HANDLER =============
@bot_client.on(events.CallbackQuery)
async def callback_handler(event):
    user_id = event.sender_id
    data = event.data.decode('utf-8')
    
    sender = await event.get_sender()
    username = sender.username if sender.username else ""
    
    if username.lower() != AUTHORIZED_USER.lower() and not await is_authorized(user_id):
        await event.answer("Access Denied!", alert=True)
        return
    
    # ============= AUTO FORWARD MENU =============
    if data == "auto_forward_menu":
        rules = await get_auto_forward_rules(user_id)
        rules_text = f"Active Rules: {len(rules)}\n\n"
        for rule in rules[:5]:
            rules_text += f"• {rule['source_channel']} -> {len(rule['dest_channels'])} channels\n"
        
        await event.edit(
            f"Auto Forward Manager\n\n{rules_text}\nChoose an option:",
            buttons=get_auto_forward_menu()
        )
    
    elif data == "auto_add":
        user_states[user_id] = {
            'mode': 'auto_forward',
            'step': 'source',
            'source': None,
            'destinations': [],
            'filters': [],
            'settings': {}
        }
        await event.edit(
            "Setup Auto Forward\n\n"
            "Step 1: Send source channel\n"
            "(Username, ID, or link)\n\n"
            "Example: @channel or -100123456789",
            buttons=[[Button.inline("Cancel", b"back_main")]]
        )
    
    elif data == "auto_list":
        rules = await get_auto_forward_rules(user_id)
        if not rules:
            await event.answer("No active rules found!", alert=True)
            return
        
        text = "Your Auto Forward Rules:\n\n"
        for idx, rule in enumerate(rules, 1):
            text += f"Rule {idx}: `{rule['rule_id']}`\n"
            text += f"   Source: `{rule['source_channel']}`\n"
            text += f"   Destinations: {len(rule['dest_channels'])}\n"
            text += f"   Forwarded: {rule.get('total_forwarded', 0)}\n\n"
        
        await event.edit(text, buttons=get_auto_forward_menu())
    
    elif data == "auto_delete":
        rules = await get_auto_forward_rules(user_id)
        if not rules:
            await event.answer("No rules to delete!", alert=True)
            return
        
        user_states[user_id] = {'mode': 'delete_auto_rule'}
        
        text = "Delete Auto Forward Rule\n\nSend rule ID to delete:\n\n"
        for rule in rules:
            text += f"`{rule['rule_id']}`\n"
        
        await event.edit(text, buttons=[[Button.inline("Back", b"auto_forward_menu")]])
    
    elif data == "auto_restart":
        await event.answer("Restarting listeners...", alert=False)
        await setup_auto_forward_listeners()
        await event.answer("Listeners restarted!", alert=True)
    
    # ============= LOGIN HANDLERS =============
    elif data == "start_login":
        login_states[user_id] = {'step': 'phone'}
        await event.edit(
            "Login to Telegram\n\nSend your phone number with country code.\n\nExample: +919876543210",
            buttons=[[Button.inline("Cancel", b"cancel_login")]]
        )
    
    elif data == "cancel_login":
        if user_id in login_states:
            del login_states[user_id]
        await event.edit("Login cancelled.", buttons=[[Button.inline("Back", b"back_start")]])
    
    elif data == "back_start":
        await event.edit("Main Menu", buttons=[[Button.inline("Login", b"start_login")]])
    
    # ============= MANUAL FORWARD HANDLERS =============
    elif data == "new_forward":
        if not user_client or not user_client.is_connected():
            await event.answer("Please login first using /login", alert=True)
            return
        
        user_states[user_id] = ForwardSession(user_id)
        await event.edit(
            "Step 1: Source Channel\n\n"
            "Send the source channel:\n"
            "• Username (@channel)\n"
            "• Channel ID (-100xxxxxx)\n"
            "• Link (t.me/channel)\n\n"
            "You must be a member!",
            buttons=[[Button.inline("Cancel", b"back_main")]]
        )
        user_states[user_id].source_channel = "waiting"
    
    elif data == "resume":
        session = await load_progress(user_id)
        if session:
            user_states[user_id] = session
            await event.edit(
                f"Resume Session\n\n"
                f"Source: `{session.source_channel}`\n"
                f"Destination: `{session.dest_channel}`\n"
                f"Progress: {session.forwarded}/{session.total}\n\n"
                f"Continue?",
                buttons=[
                    [Button.inline("Resume", b"confirm_resume")],
                    [Button.inline("Back", b"back_main")]
                ]
            )
        else:
            await event.answer("No saved session!", alert=True)
    
    elif data == "confirm_resume":
        session = user_states.get(user_id)
        if session:
            if user_id in active_tasks and active_tasks[user_id].is_running:
                queue_pos = await add_to_queue(user_id, session)
                await event.edit(
                    f"Added to Queue!\n\n"
                    f"Position: {queue_pos}\n"
                    f"Currently running task will finish first.",
                    buttons=get_main_menu()
                )
            else:
                progress_msg = await event.edit("Resuming...", buttons=None)
                session.task = asyncio.create_task(forward_messages(session, progress_msg))
                active_tasks[user_id] = session
    
    elif data == "status":
        session = active_tasks.get(user_id) or user_states.get(user_id)
        queue_info = ""
        if user_id in forwarding_queue and forwarding_queue[user_id]:
            queue_info = f"\nQueue: {len(forwarding_queue[user_id])} waiting"
        
        if session and isinstance(session, ForwardSession):
            status = "Running" if session.is_running else "Stopped"
            await event.edit(
                f"Current Status\n\n"
                f"Status: {status}\n"
                f"Source: `{session.source_channel or 'Not set'}`\n"
                f"Destination: `{session.dest_channel or 'Not set'}`\n"
                f"Forwarded: {session.forwarded}\n"
                f"Skipped: {session.skipped}\n"
                f"Failed: {session.failed}\n"
                f"Total: {session.total}{queue_info}",
                buttons=get_main_menu()
            )
        else:
            await event.answer("No active session!", alert=True)
    
    elif data == "stats":
        stats = await stats_col.find_one({'user_id': user_id})
        auto_rules = await get_auto_forward_rules(user_id)
        
        if stats or auto_rules:
            text = f"Your Statistics\n\n"
            if stats:
                text += f"Total Forwarded: {stats.get('total_forwarded', 0)}\n"
                text += f"Total Skipped: {stats.get('total_skipped', 0)}\n"
                text += f"Total Failed: {stats.get('total_failed', 0)}\n"
                text += f"Last Activity: {stats.get('last_activity', 'N/A')[:10]}\n\n"
            text += f"Auto-Forward Rules: {len(auto_rules)}"
            await event.edit(text, buttons=get_main_menu())
        else:
            await event.answer("No statistics yet!", alert=True)
    
    elif data == "stop":
        session = active_tasks.get(user_id)
        if session:
            session.is_running = False
            await save_progress(session)
            await event.answer("Stopped!", alert=True)
        else:
            await event.answer("No active forwarding!", alert=True)
    
    # ============= ADVANCED SETTINGS HANDLERS =============
    elif data == "advanced":
        session = user_states.get(user_id)
        if not session or not isinstance(session, ForwardSession):
            await event.answer("Please setup forwarding first!", alert=True)
            return
        
        current_settings = f"Current Settings:\n\n"
        current_settings += f"Keywords: {len(session.keyword_include)} include, {len(session.keyword_exclude)} exclude\n"
        current_settings += f"Size: {session.min_file_size}MB - {session.max_file_size if session.max_file_size > 0 else 'unlimited'}MB\n"
        current_settings += f"Caption: {session.caption_mode}\n"
        current_settings += f"Duplicate Check: {'ON' if session.duplicate_check else 'OFF'}\n"
        current_settings += f"Remove URLs: {'ON' if session.remove_urls else 'OFF'}\n"
        current_settings += f"Watermark: {'ON' if session.add_watermark else 'OFF'}"
        
        await event.edit(
            f"Advanced Settings\n\n{current_settings}\n\nConfigure:",
            buttons=get_advanced_menu()
        )
    
    elif data == "adv_keyword":
        session = user_states.get(user_id)
        if session and isinstance(session, ForwardSession):
            session.temp_mode = 'keyword_include'
            await event.edit(
                "Keyword Filter\n\n"
                "Send keywords to INCLUDE (comma-separated)\n"
                "Only messages with these keywords will be forwarded.\n\n"
                "Example: video, tutorial, course\n\n"
                "Send 'skip' to skip this step.",
                buttons=[[Button.inline("Back", b"advanced")]]
            )
    
    elif data == "adv_size":
        session = user_states.get(user_id)
        if session and isinstance(session, ForwardSession):
            session.temp_mode = 'size_min'
            await event.edit(
                "File Size Filter\n\n"
                "Send minimum file size in MB\n\n"
                "Example: 5 (for 5MB minimum)\n"
                "Send 0 for no minimum.",
                buttons=[[Button.inline("Back", b"advanced")]]
            )
    
    elif data == "adv_caption":
        session = user_states.get(user_id)
        if session and isinstance(session, ForwardSession):
            await event.edit(
                "Caption Settings\n\nChoose caption mode:",
                buttons=get_caption_menu()
            )
    
    elif data == "adv_duplicate":
        session = user_states.get(user_id)
        if session and isinstance(session, ForwardSession):
            session.duplicate_check = not session.duplicate_check
            status = "ON" if session.duplicate_check else "OFF"
            await event.answer(f"Duplicate Check: {status}", alert=True)
            await event.edit(f"Duplicate Check: {status}", buttons=[[Button.inline("Back", b"advanced")]])
    
    elif data == "adv_urls":
        session = user_states.get(user_id)
        if session and isinstance(session, ForwardSession):
            session.remove_urls = not session.remove_urls
            status = "ON" if session.remove_urls else "OFF"
            await event.answer(f"Remove URLs: {status}", alert=True)
            await event.edit(f"Remove URLs: {status}", buttons=[[Button.inline("Back", b"advanced")]])
    
    elif data == "adv_watermark":
        session = user_states.get(user_id)
        if session and isinstance(session, ForwardSession):
            session.temp_mode = 'watermark'
            await event.edit(
                "Add Watermark\n\n"
                "Send your watermark text\n\n"
                "Example: @YourChannel\n\n"
                "Send 'skip' to disable watermark.",
                buttons=[[Button.inline("Back", b"advanced")]]
            )
    
    # ============= CAPTION MODE HANDLERS =============
    elif data.startswith("caption_"):
        session = user_states.get(user_id)
        if session and isinstance(session, ForwardSession):
            mode = data.replace("caption_", "")
            session.caption_mode = mode
            
            if mode == "custom" or mode == "append":
                session.temp_mode = 'custom_caption'
                await event.edit(
                    f"Caption Mode: {mode}\n\n"
                    f"Send your custom caption text:",
                    buttons=[[Button.inline("Back", b"adv_caption")]]
                )
            else:
                await event.edit(
                    f"Caption Mode: {mode}",
                    buttons=[[Button.inline("Back", b"advanced")]]
                )
    
    # ============= FILTER SELECTION =============
    elif data.startswith("filter_"):
        filter_type = data.replace("filter_", "")
        
        # Check if auto-forward setup
        if user_id in user_states and isinstance(user_states[user_id], dict):
            state = user_states[user_id]
            
            if state.get('mode') == 'auto_forward' and state.get('step') == 'filters':
                if filter_type == "all":
                    state['filters'] = ['all']
                elif filter_type == "video":
                    state['filters'] = ['.mp4', '.mkv', '.avi', 'video']
                elif filter_type == "image":
                    state['filters'] = ['.jpg', '.png', '.gif', 'image']
                elif filter_type == "document":
                    state['filters'] = ['.pdf', '.zip', '.rar', 'document']
                elif filter_type == "audio":
                    state['filters'] = ['.mp3', '.m4a', '.flac', 'audio']
                
                # Ask if user wants to add to auto-forward
                await event.edit(
                    f"Filter Set: {filter_type.upper()}\n\n"
                    f"Source: `{state['source']}`\n"
                    f"Destinations: {len(state['destinations'])}\n\n"
                    f"Save as auto-forward rule?",
                    buttons=[
                        [Button.inline("Yes, Auto-Forward", b"save_auto_rule")],
                        [Button.inline("No, Manual Only", b"manual_only")],
                        [Button.inline("Cancel", b"back_main")]
                    ]
                )
                return
        
        # Manual forward filter
        session = user_states.get(user_id)
        if session and isinstance(session, ForwardSession):
            if filter_type == "all":
                session.filters = ['all']
            elif filter_type == "video":
                session.filters = ['.mp4', '.mkv', '.avi', '.mov', 'video']
            elif filter_type == "image":
                session.filters = ['.jpg', '.jpeg', '.png', '.gif', 'image']
            elif filter_type == "document":
                session.filters = ['.pdf', '.zip', '.rar', '.doc', 'document']
            elif filter_type == "audio":
                session.filters = ['.mp3', '.m4a', '.flac', '.wav', 'audio']
            
            await event.edit(
                f"Filter: {filter_type.upper()}\n\n"
                f"Source: `{session.source_channel}`\n"
                f"Destination: `{session.dest_channel}`\n\n"
                f"Ready to start?",
                buttons=[
                    [Button.inline("Start", b"start_forward")],
                    [Button.inline("Advanced", b"advanced")],
                    [Button.inline("Back", b"back_main")]
                ]
            )
    
    elif data == "save_auto_rule":
        if user_id in user_states and isinstance(user_states[user_id], dict):
            state = user_states[user_id]
            
            if state.get('mode') == 'auto_forward':
                rule_saved = await save_auto_forward_rule(
                    user_id,
                    state['source'],
                    state['destinations'],
                    state['filters'],
                    state['settings']
                )
                
                if rule_saved:
                    await event.edit(
                        f"Auto Forward Rule Created!\n\n"
                        f"Source: `{state['source']}`\n"
                        f"Destinations: {len(state['destinations'])}\n"
                        f"Filter: {state['filters']}\n\n"
                        f"Bot will now auto-forward new posts!\n\n"
                        f"Activating listener...",
                        buttons=get_main_menu()
                    )
                    await setup_auto_forward_listeners()
                    del user_states[user_id]
                else:
                    await event.edit("Failed to save rule!", buttons=get_main_menu())
    
    elif data == "manual_only":
        await event.edit("Manual forward mode selected.", buttons=get_main_menu())
        if user_id in user_states:
            del user_states[user_id]
    
    elif data == "start_forward":
        session = user_states.get(user_id)
        if session and isinstance(session, ForwardSession) and session.source_channel and session.dest_channel:
            if user_id in active_tasks and active_tasks[user_id].is_running:
                queue_pos = await add_to_queue(user_id, session)
                await event.edit(
                    f"Added to Queue!\n\nPosition: {queue_pos}",
                    buttons=get_main_menu()
                )
            else:
                progress_msg = await event.edit("Starting...", buttons=None)
                session.task = asyncio.create_task(forward_messages(session, progress_msg))
                active_tasks[user_id] = session
    
    elif data == "back_main":
        await event.edit("Main Menu", buttons=get_main_menu())

# ============= MESSAGE HANDLER =============
@bot_client.on(events.NewMessage)
async def message_handler(event):
    if event.text.startswith('/'):
        return
    
    user_id = event.sender_id
    sender = await event.get_sender()
    username = sender.username if sender.username else ""
    
    if username.lower() != AUTHORIZED_USER.lower() and not await is_authorized(user_id):
        return
    
    # ============= LOGIN FLOW =============
    if user_id in login_states:
        login_state = login_states[user_id]
        text = event.text.strip()
        
        if login_state['step'] == 'phone':
            try:
                global user_client
                user_client = TelegramClient(USER_SESSION, API_ID, API_HASH)
                await user_client.connect()
                
                result = await user_client.send_code_request(text)
                login_state['phone'] = text
                login_state['phone_code_hash'] = result.phone_code_hash
                login_state['step'] = 'code'
                
                await event.respond(
                    "Code Sent!\n\nSend the verification code:",
                    buttons=[[Button.inline("Cancel", b"cancel_login")]]
                )
            except Exception as e:
                await event.respond(f"Error: {e}\n\nTry /login again")
                if user_id in login_states:
                    del login_states[user_id]
        
        elif login_state['step'] == 'code':
            try:
                code = text.strip()
                phone = login_state['phone']
                phone_code_hash = login_state['phone_code_hash']
                
                try:
                    await user_client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                    await event.respond(
                        "Login Successful!\n\nSession saved!\n\nUse /start",
                        buttons=get_main_menu()
                    )
                    del login_states[user_id]
                    await setup_auto_forward_listeners()
                    
                except SessionPasswordNeededError:
                    login_state['step'] = '2fa'
                    await event.respond(
                        "2FA Required\n\nSend your password:",
                        buttons=[[Button.inline("Cancel", b"cancel_login")]]
                    )
            except Exception as e:
                await event.respond(f"Invalid code: {e}\n\nTry /login again")
                if user_id in login_states:
                    del login_states[user_id]
        
        elif login_state['step'] == '2fa':
            try:
                await user_client.sign_in(password=text.strip())
                await event.respond(
                    "Login Successful!\n\nSession saved!\n\nUse /start",
                    buttons=get_main_menu()
                )
                del login_states[user_id]
                await setup_auto_forward_listeners()
            except Exception as e:
                await event.respond(f"Invalid password: {e}\n\nTry /login again")
                if user_id in login_states:
                    del login_states[user_id]
        
        return
    
    # ============= AUTO FORWARD SETUP FLOW =============
    if user_id in user_states and isinstance(user_states[user_id], dict):
        state = user_states[user_id]
        
        if state.get('mode') == 'auto_forward':
            text = event.text.strip()
            
            if state['step'] == 'source':
                try:
                    parsed = await parse_channel_input(text)
                    info = await get_channel_info(user_client, parsed)
                    state['source'] = parsed
                    state['step'] = 'destinations'
                    
                    await event.respond(
                        f"Source: `{info['title']}`\n\n"
                        f"Step 2: Send destination channels\n"
                        "(One per line or comma-separated)\n\n"
                        "Example:\n@dest1\n@dest2\n-100123456",
                        buttons=[[Button.inline("Cancel", b"back_main")]]
                    )
                except Exception as e:
                    await event.respond(f"Error: {e}")
            
            elif state['step'] == 'destinations':
                try:
                    lines = text.replace(',', '\n').split('\n')
                    destinations = []
                    
                    for line in lines:
                        line = line.strip()
                        if line:
                            parsed = await parse_channel_input(line)
                            info = await get_channel_info(user_client, parsed)
                            destinations.append(parsed)
                    
                    state['destinations'] = destinations
                    state['step'] = 'filters'
                    
                    await event.respond(
                        f"Added {len(destinations)} destinations!\n\n"
                        f"Step 3: Choose filters:",
                        buttons=get_filter_menu()
                    )
                except Exception as e:
                    await event.respond(f"Error: {e}")
            
            return
        
        elif state.get('mode') == 'delete_auto_rule':
            rule_id = event.text.strip()
            if await delete_auto_forward_rule(rule_id):
                await event.respond(
                    f"Rule `{rule_id}` deleted!\n\nRestarting listeners...",
                    buttons=get_auto_forward_menu()
                )
                await setup_auto_forward_listeners()
            else:
                await event.respond("Failed to delete rule!")
            
            del user_states[user_id]
            return
    
    # ============= MANUAL FORWARD FLOW =============
    session = user_states.get(user_id)
    if not session or not isinstance(session, ForwardSession):
        return
    
    text = event.text.strip()
    
    # Handle advanced settings input
    if hasattr(session, 'temp_mode') and session.temp_mode:
        if session.temp_mode == 'keyword_include':
            if text.lower() != 'skip':
                session.keyword_include = [k.strip() for k in text.split(',')]
            session.temp_mode = 'keyword_exclude'
            await event.respond(
                "Send keywords to EXCLUDE (comma-separated)\n"
                "Messages with these keywords will be skipped.\n\n"
                "Send 'skip' to skip this step.",
                buttons=[[Button.inline("Back", b"advanced")]]
            )
            return
        
        elif session.temp_mode == 'keyword_exclude':
            if text.lower() != 'skip':
                session.keyword_exclude = [k.strip() for k in text.split(',')]
            session.temp_mode = None
            await event.respond(
                "Keyword filters set!",
                buttons=[[Button.inline("Back", b"advanced")]]
            )
            return
        
        elif session.temp_mode == 'size_min':
            try:
                session.min_file_size = float(text)
                session.temp_mode = 'size_max'
                await event.respond(
                    "Send maximum file size in MB\n\n"
                    "Example: 100 (for 100MB maximum)\n"
                    "Send 0 for no maximum.",
                    buttons=[[Button.inline("Back", b"advanced")]]
                )
            except:
                await event.respond("Invalid number! Try again.")
            return
        
        elif session.temp_mode == 'size_max':
            try:
                session.max_file_size = float(text)
                session.temp_mode = None
                await event.respond(
                    f"Size filter set!\n"
                    f"Min: {session.min_file_size}MB\n"
                    f"Max: {session.max_file_size}MB",
                    buttons=[[Button.inline("Back", b"advanced")]]
                )
            except:
                await event.respond("Invalid number! Try again.")
            return
        
        elif session.temp_mode == 'custom_caption':
            session.custom_caption = text
            session.temp_mode = None
            await event.respond(
                "Custom caption set!",
                buttons=[[Button.inline("Back", b"advanced")]]
            )
            return
        
        elif session.temp_mode == 'watermark':
            if text.lower() != 'skip':
                session.add_watermark = True
                session.watermark_text = text
            else:
                session.add_watermark = False
            session.temp_mode = None
            await event.respond(
                f"Watermark: {'ON' if session.add_watermark else 'OFF'}",
                buttons=[[Button.inline("Back", b"advanced")]]
            )
            return
    
    # Handle channel input
    if session.source_channel == "waiting":
        try:
            parsed = await parse_channel_input(text)
            info = await get_channel_info(user_client, parsed)
            session.source_channel = parsed
            
            await event.respond(
                f"Source: `{info['title']}`\n\n"
                f"Step 2: Destination\n\nSend destination channel:",
                buttons=[[Button.inline("Cancel", b"back_main")]]
            )
            session.dest_channel = "waiting"
        except Exception as e:
            await event.respond(f"{e}")
    
    elif session.dest_channel == "waiting":
        try:
            parsed = await parse_channel_input(text)
            info = await get_channel_info(user_client, parsed)
            session.dest_channel = parsed
            
            await event.respond(
                f"Destination: `{info['title']}`\n\n"
                f"Step 3: Filters\n\nChoose media type:",
                buttons=get_filter_menu()
            )
        except Exception as e:
            await event.respond(f"{e}")

# ============= MAIN FUNCTION =============
async def main():
    logger.info("Starting bot...")
    
    global user_client
    if os.path.exists(f"{USER_SESSION}.session"):
        user_client = TelegramClient(USER_SESSION, API_ID, API_HASH)
        await user_client.connect()
        if await user_client.is_user_authorized():
            logger.info("User client loaded from session")
            await setup_auto_forward_listeners()
        else:
            logger.info("Session expired, need login")
            user_client = None
    
    await bot_client.start(bot_token=BOT_TOKEN)
    me = await bot_client.get_me()
    
    print("\n" + "="*60)
    print("BOT IS RUNNING!")
    print(f"Bot: @{me.username}")
    print(f"Owner: @{AUTHORIZED_USER}")
    print(f"MongoDB: Connected")
    print(f"Auto-Forward: {'Active' if user_client else 'Login Required'}")
    print("="*60 + "\n")
    
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
