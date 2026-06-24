# GiftBot — Telegram бот для выдачи звёздных подарков

## Переменные окружения

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен бота от @BotFather |
| `ADMIN_ID` | Telegram ID администратора |

## Деплой на Koyeb (бесплатно, 24/7)

1. Зарегистрируйтесь на [koyeb.com](https://koyeb.com)
2. Нажмите **Create App** → выберите **GitHub**
3. Выберите этот репозиторий
4. В настройках укажите:
   - **Build method**: Dockerfile
   - **Environment variables**: добавьте `BOT_TOKEN` и `ADMIN_ID`
5. Нажмите **Deploy** — бот запустится и будет работать 24/7

## Локальный запуск

```bash
# Создайте .env файл
echo "BOT_TOKEN=ваш_токен" > .env
echo "ADMIN_ID=ваш_id" >> .env

# Установите зависимости
pip install -r requirements.txt

# Запустите
python bot.py
```
