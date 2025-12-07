import os
import json
import logging
import redis
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===================== REDIS КЛИЕНТ =====================
class RedisStorage:
    def __init__(self, redis_url):
        self.redis = redis.from_url(redis_url, decode_responses=True)
        logger.info("✅ Redis подключен")
    
    def save_user_data(self, user_id, key, value):
        """Сохранение данных пользователя"""
        redis_key = f"user:{user_id}:{key}"
        self.redis.set(redis_key, json.dumps(value))
        return True
    
    def get_user_data(self, user_id, key):
        """Получение данных пользователя"""
        redis_key = f"user:{user_id}:{key}"
        data = self.redis.get(redis_key)
        return json.loads(data) if data else None
    
    def save_message(self, user_id, message, sender="user"):
        """Сохранение сообщения в историю"""
        message_data = {
            "text": message,
            "sender": sender,
            "timestamp": datetime.now().isoformat()
        }
        
        # Добавляем в список последних сообщений
        redis_key = f"user:{user_id}:messages"
        self.redis.lpush(redis_key, json.dumps(message_data))
        self.redis.ltrim(redis_key, 0, 99)  # Храним 100 последних сообщений
        
        # Обновляем счетчик сообщений
        counter_key = f"stats:messages:{datetime.now().strftime('%Y-%m-%d')}"
        self.redis.incr(counter_key)
        
        return True
    
    def get_message_history(self, user_id, limit=10):
        """Получение истории сообщений"""
        redis_key = f"user:{user_id}:messages"
        messages = self.redis.lrange(redis_key, 0, limit-1)
        return [json.loads(msg) for msg in messages]
    
    def get_user_stats(self, user_id):
        """Статистика пользователя"""
        return {
            "message_count": self.redis.llen(f"user:{user_id}:messages"),
            "last_seen": self.redis.get(f"user:{user_id}:last_seen"),
            "created_at": self.redis.get(f"user:{user_id}:created_at")
        }
    
    def get_bot_stats(self):
        """Общая статистика бота"""
        today = datetime.now().strftime('%Y-%m-%d')
        return {
            "users_total": len(self.redis.keys("user:*:created_at")),
            "messages_today": self.redis.get(f"stats:messages:{today}") or 0,
            "active_today": len(self.redis.keys(f"user:*:last_seen:{today}"))
        }
    
    def update_last_seen(self, user_id):
        """Обновление времени последней активности"""
        today = datetime.now().strftime('%Y-%m-%d')
        self.redis.set(f"user:{user_id}:last_seen", datetime.now().isoformat())
        self.redis.set(f"user:{user_id}:last_seen:{today}", "1", ex=86400)

# Инициализация Redis
try:
    storage = RedisStorage(REDIS_URL)
except Exception as e:
    logger.error(f"❌ Ошибка подключения к Redis: {e}")
    storage = None

