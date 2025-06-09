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

class SyncRun(Base):
    __tablename__ = "sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String, unique=True, index=True)
    sync_type = Column(String)  # e.g., 'simple', 'variable', 'customer'
    status = Column(String)     # 'running', 'completed', 'failed'
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    duration = Column(Integer, nullable=True)
    products_processed = Column(Integer, default=0)
    products_created = Column(Integer, default=0)
    products_updated = Column(Integer, default=0)
    products_skipped = Column(Integer, default=0)
    error_message = Column(String, nullable=True) 