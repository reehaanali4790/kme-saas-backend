from config.database import SessionLocal
from models.database_models import User
from modules.auth.services import AuthService

db = SessionLocal()

# Update all user passwords
users = [
    ("admin", "admin123"),
    ("manager1", "manager123"),
    ("operator1", "operator123"),
    ("viewer1", "viewer123")
]

for username, password in users:
    user = db.query(User).filter(User.username == username).first()
    if user:
        user.password_hash = AuthService.hash_password(password)
        print(f"✅ Fixed password for: {username}")

db.commit()
print("\n✅ All passwords fixed!")
db.close()