# ===================== КОМАНДЫ БОТА =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    
    # Сохраняем информацию о пользователе
    if storage:
        storage.save_user_data(user.id, "info", {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "language_code": user.language_code
        })
        
        # Время создания
        if not storage.get_user_data(user.id, "created_at"):
            storage.save_user_data(user.id, "created_at", datetime.now().isoformat())
        
        storage.update_last_seen(user.id)
        storage.save_message(user.id, "/start", "command")
    
    welcome_text = (
        "🤖 *Добро пожаловать!*\n\n"
        "Я бот с Redis-хранилищем!\n"
        "Ваши данные теперь сохраняются на сервере.\n\n"
        "*Доступные команды:*\n"
        "/profile - Ваш профиль\n"
        "/stats - Статистика\n"
        "/history - История сообщений\n"
        "/admin - Админ-панель\n\n"
        "Просто напишите мне что-нибудь!"
    )
    
    await update.message.reply_text(welcome_text, parse_mode="Markdown")
    logger.info(f"Пользователь {user.id} начал диалог")

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /profile"""
    user = update.effective_user
    
    if storage:
        storage.update_last_seen(user.id)
        
        # Получаем данные из Redis
        user_info = storage.get_user_data(user.id, "info")
        stats = storage.get_user_stats(user.id)
        
        profile_text = (
            f"👤 *Ваш профиль*\n"
            f"🆔 ID: `{user.id}`\n"
            f"📛 Имя: {user_info.get('first_name') if user_info else user.first_name}\n"
            f"📅 С нами с: {stats.get('created_at', 'сегодня')[:10]}\n"
            f"💬 Сообщений: {stats.get('message_count', 0)}\n"
            f"🕐 Последний раз: {stats.get('last_seen', 'только что')[:16]}"
        )
        
        await update.message.reply_text(profile_text, parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Хранилище недоступно")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats"""
    if storage:
        stats = storage.get_bot_stats()
        
        stats_text = (
            "📊 *Статистика бота*\n\n"
            f"👥 Пользователей: {stats['users_total']}\n"
            f"💬 Сообщений сегодня: {stats['messages_today']}\n"
            f"🎯 Активных сегодня: {stats['active_today']}\n\n"
            f"🔄 Redis: {'✅' if storage.redis.ping() else '❌'}"
        )
        
        await update.message.reply_text(stats_text, parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Статистика недоступна")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /history - история сообщений"""
    user = update.effective_user
    
    if storage:
        history = storage.get_message_history(user.id, limit=5)
        
        if history:
            history_text = "📜 *Последние 5 сообщений:*\n\n"
            for msg in reversed(history):  # Новые сверху
                time = datetime.fromisoformat(msg['timestamp']).strftime('%H:%M')
                sender = "Вы" if msg['sender'] == "user" else "Бот"
                history_text += f"🕐 {time} | {sender}: {msg['text'][:50]}...\n"
        else:
            history_text = "📜 История сообщений пуста"
        
        await update.message.reply_text(history_text, parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ История недоступна")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admin - только для админа"""
    user = update.effective_user
    admin_id = os.environ.get("ADMIN_ID")
    
    if str(user.id) != admin_id:
        await update.message.reply_text("❌ Эта команда только для администратора")
        return
    
    if storage:
        # Получаем все ключи из Redis для отладки
        keys = storage.redis.keys("*")
        
        admin_text = (
            "🛠️ *Админ-панель*\n\n"
            f"🔑 Всего ключей в Redis: {len(keys)}\n"
            f"💾 Использовано памяти: {storage.redis.info('memory')['used_memory_human']}\n"
            f"⚡ Подключений: {storage.redis.info('clients')['connected_clients']}\n\n"
            "*Последние 10 ключей:*\n"
        )
        
        for key in keys[:10]:
            admin_text += f"• {key}\n"
        
        await update.message.reply_text(admin_text, parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Redis недоступен")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка обычных сообщений"""
    user = update.effective_user
    message = update.message.text
    
    logger.info(f"Сообщение от {user.id}: {message}")
    
    # Сохраняем в Redis
    if storage:
        storage.save_message(user.id, message, "user")
        storage.update_last_seen(user.id)
        
        # Пример: отвечаем эхом с сохранением
        response = f"Вы сказали: {message}"
        storage.save_message(user.id, response, "bot")
        
        await update.message.reply_text(response)
    else:
        await update.message.reply_text("Сообщение получено (Redis недоступен)")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /broadcast - рассылка всем пользователям (админ)"""
    user = update.effective_user
    admin_id = os.environ.get("ADMIN_ID")
    
    if str(user.id) != admin_id:
        await update.message.reply_text("❌ Только для администратора")
        return
    
    if not context.args:
        await update.message.reply_text("Используйте: /broadcast текст сообщения")
        return
    
    broadcast_text = " ".join(context.args)
    
    if storage:
        # Находим всех пользователей
        user_keys = storage.redis.keys("user:*:created_at")
        user_ids = [key.split(":")[1] for key in user_keys]
        
        await update.message.reply_text(f"📢 Рассылка {len(user_ids)} пользователям...")
        
        # Отправляем каждому
        success = 0
        for user_id in user_ids:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📢 *Важное сообщение от администратора:*\n\n{broadcast_text}",
                    parse_mode="Markdown"
                )
                success += 1
            except:
                pass
        
        await update.message.reply_text(f"✅ Отправлено {success}/{len(user_ids)} пользователям")
    else:
        await update.message.reply_text("❌ Redis недоступен")

# ===================== ОСНОВНАЯ ФУНКЦИЯ =====================
def main():
    """Запуск бота"""
    print("=" * 50)
    print("🤖 TELEGRAM BOT WITH REDIS")
    print("=" * 50)
    
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_TOKEN not found!")
        print("Add BOT_TOKEN in Railway Variables")
        return
    
    print(f"✅ Bot Token: {BOT_TOKEN[:15]}...")
    print(f"🔗 Redis URL: {REDIS_URL}")
    print("=" * 50)
    
    # Создаем приложение
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Добавляем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    
    # Обработчик сообщений
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🚀 Bot starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()