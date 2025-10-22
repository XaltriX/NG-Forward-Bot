import asyncio
import json
import os
from datetime import datetime
from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
import logging

# Configuration
API_ID = 27352735
API_HASH = "8c4512c1052a60e05b05522a2ea12e5e"
BOT_TOKEN = "tWsleexdzsvc4LEoYIMHzlrCQBJo"

# Authorized user
AUTHORIZED_USER = "NeonGhost"  # Username without @
OWNER_ID = None  # Will be set during runtime

# Session files
USER_SESSION = "user_account"
PROGRESS_FILE = "forward_progress.json"
AUTH_FILE = "authorized_users.json"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize clients
user_client = None
bot_client = TelegramClient('bot', API_ID, API_HASH)

# Store active forwarding tasks
active_tasks = {}
user_states = {}
login_states = {}

class ForwardSession:
    def __init__(self, user_id):
        self.user_id = user_id
        self.source_channel = None
        self.dest_channel = None
        self.filters = []
        self.total = 0
        self.forwarded = 0
        self.failed = 0
        self.last_msg_id = 0
        self.is_running = False
        self.task = None
        
    def to_dict(self):
        return {
            'user_id': self.user_id,
            'source_channel': self.source_channel,
            'dest_channel': self.dest_channel,
            'filters': self.filters,
            'total': self.total,
            'forwarded': self.forwarded,
            'failed': self.failed,
            'last_msg_id': self.last_msg_id
        }
    
    @classmethod
    def from_dict(cls, data):
        session = cls(data['user_id'])
        session.source_channel = data['source_channel']
        session.dest_channel = data['dest_channel']
        session.filters = data['filters']
        session.total = data['total']
        session.forwarded = data['forwarded']
        session.failed = data['failed']
        session.last_msg_id = data['last_msg_id']
        return session

