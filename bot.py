import os
import json
import logging
import redis
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
UPSTASH_REDIS_URL = os.environ.get("UPSTASH_REDIS_URL")  # Из Railway Variables

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

print("=" * 60)
print("🤖 TELEGRAM BOT WITH UPSTASH REDIS")
print("=" * 60)

# ===================== UPSTASH REDIS МЕНЕДЖЕР =====================
class UpstashRedisManager:
    def __init__(self, redis_url):
        """Инициализация подключения к Upstash Redis"""
        try:
            # Подключаемся к Upstash Redis
            self.redis = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_keepalive=True
            )
            
            # Проверяем подключение
            self.redis.ping()
            logger.info("✅ Успешное подключение к Upstash Redis")
            print(f"🔗 Redis подключен: {redis_url.split('@')[1] if '@' in redis_url else redis_url}")
            
            # Проверяем лимиты Upstash
            self.check_limits()
            
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к Redis: {e}")
            self.redis = None
            print("⚠️  Работаем без Redis (данные не сохранятся)")
    
    def check_limits(self):
        """Проверка лимитов Upstash (10K команд/день бесплатно)"""
        try:
            # Создаем счетчик для сегодняшнего дня
            today = datetime.now().strftime('%Y-%m-%d')
            key = f"upstash:commands:{today}"
            
            # Получаем текущий счетчик
            commands_today = self.redis.get(key) or 0
            
            # Предупреждение если близко к лимиту
            if int(commands_today) > 8000:
                print(f"⚠️  Внимание: использовано {commands_today}/10000 команд сегодня")
            
            print(f"📊 Команд сегодня: {commands_today}")
            
        except:
            pass
    
    def increment_command_counter(self):
        """Увеличиваем счетчик команд"""
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            key = f"upstash:commands:{today}"
            self.redis.incr(key)
            # Автоматическое удаление через 7 дней
            self.redis.expire(key, 7 * 86400)
        except:
            pass
    
    # ========== ОСНОВНЫЕ МЕТОДЫ ДЛЯ БОТА ==========
    
    def save_user(self, user_id, user_data):
        """Сохранение данных пользователя"""
        try:
            self.increment_command_counter()
            key = f"user:{user_id}"
            self.redis.hset(key, mapping={
                "username": user_data.get("username", ""),
                "first_name": user_data.get("first_name", ""),
                "last_seen": datetime.now().isoformat(),
                "message_count": 0
            })
            
            # Устанавливаем TTL 90 дней для автоматической очистки неактивных
            self.redis.expire(key, 90 * 86400)
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения пользователя: {e}")
            return False
    
    def get_user(self, user_id):
        """Получение данных пользователя"""
        try:
            self.increment_command_counter()
            key = f"user:{user_id}"
            return self.redis.hgetall(key)
        except:
            return {}
    
    def save_message(self, user_id, message, message_type="text"):
        """Сохранение сообщения"""
        try:
            self.increment_command_counter()
            
            # Сохраняем само сообщение
            message_id = self.redis.incr("global:message_id")
            message_key = f"message:{message_id}"
            
            message_data = {
                "user_id": user_id,
                "text": message[:500],  # Ограничиваем длину
                "type": message_type,
                "timestamp": datetime.now().isoformat(),
                "message_id": message_id
            }
            
            self.redis.hset(message_key, mapping=message_data)
            self.redis.expire(message_key, 30 * 86400)  # 30 дней
            
            # Обновляем счетчик сообщений пользователя
            user_key = f"user:{user_id}"
            self.redis.hincrby(user_key, "message_count", 1)
            self.redis.hset(user_key, "last_seen", datetime.now().isoformat())
            
            # Добавляем в список последних сообщений пользователя
            list_key = f"user:{user_id}:messages"
            self.redis.lpush(list_key, message_id)
            self.redis.ltrim(list_key, 0, 49)  # Храним 50 последних
            
            return message_id
            
        except Exception as e:
            logger.error(f"Ошибка сохранения сообщения: {e}")
            return None
    
    def get_user_stats(self, user_id):
        """Статистика пользователя"""
        try:
            self.increment_command_counter()
            user_data = self.get_user(user_id)
            
            # Получаем последние сообщения
            list_key = f"user:{user_id}:messages"
            last_messages_ids = self.redis.lrange(list_key, 0, 4)  # 5 последних
            
            last_messages = []
            for msg_id in last_messages_ids:
                msg = self.redis.hgetall(f"message:{msg_id}")
                if msg:
                    last_messages.append({
                        "text": msg.get("text", "")[:50] + "...",
                        "time": msg.get("timestamp", "")[:16]
                    })
            
            return {
                "message_count": user_data.get("message_count", 0),
                "last_seen": user_data.get("last_seen", "никогда"),
                "username": user_data.get("username", "неизвестно"),
                "last_messages": last_messages
            }
        except:
            return {}
    
    def get_global_stats(self):
        """Глобальная статистика бота"""
        try:
            self.increment_command_counter()
            
            # Подсчитываем пользователей (примерно)
            user_keys = self.redis.keys("user:*")
            # Фильтруем только ключи пользователей (не списки сообщений)
            real_users = [k for k in user_keys if ":messages" not in k]
            
            # Сообщения за сегодня
            today = datetime.now().strftime('%Y-%m-%d')
            today_messages = 0
            
            # Примерный подсчет (для экономии команд)
            all_messages = self.redis.keys("message:*")
            for msg_key in all_messages[:100]:  # Проверяем первые 100
                msg = self.redis.hget(msg_key, "timestamp")
                if msg and msg.startswith(today):
                    today_messages += 1
            
            return {
                "total_users": len(real_users),
                "today_messages": today_messages,
                "redis_status": "✅ Online" if self.redis else "❌ Offline",
                "memory_used": self.redis.info("memory")["used_memory_human"]
            }
        except Exception as e:
            return {"error": str(e)}
    
    def search_users(self, search_term):
        """Поиск пользователей по имени или username"""
        try:
            self.increment_command_counter()
            results = []
            
            # Ищем по всем пользователям
            user_keys = self.redis.keys("user:*")
            for key in user_keys:
                if ":messages" not in key:  # Только ключи пользователей
                    user_data = self.redis.hgetall(key)
                    username = user_data.get("username", "").lower()
                    first_name = user_data.get("first_name", "").lower()
                    search_term_lower = search_term.lower()
                    
                    if (search_term_lower in username or 
                        search_term_lower in first_name or
                        search_term in key):
                        results.append({
                            "user_id": key.split(":")[1],
                            "username": user_data.get("username", ""),
                            "first_name": user_data.get("first_name", ""),
                            "message_count": user_data.get("message_count", 0)
                        })
            
            return results[:10]  # Ограничиваем 10 результатами
        except:
            return []

