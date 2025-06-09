from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from . import models, tasks
from .db import get_db, engine
import logging
from fastapi.staticfiles import StaticFiles
from starlette.responses import HTMLResponse, FileResponse
from sqlalchemy import desc
import os
from .wc import WooCommerceClient
from .odoo import OdooClient
from .models import SyncMapping, SyncLog
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create database tables
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="WooCommerce-Odoo Sync API")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the static files directory
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return FileResponse("static/index.html")

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.post("/sync/products")
async def sync_products(db: Session = Depends(get_db)):
    """
    Trigger a sync of products from WooCommerce to Odoo
    """
    try:
        # Trigger the Celery task
        task = tasks.sync_products_wc_to_odoo.delay()
        logger.info(f"Sync task initiated with ID: {task.id}")
        
        return {
            "message": "Product sync initiated",
            "task_id": task.id
        }
    except Exception as e:
        logger.error(f"Error initiating sync: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/sync/customers-woo-to-odoo")
async def sync_customers_woo_to_odoo(db: Session = Depends(get_db)):
    """
    Trigger a sync of customers from WooCommerce to Odoo
    """
    try:
        task = tasks.sync_customers_wc_to_odoo.delay()
        logger.info(f"Customer sync task initiated with ID: {task.id}")
        
        return {
            "message": "Customer sync initiated",
            "task_id": task.id
        }
    except Exception as e:
        logger.error(f"Error initiating customer sync: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sync/status/{task_id}")
async def sync_status(task_id: str):
    """
    Check the status of a sync task
    """
    try:
        task = tasks.sync_products_wc_to_odoo.AsyncResult(task_id)
        logger.info(f"Checking status for task {task_id}: {task.status}")
        
        if task.failed():
            logger.error(f"Task failed: {task.result}")
            return {
                "task_id": task_id,
                "status": "failed",
                "error": str(task.result)
            }
        
        if task.state == 'PROGRESS':
            return {
                "task_id": task_id,
                "status": "in_progress",
                "progress": task.info
            }
            
        return {
            "task_id": task_id,
            "status": task.status,
            "result": task.result if task.ready() else None
        }
    except Exception as e:
        logger.error(f"Error checking task status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/sync/variable-products")
async def sync_variable_products(db: Session = Depends(get_db)):
    """
    Trigger a sync of variable products from WooCommerce to Odoo
    """
    try:
        task = tasks.sync_variable_products_wc_to_odoo.delay()
        logger.info(f"Variable product sync task initiated with ID: {task.id}")
        return {
            "message": "Variable product sync initiated",
            "task_id": task.id
        }
    except Exception as e:
        logger.error(f"Error initiating variable product sync: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sync/runs")
async def get_sync_runs(db: Session = Depends(get_db)):
    """
    Return the last 50 sync runs for the dashboard history UI.
    """
    runs = db.query(models.SyncRun).order_by(desc(models.SyncRun.started_at)).limit(50).all()
    return [
        {
            "id": run.id,
            "task_id": run.task_id,
            "sync_type": run.sync_type,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "duration": run.duration,
            "products_processed": run.products_processed,
            "products_created": run.products_created,
            "products_updated": run.products_updated,
            "products_skipped": run.products_skipped,
            "error_message": run.error_message,
        }
        for run in runs
    ]

def mask_secret(secret):
    if not secret:
        return ''
    if len(secret) <= 5:
        return '*' * len(secret)
    return '*' * (len(secret) - 5) + secret[-5:]

@app.get("/config")
async def get_config():
    wc_url = os.getenv("WC_URL", "")
    wc_api_key = os.getenv("WC_CONSUMER_KEY", "")
    wc_api_secret = os.getenv("WC_CONSUMER_SECRET", "")
    odoo_url = os.getenv("ODOO_URL", "")
    odoo_username = os.getenv("ODOO_USERNAME", "")
    odoo_api_key = os.getenv("ODOO_PASSWORD", "")
    return {
        "wc_url": wc_url,
        "wc_api_key": mask_secret(wc_api_key),
        "wc_api_secret": mask_secret(wc_api_secret),
        "odoo_url": odoo_url,
        "odoo_username": odoo_username,
        "odoo_api_key": mask_secret(odoo_api_key)
    }

