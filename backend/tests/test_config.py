import os
from config.settings import Settings

def test_settings_redis_url_loads(monkeypatch):
    """Verify that REDIS_URL from env loads successfully in settings."""
    test_url = "rediss://default:testtoken@localhost:6379"
    monkeypatch.setenv("REDIS_URL", test_url)
    
    # Instantiate new settings instance
    new_settings = Settings()
    assert new_settings.REDIS_URL == test_url


def test_settings_allowed_origins_comma_separated(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.com, https://api.example.com")
    new_settings = Settings()
    assert new_settings.cors_origins == ["https://app.example.com", "https://api.example.com"]
