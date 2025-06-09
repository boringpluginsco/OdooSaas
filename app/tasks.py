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
import time

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
                if product.get('type') == 'variable':
                    continue  # Skip variable products
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
                'description': description,
                
                'is_storable': True  # Enable "Track Inventory"
            }
            if product.get('stock_quantity') is not None:
                product_vals['qty_available'] = float(product['stock_quantity'])
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

                # After product creation/update, update inventory quantity
                if 'stock_quantity' in product:
                    wc_stock_quantity = product['stock_quantity']
                    if wc_stock_quantity is not None:
                        logger.info(f"Updating inventory for product {product['name']} to {wc_stock_quantity}")
                        odoo.update_product_quantity(int(mapping.odoo_id), wc_stock_quantity)
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

                # After product creation/update, update inventory quantity
                if 'stock_quantity' in product:
                    wc_stock_quantity = product['stock_quantity']
                    if wc_stock_quantity is not None:
                        logger.info(f"Updating inventory for product {product['name']} to {wc_stock_quantity}")
                        odoo.update_product_quantity(odoo_id, wc_stock_quantity)

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
def sync_variable_products_wc_to_odoo(self):
    logger.info("Starting variable product sync from WooCommerce to Odoo")
    wc = WooCommerceClient()
    odoo = OdooClient()
    db = SessionLocal()
    try:
        self.update_state(state='PROGRESS', meta={
            'current': 0,
            'total': 0,
            'status': 'Fetching variable products from WooCommerce...'
        })
        # --- CATEGORY HIERARCHY PROCESSING (reuse logic from simple sync) ---
        wc_all_categories = wc.get_categories(params={'per_page': 100})
        wc_categories_by_id = {cat['id']: cat for cat in wc_all_categories}
        wc_cat_to_odoo_cat_id_map = {}
        def get_or_create_odoo_category_recursive(wc_cat_id):
            if wc_cat_id == 0:
                return None
            if wc_cat_id in wc_cat_to_odoo_cat_id_map:
                return wc_cat_to_odoo_cat_id_map[wc_cat_id]
            wc_category = wc_categories_by_id.get(wc_cat_id)
            if not wc_category:
                logger.warning(f"WooCommerce category with ID {wc_cat_id} not found during recursive processing.")
                return None
            odoo_parent_id = None
            if wc_category['parent'] != 0:
                odoo_parent_id = get_or_create_odoo_category_recursive(wc_category['parent'])
            odoo_cat_id = odoo.get_or_create_category(wc_category['name'], odoo_parent_id)
            wc_cat_to_odoo_cat_id_map[wc_cat_id] = odoo_cat_id
            return odoo_cat_id
        for wc_cat_id in wc_categories_by_id.keys():
            get_or_create_odoo_category_recursive(wc_cat_id)
        # --- END CATEGORY HIERARCHY PROCESSING ---
        variable_products_generator = wc.get_variable_products_generator(batch_size=50)
        total_processed = 0
        
        for parent_product in variable_products_generator:
            if not parent_product.get('sku'):
                continue  # Only sync parent if SKU is defined
            
            # Check if product already exists in Odoo based on name
            parent_name = parent_product['name']
            existing_template = odoo.search_read(
                'product.template',
                [('name', '=', parent_name)],
                ['id', 'name']
            )
            if existing_template:
                logger.info(f"Product '{parent_name}' already exists in Odoo (ID: {existing_template[0]['id']}). Skipping.")
                continue
            
            wc_id = str(parent_product['id'])
            # --- CATEGORY HANDLING ---
            odoo_product_category_id = None
            if parent_product.get('categories') and parent_product['categories']:
                wc_first_category = parent_product['categories'][0]
                wc_cat_id = wc_first_category['id']
                if wc_cat_id in wc_cat_to_odoo_cat_id_map:
                    odoo_product_category_id = wc_cat_to_odoo_cat_id_map[wc_cat_id]
            # --- DESCRIPTION HANDLING ---
            description = parent_product.get('description', '')
            # --- IMAGE HANDLING ---
            image_base64 = None
            if parent_product.get('images') and len(parent_product['images']) > 0:
                image_url = parent_product['images'][0]['src']
                image_base64 = odoo.get_image_base64_from_url(image_url)

            # --- ATTRIBUTE/VARIANT LOGIC ---
            # First, get all variations to gather all possible attribute values
            variations = list(wc.get_product_variations(parent_product['id']))
            
            # Gather all attributes and their possible values from all variations
            attributes = {}
            for variation in variations:
                for attr in variation.get('attributes', []):
                    attr_name = attr.get('name')
                    attr_value = attr.get('option')
                    if attr_name and attr_value:
                        attributes.setdefault(attr_name, set()).add(attr_value)

            # Log the attributes we found
            logger.info(f"Found attributes for product {parent_product['name']}: {attributes}")

            # Build attribute lines for the template
            attribute_lines = odoo.build_attribute_lines(attributes)
            logger.info(f"Built attribute lines: {attribute_lines}")

            # Create Product Template in Odoo for the parent
            product_template_vals = {
                'name': parent_product['name'],
                'default_code': parent_product['sku'],
                'active': True,
                'description': description,
                'is_storable': True,
                'attribute_line_ids': attribute_lines
            }
            if odoo_product_category_id:
                product_template_vals['categ_id'] = odoo_product_category_id
            if image_base64:
                product_template_vals['image_1920'] = image_base64

            logger.info(f"Creating product template with SKU: '{parent_product['sku']}'")
            
            # Create the template
            odoo_template_id = odoo.create('product.template', product_template_vals)
            logger.info(f"Created product template with ID {odoo_template_id}")
            
            # Verify the default_code was set correctly
            created_template = odoo.search_read(
                'product.template',
                [('id', '=', odoo_template_id)],
                ['id', 'name', 'default_code']
            )
            if created_template:
                actual_sku = created_template[0].get('default_code')
                if actual_sku != parent_product['sku']:
                    logger.warning(f"SKU mismatch! Expected: '{parent_product['sku']}', Got: '{actual_sku}'")
                    # Force update the SKU if it wasn't set correctly
                    odoo.write('product.template', odoo_template_id, {'default_code': parent_product['sku']})
                    logger.info(f"Forced update of SKU to '{parent_product['sku']}'")
                else:
                    logger.info(f"Verified template SKU correctly set to: '{actual_sku}'")

            # After creating the template, fetch all product.template.attribute.value records for this template
            template_attribute_values = odoo.search_read(
                'product.template.attribute.value',
                [('product_tmpl_id', '=', odoo_template_id)],
                ['id', 'attribute_id', 'product_attribute_value_id']
            )
            # Fetch attribute and value names for mapping
            attribute_ids = list(set([v['attribute_id'][0] for v in template_attribute_values if v['attribute_id']]))
            value_ids = list(set([v['product_attribute_value_id'][0] for v in template_attribute_values if v['product_attribute_value_id']]))
            attr_id_to_name = {a['id']: a['name'] for a in odoo.search_read('product.attribute', [('id', 'in', attribute_ids)], ['id', 'name'])}
            value_id_to_name = {v['id']: v['name'] for v in odoo.search_read('product.attribute.value', [('id', 'in', value_ids)], ['id', 'name'])}
            # Build mapping: (attr_name, value_name) -> template_attribute_value_id
            attrval_to_templateval = {}
            for v in template_attribute_values:
                if v['attribute_id'] and v['product_attribute_value_id']:
                    attr_name = attr_id_to_name[v['attribute_id'][0]]
                    value_name = value_id_to_name[v['product_attribute_value_id'][0]]
                    attrval_to_templateval[(attr_name, value_name)] = v['id']

            # Fetch all Odoo variants for that template
            odoo_variants = odoo.search_read(
                'product.product',
                [('product_tmpl_id', '=', odoo_template_id)],
                ['id', 'product_template_attribute_value_ids', 'active', 'default_code', 'name']
            )
            # Build a mapping from attribute value ID sets to Odoo variant
            def value_id_set_key(value_ids):
                return tuple(sorted(value_ids))
            odoo_variant_map = {}
            for v in odoo_variants:
                key = value_id_set_key(v['product_template_attribute_value_ids'])
                odoo_variant_map[key] = v
            # Build a set of keys for WooCommerce variants with SKUs
            wc_variant_keys = set()
            for variation in variations:
                if not variation.get('sku'):
                    logger.info(f"Skipping variant without SKU: {variation.get('name', 'Unknown')}")
                    continue
                variant_attrs = {attr['name']: attr['option'] for attr in variation.get('attributes', []) if attr.get('name') and attr.get('option')}
                # Build the set of template attribute value IDs for this variant
                template_value_ids = []
                for attr_name, value_name in variant_attrs.items():
                    tid = attrval_to_templateval.get((attr_name, value_name))
                    if tid:
                        template_value_ids.append(tid)
                key = value_id_set_key(template_value_ids)
                wc_variant_keys.add(key)
                if key in odoo_variant_map:
                    variant_id = odoo_variant_map[key]['id']
                    product_product_vals = {
                        'default_code': variation['sku'],
                        'active': True,
                        'is_storable': True
                    }
                    if variation.get('price') is not None:
                        product_product_vals['list_price'] = float(variation['price'])
                    if variation.get('stock_quantity') is not None:
                        product_product_vals['qty_available'] = float(variation['stock_quantity'])
                    
                    # Get individual variant image
                    variant_image_base64 = None
                    if variation.get('image') and variation['image'].get('src'):
                        variant_image_url = variation['image']['src']
                        variant_image_base64 = odoo.get_image_base64_from_url(variant_image_url)
                        logger.info(f"Found image for variant {variation['sku']}: {variant_image_url}")
                    
                    if variant_image_base64:
                        product_product_vals['image_1920'] = variant_image_base64
                        logger.info(f"Setting image for variant {variation['sku']}")
                    
                    logger.info(f"Updating Odoo variant {variant_id} with SKU '{variation['sku']}'")
                    odoo.write('product.product', variant_id, product_product_vals)
                else:
                    logger.warning(f"No matching Odoo variant found for WooCommerce variant with SKU {variation['sku']} and attributes {variant_attrs}")

            # Deactivate Odoo variants that do not have a matching WooCommerce SKU
            for key, v in odoo_variant_map.items():
                if key not in wc_variant_keys:
                    logger.info(f"Deactivating Odoo variant {v['id']} (no matching WooCommerce SKU)")
                    odoo.write('product.product', v['id'], {'active': False})

            total_processed += 1
            self.update_state(state='PROGRESS', meta={
                'current': total_processed,
                'total': 0,
                'status': f'Synced variable product {parent_product["name"]}'
            })

        db.commit()
        logger.info("Variable product sync completed successfully")
        return {
            'current': total_processed,
            'total': total_processed,
            'status': 'Variable product sync completed successfully'
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Error during variable product sync: {str(e)}")
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