@app.get("/woo/customers")
async def get_woo_customers():
    wc = WooCommerceClient()
    customers = wc.get_customers(params={"per_page": 100})
    # Return only basic details for table
    return [
        {
            "id": c.get("id"),
            "email": c.get("email"),
            "first_name": c.get("first_name"),
            "last_name": c.get("last_name"),
            "username": c.get("username"),
            "date_created": c.get("date_created"),
            "billing": c.get("billing", {}),
        }
        for c in customers
    ]

@app.post("/sync/customer/{customer_id}")
async def sync_single_customer(customer_id: int, overwrite: bool = Query(False), db: Session = Depends(get_db)):
    wc = WooCommerceClient()
    odoo = OdooClient()
    # Fetch customer from WooCommerce
    customer = None
    for c in wc.get_customers(params={"per_page": 100}):
        if c.get("id") == customer_id:
            customer = c
            break
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found in WooCommerce")
    customer_email = customer.get("email")
    if not customer_email:
        raise HTTPException(status_code=400, detail="Customer missing email")
    # Check if already exists in Odoo
    odoo_partner_id = odoo.get_partner_by_email(customer_email)
    if odoo_partner_id and not overwrite:
        return {"status": "conflict", "message": f"Customer {customer_email} already exists in Odoo.", "odoo_partner_id": odoo_partner_id}
    # Prepare partner values
    billing = customer.get('billing', {})
    partner_vals = {
        'name': f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
        'email': customer_email,
        'active': True,
    }
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
    if odoo_partner_id and overwrite:
        # Overwrite existing Odoo customer
        odoo.write('res.partner', odoo_partner_id, partner_vals)
        # Log update
        log = SyncLog(
            entity_type='customer',
            entity_id=str(customer_id),
            source='woocommerce',
            action='sync',
            status='success'
        )
        db.add(log)
        db.commit()
        return {"status": "overwritten", "message": f"Customer {customer_email} updated in Odoo."}
    else:
        # Create in Odoo
        new_odoo_id = odoo.create('res.partner', partner_vals)
        # Create mapping
        mapping = SyncMapping(
            entity_type='customer',
            wc_id=str(customer_id),
            odoo_id=str(new_odoo_id)
        )
        db.add(mapping)
        db.commit()
        # Log success
        log = SyncLog(
            entity_type='customer',
            entity_id=str(customer_id),
            source='woocommerce',
            action='sync',
            status='success'
        )
        db.add(log)
        db.commit()
        return {"status": "success", "message": f"Customer {customer_email} synced to Odoo."}

@app.get("/woo/orders")
async def get_woo_orders():
    wc = WooCommerceClient()
    orders = wc.get_orders(params={"per_page": 100})
    
    # Transform orders to include Odoo reference
    transformed_orders = []
    for order in orders:
        odoo_reference = wc.get_order_odoo_reference(order)
        transformed_order = {
            "id": order.get("id"),
            "number": order.get("number"),
            "status": order.get("status"),
            "date_created": order.get("date_created"),
            "billing": order.get("billing", {}),
            "line_items": order.get("line_items", []),
            "odoo_reference": odoo_reference,
            "is_synced": odoo_reference is not None
        }
        transformed_orders.append(transformed_order)
    
    return transformed_orders

