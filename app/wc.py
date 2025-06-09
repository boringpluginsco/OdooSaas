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
        return self.wcapi.get("orders", params=params).json()

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