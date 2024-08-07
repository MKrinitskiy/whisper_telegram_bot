import io, os
import logging
import asyncio
import traceback
import html
import json
from datetime import datetime
import uuid
from threading import Thread, Lock
from queue import Empty, Queue

from numpy import block
from thread_killer import ThreadKiller
from async_rabbitmq_consumer import ReconnectingRabbitMQConsumer
from async_rabbitmq_publisher import RabbitMQPublisher
from service_defs import EnsureDirectoryExists

import telegram
from telegram import (
    Update,
    User,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    AIORateLimiter,
    filters
)
from telegram.constants import ParseMode, ChatAction

# import messaging
import config
import database
import tempfile
import base64
# import boto3
from minio import Minio
from minio.error import S3Error
# from botocore.client import Config

# setup
db = database.Database()

departing_tasks_queue = Queue(maxsize=1024)
departing_tasks_queue_threading_lock = Lock()
arriving_messages_queue = Queue(maxsize=1024)
arriving_messages_queue_threading_lock = Lock()
departing_thread_killer = ThreadKiller()
departing_thread_killer.set_tokill(False)
arriving_thread_killer = ThreadKiller()
arriving_thread_killer.set_tokill(False)


EnsureDirectoryExists('./logs')
logging.basicConfig(filename='./logs/bot.log', level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info("Connecting to Minio")

s3 = Minio("minio:9000",
        access_key=os.getenv('MINIO_ACCESS_KEY', 'default_access_key'),
        secret_key=os.getenv('MINIO_SECRET_KEY', 'default_secret_key'),
        secure=False
    )

logger.info("Connected to Minio")
logger.info("Checking if bucket exists")

bucket_name = os.getenv('MINIO_BUCKET_NAME', 'whisper_telegram_bot')
if not s3.bucket_exists(bucket_name):
    s3.make_bucket(bucket_name)
    logger.info(f"Bucket {bucket_name} created successfully")
else:
    logger.info(f"Bucket {bucket_name} already exists")

user_semaphores = {}
user_tasks = {}

HELP_MESSAGE = """Commands:
⚪ /lang – Select transcription main language
⚪ /list – List queued transcription tasks
⚪ /help – Show help
"""



def threaded_departing_tasks_rmq_feeder(tokill, departing_tasks_queue):
    """Threaded worker for sending the tasks from departing_tasks_queue to RabbitMQ messages.
    tokill is a thread_killer object that indicates whether a thread should be terminated
    departing_tasks_queue is a limited size thread-safe Queue instance.
    """
    departing_tasks_thread_logger = logging.getLogger("departing_tasks_thread_logger")
    departing_tasks_thread_logger.info("Threaded departing tasks RabbitMQ feeder started")

    rmq_publisher = RabbitMQPublisher(departing_tasks_queue,
                                      departing_tasks_queue_threading_lock,
                                      departing_tasks_thread_logger)
    rmq_publisher.run()

    departing_tasks_thread_logger.info("rmq_publisher exiting")

def threaded_arriving_tasks_rmq_consumer(tokill, arriving_messages_queue):
    arriving_messages_thread_logger = logging.getLogger("arriving_messages_thread_logger")
    arriving_messages_thread_logger.info("Threaded arriving messages RabbitMQ consumer started")

    rmq_consumer = ReconnectingRabbitMQConsumer(arriving_messages_queue,
                                                arriving_messages_queue_threading_lock,
                                                arriving_messages_thread_logger)
    rmq_consumer.run()



def received_messages_processor(tokill, arriving_messages_queue):
    """Threaded worker for processing the received messages from RabbitMQ.
    tokill is a thread_killer object that indicates whether a thread should be terminated
    arriving_messages_queue is a limited size thread-safe Queue instance that contains the received messages.
    """
    received_messages_processor_thread_logger = logging.getLogger("received_messages_processor_thread_logger")
    received_messages_processor_thread_logger.info("Threaded received messages processor started")

    while not tokill():
        try:
            message = arriving_messages_queue.get(block=True, timeout=1)
            received_messages_processor_thread_logger.info(f"Received message: {message}")
        except Empty:
            continue

    received_messages_processor_thread_logger.info("received_messages_processing exiting")



async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User):
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            update.message.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name= user.last_name
        )

    if user.id not in user_semaphores:
        user_semaphores[user.id] = asyncio.Semaphore(1)

    if db.get_user_attribute(user.id, "current_transcription_lang") is None:
        db.set_user_attribute(user.id, "current_transcription_lang", config.transcription_langs["available_transcription_langs"][0])



async def start_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    # db.start_new_dialog(user_id)

    reply_text = "Hi! I'm <b>Whisper</b> bot \n\n"
    reply_text += HELP_MESSAGE

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)
    await show_transcription_langs_handle(update, context)


async def help_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)


