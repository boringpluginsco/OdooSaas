from celery import Celery
from .wc import WooCommerceClient
from .odoo import OdooClient
from .db import SessionLocal
from .models import SyncLog, SyncMapping
import os
from dotenv import load_dotenv
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# Configure Celery with both broker and result backend
celery_app = Celery(
    'sync_tasks',
    broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    backend=os.getenv('REDIS_URL', 'redis://localhost:6379/0')
)

# Configure Celery settings
celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
)

@celery_app.task(bind=True)
def sync_products_wc_to_odoo(self):
    logger.info("Starting product sync from WooCommerce to Odoo")
    wc = WooCommerceClient()
    odoo = OdooClient()
    db = SessionLocal()
    
    try:
        # Get all products from WooCommerce
        logger.info("Fetching products from WooCommerce")
        wc_products = wc.get_products()
        logger.info(f"Found {len(wc_products)} products in WooCommerce")
        
        # Get all existing mappings
        existing_mappings = db.query(SyncMapping).filter_by(entity_type='product').all()
        wc_id_to_mapping = {mapping.wc_id: mapping for mapping in existing_mappings}
        
        # Track which WooCommerce products we've processed
        processed_wc_ids = set()
        
        # Process each WooCommerce product
        for product in wc_products:
            try:
                wc_id = str(product['id'])
                processed_wc_ids.add(wc_id)
                
                # Check if product is already mapped
                mapping = wc_id_to_mapping.get(wc_id)
                
                if mapping:
                    # Verify if product still exists in Odoo
                    odoo_product = odoo.search_read(
                        'product.product',
                        [('id', '=', int(mapping.odoo_id))],
                        ['id', 'name', 'active']
                    )
                    
                    if not odoo_product:
                        # Product was deleted in Odoo, create it again
                        logger.info(f"Product {product['name']} was deleted in Odoo, recreating")
                        odoo_id = odoo.create('product.product', {
                            'name': product['name'],
                            'list_price': float(product['price']),
                            'default_code': product['sku'],
                            'active': True
                        })
                        mapping.odoo_id = str(odoo_id)
                        db.add(mapping)
                    else:
                        # Update existing product in Odoo
                        logger.info(f"Updating existing product in Odoo: {product['name']}")
                        update_data = {
                            'name': product['name'],
                            'list_price': float(product['price']),
                            'default_code': product['sku'],
                            'active': True  # Ensure product is unarchived
                        }
                        odoo.write('product.product', int(mapping.odoo_id), update_data)
                else:
                    # Create new product in Odoo
                    logger.info(f"Creating new product in Odoo: {product['name']}")
                    odoo_id = odoo.create('product.product', {
                        'name': product['name'],
                        'list_price': float(product['price']),
                        'default_code': product['sku'],
                        'active': True
                    })
                    
                    # Create mapping
                    mapping = SyncMapping(
                        entity_type='product',
                        wc_id=wc_id,
                        odoo_id=str(odoo_id)
                    )
                    db.add(mapping)
                
                # Log sync
                log = SyncLog(
                    entity_type='product',
                    entity_id=wc_id,
                    source='woocommerce',
                    action='sync',
                    status='success'
                )
                db.add(log)
                
            except Exception as e:
                logger.error(f"Error processing product {product.get('id', 'unknown')}: {str(e)}")
                # Log the error
                log = SyncLog(
                    entity_type='product',
                    entity_id=str(product.get('id', 'unknown')),
                    source='woocommerce',
                    action='sync',
                    status='failed',
                    error_message=str(e)
                )
                db.add(log)
                continue
        
        # Check for products that exist in Odoo but were deleted in WooCommerce
        for mapping in existing_mappings:
            if mapping.wc_id not in processed_wc_ids:
                try:
                    # Verify if product still exists in Odoo
                    odoo_product = odoo.search_read(
                        'product.product',
                        [('id', '=', int(mapping.odoo_id))],
                        ['id', 'name']
                    )
                    
                    if odoo_product:
                        # Product exists in Odoo but was deleted in WooCommerce
                        logger.info(f"Product {odoo_product[0]['name']} was deleted in WooCommerce, archiving in Odoo")
                        odoo.write('product.product', int(mapping.odoo_id), {'active': False})
                        
                        # Log the archive action
                        log = SyncLog(
                            entity_type='product',
                            entity_id=mapping.wc_id,
                            source='woocommerce',
                            action='archive',
                            status='success',
                            error_message='Product deleted in WooCommerce'
                        )
                        db.add(log)
                except Exception as e:
                    logger.error(f"Error archiving product {mapping.odoo_id}: {str(e)}")
                    log = SyncLog(
                        entity_type='product',
                        entity_id=mapping.wc_id,
                        source='woocommerce',
                        action='archive',
                        status='failed',
                        error_message=str(e)
                    )
                    db.add(log)
        
        db.commit()
        logger.info("Product sync completed successfully")
        return "Sync completed successfully"
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error during sync: {str(e)}")
        raise e
    finally:
        db.close() 