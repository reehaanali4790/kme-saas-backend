from sqlalchemy.orm import Session

from modules.branding.schemas import BrandingConfigOut, BrandingConfigUpdate

DEFAULT_APP_NAME = "JM Traders"
DEFAULT_LOGO_BG = "#0F172A"


def get_or_create_config(db: Session):
    from models.database_models import BrandingConfig
    cfg = db.query(BrandingConfig).order_by(BrandingConfig.config_id).first()
    if not cfg:
        cfg = BrandingConfig(app_name=DEFAULT_APP_NAME, logo_bg_color=DEFAULT_LOGO_BG)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def to_out(cfg) -> BrandingConfigOut:
    logo_url = None
    if cfg.logo_path:
        version = int(cfg.updated_at.timestamp()) if cfg.updated_at else 0
        logo_url = f"/api/branding/logo?v={version}"
    return BrandingConfigOut(
        app_name=cfg.app_name,
        logo_bg_color=cfg.logo_bg_color,
        logo_url=logo_url,
        updated_at=cfg.updated_at,
    )


def update_config(data: BrandingConfigUpdate, db: Session, updated_by: int):
    cfg = get_or_create_config(db)
    if data.app_name is not None:
        cfg.app_name = data.app_name.strip() or DEFAULT_APP_NAME
    if data.logo_bg_color is not None:
        cfg.logo_bg_color = data.logo_bg_color
    cfg.updated_by = updated_by
    db.commit()
    db.refresh(cfg)
    return cfg
