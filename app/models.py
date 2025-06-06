from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .db import Base

class SyncLog(Base):
    __tablename__ = "sync_logs"

    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String)  # 'product', 'order', etc.
    entity_id = Column(String)    # ID from source system
    source = Column(String)       # 'woocommerce' or 'odoo'
    action = Column(String)       # 'create', 'update', 'delete'
    status = Column(String)       # 'success', 'failed'
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SyncMapping(Base):
    __tablename__ = "sync_mappings"

    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String)  # 'product', 'order', etc.
    wc_id = Column(String)        # WooCommerce ID
    odoo_id = Column(String)      # Odoo ID
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow) 