from woocommerce import API
import os
from dotenv import load_dotenv

load_dotenv()

class WooCommerceClient:
    def __init__(self):
        self.wcapi = API(
            url=os.getenv("WC_URL"),
            consumer_key=os.getenv("WC_CONSUMER_KEY"),
            consumer_secret=os.getenv("WC_CONSUMER_SECRET"),
            version="wc/v3"
        )

    def get_products(self, params=None):
        return self.wcapi.get("products", params=params).json()

    def get_products_generator(self, batch_size=100):
        """
        Generator that yields products in batches to handle large datasets efficiently.
        """
        page = 1
        while True:
            products = self.wcapi.get("products", params={
                'page': page,
                'per_page': batch_size
            }).json()
            
            if not products:
                break
                
            yield from products
            page += 1

    def get_orders(self, params=None):
        """
        Get orders from WooCommerce with their meta data.
        """
        if params is None:
            params = {}
        # Ensure we get meta_data in the response
        params['_fields'] = 'id,number,status,date_created,billing,line_items,meta_data'
        return self.wcapi.get("orders", params=params).json()

    def get_order_odoo_reference(self, order):
        """
        Extract Odoo reference from order meta data.
        
        Args:
            order (dict): WooCommerce order data
            
        Returns:
            str: Odoo reference if found, None otherwise
        """
        meta_data = order.get('meta_data', [])
        for meta in meta_data:
            if meta.get('key') == '_odoo_reference':
                return meta.get('value')
        return None

    def get_categories(self, params=None):
        # Fetch all categories, including parent information
        return self.wcapi.get("products/categories", params=params).json()

    def get_customers(self, params=None):
        # Fetch customer data from WooCommerce
        return self.wcapi.get("customers", params=params).json()

    def get_customers_generator(self, batch_size=100):
        """
        Generator that yields customers in batches to handle large datasets efficiently.
        """
        page = 1
        while True:
            customers = self.wcapi.get("customers", params={
                'page': page,
                'per_page': batch_size
            }).json()
            
            if not customers:
                break
                
            yield from customers
            page += 1

    def create_product(self, data):
        return self.wcapi.post("products", data).json()

    def update_product(self, product_id, data):
        return self.wcapi.put(f"products/{product_id}", data).json()

    def delete_product(self, product_id):
        return self.wcapi.delete(f"products/{product_id}").json()

    def get_variable_products_generator(self, batch_size=100):
        page = 1
        while True:
            products = self.wcapi.get("products", params={
                'page': page,
                'per_page': batch_size,
                'type': 'variable'
            }).json()
            if not products:
                break
            yield from products
            page += 1

    def get_product_variations(self, product_id, batch_size=100):
        page = 1
        while True:
            variations = self.wcapi.get(f"products/{product_id}/variations", params={
                'page': page,
                'per_page': batch_size
            }).json()
            if not variations:
                break
            yield from variations
            page += 1

    def update_order(self, order_id, data):
        """
        Update a WooCommerce order with new data.
        
        Args:
            order_id (int): The ID of the order to update
            data (dict): The data to update the order with
            
        Returns:
            dict: The updated order data
        """
        response = self.wcapi.put(f"orders/{order_id}", data)
        if response.status_code != 200:
            raise Exception(f"Failed to update WooCommerce order: {response.text}")
        return response.json()

    def add_order_note(self, order_id, note, customer_note=False):
        """
        Add a note to a WooCommerce order.
        
        Args:
            order_id (int): The ID of the order to add the note to
            note (str): The note text to add
            customer_note (bool): Whether this is a customer-facing note
            
        Returns:
            dict: The created note data
        """
        data = {
            "note": note,
            "customer_note": customer_note
        }
        response = self.wcapi.post(f"orders/{order_id}/notes", data)
        if response.status_code != 201:  # 201 Created
            raise Exception(f"Failed to add order note: {response.text}")
        return response.json()

    def update_order_meta(self, order_id, meta_updates):
        """
        Update multiple meta fields for a WooCommerce order.
        
        Args:
            order_id (int): The ID of the order to update
            meta_updates (dict): Dictionary of meta key-value pairs to update
            Example: {
                '_odoo_reference': 'SO123',
                '_sync_status': 'completed',
                '_sync_timestamp': '2024-03-20 10:00:00'
            }
            
        Returns:
            dict: The updated order data
        """
        try:
            # First get the current order to preserve existing meta
            current_order = self.wcapi.get(f"orders/{order_id}").json()
            meta_data = current_order.get('meta_data', [])
            
            # Create a map of existing meta for quick lookup
            existing_meta_map = {item.get('key'): item for item in meta_data}
            
            # Update or add each meta field
            for key, value in meta_updates.items():
                if key in existing_meta_map:
                    # Update existing meta
                    existing_meta_map[key]['value'] = value
                else:
                    # Add new meta
                    meta_data.append({
                        'key': key,
                        'value': value
                    })
            
            # Update the order with the modified meta_data
            response = self.wcapi.put(f"orders/{order_id}", {
                'meta_data': meta_data
            })
            
            if response.status_code != 200:
                raise Exception(f"Failed to update order meta: {response.text}")
                
            return response.json()
            
        except Exception as e:
            raise Exception(f"Error updating order meta: {str(e)}") 