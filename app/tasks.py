from celery import Celery
from .wc import WooCommerceClient
from .odoo import OdooClient
from .db import SessionLocal
from .models import SyncLog, SyncMapping
import os
from dotenv import load_dotenv
import logging
import requests
from typing import List, Dict, Any
from collections import defaultdict

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

def process_in_batches(task, items: List[Any], batch_size: int, process_func, total_count: int):
    """
    Process items in batches and update task progress.
    
    Args:
        task: Celery task instance
        items: List of items to process
        batch_size: Number of items to process in each batch
        process_func: Function to process each batch
        total_count: Total number of items to process
    """
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        try:
            process_func(batch)
            # Update progress after each batch
            task.update_state(state='PROGRESS', meta={
                'current': min(i + len(batch), total_count),
                'total': total_count,
                'status': f'Processing batch {i//batch_size + 1} of {(total_count + batch_size - 1)//batch_size}'
            })
        except Exception as e:
            logger.error(f"Error processing batch {i//batch_size + 1}: {str(e)}")
            # Continue with next batch despite error
            continue

@celery_app.task(bind=True)
def sync_products_wc_to_odoo(self):
    logger.info("Starting product sync from WooCommerce to Odoo")
    wc = WooCommerceClient()
    odoo = OdooClient()
    db = SessionLocal()
    
    try:
        # Update task state to indicate start
        self.update_state(state='PROGRESS', meta={
            'current': 0,
            'total': 0,
            'status': 'Fetching products from WooCommerce...'
        })

        # --- CATEGORY HIERARCHY PROCESSING ---
        logger.info("Fetching all WooCommerce categories")
        wc_all_categories = wc.get_categories(params={'per_page': 100})
        wc_categories_by_id = {cat['id']: cat for cat in wc_all_categories}

        # Dictionary to store the mapping from WC category ID to Odoo category ID
        wc_cat_to_odoo_cat_id_map = {}

        def get_or_create_odoo_category_recursive(wc_cat_id):
            if wc_cat_id == 0:  # Top-level category in WooCommerce has parent 0
                return None

            # If already processed, return the Odoo ID
            if wc_cat_id in wc_cat_to_odoo_cat_id_map:
                return wc_cat_to_odoo_cat_id_map[wc_cat_id]

            wc_category = wc_categories_by_id.get(wc_cat_id)
            if not wc_category:
                logger.warning(f"WooCommerce category with ID {wc_cat_id} not found during recursive processing.")
                return None

            odoo_parent_id = None
            if wc_category['parent'] != 0:
                odoo_parent_id = get_or_create_odoo_category_recursive(wc_category['parent'])

            # Create or get the current category in Odoo
            odoo_cat_id = odoo.get_or_create_category(wc_category['name'], odoo_parent_id)
            wc_cat_to_odoo_cat_id_map[wc_cat_id] = odoo_cat_id
            return odoo_cat_id

        # Process all WooCommerce categories to build the Odoo category hierarchy
        for wc_cat_id in wc_categories_by_id.keys():
            get_or_create_odoo_category_recursive(wc_cat_id)

        # --- END CATEGORY HIERARCHY PROCESSING ---

        # Get all products from WooCommerce using generator
        logger.info("Fetching products from WooCommerce")
        products_generator = wc.get_products_generator(batch_size=100)
        
        # Get all existing mappings
        existing_mappings = db.query(SyncMapping).filter_by(entity_type='product').all()
        wc_id_to_mapping = {mapping.wc_id: mapping for mapping in existing_mappings}
        
        # Track which WooCommerce products we've processed
        processed_wc_ids = set()
        
        # Process products in batches
        batch_size = 50
        current_batch = []
        total_processed = 0
        total_products = 0  # Will be updated as we process

        def process_batch(batch):
            nonlocal total_processed
            for product in batch:
                try:
                    wc_id = str(product['id'])
                    processed_wc_ids.add(wc_id)
                    total_processed += 1
                    
                    # Update progress
                    self.update_state(state='PROGRESS', meta={
                        'current': total_processed,
                        'total': total_products,
                        'status': f'Processing product {total_processed}/{total_products}: {product["name"]}'
                    })

                    # Process product
                    process_single_product(product, wc_id, wc_cat_to_odoo_cat_id_map, odoo, db, wc_id_to_mapping)
                    
                except Exception as e:
                    logger.error(f"Error processing product {product.get('id', 'unknown')}: {str(e)}")
                    log_error(db, 'product', str(product.get('id', 'unknown')), str(e))
                    continue

        def process_single_product(product, wc_id, wc_cat_to_odoo_cat_id_map, odoo, db, wc_id_to_mapping):
            # --- CATEGORY HANDLING ---
            odoo_product_category_id = None
            if product.get('categories') and product['categories']:
                wc_first_category = product['categories'][0]
                wc_cat_id = wc_first_category['id']
                if wc_cat_id in wc_cat_to_odoo_cat_id_map:
                    odoo_product_category_id = wc_cat_to_odoo_cat_id_map[wc_cat_id]
                else:
                    logger.warning(f"WooCommerce product category {wc_first_category['name']} (ID: {wc_cat_id}) not found in the processed categories map.")

            # --- DESCRIPTION HANDLING ---
            description = product.get('description', '')

            # --- IMAGE HANDLING ---
            image_base64 = None
            if product.get('images') and len(product['images']) > 0:
                image_url = product['images'][0]['src']
                image_base64 = odoo.get_image_base64_from_url(image_url)

            # Check if product is already mapped
            mapping = wc_id_to_mapping.get(wc_id)
            
            product_vals = {
                'name': product['name'],
                'list_price': float(product['price']),
                'default_code': product['sku'],
                'active': True,
                'description': description
            }
            if odoo_product_category_id:
                product_vals['categ_id'] = odoo_product_category_id
            if image_base64:
                product_vals['image_1920'] = image_base64

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
                    odoo_id = odoo.create('product.product', product_vals)
                    mapping.odoo_id = str(odoo_id)
                    db.add(mapping)
                else:
                    # Update existing product in Odoo
                    logger.info(f"Updating existing product in Odoo: {product['name']}")
                    odoo.write('product.product', int(mapping.odoo_id), product_vals)
            else:
                # Create new product in Odoo
                logger.info(f"Creating new product in Odoo: {product['name']}")
                odoo_id = odoo.create('product.product', product_vals)
                
                # Create mapping
                mapping = SyncMapping(
                    entity_type='product',
                    wc_id=wc_id,
                    odoo_id=str(odoo_id)
                )
                db.add(mapping)
            
            # Log sync
            log_success(db, 'product', wc_id)

        def log_success(db, entity_type, entity_id):
            log = SyncLog(
                entity_type=entity_type,
                entity_id=entity_id,
                source='woocommerce',
                action='sync',
                status='success'
            )
            db.add(log)

        def log_error(db, entity_type, entity_id, error_message):
            log = SyncLog(
                entity_type=entity_type,
                entity_id=entity_id,
                source='woocommerce',
                action='sync',
                status='failed',
                error_message=error_message
            )
            db.add(log)

        # Process products in batches
        for product in products_generator:
            current_batch.append(product)
            total_products += 1
            
            if len(current_batch) >= batch_size:
                process_batch(current_batch)
                current_batch = []
                db.commit()  # Commit after each batch

        # Process remaining products
        if current_batch:
            process_batch(current_batch)
            db.commit()

        # Check for products that exist in Odoo but were deleted in WooCommerce
        self.update_state(state='PROGRESS', meta={
            'current': total_products,
            'total': total_products,
            'status': 'Checking for deleted products...'
        })

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
        return {
            'current': total_products,
            'total': total_products,
            'status': 'Sync completed successfully'
        }
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error during sync: {str(e)}")
        raise e
    finally:
        db.close()

