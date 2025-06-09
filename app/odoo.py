import xmlrpc.client
import os
from dotenv import load_dotenv
import base64
import requests

load_dotenv()

class OdooClient:
    def __init__(self):
        self.url = os.getenv("ODOO_URL")
        self.db = os.getenv("ODOO_DB")
        self.username = os.getenv("ODOO_USERNAME")
        self.password = os.getenv("ODOO_PASSWORD")
        
        self.common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common')
        self.uid = self.common.authenticate(self.db, self.username, self.password, {})
        self.models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object')

    def search_read(self, model, domain, fields):
        return self.models.execute_kw(
            self.db, self.uid, self.password,
            model, 'search_read',
            [domain],
            {'fields': fields}
        )

    def create(self, model, vals):
        return self.models.execute_kw(
            self.db, self.uid, self.password,
            model, 'create',
            [vals]
        )

    def write(self, model, id, vals):
        return self.models.execute_kw(
            self.db, self.uid, self.password,
            model, 'write',
            [[id], vals]
        )

    def unlink(self, model, id):
        return self.models.execute_kw(
            self.db, self.uid, self.password,
            model, 'unlink',
            [[id]]
        )

    def get_or_create_category(self, category_name, parent_odoo_id=None):
        # Try to find the category by name
        categories = self.search_read('product.category', [('name', '=', category_name)], ['id'])
        if categories:
            return categories[0]['id']
        # Create if not found
        vals = {'name': category_name}
        if parent_odoo_id:
            vals['parent_id'] = parent_odoo_id
        return self.create('product.category', vals)

    def get_or_create_nested_category(self, wc_category):
        category_name = wc_category['name']
        parent_id = wc_category['parent'] # This is WooCommerce's parent ID
        
        # Try to find the category by name first
        existing_category = self.search_read('product.category', [('name', '=', category_name)], ['id', 'parent_id'])
        if existing_category:
            return existing_category[0]['id']
        
        odoo_parent_id = None
        if parent_id != 0:  # 0 indicates a top-level category in WooCommerce
            # We need to get the WooCommerce parent category object to recursively create it
            # This requires fetching all WC categories beforehand in the task.
            # For now, let's assume we have a mapping from WC parent ID to Odoo ID.
            # This function will rely on the main task to handle the full hierarchy.
            pass # This will be handled in tasks.py
        
        # Create category if not found, with parent if available
        vals = {'name': category_name}
        if odoo_parent_id:
            vals['parent_id'] = odoo_parent_id
        return self.create('product.category', vals)

    def get_category_by_name(self, category_name):
        categories = self.search_read('product.category', [('name', '=', category_name)], ['id'])
        if categories:
            return categories[0]['id']
        return None

    def get_image_base64_from_url(self, url):
        try:
            response = requests.get(url)
            if response.status_code == 200:
                return base64.b64encode(response.content).decode('utf-8')
        except Exception:
            pass
        return None

    def get_partner_by_email(self, email):
        partners = self.search_read('res.partner', [('email', '=', email)], ['id'])
        if partners:
            return partners[0]['id']
        return None

    def get_or_create_country(self, country_name):
        if not country_name:
            return None
        countries = self.search_read('res.country', [('name', 'ilike', country_name)], ['id'])
        if countries:
            return countries[0]['id']
        # Create if not found
        # Note: Odoo country name should match WooCommerce country name for this to work perfectly.
        # Consider a more robust mapping if country names differ.
        return self.create('res.country', {'name': country_name}) 