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