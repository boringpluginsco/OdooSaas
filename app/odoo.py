import xmlrpc.client
import os
from dotenv import load_dotenv

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