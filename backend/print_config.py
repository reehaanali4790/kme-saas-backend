import os
from config.settings import settings
from config.database import DATABASE_URL

print("--- ENV RESOLUTION TEST ---")
print("1. Raw os.environ.get('DATABASE_URL'):", os.environ.get('DATABASE_URL'))
print("2. settings.DATABASE_URL (from Pydantic .env):", settings.DATABASE_URL)
print("3. Final DATABASE_URL used by SQLAlchemy:", DATABASE_URL)