@celery_app.task(bind=True)
def sync_customers_wc_to_odoo(self):
    logger.info("Starting customer sync from WooCommerce to Odoo")
    wc = WooCommerceClient()
    odoo = OdooClient()
    db = SessionLocal()

    try:
        self.update_state(state='PROGRESS', meta={
            'current': 0,
            'total': 0,
            'status': 'Fetching customers from WooCommerce...'
        })

        # Get customers using generator
        customers_generator = wc.get_customers_generator(batch_size=100)
        
        # Process customers in batches
        batch_size = 50
        current_batch = []
        total_processed = 0
        total_customers = 0

        def process_batch(batch):
            nonlocal total_processed
            for customer in batch:
                try:
                    wc_id = str(customer['id'])
                    customer_email = customer.get('email')
                    total_processed += 1

                    if not customer_email:
                        logger.warning(f"Skipping customer {wc_id} due to missing email address.")
                        continue

                    # Update progress
                    self.update_state(state='PROGRESS', meta={
                        'current': total_processed,
                        'total': total_customers,
                        'status': f'Processing customer {total_processed}/{total_customers}: {customer_email}'
                    })

                    # Check if customer already exists in Odoo by email
                    odoo_partner_id = odoo.get_partner_by_email(customer_email)

                    if odoo_partner_id:
                        logger.info(f"Customer {customer_email} already exists in Odoo with ID {odoo_partner_id}. Skipping update for now.")
                    else:
                        process_single_customer(customer, wc_id, customer_email, odoo, db)

                except Exception as e:
                    logger.error(f"Error processing customer {customer.get('id', 'unknown')}: {str(e)}")
                    log_error(db, 'customer', str(customer.get('id', 'unknown')), str(e))
                    continue

        def process_single_customer(customer, wc_id, customer_email, odoo, db):
            logger.info(f"Creating new customer in Odoo: {customer_email}")
            billing = customer.get('billing', {})

            partner_vals = {
                'name': f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
                'email': customer_email,
                'active': True,
            }

            # Conditionally add fields if they have a non-None value
            phone = billing.get('phone')
            if phone:
                partner_vals['phone'] = phone

            street = billing.get('address_1')
            if street:
                partner_vals['street'] = street
            
            street2 = billing.get('address_2')
            if street2:
                partner_vals['street2'] = street2

            city = billing.get('city')
            if city:
                partner_vals['city'] = city

            zip_code = billing.get('postcode')
            if zip_code:
                partner_vals['zip'] = zip_code

            country_name = billing.get('country')
            country_id = odoo.get_or_create_country(country_name) if country_name else None
            if country_id:
                partner_vals['country_id'] = country_id

            new_odoo_id = odoo.create('res.partner', partner_vals)

            # Create mapping
            mapping = SyncMapping(
                entity_type='customer',
                wc_id=wc_id,
                odoo_id=str(new_odoo_id)
            )
            db.add(mapping)
            
            # Log success
            log_success(db, 'customer', wc_id)

        def log_success(db, entity_type, entity_id):
            log = SyncLog(
                entity_type=entity_type,
                entity_id=entity_id,
                source='woocommerce',
                action='sync',
                status='success'
            )
            db.add(log)

        def log_error(db, entity_type, entity_id, error_message):
            log = SyncLog(
                entity_type=entity_type,
                entity_id=entity_id,
                source='woocommerce',
                action='sync',
                status='failed',
                error_message=error_message
            )
            db.add(log)

        # Process customers in batches
        for customer in customers_generator:
            current_batch.append(customer)
            total_customers += 1
            
            if len(current_batch) >= batch_size:
                process_batch(current_batch)
                current_batch = []
                db.commit()  # Commit after each batch

        # Process remaining customers
        if current_batch:
            process_batch(current_batch)
            db.commit()

        db.commit()
        logger.info("Customer sync completed successfully")
        return {
            'current': total_customers,
            'total': total_customers,
            'status': 'Sync completed successfully'
        }
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error during customer sync: {str(e)}")
        raise e
    finally:
        db.close() 