# Инициализация Redis
redis_manager = None
if UPSTASH_REDIS_URL:
    redis_manager = UpstashRedisManager(UPSTASH_REDIS_URL)
else:
    print("⚠️  UPSTASH_REDIS_URL не установлен. Redis отключен.")

# ===================== КОМАНДЫ БОТА =====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    
    # Сохраняем пользователя в Redis
    if redis_manager:
        user_data = {
            "username": user.username or "",
            "first_name": user.first_name or "",
            "user_id": user.id
        }
        redis_manager.save_user(user.id, user_data)
    
    welcome_text = (
        "🤖 *Добро пожаловать!*\n\n"
        "Этот бот использует *Upstash Redis* для хранения данных.\n\n"
        "*📊 Доступные команды:*\n"
        "/profile - Ваша статистика\n"
        "/stats - Статистика бота\n"
        "/last - Последние сообщения\n"
        "/search - Поиск пользователей\n"
        "/admin - Админ-панель\n\n"
        "Все ваши сообщения сохраняются в облаке! 🚀"
    )
    
    await update.message.reply_text(welcome_text, parse_mode="Markdown")
    
    # Сохраняем факт использования команды
    if redis_manager:
        redis_manager.save_message(user.id, "/start", "command")

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /profile - статистика пользователя"""
    user = update.effective_user
    
    if redis_manager:
        stats = redis_manager.get_user_stats(user.id)
        
        profile_text = (
            f"👤 *Ваш профиль*\n"
            f"🆔 ID: `{user.id}`\n"
            f"📛 Username: @{stats.get('username', user.username or 'нет')}\n"
            f"💬 Сообщений: {stats.get('message_count', 0)}\n"
            f"🕐 Последняя активность: {stats.get('last_seen', 'только что')[:16]}\n\n"
            f"*Последние сообщения:*\n"
        )
        
        for i, msg in enumerate(stats.get("last_messages", []), 1):
            profile_text += f"{i}. {msg['time']}: {msg['text']}\n"
        
        if not stats.get("last_messages"):
            profile_text += "Пока нет сохраненных сообщений\n"
        
        # Добавляем информацию о Redis
        profile_text += f"\n🔗 Redis: {'✅' if redis_manager.redis else '❌'}"
        
    else:
        profile_text = "❌ Redis не доступен. Данные не сохраняются."
    
    await update.message.reply_text(profile_text, parse_mode="Markdown")
    
    if redis_manager:
        redis_manager.save_message(user.id, "/profile", "command")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats - статистика бота"""
    if redis_manager:
        stats = redis_manager.get_global_stats()
        
        stats_text = (
            "📊 *Глобальная статистика бота*\n\n"
            f"👥 Всего пользователей: {stats.get('total_users', 0)}\n"
            f"💬 Сообщений сегодня: {stats.get('today_messages', 0)}\n"
            f"🧠 Использовано памяти: {stats.get('memory_used', 'N/A')}\n"
            f"🔗 Статус Redis: {stats.get('redis_status', 'N/A')}\n\n"
            f"⚡ *Upstash Redis*\n"
            f"• Бесплатно: 10,000 команд/день\n"
            f"• Данные хранятся 90 дней\n"
            f"• Автоматическое масштабирование"
        )
    else:
        stats_text = "❌ Redis не настроен. Добавьте UPSTASH_REDIS_URL в переменные."
    
    await update.message.reply_text(stats_text, parse_mode="Markdown")
    
    if redis_manager and update.effective_user:
        redis_manager.save_message(update.effective_user.id, "/stats", "command")