async def unsupport_message_handle(update: Update, context: CallbackContext, message=None):
    error_text = f"I don't know how to process this kind of files."
    logger.error(error_text)
    await update.message.reply_text(error_text)
    return


async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True):
    _message = message or update.message.text

    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    transcription_lang = db.get_user_attribute(user_id, "current_transcription_lang")

    async def message_handle_fn():
        try:
            # send placeholder message to user
            placeholder_message = await update.message.reply_text("This bot only accepts voice messages, audio files and video files. Please, send a voice message, and audio file or a video file.")

        except Exception as e:
            error_text = f"Something went wrong during processing your message. Reason: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)
            return

    async with user_semaphores[user_id]:
        task = asyncio.create_task(message_handle_fn())
        user_tasks[user_id] = task

        try:
            await task
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Canceled", parse_mode=ParseMode.HTML)
        else:
            pass
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]


async def message_handle_with_audio_file(update: Update, context: CallbackContext, text=None, use_new_dialog_timeout=True):
    _message = update.message.caption or ''
    if len(_message) > 0:
        _message = _message + '\nHere is the text:\n'
    _message = _message + '\n' + (text or '')

    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    transcription_lang = db.get_user_attribute(user_id, "current_transcription_lang")

    buf = io.BytesIO()
    # new_file = await update.message.document.get_file()
    new_file = await update.message.audio.get_file()
    size = new_file.file_size
    logger.info(f"Received file {update.message.audio.file_name} with size {size} bytes")

    # update.message.document.file_name

    await new_file.download_to_memory(buf)
    # Generate a unique filename preserving the extension of the uploaded file
    
    filename = update.message.audio.file_name
    file_extension = os.path.splitext(filename)[-1]
    unique_filename = f"{uuid.uuid4().hex}{file_extension}"
    buf.seek(0)  # move cursor to the beginning of the buffer

    result = s3.put_object(bucket_name,
                           unique_filename,
                           buf,
                           size)
    
    logger.info("created {0} object; etag: {1}, version-id: {2}".format(
            result.object_name, result.etag, result.version_id),
    )
    
    # print(
    #     "created {0} object; etag: {1}, version-id: {2}".format(
    #         result.object_name, result.etag, result.version_id,
    #     ),
    # )

    # s3.Bucket(bucket_name).put_object(Key=unique_filename, Body=buf)
 

    async def message_handle_fn():
        # new dialog timeout
        db.set_user_attribute(user_id, "last_interaction", datetime.now())

        task_id = str(uuid.uuid4())

        try:
            with departing_tasks_queue_threading_lock:
                departing_tasks_queue.put({
                    "user_id": user_id,
                    "task_id": task_id,
                    "filename": filename,
                    "bucket": bucket_name,
                    "file_path": unique_filename,
                    "transcription_lang": transcription_lang,
                    "queue_name": "task_queue"
                })
        except Exception as e:
            error_text = f"Something went wrong during task submission to the queue. Reason: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)
            return

        try:
            # send placeholder message to user
            # placeholder_message = await update.message.reply_text("sending your file to tasks queue...")
            placeholder_message = await update.message.reply_text("received your file namely " + filename + ".")
        except Exception as e:
            error_text = f"Something went wrong during processing. Reason: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)
            return


    async with user_semaphores[user_id]:
        task = asyncio.create_task(message_handle_fn())            

        # user_tasks[user_id] = task

        try:
            await task
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Canceled", parse_mode=ParseMode.HTML)
        else:
            pass
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]


# async def voice_message_handle(update: Update, context: CallbackContext):
#     # check if bot was mentioned (for group chats)
#     if not await is_bot_mentioned(update, context):
#         return

#     await register_user_if_not_exists(update, context, update.message.from_user)
#     if await is_previous_message_not_answered_yet(update, context): return

#     user_id = update.message.from_user.id
#     db.set_user_attribute(user_id, "last_interaction", datetime.now())

#     voice = update.message.voice
#     voice_file = await context.bot.get_file(voice.file_id)
    
#     # store file in memory, not on disk
#     buf = io.BytesIO()
#     await voice_file.download_to_memory(buf)
#     buf.name = "voice.oga"  # file extension is required
#     buf.seek(0)  # move cursor to the beginning of the buffer

#     transcribed_text = await openai_utils.transcribe_audio(buf)
#     text = f"🎤: <i>{transcribed_text}</i>"
#     await update.message.reply_text(text, parse_mode=ParseMode.HTML)

#     # update n_transcribed_seconds
#     db.set_user_attribute(user_id, "n_transcribed_seconds", voice.duration + db.get_user_attribute(user_id, "n_transcribed_seconds"))

#     await message_handle(update, context, message=transcribed_text)


async def cancel_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if user_id in user_tasks:
        task = user_tasks[user_id]
        task.cancel()
    else:
        await update.message.reply_text("<i>Nothing to cancel...</i>", parse_mode=ParseMode.HTML)


