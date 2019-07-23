# -*- coding: utf-8 -*-
{
    'name': "Stock ipv",

    'summary': """
        Short (1 phrase/line) summary of the module's purpose, used as
        subtitle on modules listing or apps.openerp.com""",

    'description': """
        Stock IPV
    """,

    'author': "My Company",
    'website': "http://www.yourcompany.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/12.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['stock',
                'mrp'],

    # always loaded
    'data': [
        'security/stock_ipv_security.xml',
        'security/ir.model.access.csv',
        'data/stock_ipv_data.xml',
        'views/stock_ipv_menu.xml',
        'views/stock_ipv_view.xml',
    ],
    # only loaded in demonstration mode
    'demo': [
        'demo/demo.xml',
    ],
}