import xmlrpc.client
import os
from dotenv import load_dotenv
import base64
import requests
import logging

load_dotenv()

logger = logging.getLogger(__name__)

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

    def get_default_warehouse_location(self):
        # Search for the default internal stock location. Commonly named 'Stock'
        # You might need to adjust the domain based on your Odoo setup
        locations = self.search_read(
            'stock.location',
            [('usage', '=', 'internal'), ('name', 'ilike', 'Stock')],
            ['id', 'name']
        )
        if locations:
            return locations[0]['id']
        return None

    def update_product_quantity(self, product_id, new_quantity, location_id):
        # Ensure product_id is an integer as expected by Odoo's ORM
        if not isinstance(product_id, int):
            product_id = int(product_id)

        # Create an inventory adjustment (stock.quant) for the product
        # This is the standard way to update stock via API in Odoo
        quant_vals = {
            'product_id': product_id,
            'location_id': location_id,
            'inventory_quantity': float(new_quantity),
        }
        
        # Odoo 16+ requires an inventory_reference for manual quants
        # Let's create a simple one based on product_id
        inventory_reference = f'WooCommerce Sync - Product {product_id}'

        # Check if an inventory adjustment already exists for this product at this location
        existing_quants = self.search_read(
            'stock.quant',
            [('product_id', '=', product_id), ('location_id', '=', location_id)],
            ['id', 'inventory_quantity']
        )

        if existing_quants:
            # If quant exists, update it
            quant_id = existing_quants[0]['id']
            self.write('stock.quant', quant_id, {
                'inventory_quantity': float(new_quantity),
                'inventory_reference': inventory_reference
            })
        else:
            # Otherwise, create a new quant
            quant_id = self.create('stock.quant', quant_vals)

        # Validate the inventory adjustment
        # This applies the quantity change
        try:
            self.models.execute_kw(
                self.db, self.uid, self.password,
                'stock.quant', 'action_apply_inventory',
                [[quant_id]] # Pass the ID of the quant to apply
            )
            return True
        except Exception as e:
            print(f"Error applying inventory for product {product_id}: {e}")
            return False

    def get_or_create_attribute(self, name):
        """Find or create a product.attribute by name."""
        attrs = self.search_read('product.attribute', [('name', '=', name)], ['id'])
        if attrs:
            logger.info(f"Found existing attribute '{name}' with ID {attrs[0]['id']}")
            return attrs[0]['id']
        logger.info(f"Creating new attribute '{name}'")
        return self.create('product.attribute', {'name': name})

    def get_or_create_attribute_value(self, attribute_id, value):
        """Find or create a product.attribute.value by name and attribute."""
        vals = self.search_read('product.attribute.value', 
                              [('name', '=', value), ('attribute_id', '=', attribute_id)], 
                              ['id'])
        if vals:
            logger.info(f"Found existing attribute value '{value}' with ID {vals[0]['id']}")
            return vals[0]['id']
        logger.info(f"Creating new attribute value '{value}' for attribute ID {attribute_id}")
        return self.create('product.attribute.value', {'name': value, 'attribute_id': attribute_id})

    def build_attribute_lines(self, attributes):
        """
        Build attribute lines for product template.
        attributes: dict of {attr_name: set(values)}
        Returns: list of attribute line tuples
        """
        lines = []
        for attr_name, values in attributes.items():
            # Get or create the attribute
            attr_id = self.get_or_create_attribute(attr_name)
            
            # Get or create all values for this attribute
            value_ids = []
            for value in values:
                value_id = self.get_or_create_attribute_value(attr_id, value)
                value_ids.append(value_id)
            
            # Create the attribute line
            lines.append((0, 0, {
                'attribute_id': attr_id,
                'value_ids': [(6, 0, value_ids)]  # (6, 0, ids) replaces the list of values
            }))
            
            logger.info(f"Built attribute line for '{attr_name}' with values: {values}")
        
        return lines

    def get_variant_value_ids(self, variant_attrs):
        """
        Get the IDs of product.template.attribute.value for a variant.
        variant_attrs: dict of {attr_name: value}
        Returns: list of product.template.attribute.value IDs
        """
        value_ids = []
        for attr_name, value in variant_attrs.items():
            # Get the attribute ID
            attr_id = self.get_or_create_attribute(attr_name)
            
            # Get the attribute value ID
            attr_value_id = self.get_or_create_attribute_value(attr_id, value)
            
            # Find the corresponding product.template.attribute.value
            template_value = self.search_read(
                'product.template.attribute.value',
                [
                    ('attribute_id', '=', attr_id),
                    ('product_attribute_value_id', '=', attr_value_id)
                ],
                ['id']
            )
            
            if template_value:
                value_ids.append(template_value[0]['id'])
                logger.info(f"Found template attribute value ID {template_value[0]['id']} for attribute '{attr_name}' with value '{value}'")
            else:
                logger.warning(f"No template attribute value found for attribute '{attr_name}' with value '{value}'")
        
        return value_ids 