def get_transcription_lang_menu(page_index: int):
    n_transcription_langs_per_page = config.n_transcription_langs_per_page
    text = f"Select <b>transcription language</b> ({len(config.transcription_langs)-1} languages available):"

    # buttons
    transcription_lang_keys = list(config.transcription_langs.keys())
    transcription_lang_keys.remove("available_transcription_langs")
    page_transcription_lang_keys = transcription_lang_keys[page_index * n_transcription_langs_per_page:(page_index + 1) * n_transcription_langs_per_page]

    keyboard = []
    for transcription_lang_key in page_transcription_lang_keys:
        name = config.transcription_langs[transcription_lang_key]["name"]
        keyboard.append([InlineKeyboardButton(name, callback_data=f"set_transcription_lang|{transcription_lang_key}")])

    # pagination
    if len(transcription_lang_keys) > n_transcription_langs_per_page:
        is_first_page = (page_index == 0)
        is_last_page = ((page_index + 1) * n_transcription_langs_per_page >= len(transcription_lang_keys))

        if is_first_page:
            keyboard.append([
                InlineKeyboardButton("»", callback_data=f"show_transcription_langs|{page_index + 1}")
            ])
        elif is_last_page:
            keyboard.append([
                InlineKeyboardButton("«", callback_data=f"show_transcription_langs|{page_index - 1}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("«", callback_data=f"show_transcription_langs|{page_index - 1}"),
                InlineKeyboardButton("»", callback_data=f"show_transcription_langs|{page_index + 1}")
            ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup


async def show_transcription_langs_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_transcription_lang_menu(0)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def show_transcription_langs_callback_handle(update: Update, context: CallbackContext):
     await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
     
     user_id = update.callback_query.from_user.id
     db.set_user_attribute(user_id, "last_interaction", datetime.now())

     query = update.callback_query
     await query.answer()

     page_index = int(query.data.split("|")[1])
     if page_index < 0:
         return

     text, reply_markup = get_transcription_lang_menu(page_index)
     try:
         await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
     except telegram.error.BadRequest as e:
         if str(e).startswith("Message is not modified"):
             pass


async def set_transcription_lang_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    transcription_lang = query.data.split("|")[1]

    db.set_user_attribute(user_id, "current_transcription_lang", transcription_lang)
    # db.start_new_dialog(user_id)

    await context.bot.send_message(
        update.callback_query.message.chat.id,
        "Transcription language has been set to <b>{}</b>".format(config.transcription_langs[transcription_lang]["name"]),
        parse_mode=ParseMode.HTML
    )


async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    try:
        # collect error message
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # split text into multiple messages due to 4096 character limit
        for message_chunk in split_text_into_chunks(message, 4096):
            try:
                await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
            except telegram.error.BadRequest:
                # answer has invalid characters, so we send it without parse_mode
                await context.bot.send_message(update.effective_chat.id, message_chunk)
    except:
        await context.bot.send_message(update.effective_chat.id, "Some error in error handler")


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("/list", "List transcription tasks"),
        BotCommand("/lang", "Select transcription language"),
        BotCommand("/help", "Show help message"),
    ])



def run_bot() -> None:
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .post_init(post_init)
        .build()
    )

    # add handlers
    user_filter = filters.ALL
    if len(config.allowed_telegram_usernames) > 0:
        usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
        any_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
        user_ids = [x for x in any_ids if x > 0]
        group_ids = [x for x in any_ids if x < 0]
        user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids) | filters.Chat(chat_id=group_ids)

    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND & user_filter, message_handle))
    # application.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND & user_filter, message_handle_with_audio_file))
    application.add_handler(MessageHandler(filters.AUDIO & ~filters.COMMAND & user_filter, message_handle_with_audio_file))
    
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))

    # application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))

    application.add_handler(CommandHandler("lang", show_transcription_langs_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(show_transcription_langs_callback_handle, pattern="^show_transcription_langs"))
    application.add_handler(CallbackQueryHandler(set_transcription_lang_handle, pattern="^set_transcription_lang"))

    application.add_error_handler(error_handle)

    departing_tasks_thread = Thread(target=threaded_departing_tasks_rmq_feeder,
                                    args=(departing_thread_killer,
                                          departing_tasks_queue))
    departing_tasks_thread.start()
    
    arriving_messages_thread = Thread(target=threaded_arriving_tasks_rmq_consumer,
                                      args=(arriving_thread_killer,
                                            arriving_messages_queue))
    arriving_messages_thread.start()

    received_messages_processing_thread = Thread(target=received_messages_processor,
                                      args=(arriving_thread_killer,
                                            arriving_messages_queue))
    received_messages_processing_thread.start()



    # start the bot
    print("start polling from Telegram")
    application.run_polling()
    
    


if __name__ == "__main__":
    run_bot()