@app.post("/sync/order/{order_id}")
async def sync_single_order(order_id: int, overwrite: bool = Query(False), force_new: bool = Query(False), db: Session = Depends(get_db)):
    wc = WooCommerceClient()
    odoo = OdooClient()
    logger = logging.getLogger("sync_order")
    try:
        logger.info(f"Starting sync for WooCommerce order {order_id}")
        # Fetch order from WooCommerce
        order = None
        for o in wc.get_orders(params={"per_page": 100}):
            if o.get("id") == order_id:
                order = o
                break
        if not order:
            logger.error(f"Order {order_id} not found in WooCommerce.")
            return {"status": "error", "message": "Order not found in WooCommerce."}
        
        # Check if order is already synced
        existing_odoo_ref = wc.get_order_odoo_reference(order)
        if existing_odoo_ref and not overwrite and not force_new:
            logger.info(f"Order {order_id} already synced to Odoo as {existing_odoo_ref}")
            return {
                "status": "already_synced",
                "message": f"Order already synced to Odoo as {existing_odoo_ref}",
                "odoo_reference": existing_odoo_ref
            }
        
        logger.info(f"Fetched WooCommerce order: {order}")
        
        # Prepare partner (customer)
        customer_email = order.get("billing", {}).get("email")
        partner_id = None
        if customer_email:
            partner_id = odoo.get_partner_by_email(customer_email)
            logger.info(f"Odoo partner ID for email {customer_email}: {partner_id}")
        if not partner_id:
            # Optionally create a new partner if not found
            partner_vals = {
                'name': f"{order.get('billing', {}).get('first_name', '')} {order.get('billing', {}).get('last_name', '')}".strip(),
                'email': customer_email,
                'active': True,
            }
            partner_id = odoo.create('res.partner', partner_vals)
            logger.info(f"Created new Odoo partner {partner_id} for order {order_id}")
        
        # Prepare order lines
        order_lines = []
        for item in order.get('line_items', []):
            logger.info(f"Processing line item: {item}")
            # Find Odoo product by name or SKU
            odoo_product = odoo.search_read('product.product', [('name', '=', item.get('name'))], ['id'])
            if not odoo_product:
                # Optionally create the product if not found
                product_vals = {
                    'name': item.get('name'),
                    'default_code': item.get('sku'),
                    'list_price': float(item.get('price', 0)),
                    'active': True,
                    'is_storable': True
                }
                product_id = odoo.create('product.product', product_vals)
                logger.info(f"Created new Odoo product {product_id} for line item {item.get('name')}")
            else:
                product_id = odoo_product[0]['id']
                logger.info(f"Found Odoo product {product_id} for line item {item.get('name')}")
            order_lines.append((0, 0, {
                'product_id': product_id,
                'name': item.get('name'),
                'product_uom_qty': item.get('quantity', 1),
                'price_unit': float(item.get('price', 0)),
            }))
        logger.info(f"Prepared order lines: {order_lines}")

        # Format the date to match Odoo's expected format
        date_order = order.get('date_created')
        if date_order:
            try:
                # Parse the ISO format date and convert to Odoo's format
                parsed_date = datetime.fromisoformat(date_order.replace('Z', '+00:00'))
                formatted_date = parsed_date.strftime('%Y-%m-%d %H:%M:%S')
                logger.info(f"Formatted date from {date_order} to {formatted_date}")
            except Exception as e:
                logger.error(f"Error formatting date {date_order}: {str(e)}")
                formatted_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        else:
            formatted_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Determine if this should be a quotation or sale order based on WooCommerce status
        woo_status = order.get('status', '').lower()
        is_quotation = woo_status in ['pending', 'failed', 'on-hold']
        
        # Create sale order in Odoo
        sale_order_vals = {
            'partner_id': partner_id,
            'date_order': formatted_date,
            'order_line': order_lines,
            'origin': f"Woo Order {order_id}",
            'client_order_ref': order.get('number'),
        }

        # If it's a quotation, set the state to draft
        if is_quotation:
            sale_order_vals['state'] = 'draft'
            logger.info(f"Creating quotation for WooCommerce order {order_id} (status: {woo_status})")
        else:
            # For confirmed orders, we need to set the state to 'sale'
            sale_order_vals['state'] = 'sale'
            logger.info(f"Creating confirmed sale order for WooCommerce order {order_id} (status: {woo_status})")

        # If overwriting, find and update existing order
        if existing_odoo_ref and overwrite and not force_new:
            # Find the existing order by its reference
            existing_order = odoo.search_read('sale.order', [('name', '=', existing_odoo_ref)], ['id'])
            if existing_order:
                sale_order_id = existing_order[0]['id']
                logger.info(f"Updating existing Odoo order {sale_order_id}")
                odoo.write('sale.order', sale_order_id, sale_order_vals)
            else:
                logger.error(f"Could not find existing Odoo order with reference {existing_odoo_ref}")
                return {"status": "error", "message": f"Could not find existing Odoo order with reference {existing_odoo_ref}"}
        else:
            # Create new order
            logger.info(f"Creating sale order in Odoo with values: {sale_order_vals}")
            sale_order_id = odoo.create('sale.order', sale_order_vals)
            logger.info(f"Created Odoo {'quotation' if is_quotation else 'sale order'} {sale_order_id} for WooCommerce order {order_id}")

        # Get the Odoo order number
        odoo_order = odoo.search_read('sale.order', [('id', '=', sale_order_id)], ['name'])
        if odoo_order:
            odoo_order_number = odoo_order[0]['name']
            logger.info(f"Retrieved Odoo order number: {odoo_order_number}")

            # Save the Odoo reference and sync status back to WooCommerce
            try:
                # Prepare meta updates
                meta_updates = {
                    '_odoo_reference': odoo_order_number,
                    '_odoo_order_id': str(sale_order_id),
                    '_sync_status': 'completed',
                    '_sync_timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    '_odoo_order_type': 'quotation' if is_quotation else 'sale_order'
                }
                
                # Update the WooCommerce order meta
                wc.update_order_meta(order_id, meta_updates)
                logger.info(f"Updated WooCommerce order {order_id} with Odoo reference and sync status")

                # Add a note to the WooCommerce order
                note_text = f"Order synced to Odoo as {'Quotation' if is_quotation else 'Sale Order'}: {odoo_order_number}"
                if overwrite and not force_new:
                    note_text = f"Order overwritten in Odoo as {'Quotation' if is_quotation else 'Sale Order'}: {odoo_order_number}"
                if force_new:
                    note_text = f"New Odoo order created as {'Quotation' if is_quotation else 'Sale Order'}: {odoo_order_number}"
                wc.add_order_note(order_id, note_text, customer_note=False)
                logger.info(f"Added note to WooCommerce order {order_id}: {note_text}")

                # Create or update sync mapping
                mapping = db.query(SyncMapping).filter_by(
                    entity_type='order',
                    wc_id=str(order_id)
                ).first()
                
                if mapping:
                    mapping.odoo_id = str(sale_order_id)
                else:
                    mapping = SyncMapping(
                        entity_type='order',
                        wc_id=str(order_id),
                        odoo_id=str(sale_order_id)
                    )
                    db.add(mapping)
                
                # Log success
                log = SyncLog(
                    entity_type='order',
                    entity_id=str(order_id),
                    source='woocommerce',
                    action='sync',
                    status='success'
                )
                db.add(log)
                db.commit()

            except Exception as e:
                error_msg = f"Failed to update WooCommerce order with Odoo reference and note: {str(e)}"
                logger.error(error_msg)
                
                # Log the error
                log = SyncLog(
                    entity_type='order',
                    entity_id=str(order_id),
                    source='woocommerce',
                    action='sync',
                    status='failed',
                    error_message=error_msg
                )
                db.add(log)
                db.commit()
                
                # Continue with the sync even if WooCommerce update fails
                pass

        return {
            "status": "success", 
            "message": f"Order {order_id} synced to Odoo as {'Quotation' if is_quotation else 'Sale Order'} {odoo_order_number}"
        }
    except Exception as e:
        logger.error(f"Error syncing order {order_id}: {str(e)}", exc_info=True)
        return {"status": "error", "message": str(e)} 