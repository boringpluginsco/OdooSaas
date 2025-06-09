from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from . import models, tasks
from .db import get_db, engine
import logging
from fastapi.staticfiles import StaticFiles
from starlette.responses import HTMLResponse, FileResponse

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
            
        return {
            "task_id": task_id,
            "status": task.status,
            "result": task.result if task.ready() else None
        }
    except Exception as e:
        logger.error(f"Error checking task status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e)) 