async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /last - последние сообщения"""
    user = update.effective_user
    
    if redis_manager:
        stats = redis_manager.get_user_stats(user.id)
        
        if stats.get("last_messages"):
            last_text = "📜 *Ваши последние сообщения:*\n\n"
            for i, msg in enumerate(stats.get("last_messages", []), 1):
                last_text += f"*{i}.* `{msg['time']}`\n{msg['text']}\n\n"
        else:
            last_text = "📜 У вас пока нет сохраненных сообщений."
    else:
        last_text = "❌ Redis не доступен."
    
    await update.message.reply_text(last_text, parse_mode="Markdown")
    
    if redis_manager:
        redis_manager.save_message(user.id, "/last", "command")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /search - поиск пользователей (админ)"""
    user = update.effective_user
    admin_id = os.environ.get("ADMIN_ID")
    
    # Проверяем права админа
    if admin_id and str(user.id) != admin_id:
        await update.message.reply_text("❌ Эта команда только для администратора")
        return
    
    if not context.args:
        await update.message.reply_text("Используйте: /search <имя или username>")
        return
    
    search_term = " ".join(context.args)
    
    if redis_manager:
        results = redis_manager.search_users(search_term)
        
        if results:
            search_text = f"🔍 *Результаты поиска '{search_term}':*\n\n"
            for i, result in enumerate(results, 1):
                search_text += (
                    f"*{i}.* ID: `{result['user_id']}`\n"
                    f"   👤 {result['first_name']} (@{result['username'] or 'нет'})\n"
                    f"   💬 Сообщений: {result['message_count']}\n\n"
                )
        else:
            search_text = f"🔍 По запросу '{search_term}' ничего не найдено."
    else:
        search_text = "❌ Redis не доступен."
    
    await update.message.reply_text(search_text, parse_mode="Markdown")
    
    if redis_manager:
        redis_manager.save_message(user.id, f"/search {search_term}", "command")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admin - админ-панель"""
    user = update.effective_user
    admin_id = os.environ.get("ADMIN_ID")
    
    if admin_id and str(user.id) != admin_id:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    admin_text = (
        "🛠️ *Админ-панель*\n\n"
        "*Доступные команды:*\n"
        "/search <текст> - поиск пользователей\n"
        "/broadcast <текст> - рассылка\n"
        "/stats - статистика\n\n"
        "*Upstash Redis:*\n"
        "• Команд сегодня: (см /stats)\n"
        "• Память: (см /stats)\n"
        "• Пользователей: (см /stats)"
    )
    
    await update.message.reply_text(admin_text, parse_mode="Markdown")
    
    if redis_manager:
        redis_manager.save_message(user.id, "/admin", "command")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /broadcast - рассылка (админ)"""
    user = update.effective_user
    admin_id = os.environ.get("ADMIN_ID")
    
    if admin_id and str(user.id) != admin_id:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    if not context.args:
        await update.message.reply_text("Используйте: /broadcast <текст сообщения>")
        return
    
    if not redis_manager:
        await update.message.reply_text("❌ Redis не доступен")
        return
    
    broadcast_text = " ".join(context.args)
    
    # Получаем всех пользователей
    user_keys = redis_manager.redis.keys("user:*")
    real_users = [k for k in user_keys if ":messages" not in k]
    
    if not real_users:
        await update.message.reply_text("❌ Нет пользователей для рассылки")
        return
    
    await update.message.reply_text(f"📢 Рассылка {len(real_users)} пользователям...")
    
    success = 0
    for user_key in real_users[:50]:  # Ограничиваем 50 пользователями за раз
        try:
            user_id = user_key.split(":")[1]
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📢 *Сообщение от администратора:*\n\n{broadcast_text}",
                parse_mode="Markdown"
            )
            success += 1
        except:
            pass
    
    await update.message.reply_text(f"✅ Отправлено {success}/{len(real_users)} пользователям")
    
    redis_manager.save_message(user.id, f"/broadcast {broadcast_text[:50]}...", "command")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка обычных сообщений"""
    user = update.effective_user
    message = update.message.text
    
    logger.info(f"Сообщение от {user.id}: {message}")
    
    # Сохраняем в Redis
    if redis_manager:
        message_id = redis_manager.save_message(user.id, message, "text")
        
        if message_id:
            # Отвечаем с подтверждением
            response = f"✅ Сообщение #{message_id} сохранено в Upstash Redis!"
            await update.message.reply_text(response)
        else:
            await update.message.reply_text("📝 Сообщение получено (ошибка сохранения)")
    else:
        await update.message.reply_text("📝 Сообщение получено (Redis отключен)")

# ===================== ОСНОВНАЯ ФУНКЦИЯ =====================
def main():
    """Запуск бота"""
    print("=" * 60)
    print("🤖 TELEGRAM BOT WITH UPSTASH REDIS")
    print("=" * 60)
    
    if not BOT_TOKEN:
        print("❌ ОШИБКА: BOT_TOKEN не найден!")
        print("Добавьте BOT_TOKEN в Railway Variables")
        return
    
    print(f"✅ Bot Token: {BOT_TOKEN[:15]}...")
    print(f"🔗 Redis URL: {'SET' if UPSTASH_REDIS_URL else 'NOT SET'}")
    print("=" * 60)
    
    try:
        # Создаем приложение
        app = Application.builder().token(BOT_TOKEN).build()
        
        # Добавляем команды
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("profile", profile_command))
        app.add_handler(CommandHandler("stats", stats_command))
        app.add_handler(CommandHandler("last", last_command))
        app.add_handler(CommandHandler("search", search_command))
        app.add_handler(CommandHandler("admin", admin_command))
        app.add_handler(CommandHandler("broadcast", broadcast_command))
        
        # Обработчик обычных сообщений
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        print("🚀 Бот запускается...")
        print("📡 Ожидание сообщений...")
        print("=" * 60)
        
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
        print(f"❌ Критическая ошибка: {e}")

if __name__ == "__main__":
    main()
