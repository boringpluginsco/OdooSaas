from celery import Celery
from .wc import WooCommerceClient
from .odoo import OdooClient
from .db import SessionLocal
from .models import SyncLog, SyncMapping
import os
from dotenv import load_dotenv
import logging
import requests

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
        # --- CATEGORY HIERARCHY PROCESSING ---
        logger.info("Fetching all WooCommerce categories")
        wc_all_categories = wc.get_categories(params={'per_page': 100}) # Fetch all categories
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
                
                # --- CATEGORY HANDLING ---
                odoo_product_category_id = None  # Changed to singular variable
                if product.get('categories') and product['categories']:
                    # Use the first category for categ_id (Many2one field in Odoo product.product)
                    wc_first_category = product['categories'][0]
                    wc_cat_id = wc_first_category['id']
                    if wc_cat_id in wc_cat_to_odoo_cat_id_map:
                        odoo_product_category_id = wc_cat_to_odoo_cat_id_map[wc_cat_id]
                    else:
                        logger.warning(f"WooCommerce product category {wc_first_category['name']} (ID: {wc_cat_id}) not found in the processed categories map. This might indicate an issue with fetching all categories.")

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
                    # Assign the single Odoo category ID directly for Many2one field
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

@celery_app.task(bind=True)
def sync_customers_wc_to_odoo(self):
    logger.info("Starting customer sync from WooCommerce to Odoo")
    wc = WooCommerceClient()
    odoo = OdooClient()
    db = SessionLocal()

    try:
        logger.info("Fetching customers from WooCommerce")
        wc_customers = wc.get_customers(params={'per_page': 100}) # Fetch all customers
        logger.info(f"Found {len(wc_customers)} customers in WooCommerce")

        for customer in wc_customers:
            try:
                wc_id = str(customer['id'])
                customer_email = customer.get('email')

                if not customer_email:
                    logger.warning(f"Skipping customer {wc_id} due to missing email address.")
                    continue

                # Check if customer already exists in Odoo by email
                odoo_partner_id = odoo.get_partner_by_email(customer_email)

                if odoo_partner_id:
                    logger.info(f"Customer {customer_email} already exists in Odoo with ID {odoo_partner_id}. Skipping update for now.")
                    # In a full implementation, you would add update logic here
                else:
                    logger.info(f"Creating new customer in Odoo: {customer_email}")
                    billing = customer.get('billing', {})
                    shipping = customer.get('shipping', {})

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
                    
                    # Odoo states require lookup by code+country_id, often this is left empty or requires more complex mapping
                    # For now, we will not set state_id if it's None to avoid the error.
                    # If WooCommerce provides state code, it can be mapped here.
                    # state_id = None 
                    # if state_id:
                    #     partner_vals['state_id'] = state_id

                    new_odoo_id = odoo.create('res.partner', partner_vals)

                    # Create mapping
                    mapping = SyncMapping(
                        entity_type='customer',
                        wc_id=wc_id,
                        odoo_id=str(new_odoo_id)
                    )
                    db.add(mapping)
                    
                # Log sync
                log = SyncLog(
                    entity_type='customer',
                    entity_id=wc_id,
                    source='woocommerce',
                    action='sync',
                    status='success'
                )
                db.add(log)

            except Exception as e:
                logger.error(f"Error processing customer {customer.get('id', 'unknown')}: {str(e)}")
                # Log the error
                log = SyncLog(
                    entity_type='customer',
                    entity_id=str(customer.get('id', 'unknown')),
                    source='woocommerce',
                    action='sync',
                    status='failed',
                    error_message=str(e)
                )
                db.add(log)
                continue
        
        db.commit()
        logger.info("Customer sync completed successfully")
        return "Customer sync completed successfully"
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error during customer sync: {str(e)}")
        raise e
    finally:
        db.close() 