def load_authorized_users():
    """Load authorized users from file"""
    try:
        if os.path.exists(AUTH_FILE):
            with open(AUTH_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading authorized users: {e}")
    return []

def save_authorized_user(user_id):
    """Save authorized user to file"""
    try:
        users = load_authorized_users()
        if user_id not in users:
            users.append(user_id)
            with open(AUTH_FILE, 'w') as f:
                json.dump(users, f)
    except Exception as e:
        logger.error(f"Error saving authorized user: {e}")

def is_authorized(user_id):
    """Check if user is authorized"""
    return user_id in load_authorized_users()

def save_progress(session):
    """Save progress to file"""
    try:
        data = {}
        if os.path.exists(PROGRESS_FILE):
            with open(PROGRESS_FILE, 'r') as f:
                data = json.load(f)
        
        data[str(session.user_id)] = session.to_dict()
        
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving progress: {e}")

def load_progress(user_id):
    """Load progress from file"""
    try:
        if os.path.exists(PROGRESS_FILE):
            with open(PROGRESS_FILE, 'r') as f:
                data = json.load(f)
                if str(user_id) in data:
                    return ForwardSession.from_dict(data[str(user_id)])
    except Exception as e:
        logger.error(f"Error loading progress: {e}")
    return None

def get_main_menu():
    """Get main menu buttons"""
    return [
        [Button.inline("🚀 Start New Forward", b"new_forward")],
        [Button.inline("▶️ Resume Last Session", b"resume")],
        [Button.inline("📊 View Status", b"status")],
        [Button.inline("⏹️ Stop Forward", b"stop")]
    ]

def get_filter_menu():
    """Get filter selection menu"""
    return [
        [Button.inline("🎥 Videos (mp4, mkv, avi)", b"filter_video")],
        [Button.inline("📸 Images (jpg, png, gif)", b"filter_image")],
        [Button.inline("📄 Documents (pdf, zip, rar)", b"filter_document")],
        [Button.inline("🎵 Audio (mp3, m4a, flac)", b"filter_audio")],
        [Button.inline("✅ All Media", b"filter_all")],
        [Button.inline("🔙 Back", b"back_main")]
    ]

async def should_forward_message(message, filters):
    """Check if message matches filters"""
    if not message.media:
        return False
    
    if 'all' in filters:
        return True
    
    if 'video' in filters and message.video:
        return True
    
    if 'image' in filters and message.photo:
        return True
    
    if 'document' in filters and message.document:
        return True
    
    if 'audio' in filters and message.audio:
        return True
    
    # Check file extensions
    if message.document and hasattr(message.document, 'attributes'):
        for attr in message.document.attributes:
            if hasattr(attr, 'file_name'):
                file_name = attr.file_name.lower()
                for f in filters:
                    if file_name.endswith(f):
                        return True
    
    return False

async def forward_messages(session, progress_msg):
    """Main forwarding function with rate limit handling"""
    try:
        if not user_client or not user_client.is_connected():
            await bot_client.edit_message(
                progress_msg.chat_id, 
                progress_msg.id,
                "❌ **Error: User client not connected!**\n\nPlease login first using /login",
                buttons=get_main_menu()
            )
            return
            
        session.is_running = True
        source = await user_client.get_entity(session.source_channel)
        dest = await user_client.get_entity(session.dest_channel)
        
        # Get total messages for progress calculation
        if session.total == 0:
            async for _ in user_client.iter_messages(source, limit=None):
                session.total += 1
        
        logger.info(f"Starting forward from {source.title} to {dest.title}")
        
        # Start from last message ID if resuming
        offset_id = session.last_msg_id if session.last_msg_id > 0 else 0
        
        async for message in user_client.iter_messages(source, offset_id=offset_id, reverse=True):
            if not session.is_running:
                await bot_client.edit_message(
                    progress_msg.chat_id, 
                    progress_msg.id,
                    "⏸️ **Forwarding Stopped**\n\nYou can resume anytime!",
                    buttons=get_main_menu()
                )
                break
            
            try:
                # Check if message matches filters
                if await should_forward_message(message, session.filters):
                    # Forward with retry logic
                    retry_count = 0
                    max_retries = 3
                    
                    while retry_count < max_retries:
                        try:
                            # Send as copy (no forward tag) to hide source
                            if message.photo:
                                await user_client.send_file(
                                    dest,
                                    message.photo,
                                    caption=message.text if message.text else ""
                                )
                            elif message.video:
                                await user_client.send_file(
                                    dest,
                                    message.video,
                                    caption=message.text if message.text else ""
                                )
                            elif message.document:
                                await user_client.send_file(
                                    dest,
                                    message.document,
                                    caption=message.text if message.text else ""
                                )
                            elif message.audio:
                                await user_client.send_file(
                                    dest,
                                    message.audio,
                                    caption=message.text if message.text else ""
                                )
                            else:
                                # Fallback to regular forward
                                await user_client.send_message(
                                    dest,
                                    message.text if message.text else "Media"
                                )
                            
                            session.forwarded += 1
                            session.last_msg_id = message.id
                            break
                        except FloodWaitError as e:
                            wait_time = e.seconds
                            logger.warning(f"FloodWait: {wait_time}s")
                            
                            await bot_client.edit_message(
                                progress_msg.chat_id,
                                progress_msg.id,
                                f"⏳ **Rate Limited!**\n\n"
                                f"⏰ Waiting {wait_time} seconds...\n"
                                f"📊 Progress: {session.forwarded}/{session.total}\n"
                                f"❌ Failed: {session.failed}"
                            )
                            
                            await asyncio.sleep(wait_time)
                            retry_count += 1
                        except Exception as e:
                            logger.error(f"Error forwarding message {message.id}: {e}")
                            session.failed += 1
                            retry_count += 1
                            await asyncio.sleep(2)
                    
                    # Update progress every 5 messages
                    if session.forwarded % 5 == 0:
                        progress_percent = (session.forwarded / session.total * 100) if session.total > 0 else 0
                        progress_bar = "▰" * int(progress_percent / 10) + "▱" * (10 - int(progress_percent / 10))
                        
                        await bot_client.edit_message(
                            progress_msg.chat_id,
                            progress_msg.id,
                            f"🚀 **Forwarding in Progress**\n\n"
                            f"📊 Progress: {progress_bar} {progress_percent:.1f}%\n\n"
                            f"✅ Forwarded: {session.forwarded}\n"
                            f"❌ Failed: {session.failed}\n"
                            f"📝 Total: {session.total}\n\n"
                            f"⏱️ Last Update: {datetime.now().strftime('%H:%M:%S')}",
                            buttons=[[Button.inline("⏹️ Stop", b"stop")]]
                        )
                        
                        save_progress(session)
                    
                    # Smart delay to avoid rate limits
                    await asyncio.sleep(1.5)
                
            except Exception as e:
                logger.error(f"Error processing message {message.id}: {e}")
                session.failed += 1
        
        # Completion message
        if session.is_running:
            await bot_client.edit_message(
                progress_msg.chat_id,
                progress_msg.id,
                f"✅ **Forwarding Complete!**\n\n"
                f"✅ Successfully forwarded: {session.forwarded}\n"
                f"❌ Failed: {session.failed}\n"
                f"📝 Total processed: {session.total}\n\n"
                f"🎉 All done!",
                buttons=get_main_menu()
            )
            session.is_running = False
            save_progress(session)
        
    except FloodWaitError as e:
        logger.warning(f"FloodWait in main loop: {e.seconds}s")
        await asyncio.sleep(e.seconds)
        # Automatically resume
        await forward_messages(session, progress_msg)
    except Exception as e:
        logger.error(f"Fatal error in forward_messages: {e}")
        await bot_client.send_message(
            progress_msg.chat_id,
            f"❌ **Error occurred:**\n`{str(e)}`\n\nProgress saved. You can resume later!",
            buttons=get_main_menu()
        )
        session.is_running = False
        save_progress(session)

@bot_client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    """Handle /start command"""
    user_id = event.sender_id
    
    # Check authorization
    sender = await event.get_sender()
    username = sender.username if sender.username else ""
    
    if username.lower() != AUTHORIZED_USER.lower() and not is_authorized(user_id):
        await event.respond(
            "🚫 **Access Denied!**\n\n"
            "This is a personal bot made with ❤️ and dedication by @NeonGhost.\n\n"
            "🔒 If you want your own bot like this, please contact:\n"
            "👤 @NeonGhost\n\n"
            "✨ Custom bots available on request!"
        )
        logger.warning(f"Unauthorized access attempt from {user_id} (@{username})")
        return
    
    # Save authorized user
    if user_id not in load_authorized_users():
        save_authorized_user(user_id)
        global OWNER_ID
        OWNER_ID = user_id
    
    # Check if user client is logged in
    if not user_client or not user_client.is_connected():
        await event.respond(
            "👋 **Welcome to Channel Forwarder Bot!**\n\n"
            "⚠️ You need to login first!\n\n"
            "Use /login to connect your Telegram account.",
            buttons=[[Button.inline("🔐 Login Now", b"start_login")]]
        )
        return
    
    await event.respond(
        "🎯 **Telegram Channel Forwarder Bot**\n\n"
        "Welcome back! Your session is active.\n\n"
        "✨ **Features:**\n"
        "• Smart file filtering\n"
        "• Auto rate-limit handling\n"
        "• Resume support\n"
        "• Live progress tracking\n"
        "• Hidden forwarding (no source name)\n\n"
        "Choose an option below:",
        buttons=get_main_menu()
    )

@bot_client.on(events.NewMessage(pattern='/login'))
async def login_handler(event):
    """Handle /login command"""
    user_id = event.sender_id
    
    # Check authorization
    sender = await event.get_sender()
    username = sender.username if sender.username else ""
    
    if username.lower() != AUTHORIZED_USER.lower() and not is_authorized(user_id):
        await event.respond(
            "🚫 **Access Denied!**\n\n"
            "This bot is for authorized users only.\n"
            "Contact @NeonGhost for access."
        )
        return
    
    if user_client and user_client.is_connected():
        await event.respond(
            "✅ You're already logged in!\n\n"
            "Use /start to begin forwarding.",
            buttons=get_main_menu()
        )
        return
    
    login_states[user_id] = {'step': 'phone'}
    await event.respond(
        "📱 **Login to Telegram**\n\n"
        "Please send your phone number with country code.\n\n"
        "Example: +919876543210\n\n"
        "⚠️ Your number is safe and stored locally.",
        buttons=[[Button.inline("❌ Cancel", b"cancel_login")]]
    )

@bot_client.on(events.CallbackQuery)
async def callback_handler(event):
    """Handle button callbacks"""
    user_id = event.sender_id
    data = event.data.decode('utf-8')
    
    # Check authorization for all callbacks
    sender = await event.get_sender()
    username = sender.username if sender.username else ""
    
    if username.lower() != AUTHORIZED_USER.lower() and not is_authorized(user_id):
        await event.answer("🚫 Access Denied! Contact @NeonGhost", alert=True)
        return
    
    if data == "start_login":
        login_states[user_id] = {'step': 'phone'}
        await event.edit(
            "📱 **Login to Telegram**\n\n"
            "Please send your phone number with country code.\n\n"
            "Example: +919876543210",
            buttons=[[Button.inline("❌ Cancel", b"cancel_login")]]
        )
    
    elif data == "cancel_login":
        if user_id in login_states:
            del login_states[user_id]
        await event.edit(
            "❌ Login cancelled.",
            buttons=[[Button.inline("🔙 Back", b"back_start")]]
        )
    
    elif data == "back_start":
        await event.edit(
            "🏠 **Main Menu**",
            buttons=[[Button.inline("🔐 Login", b"start_login")]]
        )
    
    elif data == "new_forward":
        if not user_client or not user_client.is_connected():
            await event.answer("❌ Please login first using /login", alert=True)
            return
            
        user_states[user_id] = ForwardSession(user_id)
        await event.edit(
            "📥 **Step 1: Source Channel**\n\n"
            "Please send me the source channel:\n"
            "• Channel username (@channel)\n"
            "• Channel ID (-100xxxxxx)\n"
            "• Channel link (t.me/channel)\n\n"
            "💡 Make sure your account is a member!",
            buttons=[[Button.inline("🔙 Cancel", b"back_main")]]
        )
        user_states[user_id].source_channel = "waiting"
        
    elif data == "resume":
        session = load_progress(user_id)
        if session:
            user_states[user_id] = session
            await event.edit(
                f"📂 **Resume Session**\n\n"
                f"Source: `{session.source_channel}`\n"
                f"Destination: `{session.dest_channel}`\n"
                f"Progress: {session.forwarded}/{session.total}\n\n"
                f"Continue forwarding?",
                buttons=[
                    [Button.inline("▶️ Resume", b"confirm_resume")],
                    [Button.inline("🔙 Back", b"back_main")]
                ]
            )
        else:
            await event.answer("❌ No saved session found!", alert=True)
    
    elif data == "confirm_resume":
        session = user_states.get(user_id)
        if session:
            progress_msg = await event.edit(
                "🚀 **Resuming Forward...**\n\nPlease wait...",
                buttons=None
            )
            session.task = asyncio.create_task(forward_messages(session, progress_msg))
            active_tasks[user_id] = session
        
    elif data == "status":
        session = active_tasks.get(user_id) or user_states.get(user_id)
        if session:
            status = "🟢 Running" if session.is_running else "🔴 Stopped"
            await event.edit(
                f"📊 **Current Status**\n\n"
                f"Status: {status}\n"
                f"Source: `{session.source_channel or 'Not set'}`\n"
                f"Destination: `{session.dest_channel or 'Not set'}`\n"
                f"Forwarded: {session.forwarded}\n"
                f"Failed: {session.failed}\n"
                f"Total: {session.total}",
                buttons=get_main_menu()
            )
        else:
            await event.answer("❌ No active session!", alert=True)
    
    elif data == "stop":
        session = active_tasks.get(user_id)
        if session:
            session.is_running = False
            save_progress(session)
            await event.answer("⏹️ Forwarding stopped!", alert=True)
        else:
            await event.answer("❌ No active forwarding!", alert=True)
    
    elif data.startswith("filter_"):
        filter_type = data.replace("filter_", "")
        session = user_states.get(user_id)
        
        if session:
            if filter_type == "all":
                session.filters = ['all']
            elif filter_type == "video":
                session.filters = ['.mp4', '.mkv', '.avi', '.mov', 'video']
            elif filter_type == "image":
                session.filters = ['.jpg', '.jpeg', '.png', '.gif', 'image']
            elif filter_type == "document":
                session.filters = ['.pdf', '.zip', '.rar', '.doc', '.docx', 'document']
            elif filter_type == "audio":
                session.filters = ['.mp3', '.m4a', '.flac', '.wav', 'audio']
            
            await event.edit(
                f"✅ **Filter Set: {filter_type.upper()}**\n\n"
                f"Source: `{session.source_channel}`\n"
                f"Destination: `{session.dest_channel}`\n"
                f"Filters: {', '.join(session.filters)}\n\n"
                f"Ready to start?",
                buttons=[
                    [Button.inline("🚀 Start Forwarding", b"start_forward")],
                    [Button.inline("🔙 Back", b"back_main")]
                ]
            )
    
    elif data == "start_forward":
        session = user_states.get(user_id)
        if session and session.source_channel and session.dest_channel:
            progress_msg = await event.edit(
                "🚀 **Starting Forward...**\n\nInitializing...",
                buttons=None
            )
            session.task = asyncio.create_task(forward_messages(session, progress_msg))
            active_tasks[user_id] = session
    
    elif data == "back_main":
        await event.edit(
            "🏠 **Main Menu**\n\nChoose an option:",
            buttons=get_main_menu()
        )

@bot_client.on(events.NewMessage)
async def message_handler(event):
    """Handle text messages"""
    if event.text.startswith('/'):
        return
        
    user_id = event.sender_id
    
    # Check authorization
    sender = await event.get_sender()
    username = sender.username if sender.username else ""
    
    if username.lower() != AUTHORIZED_USER.lower() and not is_authorized(user_id):
        return
    
    # Handle login flow
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
                    "✅ **Code Sent!**\n\n"
                    "📲 Please send the verification code you received.\n\n"
                    "Example: 12345",
                    buttons=[[Button.inline("❌ Cancel", b"cancel_login")]]
                )
            except Exception as e:
                await event.respond(f"❌ Error: {str(e)}\n\nPlease try again with /login")
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
                        "✅ **Login Successful!**\n\n"
                        "🎉 Your session has been saved!\n"
                        "You can now use the bot anytime.\n\n"
                        "Use /start to begin forwarding!",
                        buttons=get_main_menu()
                    )
                    
                    del login_states[user_id]
                    logger.info(f"User {user_id} logged in successfully")
                    
                except SessionPasswordNeededError:
                    login_state['step'] = '2fa'
                    await event.respond(
                        "🔐 **2FA Enabled**\n\n"
                        "Please send your 2FA password:",
                        buttons=[[Button.inline("❌ Cancel", b"cancel_login")]]
                    )
            except Exception as e:
                await event.respond(
                    f"❌ Invalid code!\n\n"
                    f"Error: {str(e)}\n\n"
                    f"Please try /login again."
                )
                if user_id in login_states:
                    del login_states[user_id]
        
        elif login_state['step'] == '2fa':
            try:
                password = text.strip()
                await user_client.sign_in(password=password)
                
                await event.respond(
                    "✅ **Login Successful!**\n\n"
                    "🎉 Your session has been saved!\n\n"
                    "Use /start to begin forwarding!",
                    buttons=get_main_menu()
                )
                
                del login_states[user_id]
                logger.info(f"User {user_id} logged in with 2FA")
                
            except Exception as e:
                await event.respond(
                    f"❌ Invalid password!\n\n"
                    f"Error: {str(e)}\n\n"
                    f"Please try /login again."
                )
                if user_id in login_states:
                    del login_states[user_id]
        
        return
    
    # Handle forwarding flow
    session = user_states.get(user_id)
    
    if not session:
        return
    
    text = event.text.strip()
    
    # Handle source channel input
    if session.source_channel == "waiting":
        session.source_channel = text
        await event.respond(
            f"✅ **Source set:** `{text}`\n\n"
            f"📤 **Step 2: Destination Channel**\n\n"
            f"Now send me the destination channel:",
            buttons=[[Button.inline("🔙 Cancel", b"back_main")]]
        )
        session.dest_channel = "waiting"
    
    # Handle destination channel input
    elif session.dest_channel == "waiting":
        session.dest_channel = text
        await event.respond(
            f"✅ **Destination set:** `{text}`\n\n"
            f"🎯 **Step 3: Choose Filters**\n\n"
            f"Select what type of files to forward:",
            buttons=get_filter_menu()
        )

async def main():
    """Main function"""
    logger.info("Starting bot...")
    
    # Try to load existing user session
    global user_client
    if os.path.exists(f"{USER_SESSION}.session"):
        user_client = TelegramClient(USER_SESSION, API_ID, API_HASH)
        await user_client.connect()
        if await user_client.is_user_authorized():
            logger.info("User client loaded from saved session")
        else:
            logger.info("Session expired, need to login again")
            user_client = None
    
    # Start bot client
    await bot_client.start(bot_token=BOT_TOKEN)
    logger.info("Bot client started")
    
    me = await bot_client.get_me()
    logger.info(f"Bot started as @{me.username}")
    
    print("\n" + "="*50)
    print("✅ Bot is running!")
    print(f"🤖 Bot: @{me.username}")
    print(f"👤 Authorized: @{AUTHORIZED_USER}")
    print("="*50 + "\n")
    
    # Keep running
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
